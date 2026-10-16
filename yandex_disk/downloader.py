"""Recover a Yandex Disk tree with verified transfers and atomic replacement."""
from __future__ import annotations

import concurrent.futures
import hashlib
import http.client
import json
import os
import re
import sqlite3
import stat as stat_module
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable

from .api import ApiError, Resource, YandexDiskAPI
from .token import TokenError


STATE_DIR = ".disk-downloader"
CHUNK_SIZE = 1024 * 1024
PREVIEW_SIZE = "XXXL"
PREVIEW_VARIANT = PREVIEW_SIZE + ":uncropped:inplace:v1"
MAX_PREVIEW_BYTES = 32 * 1024 * 1024


class RecoveryError(Exception):
    pass


class IntegrityError(RecoveryError):
    pass


class _HTTPSOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise RecoveryError("Сервер скачивания перенаправил запрос на небезопасный адрес")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fingerprint(resource: Resource) -> str:
    version = {"size": resource.size}
    if resource.sha256:
        version["sha256"] = resource.sha256
    elif resource.md5:
        version["md5"] = resource.md5
    else:
        version["modified"] = resource.modified
    return json.dumps(version, sort_keys=True)


def valid_name(name: str) -> str:
    if not name or name in {".", ".."} or any(c in name for c in "/\\"):
        raise RecoveryError("Небезопасное имя файла в ответе API")
    if any(ord(c) < 32 for c in name):
        raise RecoveryError("Имя файла содержит управляющие символы")
    if os.name == "nt":
        reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                    *(f"COM{i}" for i in "123456789¹²³"), *(f"LPT{i}" for i in "123456789¹²³")}
        if (any(c in name for c in '<>:"|?*') or name.endswith((".", " "))
                or name.split(".")[0].rstrip(" ").upper() in reserved):
            raise RecoveryError("Имя файла нельзя сохранить на Windows без переименования")
    return name


def redirected_path(path: Path) -> bool:
    """Reject symlinks and Windows directory junctions inside a recovery target."""
    if path.is_symlink():
        return True
    if os.name != "nt":
        return False
    try:
        tag = getattr(path.lstat(), "st_reparse_tag", 0)
    except FileNotFoundError:
        return False
    # Python 3.10/3.11 have st_reparse_tag but not Path.is_junction().
    return tag == getattr(stat_module, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)


class LocalNames:
    """Exact local entry names per directory, listed once per run.

    Names this run creates are added explicitly. A miss lists the directory
    again, so a stale listing costs one extra listing but never reports a
    conflict for an entry that has the exact name.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.listings: dict[Path, set[str]] = {}

    def contains(self, directory: Path, name: str) -> bool:
        name = unicodedata.normalize("NFC", name)
        with self.lock:
            listing = self.listings.get(directory)
            if listing is not None and name in listing:
                return True
        listing = {unicodedata.normalize("NFC", entry) for entry in os.listdir(directory)}
        with self.lock:
            self.listings[directory] = listing
        return name in listing

    def add(self, path: Path) -> None:
        with self.lock:
            listing = self.listings.get(path.parent)
            if listing is not None:
                listing.add(unicodedata.normalize("NFC", path.name))


def safe_path(root: Path, relative: str, names: LocalNames) -> Path:
    parts = PurePosixPath(relative).parts
    if not parts or PurePosixPath(relative).is_absolute():
        raise RecoveryError("Некорректный локальный путь")
    if unicodedata.normalize("NFC", parts[0]).casefold() == STATE_DIR:
        raise RecoveryError(f"Имя {STATE_DIR} занято служебными данными; выберите другую исходную папку")
    current = root
    for part in parts:
        valid_name(part)
        current = current / part
        if redirected_path(current):
            raise RecoveryError("Локальный путь содержит символическую ссылку или junction")
        # Detect aliases on case-insensitive filesystems before writing either name.
        if current.exists() and not names.contains(current.parent, part):
            raise RecoveryError("Конфликт регистра или Unicode в локальном имени")
    return current


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@contextmanager
def output_lock(state_dir: Path):
    path = state_dir / "run.lock"
    if redirected_path(path):
        raise RecoveryError("Служебный файл блокировки является символической ссылкой")
    stream = path.open("a+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            if not stream.read(1):
                stream.write(b" ")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RecoveryError("Эту папку уже обрабатывает другой запуск") from None
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RecoveryError("Эту папку уже обрабатывает другой запуск") from None
        acquired = True
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


class State:
    def __init__(self, state_dir: Path, source_id: str):
        self.lock = threading.Lock()
        self.connection = sqlite3.connect(state_dir / "state.sqlite3", check_same_thread=False)
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL, sha256 TEXT NOT NULL, saved_at TEXT NOT NULL)""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS previews (
            path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, variant TEXT NOT NULL,
            local_path TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
            sha256 TEXT NOT NULL, saved_at TEXT NOT NULL)""")
        source_hash = hashlib.sha256(source_id.encode()).hexdigest()
        existing = self.connection.execute("SELECT value FROM settings WHERE key='source'").fetchone()
        if existing and existing[0] != source_hash:
            self.connection.close()
            raise RecoveryError("Эта папка содержит копию другого источника. Укажите новую папку назначения")
        self.connection.execute("INSERT OR IGNORE INTO settings VALUES ('source', ?)", (source_hash,))
        self.connection.commit()

    def get(self, path: str):
        with self.lock:
            return self.connection.execute("SELECT fingerprint,size,mtime_ns,sha256 FROM files WHERE path=?", (path,)).fetchone()

    def save(self, relative: str, resource: Resource, target: Path, sha256: str):
        stat = target.stat()
        with self.lock:
            with self.connection:
                self.connection.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?)",
                                        (relative, fingerprint(resource), stat.st_size, stat.st_mtime_ns, sha256, utc_now()))
                self.connection.execute("DELETE FROM previews WHERE path=?", (relative,))

    def close(self):
        self.connection.close()

    def get_preview(self, path: str):
        with self.lock:
            return self.connection.execute(
                "SELECT fingerprint,variant,local_path,size,mtime_ns,sha256 FROM previews WHERE path=?",
                (path,),
            ).fetchone()

    def save_preview(self, relative: str, resource: Resource, local_path: str, target: Path, sha256: str):
        stat = target.stat()
        with self.lock:
            with self.connection:
                self.connection.execute("INSERT OR REPLACE INTO previews VALUES (?,?,?,?,?,?,?,?)",
                                        (relative, fingerprint(resource), PREVIEW_VARIANT, local_path,
                                         stat.st_size, stat.st_mtime_ns, sha256, utc_now()))
                self.connection.execute("DELETE FROM files WHERE path=?", (relative,))


@dataclass
class Report:
    started_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    downloaded: int = 0
    updated: int = 0
    skipped: int = 0
    directories: int = 0
    bytes_received: int = 0
    interrupted: bool = False
    errors: list[dict[str, str]] = field(default_factory=list)
    previews: int = 0

    @property
    def complete(self) -> bool:
        return not self.errors and not self.interrupted


def error_text(error: Exception) -> str:
    # urllib exceptions contain signed URLs. Never stringify those exceptions.
    if isinstance(error, urllib.error.HTTPError):
        return f"Сервер скачивания: HTTP {error.code}"
    if isinstance(error, urllib.error.URLError):
        return "Соединение с сервером скачивания недоступно"
    if isinstance(error, (TimeoutError, ConnectionError)):
        return "Соединение прервано или превышено время ожидания"
    if isinstance(error, http.client.HTTPException):
        return "Сервер прервал передачу файла"
    if isinstance(error, OSError):
        return f"Ошибка ввода/вывода: {error.strerror or type(error).__name__}"
    if isinstance(error, sqlite3.Error):
        return f"Ошибка базы состояния: {type(error).__name__}"
    if isinstance(error, (RecoveryError, ApiError, TokenError)):
        return str(error)
    return f"Ошибка обработки: {type(error).__name__}"


class Downloader:
    def __init__(self, api: YandexDiskAPI, output: Path | str, *, workers: int = 3,
                 timeout: float = 60, retries: int = 3, verify: bool = False,
                 log: Callable[[str], None] = print, opener: Callable = urllib.request.urlopen,
                 stop_event: threading.Event | None = None,
                 on_progress: Callable[[dict[str, int]], None] | None = None,
                 save_previews: bool = False):
        self.api = api
        self.output = Path(output).expanduser().resolve()
        self.workers = workers
        self.timeout = timeout
        self.retries = retries
        self.verify = verify
        self.save_previews = save_previews
        self.log_callback = log
        self.on_progress = on_progress
        self.opener = (urllib.request.build_opener(_HTTPSOnlyRedirect()).open
                       if opener is urllib.request.urlopen else opener)
        self.stop = stop_event if stop_event is not None else threading.Event()
        self.log_lock = threading.Lock()
        self.report_lock = threading.Lock()
        self.report = Report()
        self.state_dir = self.output / STATE_DIR
        self.state: State | None = None
        self.known_paths: dict[str, str] = {}
        self.local_names = LocalNames()
        self.case_sensitive = True
        self.log_error_reported = False

    def log(self, message: str):
        with self.log_lock:
            try:
                self.log_callback(message)
            except (OSError, ValueError):
                pass
            try:
                with (self.state_dir / "events.log").open("a", encoding="utf-8") as stream:
                    stream.write(f"{utc_now()} {message}\n")
            except OSError as error:
                if not self.log_error_reported:
                    self.log_error_reported = True
                    with self.report_lock:
                        self.report.errors.append({"path": STATE_DIR + "/events.log", "error": error_text(error)})

    def fail(self, path: str, error: Exception):
        message = error_text(error)
        with self.report_lock:
            self.report.errors.append({"path": path, "error": message})
        self.log(f"[ОШИБКА] {path}: {message}")

    def notify_progress(self):
        if self.on_progress is None:
            return
        with self.report_lock:
            counts = {name: getattr(self.report, name)
                      for name in ("downloaded", "updated", "skipped", "bytes_received", "previews")}
        try:
            self.on_progress(counts)
        except Exception:
            # Displaying progress must never turn a completed save into a failure.
            pass

    def run(self) -> Report:
        if self.stop.is_set():
            self.report.interrupted = True
            self.report.finished_at = utc_now()
            return self.report
        self.output.mkdir(parents=True, exist_ok=True)
        if redirected_path(self.state_dir):
            raise RecoveryError("Служебная папка является символической ссылкой")
        self.state_dir.mkdir(exist_ok=True)
        # Protect state files as well as recovered data from local path redirection.
        if any(redirected_path(p) for p in self.state_dir.rglob("*")):
            raise RecoveryError("Служебная папка содержит символические ссылки")
        with output_lock(self.state_dir):
            probe = self.state_dir / ("CaseProbe-" + uuid.uuid4().hex)
            probe.touch()
            self.case_sensitive = not probe.with_name(probe.name.lower()).exists()
            probe.unlink()
            self.state = State(self.state_dir, self.api.source_id)
            self.api.stop_event = self.stop
            (self.state_dir / "partials").mkdir(exist_ok=True)
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.workers)
            pending = set()
            try:
                self.log(f"Начало обхода. Папка назначения: {self.output}")
                for resource, relative in self.walk():
                    if self.stop.is_set():
                        break
                    pending.add(executor.submit(self.process_file, resource, relative))
                    if len(pending) >= self.workers * 2:
                        done, pending = concurrent.futures.wait(pending, return_when=concurrent.futures.FIRST_COMPLETED)
                        for future in done:
                            future.result()
                for future in concurrent.futures.as_completed(pending):
                    future.result()
            except KeyboardInterrupt:
                self.stop.set()
                self.report.interrupted = True
                self.log("Остановка: готовые файлы и частичные загрузки сохранены")
            except Exception as error:
                self.fail("/", error)
            finally:
                executor.shutdown(wait=True, cancel_futures=True)
                if self.stop.is_set():
                    self.report.interrupted = True
                self.report.finished_at = utc_now()
                try:
                    atomic_json(self.state_dir / "last-run.json", asdict(self.report))
                finally:
                    self.state.close()
        return self.report

    def walk(self):
        try:
            root = self.api.get_resource(self.api.root_path)
        except Exception as error:
            self.fail("/", error)
            return
        if root.kind == "file":
            try:
                yield root, valid_name(root.name)
            except RecoveryError as error:
                self.fail("/", error)
            return
        queue = deque([(self.api.root_path, "")])
        while queue and not self.stop.is_set():
            remote, relative = queue.popleft()
            try:
                if relative:
                    directory = safe_path(self.output, relative, self.local_names)
                    directory.mkdir(parents=True, exist_ok=True)
                    self.local_names.add(directory)
                self.report.directories += 1
                for child in self.api.iter_children(remote):
                    expected = remote.rstrip("/") + "/" + child.name
                    if child.path != expected:
                        self.fail(relative or "/", RecoveryError("API вернул дочерний элемент с неожиданным путём"))
                        continue
                    child_relative = f"{relative}/{child.name}" if relative else child.name
                    try:
                        valid_name(child.name)
                        safe_path(self.output, child_relative, self.local_names)
                        key = unicodedata.normalize("NFC", child_relative)
                        if not self.case_sensitive:
                            key = key.casefold()
                        if key in self.known_paths:
                            raise RecoveryError("Повторный путь или конфликт имён на локальной файловой системе")
                        self.known_paths[key] = child_relative
                    except RecoveryError as error:
                        self.fail(child_relative, error)
                        continue
                    if child.kind == "dir":
                        queue.append((child.path, child_relative))
                    else:
                        yield child, child_relative
            except Exception as error:
                self.fail(relative or "/", error)

    def hashes(self, path: Path) -> tuple[str, str]:
        sha256 = hashlib.sha256()
        md5 = hashlib.md5()
        with path.open("rb") as stream:
            while chunk := stream.read(CHUNK_SIZE):
                if self.stop.is_set():
                    raise RecoveryError("Загрузка остановлена")
                sha256.update(chunk)
                md5.update(chunk)
        return sha256.hexdigest(), md5.hexdigest()

    def validate(self, path: Path, resource: Resource) -> str:
        if resource.size is not None and path.stat().st_size != resource.size:
            raise IntegrityError("Размер скачанного файла не совпадает с размером на Диске")
        sha256, md5 = self.hashes(path)
        if resource.sha256 and sha256 != resource.sha256.lower():
            raise IntegrityError("SHA-256 не совпадает; файл будет скачан повторно")
        if resource.md5 and md5 != resource.md5.lower():
            raise IntegrityError("MD5 не совпадает; файл будет скачан повторно")
        return sha256

    def current(self, relative: str, resource: Resource, target: Path) -> bool:
        if not target.is_file():
            return False
        preview = self.state.get_preview(relative)
        if preview and preview[2] == relative:
            # A preview must never become a completed original merely because
            # its own local checksum is unchanged. Retry originals even without --verify.
            return False
        row = self.state.get(relative)
        stat = target.stat()
        if row and row[0] == fingerprint(resource) and stat.st_size == row[1] and stat.st_mtime_ns == row[2]:
            return not self.verify or self.hashes(target)[0] == row[3]
        # Adopt a pre-existing copy only if the provider's checksum verifies it.
        if resource.sha256 or resource.md5:
            try:
                sha256 = self.validate(target, resource)
            except IntegrityError:
                return False
            self.state.save(relative, resource, target, sha256)
            return True
        return False

    @staticmethod
    def preview_eligible(error: Exception) -> bool:
        """Network/provider failures can have a usable preview; local failures cannot."""
        if isinstance(error, ApiError):
            return error.status not in {400, 401, 403} and (error.retryable or error.status == 404)
        if isinstance(error, urllib.error.HTTPError):
            # A signed original link can be forbidden/expired even while the
            # owner is still authorized to request its separate preview.
            return error.code not in {400, 401}
        return isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError,
                                  http.client.HTTPException, IntegrityError))

    def current_preview(self, resource: Resource, relative: str, target: Path) -> bool:
        row = self.state.get_preview(relative)
        if not row or row[0] != fingerprint(resource) or row[1] != PREVIEW_VARIANT:
            return False
        if row[2] != relative:
            return False
        if not target.is_file():
            return False
        stat = target.stat()
        if stat.st_size != row[3] or stat.st_mtime_ns != row[4]:
            return False
        return not self.verify or self.hashes(target)[0] == row[5]

    def owns_preview(self, relative: str, target: Path) -> bool:
        """Only our unchanged preview can be replaced with another preview."""
        row = self.state.get_preview(relative)
        return bool(row and row[2] == relative and target.is_file() and self.hashes(target)[0] == row[5])

    @staticmethod
    def preview_format(head: bytes, tail: bytes, length: int) -> str:
        # A preview has no original checksum. Check the transport length and
        # image signature/trailer so an HTML error or truncated body stays temporary.
        if head.startswith(b"\xff\xd8\xff") and tail.endswith(b"\xff\xd9") and length >= 4:
            return "jpg"
        if head.startswith(b"\x89PNG\r\n\x1a\n") and tail.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82") and length >= 45:
            return "png"
        if head.startswith((b"GIF87a", b"GIF89a")) and tail.endswith(b";") and length >= 14:
            return "gif"
        if (head.startswith(b"RIFF") and head[8:12] == b"WEBP" and length >= 20
                and int.from_bytes(head[4:8], "little") + 8 == length):
            return "webp"
        raise IntegrityError("Ответ превью не является поддерживаемым полным изображением")

    def recover_preview(self, resource: Resource, relative: str) -> None:
        if self.stop.is_set():
            return
        target = safe_path(self.output, relative, self.local_names)
        if self.current_preview(resource, relative, target):
            self.log(f"[ПРЕВЬЮ ЕСТЬ] {relative}")
            return
        if target.exists() and not self.owns_preview(relative, target):
            self.log(f"[ПРЕВЬЮ ПРОПУЩЕНО] {relative}: сохранена существующая локальная копия")
            return
        before = target.stat() if target.exists() else None
        url = self.api.preview_url(resource.path, size=PREVIEW_SIZE)
        if self.stop.is_set():
            return
        if url is None:
            self.log(f"[ПРЕВЬЮ НЕТ] {relative}: API не предоставил превью")
            return
        temporary = None
        try:
            with self.api.open_preview(url, timeout=self.timeout) as response:
                if response.status != 200:
                    raise RecoveryError(f"Сервер превью: HTTP {response.status}")
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise IntegrityError("Сервер изменил кодировку превью")
                declared_length = response.headers.get("Content-Length")
                expected_length = int(declared_length) if declared_length is not None else None
                if expected_length is not None and not 0 < expected_length <= MAX_PREVIEW_BYTES:
                    raise IntegrityError("Недопустимый размер превью")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type and content_type not in {"application/octet-stream", "image/jpeg", "image/png", "image/gif", "image/webp"}:
                    raise IntegrityError("Сервер вернул неподдерживаемый тип превью")
                head = tail = b""
                length = 0
                sha256 = hashlib.sha256()
                with tempfile.NamedTemporaryFile(dir=self.state_dir / "partials", prefix=".preview-", suffix=".part", delete=False) as stream:
                    temporary = Path(stream.name)
                    for chunk in self.chunks(response):
                        if self.stop.is_set():
                            return
                        length += len(chunk)
                        if length > MAX_PREVIEW_BYTES or (expected_length is not None and length > expected_length):
                            raise IntegrityError("Сервер отправил больше данных превью, чем ожидалось")
                        head = (head + chunk)[:32] if len(head) < 32 else head
                        tail = (tail + chunk)[-12:]
                        stream.write(chunk)
                        sha256.update(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
            if expected_length is not None and length != expected_length:
                raise IntegrityError("Сервер передал превью не полностью")
            extension = self.preview_format(head, tail, length)
            if content_type.startswith("image/") and content_type != {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}[extension]:
                raise IntegrityError("Формат превью не совпадает с типом ответа сервера")
            if self.stop.is_set():
                return
            target = safe_path(self.output, relative, self.local_names)
            after = target.stat() if target.exists() else None
            if before != after:
                raise RecoveryError("Локальный файл изменился во время получения превью; копия сохранена")
            os.replace(temporary, target)
            self.local_names.add(target)
            self.state.save_preview(relative, resource, relative, target, sha256.hexdigest())
            with self.report_lock:
                self.report.previews += 1
            self.notify_progress()
            self.log(f"[ПРЕВЬЮ] {relative}: сохранено вместо оригинала (формат {extension.upper()})")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def process_file(self, resource: Resource, relative: str):
        if self.stop.is_set():
            return
        try:
            target = safe_path(self.output, relative, self.local_names)
            if target.exists() and not target.is_file():
                raise RecoveryError("На месте файла уже находится локальная папка")
            target.parent.mkdir(parents=True, exist_ok=True)
            if self.current(relative, resource, target):
                with self.report_lock:
                    self.report.skipped += 1
                self.log(f"[ЕСТЬ] {relative}")
                return
            key = hashlib.sha256(relative.encode()).hexdigest()
            partial = self.state_dir / "partials" / f"{key}.part"
            metadata = partial.with_suffix(".json")
            version = {"path": relative, "fingerprint": fingerprint(resource)}
            try:
                previous = json.loads(metadata.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                previous = None
            if previous != version:
                partial.unlink(missing_ok=True)
                atomic_json(metadata, version)
            try:
                sha256 = self.download(resource, partial, relative)
            except Exception as error:
                if self.save_previews and not self.stop.is_set() and self.preview_eligible(error):
                    self.fail(relative, error)
                    try:
                        self.recover_preview(resource, relative)
                    except Exception as preview_error:
                        if not self.stop.is_set():
                            self.log(f"[ПРЕВЬЮ НЕ СОХРАНЕНО] {relative}: {error_text(preview_error)}")
                    return
                raise
            if self.stop.is_set():
                return
            # Recheck after transfer, before any write to a final path.
            target = safe_path(self.output, relative, self.local_names)
            existed = target.exists()
            if existed:
                if not target.is_file():
                    raise RecoveryError("Локальный файл заменён папкой во время скачивания")
            os.replace(partial, target)
            self.local_names.add(target)
            self.state.save(relative, resource, target, sha256)
            metadata.unlink(missing_ok=True)
            with self.report_lock:
                if existed:
                    self.report.updated += 1
                else:
                    self.report.downloaded += 1
            self.notify_progress()
            self.log(f"[{'ОБНОВЛЁН' if existed else 'СКАЧАН'}] {relative}")
        except Exception as error:
            if not self.stop.is_set():
                self.fail(relative, error)

    def chunks(self, response):
        while True:
            try:
                chunk = response.read(CHUNK_SIZE)
            except http.client.IncompleteRead as error:
                if error.partial:
                    yield error.partial
                raise
            if not chunk:
                return
            yield chunk

    def download(self, resource: Resource, partial: Path, relative: str) -> str:
        for attempt in range(self.retries + 1):
            if self.stop.is_set():
                raise RecoveryError("Загрузка остановлена")
            try:
                offset = partial.stat().st_size if partial.exists() else 0
                if resource.size is not None and offset > resource.size:
                    partial.unlink()
                    offset = 0
                if partial.exists() and resource.size is not None and offset == resource.size:
                    return self.validate(partial, resource)
                url = self.api.download_url(resource.path)
                parsed = urllib.parse.urlsplit(url)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                    raise RecoveryError("API вернул некорректную ссылку скачивания")
                headers = {"Accept-Encoding": "identity", "User-Agent": "DiskDownloader/1.0"}
                if offset:
                    headers["Range"] = f"bytes={offset}-"
                    self.log(f"[ДОКАЧКА] {relative}: с байта {offset}")
                request = urllib.request.Request(url, headers=headers)
                with self.opener(request, timeout=self.timeout) as response:
                    status = response.status
                    length = response.headers.get("Content-Length")
                    expected_length = int(length) if length is not None else None
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise IntegrityError("Сервер изменил кодировку потока")
                    if status == 206:
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                        if not match:
                            raise IntegrityError("Некорректный Content-Range сервера")
                        start, end, total = map(int, match.groups())
                        if start != offset or end < start or end >= total or (resource.size is not None and total != resource.size):
                            raise IntegrityError("Сервер вернул другой диапазон байтов")
                        if expected_length is not None and expected_length != end - start + 1:
                            raise IntegrityError("Некорректный размер диапазона сервера")
                        expected_length = end - start + 1
                        mode = "ab" if offset else "wb"
                    elif status == 200:
                        mode = "wb"
                        offset = 0
                    else:
                        raise RecoveryError(f"Неожиданный HTTP-статус скачивания: {status}")
                    received = 0
                    with partial.open(mode) as stream:
                        for chunk in self.chunks(response):
                            if self.stop.is_set():
                                raise RecoveryError("Загрузка остановлена")
                            if resource.size is not None and offset + received + len(chunk) > resource.size:
                                raise IntegrityError("Сервер отправил больше байтов, чем ожидалось")
                            stream.write(chunk)
                            received += len(chunk)
                            with self.report_lock:
                                self.report.bytes_received += len(chunk)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if expected_length is not None and received != expected_length:
                        # Keep a valid prefix for the next Range request.
                        raise ConnectionError("Неполный поток")
                if resource.size is not None and partial.stat().st_size < resource.size:
                    raise ConnectionError("Неполный файл")
                return self.validate(partial, resource)
            except (ApiError, urllib.error.URLError, OSError, RecoveryError, ValueError, http.client.HTTPException) as error:
                if isinstance(error, IntegrityError):
                    partial.unlink(missing_ok=True)
                if self.stop.is_set() or attempt == self.retries:
                    raise
                if isinstance(error, ApiError) and not error.retryable:
                    raise
                if isinstance(error, OSError) and not isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError)):
                    # Disk full/permissions are not fixed by retrying a network transfer.
                    raise
                delay = min(2 ** attempt, 30)
                if isinstance(error, urllib.error.HTTPError):
                    try:
                        delay = min(max(float(error.headers.get("Retry-After", delay)), delay), 60)
                    except (TypeError, ValueError):
                        pass
                    error.close()
                self.log(f"[ПОВТОР] {relative}: {error_text(error)}; через {delay:g} с")
                self.stop.wait(delay)
        raise RecoveryError("Попытки скачивания исчерпаны")
