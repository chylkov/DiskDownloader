"""CLI and configuration checks without credentials, real Disk requests, or prompts."""
import contextlib
import getpass
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import disk_downloader as cli
from yandex_disk.downloader import Report


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.config = self.root / "settings" / "download.json"
        self.config.parent.mkdir()

    def write(self, value):
        self.config.write_text(json.dumps(value), encoding="utf-8")

    def args(self, *arguments):
        return cli.parser().parse_args(["--config", str(self.config), *arguments])

    def test_no_settings_without_terminal_does_not_prompt(self):
        with patch.object(cli.sys.stdin, "isatty", return_value=False), patch("builtins.input") as prompt:
            with self.assertRaisesRegex(cli.ConfigurationError, "Нет настроек"):
                cli.build_config(self.args())
        prompt.assert_not_called()

    def test_saved_relative_output_uses_config_directory(self):
        self.write({"disk_path": "/Архив", "output": "recovered"})
        config, _ = cli.build_config(self.args())
        self.assertEqual(Path(config["output"]), self.config.parent / "recovered")

    def test_command_relative_output_uses_working_directory(self):
        self.write({"disk_path": "/Архив", "output": "recovered"})
        config, _ = cli.build_config(self.args("--output", "new-output"))
        self.assertEqual(Path(config["output"]), Path.cwd() / "new-output")

    def test_command_private_source_clears_public_source(self):
        self.write({"public_url": "https://disk.yandex.ru/d/example", "output": "recovered"})
        config, _ = cli.build_config(self.args("--disk-path", "/Мои файлы"))
        self.assertEqual(config["disk_path"], "/Мои файлы")
        self.assertNotIn("public_url", config)

    def test_command_public_source_clears_private_source(self):
        self.write({"disk_path": "/Архив", "output": "recovered"})
        config, _ = cli.build_config(self.args("--public-url", "https://disk.yandex.ru/d/example"))
        self.assertNotIn("disk_path", config)
        self.assertIsNone(cli.obtain_token(config))

    def test_arguments_do_not_create_config_implicitly(self):
        config, _ = cli.build_config(self.args("--disk-path", "/Архив", "--output", str(self.root / "files")))
        self.assertEqual(config["disk_path"], "/Архив")
        self.assertFalse(self.config.exists())

    def test_token_is_rejected_in_settings_without_exposing_value(self):
        self.write({"disk_path": "/Архив", "output": "files", "token": "SECRET_VALUE"})
        with self.assertRaises(cli.ConfigurationError) as raised:
            cli.load_config(self.config)
        self.assertNotIn("SECRET_VALUE", str(raised.exception))

    def test_invalid_json_has_safe_message(self):
        self.config.write_text('{"private_secret": OOPS', encoding="utf-8")
        with self.assertRaises(cli.ConfigurationError) as raised:
            cli.load_config(self.config)
        self.assertNotIn("private_secret", str(raised.exception))

    def test_numeric_and_boolean_validation(self):
        valid = {"disk_path": "/Архив", "output": "files"}
        for name, value in (("workers", 0), ("workers", 33), ("workers", True), ("retries", -1),
                            ("retries", 11), ("timeout", 0), ("timeout", 601), ("timeout", float("nan")),
                            ("watch", -1), ("watch", 59), ("watch", float("inf")), ("verify", "true")):
            with self.subTest(name=name, value=value), self.assertRaises(cli.ConfigurationError):
                cli.validate({**valid, name: value})
        self.assertEqual(cli.validate({**valid, "watch": 60})["watch"], 60)

    def test_conflicting_sources_rejected(self):
        with self.assertRaises(cli.ConfigurationError):
            cli.validate({"disk_path": "/Архив", "public_url": "https://example.com", "output": "files"})

    def test_wizard_defaults_private_and_saves_both_folders(self):
        with patch.object(cli.sys.stdin, "isatty", return_value=True), patch("builtins.input", side_effect=["", "/Архив", "copy"]), contextlib.redirect_stdout(io.StringIO()):
            config, _ = cli.build_config(self.args())
        self.assertEqual(config["disk_path"], "/Архив")
        self.assertEqual(Path(config["output"]), self.config.parent / "copy")
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["output"], "copy")
        self.assertNotIn("token", saved)
        if os.name != "nt":
            self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_wizard_explicit_output_preserves_command_path_semantics(self):
        with patch.object(cli.sys.stdin, "isatty", return_value=True), patch("builtins.input", side_effect=["", "/Архив", ""]), contextlib.redirect_stdout(io.StringIO()):
            config, _ = cli.build_config(self.args("--configure", "--output", "from-cwd"))
        self.assertEqual(Path(config["output"]), Path.cwd() / "from-cwd")

    def test_existing_config_does_not_open_wizard(self):
        self.write({"disk_path": "/Архив", "output": "files"})
        with patch.object(cli.sys.stdin, "isatty", return_value=True), patch("builtins.input") as prompt:
            cli.build_config(self.args())
        prompt.assert_not_called()

    def test_private_token_requires_terminal_or_environment(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(cli.sys.stdin, "isatty", return_value=False), patch.object(getpass, "getpass") as prompt:
            with self.assertRaisesRegex(cli.ConfigurationError, "YANDEX_DISK_TOKEN"):
                cli.obtain_token({"disk_path": "/Архив"})
        prompt.assert_not_called()

    def test_environment_token_does_not_prompt(self):
        with patch.dict(os.environ, {"YANDEX_DISK_TOKEN": "token123"}, clear=True), patch.object(getpass, "getpass") as prompt:
            self.assertEqual(cli.obtain_token({"disk_path": "/Архив"}), "token123")
        prompt.assert_not_called()

    def test_hidden_token_warning_prevents_insecure_input(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(cli.sys.stdin, "isatty", return_value=True), patch.object(getpass, "getpass", side_effect=getpass.GetPassWarning()):
            with self.assertRaisesRegex(cli.ConfigurationError, "Скрытый ввод недоступен"):
                cli.obtain_token({"disk_path": "/Архив"})

    def run_main(self, report=None, failure=None, watch=0):
        self.write({"disk_path": "/Архив", "output": str(self.root / "files"), "watch": watch})
        output = io.StringIO()
        with patch.dict(os.environ, {"YANDEX_DISK_TOKEN": "SECRET_VALUE"}, clear=True), patch.object(cli, "YandexDiskAPI"), patch.object(cli, "Downloader") as downloader, contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            if failure is not None:
                downloader.return_value.run.side_effect = failure
            else:
                downloader.return_value.run.return_value = report or Report()
            code = cli.main(["--config", str(self.config)])
        self.assertNotIn("SECRET_VALUE", output.getvalue())
        return code, output.getvalue()

    def test_successful_run_exit_code(self):
        code, text = self.run_main(Report(downloaded=3, skipped=2))
        self.assertEqual(code, 0)
        self.assertIn("Скачано: 3", text)

    def test_partial_run_exit_code(self):
        code, _ = self.run_main(Report(errors=[{"path": "x", "error": "unavailable"}]))
        self.assertEqual(code, 2)

    def test_interrupted_run_exit_code(self):
        code, _ = self.run_main(Report(interrupted=True), watch=60)
        self.assertEqual(code, 130)

    def test_keyboard_interrupt_exit_code(self):
        code, _ = self.run_main(failure=KeyboardInterrupt())
        self.assertEqual(code, 130)

    def test_sqlite_failure_is_readable_and_does_not_expose_fields(self):
        code, text = self.run_main(failure=sqlite3.DatabaseError("SECRET_VALUE"))
        self.assertEqual(code, 1)
        self.assertIn("базу учёта", text)

    def test_watch_retries_after_partial_failure_with_fresh_downloader(self):
        self.write({"disk_path": "/Архив", "output": str(self.root / "files"), "watch": 60})
        failure = Report(errors=[{"path": "x", "error": "unavailable"}])
        with patch.dict(os.environ, {"YANDEX_DISK_TOKEN": "token123"}, clear=True), patch.object(cli, "YandexDiskAPI"), patch.object(cli, "Downloader") as downloader, patch.object(cli.time, "sleep", side_effect=[None, KeyboardInterrupt()]), contextlib.redirect_stdout(io.StringIO()):
            downloader.return_value.run.side_effect = [failure, Report()]
            self.assertEqual(cli.main(["--config", str(self.config)]), 130)
        self.assertEqual(downloader.call_count, 2)


if __name__ == "__main__":
    unittest.main()
