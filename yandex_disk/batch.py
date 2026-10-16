"""Token-free folder plans and sequential recovery of multiple private folders."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
from typing import Callable
import unicodedata

from .api import YandexDiskAPI, _normalized_disk_path
from .downloader import Downloader, Report, error_text


class ConfigurationError(ValueError):
    """A safe, actionable error in the saved folder plan."""


def _text(value: object, message: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value):
        raise ConfigurationError(message)
    return value.strip()


def _expanded_path(value: str) -> Path:
    try:
        return Path(value).expanduser()
    except (OSError, RuntimeError):
        raise ConfigurationError("Не удалось определить локальную папку назначения.") from None


def _resolved_path(path: Path) -> Path:
    try:
        # Windows non-strict resolve() also suppresses unfollowable symlinks.
        # Validate the existing prefix strictly, while allowing new folders.
        prefix = path
        while True:
            try:
                prefix.resolve(strict=True)
                break
            except FileNotFoundError:
                if prefix.parent == prefix:
                    raise
                prefix = prefix.parent
        return path.resolve()
    except (OSError, RuntimeError):
        raise ConfigurationError("Не удалось определить локальную папку назначения. Проверьте путь и символические ссылки.") from None


@dataclass(frozen=True)
class FolderTask:
    disk_path: str
    output: str
    enabled: bool = True

    def __post_init__(self) -> None:
        remote = _text(self.disk_path, "Укажите путь к папке на Яндекс Диске.")
        try:
            remote = _normalized_disk_path(remote)
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from None
        local = _expanded_path(_text(self.output, "Укажите локальную папку назначения."))
        if not local.is_absolute():
            raise ConfigurationError("Локальная папка назначения должна иметь абсолютный путь.")
        if type(self.enabled) is not bool:
            raise ConfigurationError("Параметр enabled должен быть true или false.")
        object.__setattr__(self, "disk_path", remote)
        object.__setattr__(self, "output", str(_resolved_path(local)))


def _destination_parts(output: str) -> tuple[str, ...]:
    # resolve() detects aliases through existing symlinks; normalization also
    # prevents the usual case/Unicode aliases on the user's macOS filesystem.
    parts = tuple(unicodedata.normalize("NFC", part) for part in _resolved_path(Path(output)).parts)
    if os.name == "nt" or sys.platform == "darwin":
        parts = tuple(part.casefold() for part in parts)
    return parts


@dataclass
class BatchSettings:
    tasks: list[FolderTask] = field(default_factory=list)
    workers: int = 3
    retries: int = 3
    timeout: float = 60
    verify: bool = False
    repeat: bool = False
    repeat_delay: float = 60
    save_previews: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.tasks, list) or any(not isinstance(task, FolderTask) for task in self.tasks):
            raise ConfigurationError("Список папок должен содержать задания FolderTask.")
        self.tasks = list(self.tasks)
        for name, low, high in (("workers", 1, 32), ("retries", 0, 10)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ConfigurationError(f"Параметр {name} должен быть целым числом от {low} до {high}.")
        if type(self.timeout) not in (int, float) or not math.isfinite(self.timeout) or not 1 <= self.timeout <= 600:
            raise ConfigurationError("Таймаут должен быть от 1 до 600 секунд.")
        if type(self.verify) is not bool:
            raise ConfigurationError("Параметр verify должен быть true или false.")
        if type(self.repeat) is not bool:
            raise ConfigurationError("Параметр repeat должен быть true или false.")
        if type(self.save_previews) is not bool:
            raise ConfigurationError("Параметр save_previews должен быть true или false.")
        if (type(self.repeat_delay) not in (int, float) or not math.isfinite(self.repeat_delay)
                or not 1 <= self.repeat_delay <= 86400):
            raise ConfigurationError("Пауза между кругами должна быть от 1 до 86400 секунд.")
        self.timeout = float(self.timeout)
        self.repeat_delay = float(self.repeat_delay)
        destinations: list[tuple[str, ...]] = []
        for task in self.tasks:
            if not task.enabled:
                continue
            parts = _destination_parts(task.output)
            for previous in destinations:
                if parts[:len(previous)] == previous or previous[:len(parts)] == parts:
                    raise ConfigurationError(
                        "Папки назначения выбранных заданий совпадают или вложены друг в друга. "
                        "Выберите для каждой исходной папки отдельную папку назначения."
                    )
            destinations.append(parts)


_FIELDS = {"version", "tasks", "workers", "retries", "timeout", "verify", "repeat", "repeat_delay", "save_previews"}
_TASK_FIELDS = {"disk_path", "output", "enabled"}
_LEGACY_FIELDS = {"disk_path", "public_url", "output", "workers", "retries", "timeout", "verify", "watch", "save_previews"}


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise ConfigurationError("Не удалось прочитать JSON-настройки. Проверьте файл настроек.") from None
    if not isinstance(value, dict):
        raise ConfigurationError("Настройки должны быть JSON-объектом.")
    return value


def _unknown_fields(value: dict, allowed: set[str]) -> None:
    if set(value) - allowed:
        raise ConfigurationError("В настройках есть неизвестные поля. Токен в файл настроек сохранять нельзя.")


def _config_output(value: object, base: Path) -> str:
    local = _expanded_path(_text(value, "Укажите локальную папку назначения."))
    return str(_resolved_path(base / local))


def load_settings(path: Path, legacy_path: Path | None = None) -> BatchSettings:
    """Read a plan, or import one private CLI task without changing either file."""
    path = Path(path).expanduser()
    if path.exists():
        value = _load_json(path)
        _unknown_fields(value, _FIELDS)
        if type(value.get("version")) is not int or value["version"] != 1:
            raise ConfigurationError("Неподдерживаемая версия настроек. Требуется version: 1.")
        if not isinstance(value.get("tasks"), list):
            raise ConfigurationError("Параметр tasks должен быть списком папок.")
        tasks = []
        for raw in value["tasks"]:
            if not isinstance(raw, dict):
                raise ConfigurationError("Каждое задание должно быть JSON-объектом.")
            _unknown_fields(raw, _TASK_FIELDS)
            tasks.append(FolderTask(
                disk_path=raw.get("disk_path"),
                output=_config_output(raw.get("output"), path.resolve().parent),
                enabled=raw.get("enabled", True),
            ))
        options = {name: value[name] for name in ("workers", "retries", "timeout", "verify", "repeat", "repeat_delay", "save_previews") if name in value}
        return BatchSettings(tasks, **options)
    if legacy_path is None or not Path(legacy_path).expanduser().exists():
        return BatchSettings()
    legacy_path = Path(legacy_path).expanduser()
    value = _load_json(legacy_path)
    _unknown_fields(value, _LEGACY_FIELDS)
    # Public links have a different identity and must never be silently changed
    # into private account paths when the native form first opens.
    if value.get("public_url") is not None:
        if value.get("disk_path") is not None:
            raise ConfigurationError("В старых настройках указано два источника. Оставьте один источник.")
        return BatchSettings()
    if value.get("disk_path") is None:
        return BatchSettings()
    task = FolderTask(value["disk_path"], _config_output(value.get("output"), legacy_path.resolve().parent))
    options = {name: value[name] for name in ("workers", "retries", "timeout", "verify", "save_previews") if name in value}
    return BatchSettings([task], **options)


def save_settings(path: Path, settings: BatchSettings) -> None:
    """Atomically replace only the requested plan; credentials have no field."""
    settings = BatchSettings(settings.tasks, settings.workers, settings.retries, settings.timeout, settings.verify,
                             settings.repeat, settings.repeat_delay, settings.save_previews)
    payload = {
        "version": 1,
        "tasks": [{"disk_path": task.disk_path, "output": task.output, "enabled": task.enabled} for task in settings.tasks],
        "workers": settings.workers,
        "retries": settings.retries,
        "timeout": settings.timeout,
        "verify": settings.verify,
    }
    # Keep default plans compatible with the original v1 schema while allowing
    # repeat options in the same version. Credentials are never serialized.
    if settings.repeat:
        payload["repeat"] = True
    if settings.repeat_delay != 60:
        payload["repeat_delay"] = settings.repeat_delay
    if settings.save_previews:
        payload["save_previews"] = True
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".download-gui-", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.chmod(temporary_path, 0o600)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


@dataclass(frozen=True)
class TaskResult:
    index: int
    task: FolderTask
    status: str
    report: Report | None = None
    error: str | None = None


@dataclass
class BatchResult:
    items: list[TaskResult] = field(default_factory=list)
    cancelled: bool = False
    cycles: int = 1
    previous_totals: dict[str, int] = field(default_factory=dict)

    @property
    def totals(self) -> dict[str, int]:
        values = {name: self.previous_totals.get(name, 0)
                  for name in ("downloaded", "updated", "skipped", "previews", "directories", "bytes_received", "errors")}
        for item in self.items:
            if item.report is not None:
                for name in values:
                    if name == "errors":
                        values[name] += len(item.report.errors)
                    else:
                        values[name] += getattr(item.report, name)
            if item.error is not None:
                values["errors"] += 1
        return values

    @property
    def downloaded(self) -> int:
        return self.totals["downloaded"]

    @property
    def updated(self) -> int:
        return self.totals["updated"]

    @property
    def skipped(self) -> int:
        return self.totals["skipped"]

    @property
    def previews(self) -> int:
        return self.totals["previews"]

    @property
    def directories(self) -> int:
        return self.totals["directories"]

    @property
    def bytes_received(self) -> int:
        return self.totals["bytes_received"]

    @property
    def errors(self) -> int:
        return self.totals["errors"]


class BatchRunner:
    """Run selected folders in order and leave all recovery state in each target."""

    def __init__(
        self, settings: BatchSettings, token: str,
        emit: Callable[[str, dict], None],
        api_factory=YandexDiskAPI, downloader_factory=Downloader,
    ) -> None:
        # A snapshot isolates the worker from edits to the form's task list.
        self.settings = BatchSettings(settings.tasks, settings.workers, settings.retries, settings.timeout, settings.verify,
                                      settings.repeat, settings.repeat_delay, settings.save_previews)
        self._token = token
        self._emit = emit
        self._api_factory = api_factory
        self._downloader_factory = downloader_factory
        self._stop = threading.Event()
        self._active_lock = threading.Lock()
        self._active: Downloader | None = None
        self._run_lock = threading.Lock()

    def stop(self) -> None:
        self._stop.set()
        with self._active_lock:
            if self._active is not None:
                self._active.stop.set()

    def _safe(self, message: str) -> str:
        # API/downloader messages are already sanitized. This additionally
        # protects UI logs from accidental token interpolation in callbacks.
        return message.replace(self._token.strip(), "[токен скрыт]") if isinstance(self._token, str) and self._token.strip() else message

    def _log(self, message: str) -> None:
        self._notify("log", {"message": self._safe(message)})

    def _notify(self, kind: str, payload: dict) -> None:
        try:
            self._emit(kind, payload)
        except Exception:
            # A closed window must not corrupt a download or prevent a final
            # result. The UI callback queues events and does no network work.
            pass

    def run(self) -> BatchResult:
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("Этот список папок уже скачивается.")
        try:
            if not any(task.enabled for task in self.settings.tasks):
                result = BatchResult(cancelled=self._stop.is_set(), cycles=0)
                self._notify("finished", {"result": result})
                return result
            previous_totals: dict[str, int] = {}
            cycle = 1
            while True:
                if self.settings.repeat and not self._stop.is_set():
                    self._notify("cycle_started", {"cycle": cycle})
                current = self._run_cycle(cycle)
                result = BatchResult(items=current.items, cancelled=current.cancelled,
                                     cycles=cycle, previous_totals=previous_totals)
                if self.settings.repeat:
                    self._notify("cycle_finished", {"cycle": cycle, "result": current})
                if not self.settings.repeat or self._stop.is_set():
                    break
                self._notify("cycle_wait", {"cycle": cycle, "delay": self.settings.repeat_delay})
                if self._stop.is_set() or self._stop.wait(self.settings.repeat_delay):
                    break
                # Keep only aggregate counts and the latest round's items,
                # regardless of how long continuous recovery remains running.
                previous_totals = result.totals
                cycle += 1
            result.cancelled = self._stop.is_set()
            self._notify("finished", {"result": result})
            return result
        finally:
            self._run_lock.release()

    def _progress(self, index: int, cycle: int, snapshot: dict[str, int]) -> None:
        payload = {name: snapshot.get(name, 0)
                   for name in ("downloaded", "updated", "skipped", "previews", "bytes_received")}
        self._notify("progress", {"index": index, "cycle": cycle, **payload})

    def _run_cycle(self, cycle: int) -> BatchResult:
        result = BatchResult()
        for index, task in enumerate(self.settings.tasks):
            if not task.enabled:
                continue
            if self._stop.is_set():
                result.items.append(TaskResult(index, task, "not_started"))
                continue
            self._notify("task_started", {"index": index, "task": task, "cycle": cycle})
            report = None
            failure = None
            try:
                if not self._stop.is_set():
                    api = self._api_factory(
                        disk_path=task.disk_path, token=self._token,
                        timeout=self.settings.timeout, retries=self.settings.retries, log=self._log,
                    )
                    api.stop_event = self._stop
                    if not self._stop.is_set():
                        downloader = self._downloader_factory(
                            api, Path(task.output), workers=self.settings.workers,
                            retries=self.settings.retries, timeout=self.settings.timeout,
                            verify=self.settings.verify, save_previews=self.settings.save_previews,
                            log=self._log, stop_event=self._stop,
                            on_progress=lambda snapshot, index=index: self._progress(index, cycle, snapshot),
                        )
                        with self._active_lock:
                            self._active = downloader
                        if not self._stop.is_set():
                            report = downloader.run()
            except Exception as exc:
                failure = self._safe(error_text(exc))
                self._log(f"[ОШИБКА] {task.disk_path}: {failure}")
            finally:
                with self._active_lock:
                    self._active = None
            if self._stop.is_set() or (report is not None and report.interrupted):
                if report is not None:
                    report.interrupted = True
                status = "cancelled"
            elif failure is not None:
                status = "failed"
            elif report is not None and report.errors:
                status = "partial"
            else:
                status = "complete"
            item = TaskResult(index, task, status, report, failure)
            result.items.append(item)
            self._notify("task_finished", {"index": index, "result": item, "cycle": cycle})
            if status == "cancelled":
                self._stop.set()
        result.cancelled = self._stop.is_set()
        return result
