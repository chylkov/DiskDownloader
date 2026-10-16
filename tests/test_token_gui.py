"""Token UI checks use a fake vault and never read the user's Keychain.

Set DISK_DOWNLOADER_GUI_TESTS=1 in a desktop session to exercise Tk widgets.
Only the fake vault runs in the credential worker; API calls stay mocked.
"""

import gc
import json
import os
from pathlib import Path
import queue
import tempfile
from threading import Thread
import time
import tkinter as tk
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import disk_downloader_gui as gui


def assert_token_browser_call(test, browser_open):
    browser_open.assert_called_once_with(gui.OAUTH_TOKEN_URL, new=2)
    address = urlsplit(browser_open.call_args.args[0])
    test.assertEqual((address.scheme, address.netloc, address.path),
                     ("https", "oauth.yandex.ru", "/authorize"))
    query = parse_qs(address.query)
    test.assertEqual(query.get("client_id"), ["98979c5296c34f9faea9ab76847efb68"])
    test.assertEqual(query.get("response_type"), ["token"])
    test.assertNotIn("access_token", query)
    test.assertEqual(address.fragment, "")


class TokenPageWorkerTests(unittest.TestCase):
    def test_worker_opens_only_public_oauth_url_and_queues_completion(self):
        events = queue.Queue()
        with patch("webbrowser.open", return_value=True) as browser_open:
            gui._open_token_page(events, 4, 8)
        assert_token_browser_call(self, browser_open)
        self.assertEqual(events.get_nowait(), ("token_browser_result", {
            "opened": True, "generation": 4, "revision": 8,
        }))
        self.assertTrue(events.empty())

    def test_worker_handles_false_or_exception_without_leaking_internal_data(self):
        for failure in (False, RuntimeError("PRIVATE_TOKEN https://private.invalid/secret")):
            with self.subTest(failure=type(failure).__name__):
                events = queue.Queue()
                with patch("webbrowser.open", return_value=False,
                           side_effect=failure if isinstance(failure, Exception) else None):
                    gui._open_token_page(events, 2, 3)
                event = events.get_nowait()
                self.assertEqual(event, ("token_browser_result", {
                    "opened": False, "generation": 2, "revision": 3,
                }))
                self.assertNotIn("PRIVATE", repr(event))
                self.assertNotIn("private.invalid", repr(event))
                self.assertTrue(events.empty())


class FakeTokenStore:
    def __init__(self, token=None, *, available=True):
        self.available = available
        self.token = token
        self.loads = 0
        self.saved = []
        self.deleted = 0
        self.save_error = None

    def load(self):
        self.loads += 1
        return self.token

    def save(self, token):
        if self.save_error:
            raise self.save_error
        self.saved.append(token)
        self.token = token

    def delete(self):
        self.deleted += 1
        self.token = None


@unittest.skipUnless(os.environ.get("DISK_DOWNLOADER_GUI_TESTS") == "1", "requires a desktop Tk session")
class TokenFormTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gui.configure_tk_runtime()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.config = self.directory / "download-gui.json"
        self.store = FakeTokenStore()
        self.enterContext(patch.dict(os.environ, {"YANDEX_DISK_TOKEN": ""}))
        self.errors = self.enterContext(patch.object(gui.messagebox, "showerror"))
        self.enterContext(patch.object(gui.messagebox, "showinfo"))
        self.enterContext(patch.object(gui.messagebox, "askyesno", return_value=True))
        self.api_factory = self.enterContext(patch.object(gui, "YandexDiskAPI"))
        self.browser_open = self.enterContext(patch("webbrowser.open", return_value=True))
        self.root = tk.Tk()
        self.root.withdraw()
        # Destroyed Tk objects must be freed on the main thread, not by a later
        # garbage collection inside some worker thread (Tcl aborts the process).
        self.addCleanup(gc.collect)
        self.addCleanup(self.destroy_root)
        self.app = None

    def open_app(self):
        self.app = gui.DiskDownloaderApp(self.root, config_path=self.config,
                                         legacy_path=None, token_store=self.store)
        self.app.base_var.set(str(self.directory / "copies"))
        return self.app

    def test_windows_store_is_named_without_mac_specific_ui_text(self):
        self.store.storage_name = "Диспетчер учётных данных Windows"
        app = self.open_app()
        self.assertIn(self.store.storage_name, app.storage_hint)
        self.assertNotIn("macOS", app.credential_var.get())
        self.finish_workers()
        self.deliver_events()
        self.assertIn(self.store.storage_name, app.credential_var.get())

    def destroy_root(self):
        try:
            if self.app and not self.app.closed:
                self.app.destroy()
            else:
                self.root.destroy()
        except tk.TclError:
            pass

    def finish_workers(self):
        deadline = time.monotonic() + 3
        while self.app.credential_jobs.unfinished_tasks:
            if time.monotonic() >= deadline:
                self.fail("fake credential worker did not finish within three seconds")
            time.sleep(0.005)

    def deliver_events(self):
        while True:
            try:
                kind, data = self.app.events.get_nowait()
            except queue.Empty:
                return
            self.app.handle_event(kind, data)

    def finish_and_deliver(self):
        self.finish_workers()
        self.deliver_events()

    def last_error(self):
        self.errors.assert_called()
        return self.errors.call_args.args[1]

    def token_button(self):
        matches = [control for control in self.app.controls
                   if isinstance(control, gui.ttk.Button) and control.cget("text") == "Получить токен"]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def start_token_browser(self):
        with patch.object(gui.threading, "Thread") as factory:
            self.token_button().invoke()
        factory.return_value.start.assert_called_once_with()
        self.browser_open.assert_not_called()
        options = factory.call_args.kwargs
        self.assertIsNone(getattr(options["target"], "__self__", None), "worker must not retain the Tk application")
        self.assertTrue(all(value is not self.app for value in options.get("args", ())))
        return options

    def finish_token_browser(self, options):
        thread = Thread(target=options["target"], args=options.get("args", ()),
                        kwargs=options.get("kwargs", {}))
        thread.start()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive(), "mock browser worker did not finish")
        kind, data = self.app.events.get_nowait()
        self.assertEqual(kind, "token_browser_result")
        return kind, data

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("mock worker did not finish within three seconds")
            time.sleep(0.005)

    def test_saved_token_prefills_only_after_main_thread_handles_result(self):
        self.store.token = "FAKE_SAVED_TOKEN"
        app = self.open_app()
        self.assertEqual(app.token_var.get(), "")
        self.finish_workers()
        self.assertEqual(self.store.loads, 1)
        self.assertEqual(app.token_var.get(), "", "worker must not mutate a Tk variable")
        self.deliver_events()
        self.assertEqual(app.token_var.get(), "FAKE_SAVED_TOKEN")
        self.assertFalse(self.config.exists())
        self.api_factory.assert_not_called()

    def test_environment_token_takes_priority_over_keychain(self):
        self.store.token = "FAKE_OLD_TOKEN"
        with patch.dict(os.environ, {"YANDEX_DISK_TOKEN": "FAKE_ENV_TOKEN"}):
            app = self.open_app()
        self.finish_and_deliver()
        self.assertEqual(app.token_var.get(), "FAKE_ENV_TOKEN")
        self.assertEqual(self.store.saved, [])

    def test_delayed_load_does_not_replace_manually_typed_token(self):
        self.store.token = "FAKE_SAVED_TOKEN"
        app = self.open_app()
        app.token_var.set("FAKE_NEW_TOKEN")
        self.finish_and_deliver()
        self.assertEqual(app.token_var.get(), "FAKE_NEW_TOKEN")

    def test_delayed_load_does_not_refill_field_cleared_by_user(self):
        self.store.token = "FAKE_SAVED_TOKEN"
        app = self.open_app()
        app.token_var.set("FAKE_NEW_TOKEN")
        app.token_var.set("")
        self.finish_and_deliver()
        self.assertEqual(app.token_var.get(), "")

    def test_manual_save_keeps_token_out_of_json_and_visible_log(self):
        app = self.open_app()
        self.finish_and_deliver()
        app.add_tasks(["/Фото"])
        secret = "FAKE_MANUAL_TOKEN"
        app.token_var.set("  " + secret + "  ")
        app.save_token()
        self.finish_and_deliver()
        self.assertEqual(self.store.saved, [secret])
        content = self.config.read_text(encoding="utf-8")
        self.assertNotIn(secret, content)
        self.assertNotIn("token", json.dumps(json.loads(content)).casefold())
        self.assertNotIn(secret, app.log_widget.get("1.0", "end"))

    def test_forget_removes_saved_record_and_clears_current_field(self):
        self.store.token = "FAKE_SAVED_TOKEN"
        app = self.open_app()
        self.finish_and_deliver()
        app.forget_token()
        self.finish_and_deliver()
        self.assertEqual(self.store.deleted, 1)
        self.assertIsNone(self.store.token)
        self.assertEqual(app.token_var.get(), "")

    def test_pending_load_cannot_restore_token_after_forget(self):
        self.store.token = "FAKE_SAVED_TOKEN"
        app = self.open_app()
        self.finish_workers()
        app.forget_token()
        self.finish_and_deliver()
        self.assertEqual(self.store.deleted, 1)
        self.assertEqual(app.token_var.get(), "")

    def test_storage_failure_is_sanitized_and_does_not_stop_connection(self):
        app = self.open_app()
        self.finish_and_deliver()
        secret = "FAKE_PERMISSION_TEST_TOKEN"
        self.store.save_error = RuntimeError("permission denied for " + secret)
        app.token_var.set(secret)
        with patch.object(app, "load_remote") as load_remote:
            app.connect()
            self.finish_and_deliver()
        load_remote.assert_called_once()
        self.assertEqual(app.token_var.get(), secret)
        self.assertEqual(self.store.saved, [])
        visible = app.log_widget.get("1.0", "end") + app.credential_var.get()
        self.assertNotIn(secret, visible)
        self.assertNotIn("permission denied for", visible)
        self.assertNotEqual(app.credential_var.get(), "")
        self.assertFalse(self.config.exists())

    def test_connect_and_start_remember_same_token_only_once(self):
        app = self.open_app()
        self.finish_and_deliver()
        app.token_var.set("FAKE_REUSED_TOKEN")
        with patch.object(app, "load_remote"):
            app.connect()
            app.connect()
            self.finish_and_deliver()
        app.add_tasks(["/Фото"])
        with patch.object(gui, "BatchRunner"), patch.object(gui.threading, "Thread"):
            app.start_download()
            self.finish_and_deliver()
        self.assertTrue(app.running)
        self.assertEqual(self.store.saved, ["FAKE_REUSED_TOKEN"])

    def test_connect_saves_copied_token_without_a_manual_save_action(self):
        app = self.open_app()
        self.finish_and_deliver()
        canonical = "FAKE_AUTO_SAVED_TOKEN"
        app.token_var.set("https://oauth.yandex.ru/verification_code#access_token="
                          + canonical + "&token_type=bearer")
        with patch.object(app, "load_remote") as load_remote:
            app.connect()
            self.finish_and_deliver()
        load_remote.assert_called_once_with("disk:/")
        self.assertEqual(app.token_var.get(), canonical)
        self.assertEqual(self.store.saved, [canonical])
        self.assertNotIn(canonical, app.log_widget.get("1.0", "end") + app.credential_var.get())
        self.assertFalse(self.config.exists())
        self.browser_open.assert_not_called()

    def test_get_token_replaces_old_buttons_and_remains_available_without_a_vault(self):
        self.store.available = False
        app = self.open_app()
        self.finish_and_deliver()
        labels = {control.cget("text") for control in app.controls if isinstance(control, gui.ttk.Button)}
        self.assertIn("Получить токен", labels)
        self.assertIn("Удалить", labels)
        self.assertNotIn("Вставить", labels)
        self.assertNotIn("Сохранить", labels)
        self.assertNotIn("disabled", self.token_button().state())
        self.assertIn("disabled", app.forget_token_button.state())
        app.set_running(True)
        self.assertIn("disabled", self.token_button().state())
        app.set_running(False)
        self.assertNotIn("disabled", self.token_button().state())
        self.assertEqual((self.store.loads, self.store.saved, self.store.deleted), (0, [], 0))

    def test_get_token_opens_browser_in_a_worker_and_leaves_token_vault_and_api_untouched(self):
        app = self.open_app()
        self.finish_and_deliver()
        secret = "FAKE_EXISTING_TOKEN"
        app.token_var.set(secret)
        options = self.start_token_browser()
        self.assertTrue(app.token_browser_pending)
        self.assertIn("disabled", self.token_button().state())
        pending_hint = app.credential_var.get()
        app.handle_event("log", {"message": "Интерфейс обрабатывает события во время открытия браузера"})
        with patch.object(gui.threading, "Thread") as duplicate:
            app.get_token()
        duplicate.assert_not_called()
        event = self.finish_token_browser(options)
        self.assertEqual(app.credential_var.get(), pending_hint, "worker must not mutate Tk before the event is handled")
        self.assertTrue(app.token_browser_pending)
        app.handle_event(*event)
        assert_token_browser_call(self, self.browser_open)
        self.assertFalse(app.token_browser_pending)
        self.assertNotIn("disabled", self.token_button().state())
        self.assertIn("токен", app.credential_var.get().casefold())
        self.assertEqual(app.token_var.get(), secret)
        self.assertEqual(self.store.saved, [])
        self.api_factory.assert_not_called()
        self.errors.assert_not_called()
        self.assertNotIn(secret, app.log_widget.get("1.0", "end") + app.credential_var.get()
                         + repr(self.browser_open.call_args))
        self.assertFalse(self.config.exists())

    def test_browser_failure_is_safe_and_allows_retry(self):
        app = self.open_app()
        self.finish_and_deliver()
        app.token_var.set("FAKE_UNCHANGED_TOKEN")
        for failure in (False, RuntimeError("PRIVATE_BROWSER_ERROR https://private.invalid/secret")):
            with self.subTest(failure=type(failure).__name__):
                self.browser_open.reset_mock()
                self.browser_open.return_value = False
                self.browser_open.side_effect = failure if isinstance(failure, Exception) else None
                self.errors.reset_mock()
                options = self.start_token_browser()
                app.handle_event(*self.finish_token_browser(options))
                self.assertFalse(app.token_browser_pending)
                self.assertNotIn("disabled", self.token_button().state())
                self.errors.assert_called_once()
                visible = self.last_error() + app.credential_var.get() + app.log_widget.get("1.0", "end")
                self.assertIn("браузер", visible.casefold())
                self.assertNotIn("PRIVATE_BROWSER_ERROR", visible)
                self.assertNotIn("private.invalid", visible)
                self.assertEqual(app.token_var.get(), "FAKE_UNCHANGED_TOKEN")
        self.assertEqual(self.store.saved, [])
        self.api_factory.assert_not_called()

    def test_browser_completion_does_not_overwrite_newer_token_input_or_hint(self):
        app = self.open_app()
        self.finish_and_deliver()
        self.browser_open.return_value = False
        options = self.start_token_browser()
        app.token_var.set("FAKE_MORE_RECENT_TOKEN")
        app.credential_var.set("Подсказка для нового ввода")
        app.handle_event(*self.finish_token_browser(options))
        self.assertEqual(app.token_var.get(), "FAKE_MORE_RECENT_TOKEN")
        self.assertEqual(app.credential_var.get(), "Подсказка для нового ввода")
        self.assertFalse(app.token_browser_pending)
        self.assertNotIn("disabled", self.token_button().state())
        self.errors.assert_not_called()

    def test_browser_completion_during_copy_keeps_token_button_disabled(self):
        app = self.open_app()
        self.finish_and_deliver()
        self.browser_open.return_value = False
        options = self.start_token_browser()
        app.set_running(True)
        app.credential_var.set("Копирование продолжается")
        app.handle_event(*self.finish_token_browser(options))
        self.assertFalse(app.token_browser_pending)
        self.assertIn("disabled", self.token_button().state())
        self.assertEqual(app.credential_var.get(), "Копирование продолжается")
        self.errors.assert_not_called()
        app.set_running(False)
        self.assertNotIn("disabled", self.token_button().state())

    def test_browser_thread_start_failure_is_safe_and_does_not_leave_button_pending(self):
        app = self.open_app()
        self.finish_and_deliver()
        with patch.object(gui.threading, "Thread", side_effect=RuntimeError("PRIVATE_THREAD_ERROR")):
            self.token_button().invoke()
        self.assertFalse(app.token_browser_pending)
        self.assertNotIn("disabled", self.token_button().state())
        self.errors.assert_called_once()
        self.assertNotIn("PRIVATE_THREAD_ERROR", self.last_error() + app.credential_var.get())
        self.browser_open.assert_not_called()

    def test_unavailable_vault_does_not_read_or_write_credentials(self):
        self.store.available = False
        app = self.open_app()
        self.finish_and_deliver()
        app.token_var.set("FAKE_TEMPORARY_TOKEN")
        app.save_token()
        app.forget_token()
        self.finish_and_deliver()
        self.assertEqual(self.store.loads, 0)
        self.assertEqual(self.store.saved, [])
        self.assertEqual(self.store.deleted, 0)

    def test_switching_back_to_saved_token_supersedes_another_pending_save(self):
        self.store.token = "FAKE_ORIGINAL_TOKEN"
        app = self.open_app()
        self.finish_and_deliver()
        app.token_var.set("FAKE_ALTERNATE_TOKEN")
        app.save_token()
        app.token_var.set("FAKE_ORIGINAL_TOKEN")
        app.save_token()
        self.finish_and_deliver()
        self.assertEqual(self.store.saved, ["FAKE_ALTERNATE_TOKEN", "FAKE_ORIGINAL_TOKEN"])
        self.assertEqual(self.store.token, "FAKE_ORIGINAL_TOKEN")
        self.assertEqual(app.remembered_token, "FAKE_ORIGINAL_TOKEN")

    def test_token_actions_and_field_are_disabled_while_copying(self):
        app = self.open_app()
        self.finish_and_deliver()
        app.set_running(True)
        self.assertIn("disabled", app.token_entry.state())
        button_names = {"Получить токен", "Удалить"}
        buttons = [control for control in app.controls
                   if isinstance(control, gui.ttk.Button) and control.cget("text") in button_names]
        self.assertEqual({button.cget("text") for button in buttons}, button_names)
        for button in buttons:
            self.assertIn("disabled", button.state())
        app.token_var.set("FAKE_DISABLED_TOKEN")
        app.get_token()
        app.save_token()
        app.forget_token()
        self.finish_and_deliver()
        self.assertEqual(self.store.saved, [])
        self.assertEqual(self.store.deleted, 0)

    def test_save_removes_unicode_clipboard_formatting_and_wrapped_lines(self):
        app = self.open_app()
        self.finish_and_deliver()
        canonical = "y0__FAKE_GUI_TOKEN-12"
        pasted = "\ufeff\u00a0y0__\u200bFAKE_\r\nGUI_TOKEN-12\u2060\u00a0"
        app.token_var.set(pasted)
        app.save_token()
        self.finish_and_deliver()
        self.assertEqual(app.token_var.get(), canonical)
        self.assertEqual(self.store.saved, [canonical])
        self.errors.assert_not_called()
        self.assertIn("сохранён", app.credential_var.get().casefold())
        visible = app.log_widget.get("1.0", "end") + app.credential_var.get()
        self.assertNotIn(canonical, visible)
        self.assertNotIn(pasted, visible)

    def test_verification_url_is_extracted_before_saving_and_connecting(self):
        app = self.open_app()
        self.finish_and_deliver()
        canonical = "y0__FAKE_GUI_URL_TOKEN-12"
        copied_url = ("https://oauth.yandex.ru/verification_code#access_token="
                      + canonical + "&token_type=bearer&expires_in=31536000")
        app.token_var.set(copied_url)
        app.save_token()
        self.finish_and_deliver()
        self.assertEqual(app.token_var.get(), canonical)
        self.assertEqual(self.store.saved, [canonical])

        self.api_factory.return_value.get_resource.return_value = gui.Resource("Диск", "disk:/", "dir")
        self.api_factory.return_value.iter_children.return_value = []
        app.token_var.set(copied_url)
        app.connect()
        self.wait_for(lambda: self.api_factory.called)
        self.assertEqual(self.api_factory.call_args.kwargs["token"], canonical)
        self.assertEqual(app.token_var.get(), canonical)
        self.finish_and_deliver()
        self.assertEqual(self.store.saved, [canonical], "canonical token should not be saved twice")
        self.errors.assert_not_called()

    def test_invalid_cyrillic_token_never_contacts_disk_or_vault(self):
        app = self.open_app()
        self.finish_and_deliver()
        app.add_tasks(["/Фото"])
        invalid = "FAKE_Я_INVALID_TOKEN"
        with patch.object(app, "load_remote") as load_remote, patch.object(gui, "BatchRunner") as runner:
            for action in (app.save_token, app.connect, app.start_download):
                with self.subTest(action=action.__name__):
                    self.errors.reset_mock()
                    app.token_var.set(invalid)
                    action()
                    self.finish_and_deliver()
                    error = self.last_error()
                    self.assertIn("символ", error.casefold())
                    self.assertIn("U+042F", error)
                    self.assertNotIn(invalid, error)
                    self.assertFalse(app.running)
            load_remote.assert_not_called()
            runner.assert_not_called()
        self.api_factory.assert_not_called()
        self.assertEqual(self.store.saved, [])
        self.assertNotIn(invalid, app.log_widget.get("1.0", "end"))
        self.assertNotIn(invalid, app.credential_var.get())

    def test_empty_token_has_distinct_actionable_diagnostic(self):
        app = self.open_app()
        self.finish_and_deliver()
        app.token_var.set("\ufeff\u200b\u00a0\r\n")
        app.save_token()
        self.finish_and_deliver()
        error = self.last_error()
        self.assertIn("не указан", error.casefold())
        self.assertNotIn("недопустимые символы", error.casefold())
        self.assertNotIn("скрытый токен", error.casefold())
        self.assertEqual(self.store.saved, [])

    def test_copied_masked_token_explains_copying_real_value(self):
        app = self.open_app()
        self.finish_and_deliver()
        for masked in ("••••••", "......", "********"):
            with self.subTest(masked=masked):
                self.errors.reset_mock()
                app.token_var.set(masked)
                app.save_token()
                self.finish_and_deliver()
                error = self.last_error()
                self.assertIn("скрытый токен", error.casefold())
                self.assertIn("копирован", error.casefold())
                self.assertNotIn(masked, error)
        self.assertEqual(self.store.saved, [])
        self.api_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
