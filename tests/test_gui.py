"""Native form checks without account access or real network requests.

Run with DISK_DOWNLOADER_GUI_TESTS=1 on a desktop session. Tk cannot open an
AppKit window from the macOS execution sandbox, even when it is withdrawn.
"""

import gc
import json
import os
from pathlib import Path
import tempfile
from threading import Thread
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import disk_downloader_gui as gui
from yandex_disk.api import Resource
from yandex_disk.batch import BatchResult, BatchSettings, TaskResult
from yandex_disk.downloader import Report


@unittest.skipUnless(os.environ.get("DISK_DOWNLOADER_GUI_TESTS") == "1", "requires a desktop Tk session")
class NativeFormTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gui.configure_tk_runtime()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.config = self.directory / "download-gui.json"
        self.root = tk.Tk()
        self.root.withdraw()
        # Destroyed Tk objects must be freed on the main thread, not by a later
        # garbage collection inside some worker thread (Tcl aborts the process).
        self.addCleanup(gc.collect)
        self.addCleanup(self.destroy_root)
        self.errors = self.enterContext(patch.object(gui.messagebox, "showerror"))
        self.enterContext(patch.object(gui.messagebox, "showinfo"))
        self.enterContext(patch.object(gui.messagebox, "askyesno", return_value=True))
        self.api_factory = self.enterContext(patch.object(gui, "YandexDiskAPI"))
        self.token_store = self.enterContext(patch.object(gui, "TokenStore")).return_value
        self.token_store.available = False
        self.app = gui.DiskDownloaderApp(self.root, config_path=self.config, legacy_path=None)
        self.app.base_var.set(str(self.directory / "copies"))

    def destroy_root(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def test_opening_form_does_not_contact_account(self):
        self.assertEqual(self.app.settings.tasks, [])
        self.assertFalse(self.config.exists())
        self.assertFalse(self.app.running)
        self.api_factory.assert_not_called()

    def test_many_folders_receive_separate_targets_and_duplicate_is_skipped(self):
        self.app.add_tasks(["/Фото/Архив", "/Документы/Архив", "/Фото/Архив"])
        tasks = self.app.settings.tasks
        self.assertEqual([task.disk_path for task in tasks], ["disk:/Фото/Архив", "disk:/Документы/Архив"])
        self.assertEqual(len({task.output for task in tasks}), 2)
        for task in tasks:
            self.assertEqual(Path(task.output).parent, self.directory / "copies")
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["tasks"]), 2)
        self.app.add_tasks(["/Фото/Архив"])
        self.assertEqual(self.app.settings.tasks, tasks)

    def test_add_selected_keeps_both_remote_paths(self):
        first = self.app.remote_tree.insert("", "end", text="Фото")
        second = self.app.remote_tree.insert("", "end", text="Документы")
        self.app.browser_nodes.update({first: "disk:/Фото", second: "disk:/Документы"})
        self.app.remote_tree.selection_set(first, second)
        self.app.add_selected()
        self.assertEqual([task.disk_path for task in self.app.settings.tasks], ["disk:/Фото", "disk:/Документы"])

    def test_token_never_enters_saved_folder_plan(self):
        secret = "GUI_TEST_SECRET_TOKEN"
        self.app.token_var.set(secret)
        self.app.add_tasks(["/Фото"])
        text = self.config.read_text(encoding="utf-8")
        self.assertNotIn(secret, text)
        self.assertNotIn("token", text.casefold())

    def test_invalid_multi_add_does_not_save_partial_plan(self):
        self.app.add_tasks(["/Исходная"])
        previous_tasks = list(self.app.settings.tasks)
        previous_file = self.config.read_bytes()
        try:
            self.app.add_tasks(["/Хорошая", "/Плохая\nпапка"])
        except ValueError:
            pass
        self.assertEqual(self.app.settings.tasks, previous_tasks)
        self.assertEqual(self.config.read_bytes(), previous_file)

    def test_failed_save_does_not_change_in_memory_queue(self):
        self.app.add_tasks(["/Исходная"])
        previous_tasks = list(self.app.settings.tasks)
        previous_file = self.config.read_bytes()
        with patch.object(gui, "save_settings", side_effect=OSError("test failure")):
            try:
                self.app.add_tasks(["/Новая"])
            except OSError:
                pass
        self.assertEqual(self.app.settings.tasks, previous_tasks)
        self.assertEqual(self.config.read_bytes(), previous_file)

    def test_browser_discards_stale_result(self):
        self.app.browser_generation = 2
        before = dict(self.app.browser_nodes)
        self.app.handle_event("remote_result", {
            "generation": 1, "parent": None, "path": "disk:/",
            "folders": [Resource("Устаревшая", "disk:/Устаревшая", "dir")], "error": None,
        })
        self.assertEqual(self.app.browser_nodes, before)

    def test_browser_keeps_folders_returned_before_listing_error(self):
        self.app.browser_generation = 2
        self.app.handle_event("remote_result", {
            "generation": 2, "parent": None, "path": "disk:/",
            "folders": [Resource("Доступная", "disk:/Доступная", "dir")],
            "error": "Часть списка временно недоступна",
        })
        self.assertIn("disk:/Доступная", self.app.browser_nodes.values())

    def test_stop_requests_cooperative_cancellation_and_retains_tasks(self):
        self.app.add_tasks(["/Фото", "/Документы"])
        tasks = list(self.app.settings.tasks)
        self.app.runner = Mock()
        self.app.running = True
        self.app.stop_download()
        self.app.runner.stop.assert_called_once_with()
        self.assertEqual(self.app.settings.tasks, tasks)
        self.assertTrue(self.app.running, "wait for worker completion before enabling a new run")

    def test_destination_conflict_keeps_previous_plan(self):
        self.app.add_tasks(["/Фото", "/Документы"])
        tasks = list(self.app.settings.tasks)
        previous_file = self.config.read_bytes()
        self.app.task_tree.selection_set("task-1")
        with patch.object(gui.filedialog, "askdirectory", return_value=tasks[0].output):
            self.app.change_destination()
        self.assertEqual(self.app.settings.tasks, tasks)
        self.assertEqual(self.config.read_bytes(), previous_file)
        self.errors.assert_called()

    def test_fractional_timeout_is_preserved_when_plan_opens(self):
        self.app.destroy()
        gui.save_settings(self.config, BatchSettings(timeout=12.5))
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = gui.DiskDownloaderApp(self.root, config_path=self.config, legacy_path=None)
        self.assertEqual(self.app.timeout_var.get(), "12.5")
        self.assertEqual(self.app.collect_settings().timeout, 12.5)

    def test_last_finished_task_leaves_progress_full(self):
        self.app.add_tasks(["/Фото", "/Документы"])
        self.app.progress.configure(value=0, maximum=2)
        for index, task in enumerate(self.app.settings.tasks):
            result = TaskResult(index, task, "complete", Report())
            self.app.handle_event("task_finished", {"index": index, "result": result})
        self.assertEqual(float(self.app.progress["value"]), 2)
        self.assertEqual(float(self.app.progress["maximum"]), 2)

    def test_start_invalidates_inflight_browser_and_releases_loading_guard(self):
        self.app.add_tasks(["/Фото"])
        self.app.token_var.set("GUI_TEST_TOKEN")
        old_stop = self.app.browser_stop
        old_generation = self.app.browser_generation
        self.app.browser_loading.add("inflight-folder")
        with patch.object(gui, "BatchRunner"), patch.object(gui.threading, "Thread"):
            self.app.start_download()
        self.assertTrue(old_stop.is_set())
        self.assertIsNot(self.app.browser_stop, old_stop)
        self.assertEqual(self.app.browser_loading, set())
        self.assertGreater(self.app.browser_generation, old_generation)
        before = dict(self.app.browser_nodes)
        self.app.handle_event("remote_result", {
            "generation": old_generation, "parent": None, "path": "disk:/",
            "folders": [Resource("Поздняя", "disk:/Поздняя", "dir")], "error": None,
        })
        self.assertEqual(self.app.browser_nodes, before)

    def test_worker_only_queues_completion_until_main_thread_polls(self):
        self.app.add_tasks(["/Фото"])
        self.app.token_var.set("GUI_TEST_TOKEN")
        with patch.object(gui, "BatchRunner") as factory, patch.object(gui.threading, "Thread") as thread_factory:
            self.app.start_download()
            worker = thread_factory.call_args.kwargs["target"]
            emit = factory.call_args.kwargs["emit"]
        task = self.app.settings.tasks[0]
        result = BatchResult(items=[TaskResult(0, task, "complete", Report(downloaded=2))])
        factory.return_value.run.side_effect = lambda: emit("finished", {"result": result})
        background = Thread(target=worker)
        background.start()
        background.join(timeout=3)
        self.assertFalse(background.is_alive())
        self.assertTrue(self.app.running)
        self.assertEqual(self.app.status_var.get(), "Начинается копирование…")
        self.app.poll_events()
        self.assertFalse(self.app.running)
        self.assertIn("Скачано: 2", self.app.status_var.get())

    def test_queued_log_lines_keep_their_order_around_other_events(self):
        self.app.events.put(("log", {"message": "[СКАЧАН] первый.jpg"}))
        self.app.events.put(("batch_error", {"message": "сбой обхода"}))
        self.app.events.put(("log", {"message": "[СКАЧАН] второй.jpg"}))
        self.app.poll_events()
        text = self.app.log_widget.get("1.0", "end")
        self.assertLess(text.index("первый.jpg"), text.index("[ОШИБКА] сбой обхода"))
        self.assertLess(text.index("[ОШИБКА] сбой обхода"), text.index("второй.jpg"))

    def test_log_backlog_keeps_latest_lines_within_retained_limit(self):
        for index in range(gui.LOG_LINES + 500):
            self.app.events.put(("log", {"message": f"[ЕСТЬ] файл {index}"}))
        self.app.poll_events()
        lines = self.app.log_widget.get("1.0", "end-1c").splitlines()
        self.assertTrue(lines[-1].endswith(f"[ЕСТЬ] файл {gui.LOG_LINES + 499}"))
        self.assertLessEqual(len(lines), gui.LOG_LINES)
        self.assertTrue(self.app.events.empty())


class DisplayPathTests(unittest.TestCase):
    def test_home_folder_is_shortened_only_as_a_whole_component(self):
        home = Path(tempfile.gettempdir()) / "display-path-user"
        sibling = home.with_name(home.name + "backup") / "Фото"
        external = home.parent / "Backup" / "Фото"
        with patch.object(gui.Path, "home", return_value=home):
            self.assertEqual(gui.display_path(str(home / "Downloads" / "Фото")),
                             os.path.join("~", "Downloads", "Фото"))
            self.assertEqual(gui.display_path(str(home)), "~")
            self.assertEqual(gui.display_path(str(sibling)), str(sibling))
            self.assertEqual(gui.display_path(str(external)), str(external))


if __name__ == "__main__":
    unittest.main()
