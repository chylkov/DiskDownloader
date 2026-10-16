#!/usr/bin/env python3
"""Download a Yandex Disk tree, then recover newly available files on reruns."""
from __future__ import annotations

import argparse
import getpass
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import warnings

from yandex_disk.api import ApiError, YandexDiskAPI
from yandex_disk.downloader import Downloader, RecoveryError, Report, STATE_DIR, error_text
from yandex_disk.credentials import CredentialError, TokenStore
from yandex_disk.token import normalize_token


class ConfigurationError(Exception):
    """An actionable configuration error that never includes credentials."""


DEFAULTS = {"workers": 3, "retries": 3, "timeout": 60.0, "verify": False, "watch": 0.0}
FIELDS = {"disk_path", "public_url", "output", *DEFAULTS}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Скачать папку Яндекс Диска с подпапками и докачивать её при следующих запусках.",
        epilog="Токен: YANDEX_DISK_TOKEN, системное хранилище macOS/Windows или скрытый ввод; в JSON он не сохраняется.",
    )
    source = result.add_mutually_exclusive_group()
    source.add_argument("--disk-path", metavar="ПУТЬ", help="личная папка: /Архив или disk:/Архив")
    source.add_argument("--public-url", metavar="ССЫЛКА", help="публичная ссылка на папку (без OAuth)")
    result.add_argument("--output", metavar="ПАПКА", help="куда сохранить содержимое исходной папки")
    result.add_argument("--config", default="download.json", metavar="ФАЙЛ", help="настройки JSON (по умолчанию download.json)")
    result.add_argument("--configure", action="store_true", help="пошагово настроить, сохранить настройки и начать скачивание")
    result.add_argument("--workers", type=int, metavar="N", help="число параллельных загрузок: 1–32 (по умолчанию 3)")
    result.add_argument("--retries", type=int, metavar="N", help="повторы после ошибки: 0–10 (по умолчанию 3)")
    result.add_argument("--timeout", type=float, metavar="СЕК", help="таймаут одного сетевого ожидания: 1–600 с (по умолчанию 60)")
    result.add_argument("--verify", action=argparse.BooleanOptionalAction, default=None,
                        help="при повторном запуске проверять SHA-256 уже сохранённых файлов")
    result.add_argument("--watch", type=float, metavar="СЕК", help="повторять обход с паузой от 60 с; 0 — один запуск")
    return result


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ConfigurationError("Не удалось прочитать JSON-настройки. Проверьте файл, указанный в --config.") from exc
    if not isinstance(value, dict):
        raise ConfigurationError("Настройки должны быть JSON-объектом.")
    if any(key in value for key in ("token", "oauth_token", "access_token", "YANDEX_DISK_TOKEN")):
        raise ConfigurationError("Удалите токен из настроек: используйте скрытый ввод или YANDEX_DISK_TOKEN.")
    if set(value) - FIELDS:
        raise ConfigurationError("В настройках есть неизвестные поля. Допустимы: disk_path, public_url, output, workers, retries, timeout, verify, watch.")
    return value


def validate(config: dict) -> dict:
    result = {**DEFAULTS, **config}
    for name in ("disk_path", "public_url"):
        if name in result and result[name] is not None:
            if not isinstance(result[name], str) or not result[name].strip() or any(ord(c) < 32 for c in result[name]):
                raise ConfigurationError("Путь на Диске или публичная ссылка должны быть непустой строкой без управляющих символов.")
            result[name] = result[name].strip()
    sources = [name for name in ("disk_path", "public_url") if result.get(name) is not None]
    if len(sources) != 1:
        raise ConfigurationError("Укажите один источник: --disk-path для личной папки или --public-url для публичной ссылки.")
    output = result.get("output")
    if not isinstance(output, str) or not output.strip() or any(ord(c) < 32 for c in output):
        raise ConfigurationError("Укажите локальную папку назначения через --output или в настройках.")
    result["output"] = output.strip()
    for name, low, high in (("workers", 1, 32), ("retries", 0, 10)):
        value = result[name]
        if type(value) is not int or not low <= value <= high:
            raise ConfigurationError(f"Параметр {name} должен быть целым числом от {low} до {high}.")
    for name in ("timeout", "watch"):
        value = result[name]
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ConfigurationError(f"Параметр {name} должен быть конечным числом.")
    if not 1 <= result["timeout"] <= 600:
        raise ConfigurationError("Таймаут должен быть от 1 до 600 секунд.")
    if result["watch"] != 0 and result["watch"] < 60:
        raise ConfigurationError("Пауза --watch должна быть не меньше 60 секунд; 0 выключает повторы.")
    if type(result["verify"]) is not bool:
        raise ConfigurationError("Параметр verify должен быть true или false.")
    return result


def save_config(path: Path, config: dict) -> None:
    """Replace the config atomically, with permissions restricted to its owner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".download-config-", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.chmod(temporary_path, 0o600)
            json.dump({key: value for key, value in config.items() if key in FIELDS}, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def ask(prompt: str, default: str | None = None) -> str:
    label = f"{prompt} [{default}]" if default else prompt
    while True:
        answer = input(label + ": ").strip()
        if answer:
            return answer
        if default is not None:
            return default
        print("Введите значение.")


def configure(existing: dict, path: Path) -> dict:
    if not sys.stdin.isatty():
        raise ConfigurationError("Пошаговая настройка требует терминал. Откройте Terminal или укажите --disk-path и --output.")
    print("Настройка DiskDownloader. На Диске ничего не меняется.")
    print("1 — папка внутри вашего аккаунта; 2 — папка по публичной ссылке.")
    default_mode = "2" if existing.get("public_url") and not existing.get("disk_path") else "1"
    while True:
        mode = ask("Режим", default_mode)
        if mode in ("1", "2"):
            break
        print("Введите 1 или 2.")
    result = {key: value for key, value in existing.items() if key in DEFAULTS}
    if mode == "1":
        result["disk_path"] = ask("Путь к папке на Диске (например /Архив)", existing.get("disk_path"))
        print("Для личной папки нужен OAuth-токен с разрешением cloud_api:disk.read. Инструкция есть в README.md.")
    else:
        result["public_url"] = ask("Публичная ссылка на папку", existing.get("public_url"))
    result["output"] = ask("Локальная папка назначения (относительный путь — от папки настроек)", existing.get("output") or "recovered")
    result = validate(result)
    save_config(path, result)
    print(f"Настройки сохранены: {path}")
    return result


def build_config(args: argparse.Namespace) -> tuple[dict, Path]:
    path = Path(args.config).expanduser().resolve()
    config = load_config(path)
    if args.disk_path is not None:
        config.pop("public_url", None)
        config["disk_path"] = args.disk_path
    elif args.public_url is not None:
        config.pop("disk_path", None)
        config["public_url"] = args.public_url
    for name in ("output", *DEFAULTS):
        value = getattr(args, name)
        if value is not None:
            if name == "output":
                value = str(Path(value).expanduser().resolve())
            config[name] = value
    if args.configure or (not config and sys.stdin.isatty()):
        config = configure(config, path)
        output_base = path.parent
    else:
        output_base = Path.cwd() if args.output is not None else path.parent
    if not config:
        raise ConfigurationError("Нет настроек. Запустите python3 disk_downloader.py --configure в терминале или укажите --disk-path '/Папка' --output './recovered'.")
    config = validate(config)
    output = Path(config["output"]).expanduser()
    config["output"] = str((output_base / output).resolve())
    return config, path


def obtain_token(config: dict, token_store=None) -> str | None:
    if config.get("public_url"):
        return None
    token = os.environ.get("YANDEX_DISK_TOKEN", "").strip()
    if token:
        return normalize_token(token)
    if token_store is not None and token_store.available:
        try:
            token = token_store.load()
        except CredentialError as error:
            print(f"Хранилище токенов: {error}", file=sys.stderr)
        else:
            if token:
                print("Используется токен из системного хранилища.")
                return normalize_token(token)
    if not sys.stdin.isatty():
        raise ConfigurationError("Для личной папки задайте YANDEX_DISK_TOKEN или запустите скрипт в терминале для скрытого ввода токена.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            token = getpass.getpass("OAuth-токен Яндекс Диска (ввод скрыт): ").strip()
    except getpass.GetPassWarning as exc:
        raise ConfigurationError("Скрытый ввод недоступен. Используйте YANDEX_DISK_TOKEN в терминале.") from exc
    if not token:
        raise ConfigurationError("OAuth-токен не введён. Инструкция по получению есть в README.md.")
    token = normalize_token(token)
    if token_store is not None and token_store.available:
        try:
            token_store.save(token)
        except CredentialError as error:
            print(f"Не удалось запомнить токен: {error}", file=sys.stderr)
        else:
            print("Токен сохранён в системном хранилище.")
    return token


def size_text(size: int) -> str:
    value = float(size)
    for unit in ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ"):
        if value < 1024 or unit == "ТиБ":
            return f"{value:.1f} {unit}" if unit != "Б" else f"{size} Б"
        value /= 1024
    return str(size)


def show_report(report: Report, output: Path) -> int:
    print()
    print(f"Скачано: {report.downloaded}; обновлено: {report.updated}; уже сохранено: {report.skipped}; ошибок: {len(report.errors)}.")
    print(f"Получено за этот запуск: {size_text(report.bytes_received)}. Папок просмотрено: {report.directories}.")
    print(f"Отчёт: {output / STATE_DIR / 'last-run.json'}")
    if report.interrupted:
        print("Запуск остановлен. Повторный запуск продолжит восстановление.")
        return 130
    if report.errors:
        print("Часть данных недоступна. Сохранённые файлы остаются на месте; повторите запуск позже.")
        return 2
    print("Обход завершён без ошибок. Новые доступные файлы будут найдены при следующем запуске.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config, config_path = build_config(args)
        store = TokenStore(str(config_path.parent)) if not config.get("public_url") else None
        token = obtain_token(config, token_store=store)
        api = YandexDiskAPI(public_url=config.get("public_url"), disk_path=config.get("disk_path"),
                            token=token, timeout=config["timeout"], retries=config["retries"], log=print)
        output = Path(config["output"])
        print(f"Содержимое исходной папки будет сохранено в: {output}")
        print("Каждый запуск заново обходит источник. Локальные копии исчезнувших с Диска файлов сохраняются.")
        while True:
            report = Downloader(api, output, workers=config["workers"], retries=config["retries"],
                                timeout=config["timeout"], verify=config["verify"], log=print).run()
            code = show_report(report, output)
            if code == 130 or not config["watch"]:
                return code
            print(f"Следующий обход через {config['watch']:g} с. Для остановки нажмите Ctrl+C.")
            time.sleep(config["watch"])
    except KeyboardInterrupt:
        print("\nОстановлено. Повторный запуск продолжит восстановление.")
        return 130
    except (ConfigurationError, ValueError) as exc:
        # Our own validation messages and API constructor validation do not contain secrets.
        print(f"Ошибка настроек: {exc}", file=sys.stderr)
        return 1
    except (ApiError, RecoveryError, OSError) as exc:
        print(f"Не удалось выполнить запуск: {error_text(exc)}", file=sys.stderr)
        return 1
    except sqlite3.Error:
        print("Не удалось открыть или обновить базу учёта файлов в служебной папке. Проверьте доступ и свободное место на диске.", file=sys.stderr)
        return 1
    except EOFError:
        print("Ввод закрыт. Запустите в терминале или передайте настройки через параметры.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
