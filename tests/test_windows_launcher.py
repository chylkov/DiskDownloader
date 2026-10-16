"""Windows startup checks without an account, installed packages, or GUI windows.

Portable tests use a fake child process. On Windows, isolated batch smoke tests
run a tiny replacement helper rather than the downloader or its real settings.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import venv

import launch_windows as launcher


class WindowsLauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve() / "Диск с пробелами & 100% ! (копия)"
        self.project.mkdir()
        for script in launcher.SCRIPTS.values():
            (self.project / script).write_text("# test fixture, never executed\n", encoding="utf-8")
        self.runner = Mock(return_value=SimpleNamespace(returncode=0))
        self.enterContext(patch.object(launcher.sys, "version_info", (3, 12, 0)))
        self.tk_import = self.enterContext(patch.object(launcher.importlib, "import_module"))

    def launch(self, mode="gui", arguments=None):
        return launcher.launch(mode, arguments or [], project_dir=self.project, run=self.runner)

    def test_gui_checks_tk_and_keeps_unicode_paths_and_arguments_out_of_the_shell(self):
        arguments = ["--output", 'C:\\Мои файлы & копии\\100% ! (архив)',
                     "--disk-path", '/Фото с пробелами & "кавычкой"']
        self.assertEqual(self.launch(arguments=arguments), 0)
        self.tk_import.assert_called_once_with("tkinter")
        positional, options = self.runner.call_args
        self.assertEqual(positional, ([sys.executable, "-X", "utf8",
                                       str(self.project / "disk_downloader_gui.py"), *arguments],))
        self.assertEqual(options["cwd"], str(self.project))
        self.assertIs(options["shell"], False)

    def test_cli_does_not_require_tk_and_returns_child_status(self):
        self.runner.return_value.returncode = 23
        self.assertEqual(self.launch("cli", ["--help"]), 23)
        self.tk_import.assert_not_called()
        self.assertEqual(self.runner.call_args.args[0][-2:],
                         [str(self.project / "disk_downloader.py"), "--help"])

    def test_missing_tk_is_actionable_and_never_starts_the_app_or_reveals_errors(self):
        for error in (ImportError("SECRET_EXCEPTION"), OSError("SECRET_EXCEPTION"),
                      RuntimeError("SECRET_EXCEPTION")):
            with self.subTest(error=type(error).__name__):
                self.tk_import.side_effect = error
                with self.assertRaisesRegex(launcher.LauncherError, "Tkinter") as raised:
                    self.launch()
                self.assertNotIn("SECRET_EXCEPTION", str(raised.exception))
        self.runner.assert_not_called()

    def test_old_python_fails_before_tk_or_child_start(self):
        with patch.object(launcher.sys, "version_info", (3, 9, 20)):
            with self.assertRaisesRegex(launcher.LauncherError, "3.10"):
                self.launch()
        self.tk_import.assert_not_called()
        self.runner.assert_not_called()

    def test_python_310_is_supported(self):
        with patch.object(launcher.sys, "version_info", (3, 10, 0)):
            self.assertEqual(self.launch(), 0)

    def test_child_environment_is_utf8_and_disables_auto_installs_and_argument_debugging(self):
        keys = ("PYLAUNCHER_ALLOW_INSTALL", "PYLAUNCHER_DEBUG", "PYLAUNCHER_DRYRUN",
                "PYMANAGER_DEBUG", "PYMANAGER_VERBOSE")
        with patch.dict(os.environ, {**dict.fromkeys(keys, "1"), "PYTHON_MANAGER_AUTOMATIC_INSTALL": "true"}):
            self.launch()
            self.assertTrue(all(os.environ[key] == "1" for key in keys), "parent environment must stay unchanged")
        environment = self.runner.call_args.kwargs["env"]
        self.assertEqual(environment["PYTHONUTF8"], "1")
        self.assertEqual(environment["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(environment["PYTHON_MANAGER_AUTOMATIC_INSTALL"], "false")
        self.assertTrue(all(key not in environment for key in keys))

    def test_spawn_failure_is_safe_and_keyboard_interrupt_is_preserved(self):
        self.runner.side_effect = OSError("SECRET_TOKEN https://signed.invalid/private")
        with self.assertRaises(launcher.LauncherError) as raised:
            self.launch()
        self.assertNotIn("SECRET", str(raised.exception))
        self.assertNotIn("signed.invalid", str(raised.exception))
        self.runner.side_effect = KeyboardInterrupt()
        self.assertEqual(self.launch(), 130)

    def test_missing_application_does_not_start_a_child(self):
        (self.project / "disk_downloader_gui.py").unlink()
        with self.assertRaisesRegex(launcher.LauncherError, "целиком"):
            self.launch()
        self.runner.assert_not_called()

    def test_main_handles_unknown_mode_without_printing_supplied_arguments(self):
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            self.assertEqual(launcher.main(["SECRET_MODE", "SECRET_ARGUMENT"]), 1)
        self.assertIn("run-gui.bat", stream.getvalue())
        self.assertNotIn("SECRET", stream.getvalue())
        self.runner.assert_not_called()

    def test_main_preserves_forwarded_arguments_and_exit_status(self):
        with patch.object(launcher, "launch", return_value=7) as launch:
            self.assertEqual(launcher.main(["cli", "--output", "Копия & архив"]), 7)
        launch.assert_called_once_with("cli", ["--output", "Копия & архив"])

    def prepare_bootstrap_fixture(self, *, managed_exists=True):
        (self.project / "bootstrap-python.ps1").write_text("# never executed\n", encoding="utf-8")
        appdata = self.project / "Local App Data & данные"
        self.enterContext(patch.dict(os.environ, {"LOCALAPPDATA": str(appdata),
                                                "DISK_DOWNLOADER_BOOTSTRAP_ATTEMPTED": ""}))
        executable = appdata / "DiskDownloader" / "Python" / "python.exe"
        if managed_exists:
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"fake managed interpreter, never executed")
        self.tk_import.side_effect = ImportError("PRIVATE_EXCEPTION")
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        return executable

    def test_missing_tk_installs_once_verifies_private_runtime_then_runs_app_directly(self):
        executable = self.prepare_bootstrap_fixture()
        self.runner.side_effect = [SimpleNamespace(returncode=0), SimpleNamespace(returncode=0),
                                   SimpleNamespace(returncode=7)]
        arguments = ["--output", "Копия & Архив!", "--disk-path", "/Фото с пробелами"]
        self.assertEqual(self.launch(arguments=arguments), 7)
        self.assertEqual(self.runner.call_count, 3)
        bootstrap, probe, application = self.runner.call_args_list
        self.assertEqual(bootstrap.args[0], ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                             "-File", str(self.project / "bootstrap-python.ps1")])
        self.assertEqual(probe.args[0][0], str(executable))
        self.assertIn("import sys, tkinter", probe.args[0][-1])
        self.assertEqual(application.args[0], [str(executable), "-X", "utf8",
                                               str(self.project / "disk_downloader_gui.py"), *arguments])
        for call in self.runner.call_args_list:
            self.assertIs(call.kwargs["shell"], False)
            self.assertEqual(call.kwargs["cwd"], str(self.project))

    def test_failed_bootstrap_preserves_its_status_and_does_not_start_the_application(self):
        self.prepare_bootstrap_fixture()
        self.runner.return_value.returncode = 19
        with self.assertRaises(launcher.LauncherError) as raised:
            self.launch()
        self.assertEqual(raised.exception.exit_code, 19)
        self.assertEqual(self.runner.call_count, 1)
        stream = io.StringIO()
        with patch.object(launcher, "launch", side_effect=raised.exception), contextlib.redirect_stderr(stream):
            self.assertEqual(launcher.main(["gui"]), 19)

    def test_successful_bootstrap_without_an_interpreter_does_not_start_the_application(self):
        self.prepare_bootstrap_fixture(managed_exists=False)
        with self.assertRaisesRegex(launcher.LauncherError, "не найден Python"):
            self.launch()
        self.assertEqual(self.runner.call_count, 1)

    def test_bad_private_runtime_does_not_repeat_installation_or_start_the_application(self):
        self.prepare_bootstrap_fixture()
        self.runner.side_effect = [SimpleNamespace(returncode=0), SimpleNamespace(returncode=1)]
        with self.assertRaisesRegex(launcher.LauncherError, "не прошёл проверку"):
            self.launch()
        self.assertEqual(self.runner.call_count, 2)

    def test_batch_bootstrap_marker_prevents_a_second_install_attempt_for_missing_tk(self):
        self.prepare_bootstrap_fixture()
        with patch.dict(os.environ, {"DISK_DOWNLOADER_BOOTSTRAP_ATTEMPTED": "1"}):
            with self.assertRaisesRegex(launcher.LauncherError, "Tkinter недоступен"):
                self.launch()
        self.runner.assert_not_called()

    def test_bootstrap_failure_and_timeout_do_not_reveal_arguments_or_internal_errors(self):
        self.prepare_bootstrap_fixture()
        for error in (OSError("PRIVATE_EXCEPTION"),
                      subprocess.TimeoutExpired(["PRIVATE_ARGUMENT"], 30)):
            with self.subTest(error=type(error).__name__):
                self.runner.side_effect = error
                with self.assertRaises(launcher.LauncherError) as raised:
                    self.launch()
                self.assertNotIn("PRIVATE", str(raised.exception))


@unittest.skipUnless(os.name == "nt", "requires Windows cmd.exe")
class WindowsBatchSmokeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.project = self.base / "Диск & Архив! 100% (копия)"
        self.project.mkdir()
        source = Path(launcher.__file__).parent
        for filename in ("run.bat", "run-gui.bat"):
            shutil.copyfile(source / filename, self.project / filename)
        (self.project / "launch_windows.py").write_text(
            "import json, os, pathlib, sys\n"
            "pathlib.Path(__file__).with_name('observed.json').write_text(\n"
            "    json.dumps({'args': sys.argv[1:], 'python': sys.executable,\n"
            "                'bootstrapped': os.environ.get('DISK_DOWNLOADER_BOOTSTRAP_ATTEMPTED')}, ensure_ascii=False),\n"
            "    encoding='utf-8')\n"
            "raise SystemExit(23 if sys.argv[1] == 'gui' else 0)\n",
            encoding="utf-8",
        )
        self.observed = self.project / "observed.json"

    def run_batch(self, filename, arguments=()):
        # Fixtures contain no double quotes or paired % variables. Explicitly
        # quote every token for cmd, including ampersands and parentheses.
        tokens = [str(self.project / filename), *arguments]
        command = '"' + " ".join('"' + token + '"' for token in tokens) + '"'
        comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
        environment = os.environ.copy()
        environment["PYLAUNCHER_DEBUG"] = "1"
        environment["PYLAUNCHER_DRYRUN"] = "1"
        environment["LOCALAPPDATA"] = str(self.base / "Local App Data & данные")
        environment["DISK_DOWNLOADER_TEST_PYTHON"] = sys.executable
        return subprocess.run(
            f'"{comspec}" /d /s /c {command}', shell=False, cwd=str(self.base), env=environment,
            input="\n", capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )

    def test_cli_bootstraps_from_another_working_directory_and_passes_quoted_arguments(self):
        arguments = ["--output", "Копия & Архив! 100% (папка)", "--disk-path", "/Фото & видео"]
        result = self.run_batch("run.bat", arguments)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(self.observed.read_text(encoding="utf-8"))
        self.assertEqual(payload["args"], ["cli", *arguments])
        self.assertNotIn(arguments[-1], result.stdout + result.stderr)

    def test_gui_errors_are_visible_and_keep_the_application_exit_status(self):
        result = self.run_batch("run-gui.bat")
        self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
        self.assertEqual(json.loads(self.observed.read_text(encoding="utf-8"))["args"], ["gui"])
        self.assertIn("Нажмите", result.stdout)

    def test_broken_windows_environment_stops_instead_of_using_global_python(self):
        (self.project / ".venv" / "Scripts").mkdir(parents=True)
        result = self.run_batch("run.bat")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(".venv", result.stdout)
        self.assertFalse(self.observed.exists())

    def test_macos_environment_is_ignored_on_windows(self):
        binary = self.project / ".venv" / "bin" / "python3"
        binary.parent.mkdir(parents=True)
        binary.write_text("not a Windows executable", encoding="utf-8")
        result = self.run_batch("run.bat")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.observed.exists())

    def test_project_windows_environment_has_priority(self):
        # No pip or downloads: only an isolated stdlib venv in the temp fixture.
        directory = self.project / ".venv"
        venv.EnvBuilder(with_pip=False).create(directory)
        managed = self.managed_python()
        managed.parent.mkdir(parents=True)
        managed.write_bytes(b"must not execute this invalid managed runtime")
        result = self.run_batch("run.bat")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        executable = Path(json.loads(self.observed.read_text(encoding="utf-8"))["python"])
        self.assertEqual(executable.resolve(), (directory / "Scripts" / "python.exe").resolve())

    def managed_python(self):
        return self.base / "Local App Data & данные" / "DiskDownloader" / "Python" / "python.exe"

    def test_existing_private_runtime_has_priority_over_global_python(self):
        executable = self.managed_python()
        venv.EnvBuilder(with_pip=False).create(executable.parent)
        # The product uses a full install. The fixture uses a no-pip venv, whose
        # Windows executable lives in Scripts; copy only its launcher to root.
        shutil.copyfile(executable.parent / "Scripts" / "python.exe", executable)
        result = self.run_batch("run.bat")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        chosen = Path(json.loads(self.observed.read_text(encoding="utf-8"))["python"])
        self.assertEqual(chosen.resolve(), executable.resolve())

    def test_damaged_private_runtime_runs_bootstrap_and_rechecks_it_before_launch(self):
        executable = self.managed_python()
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"broken test runtime")
        # Isolated replacement bootstrap: copies this test's already installed
        # Python into a temp venv without pip, installers, or network access.
        (self.project / "bootstrap-python.ps1").write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "$target = Join-Path $env:LOCALAPPDATA 'DiskDownloader\\Python'\n"
            "& $env:DISK_DOWNLOADER_TEST_PYTHON -m venv --without-pip $target\n"
            "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
            "Copy-Item -LiteralPath (Join-Path $target 'Scripts\\python.exe') -Destination (Join-Path $target 'python.exe') -Force\n"
            "exit 0\n",
            encoding="utf-8",
        )
        result = self.run_batch("run.bat")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(self.observed.read_text(encoding="utf-8"))
        self.assertEqual(Path(payload["python"]).resolve(), executable.resolve())
        self.assertEqual(payload["bootstrapped"], "1")

    def test_bootstrap_failure_does_not_start_the_application_and_preserves_status(self):
        executable = self.managed_python()
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"broken test runtime")
        (self.project / "bootstrap-python.ps1").write_text("exit 19\n", encoding="utf-8")
        result = self.run_batch("run.bat")
        self.assertEqual(result.returncode, 19, result.stdout + result.stderr)
        self.assertFalse(self.observed.exists())


if __name__ == "__main__":
    unittest.main()
