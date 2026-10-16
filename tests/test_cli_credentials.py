"""Persistent credential flows without touching the user's actual Keychain."""
import contextlib
import io
import os
import unittest
from unittest.mock import Mock, patch

import disk_downloader as cli
from yandex_disk.credentials import CredentialError


class CLICredentialTests(unittest.TestCase):
    def setUp(self):
        self.store = Mock(available=True)
        self.store.load.return_value = None
        self.config = {"disk_path": "/Фото"}
        self.output = io.StringIO()
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(contextlib.redirect_stdout(self.output))
        self.enterContext(contextlib.redirect_stderr(self.output))

    def test_saved_token_runs_without_terminal_or_prompt(self):
        self.store.load.return_value = "SAVED_TEST_TOKEN"
        with patch.object(cli.sys.stdin, "isatty", return_value=False), patch.object(cli.getpass, "getpass") as prompt:
            self.assertEqual(cli.obtain_token(self.config, self.store), "SAVED_TEST_TOKEN")
        prompt.assert_not_called()
        self.store.save.assert_not_called()
        self.assertNotIn("SAVED_TEST_TOKEN", self.output.getvalue())

    def test_environment_overrides_saved_token_without_modifying_keychain(self):
        with patch.dict(os.environ, {"YANDEX_DISK_TOKEN": "ENV_TEST_TOKEN"}):
            self.assertEqual(cli.obtain_token(self.config, self.store), "ENV_TEST_TOKEN")
        self.store.load.assert_not_called()
        self.store.save.assert_not_called()

    def test_public_link_never_accesses_keychain(self):
        self.assertIsNone(cli.obtain_token({"public_url": "https://disk.yandex.ru/d/example"}, self.store))
        self.store.load.assert_not_called()
        self.store.save.assert_not_called()

    def test_new_hidden_token_is_remembered_for_next_launch(self):
        with patch.object(cli.sys.stdin, "isatty", return_value=True), patch.object(cli.getpass, "getpass", return_value=" NEW_TEST_TOKEN "):
            self.assertEqual(cli.obtain_token(self.config, self.store), "NEW_TEST_TOKEN")
        self.store.save.assert_called_once_with("NEW_TEST_TOKEN")
        self.assertNotIn("NEW_TEST_TOKEN", self.output.getvalue())

    def test_denied_storage_still_allows_using_entered_token(self):
        self.store.save.side_effect = CredentialError("Доступ к Связке ключей отменён.")
        with patch.object(cli.sys.stdin, "isatty", return_value=True), patch.object(cli.getpass, "getpass", return_value="MANUAL_TEST_TOKEN"):
            self.assertEqual(cli.obtain_token(self.config, self.store), "MANUAL_TEST_TOKEN")
        self.assertNotIn("MANUAL_TEST_TOKEN", self.output.getvalue())

    def test_failed_load_falls_back_to_hidden_prompt(self):
        self.store.load.side_effect = CredentialError("Связка ключей заблокирована или сейчас недоступна.")
        with patch.object(cli.sys.stdin, "isatty", return_value=True), patch.object(cli.getpass, "getpass", return_value="MANUAL_TEST_TOKEN"):
            self.assertEqual(cli.obtain_token(self.config, self.store), "MANUAL_TEST_TOKEN")
        self.assertNotIn("MANUAL_TEST_TOKEN", self.output.getvalue())

    def test_unavailable_platform_does_not_attempt_persistence(self):
        self.store.available = False
        with patch.object(cli.sys.stdin, "isatty", return_value=True), patch.object(cli.getpass, "getpass", return_value="MANUAL_TEST_TOKEN"):
            self.assertEqual(cli.obtain_token(self.config, self.store), "MANUAL_TEST_TOKEN")
        self.store.load.assert_not_called()
        self.store.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
