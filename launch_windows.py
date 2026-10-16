"""Checked Windows entry point shared by the two double-click launchers.

The batch files locate or prepare a real Python 3.10+ interpreter first. This
module checks GUI support, prepares the app's private runtime if Tk is missing,
and starts the application without a command shell, preserving its exit status.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import subprocess
import sys


MINIMUM_PYTHON = (3, 10)
SCRIPTS = {"gui": "disk_downloader_gui.py", "cli": "disk_downloader.py"}


class LauncherError(Exception):
    """A safe startup error that never includes command arguments or tokens."""

    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


class TkUnavailableError(LauncherError):
    """The GUI can recover by installing the app's private Python runtime."""


def check_runtime(mode: str) -> None:
    if mode not in SCRIPTS:
        raise LauncherError("Используйте run-gui.bat для формы или run.bat для командной строки.")
    if sys.version_info < MINIMUM_PYTHON:
        raise LauncherError("Нужен Python 3.10 или новее: https://www.python.org/downloads/windows/")
    if mode == "gui":
        try:
            importlib.import_module("tkinter")
        except (ImportError, OSError, RuntimeError):
            raise TkUnavailableError(
                "В выбранном Python недоступен Tkinter. Установите Python с поддержкой Tcl/Tk "
                "с https://www.python.org/downloads/windows/ . Если используется .venv, "
                "создайте её заново этим Python."
            ) from None


def child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    # Only our pinned bootstrap installs a runtime. Disable automatic installs
    # by the Windows Python manager and launcher modes that expose arguments.
    environment["PYTHON_MANAGER_AUTOMATIC_INSTALL"] = "false"
    for key in ("PYLAUNCHER_ALLOW_INSTALL", "PYLAUNCHER_DEBUG", "PYLAUNCHER_DRYRUN",
                "PYMANAGER_DEBUG", "PYMANAGER_VERBOSE"):
        environment.pop(key, None)
    return environment


def prepare_python(directory: Path, environment: dict[str, str], run) -> Path:
    """Install/repair the private runtime once and verify it before launching."""
    if environment.get("DISK_DOWNLOADER_BOOTSTRAP_ATTEMPTED") == "1":
        raise LauncherError("После автоматической установки Tkinter недоступен. "
                            "Повторите запуск или установите Python с Tcl/Tk вручную.")
    bootstrap = directory / "bootstrap-python.ps1"
    if not bootstrap.is_file():
        raise LauncherError("Не найден bootstrap-python.ps1 для установки Python с Tkinter. "
                            "Распакуйте проект целиком и повторите запуск.")
    appdata = environment.get("LOCALAPPDATA")
    if not appdata:
        raise LauncherError("Не удалось определить папку данных Windows для установки Python с Tkinter.")
    executable = Path(appdata) / "DiskDownloader" / "Python" / "python.exe"
    print("В выбранном Python нет Tkinter. Подготовка Python для DiskDownloader…", flush=True)
    command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(bootstrap)]
    try:
        result = run(command, cwd=str(directory), env=environment, shell=False)
        if result.returncode != 0:
            raise LauncherError("Автоматическая установка Python не завершена.", result.returncode)
        if not executable.is_file():
            raise LauncherError("После установки не найден Python для DiskDownloader. Повторите запуск.")
        probe = run(
            [str(executable), "-I", "-c",
             "import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 10) else 1)"],
            cwd=str(directory), env=environment, shell=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        raise LauncherError("Не удалось подготовить Python с Tkinter автоматически. "
                            "Повторите запуск или установите Python с Tcl/Tk вручную.") from None
    if probe.returncode != 0:
        raise LauncherError("Установленный Python не прошёл проверку версии или Tkinter. Повторите запуск.")
    return executable


def launch(mode: str, arguments: list[str], *, project_dir: Path | None = None,
           run=None) -> int:
    """Launch one application; GUI tests can supply a fake subprocess runner."""
    needs_tk = False
    try:
        check_runtime(mode)
    except TkUnavailableError:
        needs_tk = True
    directory = Path(project_dir if project_dir is not None else Path(__file__).parent).resolve()
    script = directory / SCRIPTS[mode]
    if not script.is_file():
        raise LauncherError("Не найден файл приложения. Распакуйте проект целиком и повторите запуск.")
    environment = child_environment()
    runner = subprocess.run if run is None else run
    try:
        # Launch the application directly after verification, never the helper
        # again: a damaged Tk installation cannot cause a bootstrap loop.
        executable = str(prepare_python(directory, environment, runner)) if needs_tk else sys.executable
        command = [executable, "-X", "utf8", str(script), *arguments]
        result = runner(command, cwd=str(directory), env=environment, shell=False)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError):
        raise LauncherError("Не удалось запустить приложение выбранным Python. Проверьте установку Python и .venv.") from None
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if not arguments:
            raise LauncherError("Используйте run-gui.bat для формы или run.bat для командной строки.")
        return launch(arguments[0], arguments[1:])
    except LauncherError as error:
        print(str(error), file=sys.stderr)
        return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
