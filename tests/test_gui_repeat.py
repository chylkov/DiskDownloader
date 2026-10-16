"""Saved-file counters and repeat controls without account or clipboard access.

Set DISK_DOWNLOADER_GUI_TESTS=1 in a desktop Tk session. The form uses an
unavailable fake vault, mocked API, and mocked clipboard installation.
"""

import gc
import os
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import disk_downloader_gui as gui
from yandex_disk.batch import BatchResult, BatchSettings, TaskResult
from yandex_disk.downloader import Report


@unittest.skipUnless(os.environ.get("DISK_DOWNLOADER_GUI_TESTS") == "1", "requires a desktop Tk session")
class RepeatFormTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gui.configure_tk_runtime()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.config = self.directory / "download-gui.json"
        self.enterContext(patch.dict(os.environ, {"YANDEX_DISK_TOKEN": ""}))
        self.errors = self.enterContext(patch.object(gui.messagebox, "showerror"))
        self.enterContext(patch.object(gui.messagebox, "showinfo"))
        self.api_factory = self.enterContext(patch.object(gui, "YandexDiskAPI"))
        self.enterContext(patch.object(gui, "install_clipboard_support"))
        self.store = Mock(available=False)
        self.root = None
        self.app = None
        # Destroyed Tk objects must be freed on the main thread, not by a later
        # garbage collection inside some worker thread (Tcl aborts the process).
        self.addCleanup(gc.collect)
        self.addCleanup(self.destroy_form)
        self.open_form()

    def open_form(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = gui.DiskDownloaderApp(self.root, config_path=self.config,
                                         legacy_path=None, token_store=self.store)
        self.app.base_var.set(str(self.directory / "copies"))
        return self.app

    def destroy_form(self):
        try:
            if self.app is not None and not self.app.closed:
                self.app.destroy()
            elif self.root is not None:
                self.root.destroy()
        except tk.TclError:
            pass

    def start_run(self, *, repeat=False, folders=("/Фото", "/Документы")):
        self.app.add_tasks(list(folders))
        self.app.token_var.set("FAKE_REPEAT_GUI_TOKEN")
        self.app.repeat_var.set(repeat)
        with patch.object(gui, "BatchRunner") as factory, patch.object(gui.threading, "Thread"):
            self.app.start_download()
        self.assertTrue(self.app.running)
        self.errors.assert_not_called()
        return factory

    def progress(self, index=0, *, cycle=1, downloaded=0, updated=0, skipped=0, previews=0, bytes_received=0):
        self.app.handle_event("progress", {
            "index": index, "cycle": cycle, "downloaded": downloaded,
            "updated": updated, "skipped": skipped, "previews": previews, "bytes_received": bytes_received,
        })

    def task_finished(self, index=0, *, cycle=1, downloaded=0, updated=0, skipped=0,
                      previews=0, status="complete", errors=None):
        report = Report(downloaded=downloaded, updated=updated, skipped=skipped,
                        previews=previews,
                        errors=[] if errors is None else errors)
        item = TaskResult(index, self.app.tasks[index], status, report)
        self.app.handle_event("task_finished", {"index": index, "cycle": cycle, "result": item})
        return item

    def test_new_form_defaults_to_one_pass_and_displays_zero_saved(self):
        self.assertFalse(self.app.repeat_var.get())
        self.assertFalse(self.app.save_previews_var.get())
        self.assertEqual(float(self.app.repeat_delay_var.get()), 60)
        self.assertEqual(self.app.saved_files, 0)
        self.assertEqual(self.app.saved_previews, 0)
        self.assertIn("0", self.app.saved_files_var.get())
        self.api_factory.assert_not_called()
        self.store.load.assert_not_called()
        self.store.save.assert_not_called()

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        counter_labels = [widget for widget in descendants(self.root)
                          if isinstance(widget, gui.ttk.Label)
                          and str(widget.cget("textvariable")) == str(self.app.saved_files_var)]
        self.assertEqual(len(counter_labels), 1, "counter must be rendered as a visible label")

    def test_repeat_choice_and_fractional_pause_survive_reopening(self):
        self.app.repeat_var.set(True)
        self.app.repeat_delay_var.set("12.5")
        self.app.add_tasks(["/Фото"])
        self.destroy_form()
        self.open_form()
        self.assertTrue(self.app.repeat_var.get())
        self.assertEqual(float(self.app.repeat_delay_var.get()), 12.5)
        settings = self.app.collect_settings()
        self.assertTrue(settings.repeat)
        self.assertEqual(settings.repeat_delay, 12.5)
        self.assertEqual(self.app.saved_files, 0, "saved count belongs to the current run")

    def test_start_passes_repeat_settings_to_runner_and_locks_repeat_control(self):
        self.app.repeat_delay_var.set("75")
        factory = self.start_run(repeat=True)
        settings = factory.call_args.args[0]
        self.assertIsInstance(settings, BatchSettings)
        self.assertTrue(settings.repeat)
        self.assertEqual(settings.repeat_delay, 75)
        repeat_controls = [control for control in self.app.controls
                           if isinstance(control, gui.ttk.Checkbutton)
                           and control.cget("text") == "Проходить по кругу"]
        self.assertEqual(len(repeat_controls), 1)
        self.assertTrue(repeat_controls[0].instate(["disabled"]))

    def test_preview_choice_survives_reopening_and_locks_during_copy(self):
        self.app.save_previews_var.set(True)
        self.app.add_tasks(["/Фото"])
        self.destroy_form()
        self.open_form()
        self.assertTrue(self.app.save_previews_var.get())
        self.assertTrue(self.app.collect_settings().save_previews)
        factory = self.start_run()
        self.assertTrue(factory.call_args.args[0].save_previews)
        controls = [control for control in self.app.controls
                    if isinstance(control, gui.ttk.Checkbutton)
                    and control.cget("text") == "Сохранять превью при ошибке оригинала"]
        self.assertEqual(len(controls), 1)
        self.assertTrue(controls[0].instate(["disabled"]))

    def test_preview_controls_and_start_stop_fit_the_smallest_window(self):
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        preview_control = next(control for control in self.app.controls
                               if isinstance(control, gui.ttk.Checkbutton)
                               and control.cget("text") == "Сохранять превью при ошибке оригинала")
        preview_label = next(widget for widget in descendants(self.root)
                             if isinstance(widget, gui.ttk.Label)
                             and str(widget.cget("textvariable")) == str(self.app.previews_var))
        minimum_width, minimum_height = self.root.minsize()
        self.root.geometry(f"{minimum_width}x{minimum_height}")
        self.root.deiconify()
        for running in (False, True):
            with self.subTest(running=running):
                if running:
                    self.start_run(repeat=True)
                    self.progress(downloaded=12345, previews=12345)
                self.root.update_idletasks()
                for widget in (preview_control, preview_label, self.app.start_button, self.app.stop_button):
                    self.assertTrue(widget.winfo_ismapped())
                    self.assertGreaterEqual(widget.winfo_width(), widget.winfo_reqwidth(), str(widget))
                    self.assertGreaterEqual(widget.winfo_height(), widget.winfo_reqheight(), str(widget))
                    x = widget.winfo_rootx() - self.root.winfo_rootx()
                    y = widget.winfo_rooty() - self.root.winfo_rooty()
                    self.assertGreaterEqual(x, 0)
                    self.assertGreaterEqual(y, 0)
                    self.assertLessEqual(x + widget.winfo_width(), self.root.winfo_width())
                    self.assertLessEqual(y + widget.winfo_height(), self.root.winfo_height())
                    ancestor = widget.master
                    while ancestor is not self.root:
                        self.assertGreaterEqual(widget.winfo_rootx(), ancestor.winfo_rootx())
                        self.assertGreaterEqual(widget.winfo_rooty(), ancestor.winfo_rooty())
                        self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(),
                                             ancestor.winfo_rootx() + ancestor.winfo_width())
                        self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                             ancestor.winfo_rooty() + ancestor.winfo_height(),
                                             f"{widget} is clipped by {ancestor}")
                        ancestor = ancestor.master

    def test_preview_progress_is_separate_monotonic_and_not_derived_from_log(self):
        self.start_run()
        self.progress(downloaded=2, previews=3)
        self.progress(downloaded=1, previews=1)
        self.progress(index=1, previews=2)
        self.progress(index=1, previews=2)
        self.app.handle_event("log", {"message": "[ПРЕВЬЮ] Только строка журнала"})
        self.assertEqual((self.app.saved_files, self.app.saved_previews), (2, 5))
        self.assertEqual(self.app.cycle_saved_previews, 5)
        self.assertEqual(self.app.previews_var.get(), "Превью: 5")
        self.assertNotIn("Превью", self.app.saved_files_var.get())
        first = self.task_finished(downloaded=2, previews=3, status="partial")
        second = self.task_finished(index=1, previews=2, status="partial")
        self.app.handle_event("finished", {"result": BatchResult(items=[first, second])})
        self.assertEqual((self.app.saved_files, self.app.saved_previews), (2, 5))
        self.assertIn("превью: 5", self.app.status_var.get())

    def test_final_report_recovers_preview_count_when_progress_events_are_missing(self):
        self.start_run()
        item = TaskResult(0, self.app.tasks[0], "partial", Report(previews=4))
        self.app.handle_event("finished", {"result": BatchResult(items=[item])})
        self.assertEqual(self.app.saved_previews, 4)
        self.assertEqual(self.app.cycle_saved_previews, 4)
        self.assertEqual(self.app.saved_files, 0)

    def test_preview_counter_accumulates_cycles_and_ignores_old_cycle_events(self):
        self.start_run(repeat=True)
        self.progress(previews=2)
        first = self.task_finished(previews=2, status="partial")
        self.app.handle_event("cycle_finished", {"cycle": 1, "result": BatchResult(items=[first])})
        self.app.handle_event("cycle_started", {"cycle": 2})
        self.assertEqual((self.app.saved_previews, self.app.cycle_saved_previews), (2, 0))
        self.progress(cycle=1, previews=99)
        self.progress(cycle=2, previews=1)
        self.assertEqual((self.app.saved_previews, self.app.cycle_saved_previews), (3, 1))
        self.assertEqual(self.app.previews_var.get(), "Превью: 3")
        # A late final report can include a circle whose events were lost.
        final = TaskResult(0, self.app.tasks[0], "partial", Report(previews=4))
        result = BatchResult(items=[final], cycles=3, previous_totals={"previews": 3})
        self.app.handle_event("finished", {"result": result})
        self.assertEqual((self.app.saved_previews, self.app.cycle_saved_previews), (7, 4))
        self.assertEqual(self.app.saved_files, 0)

    def test_existing_files_partial_bytes_and_log_messages_do_not_increment_counter(self):
        self.start_run()
        self.progress(skipped=800, bytes_received=123456)
        self.app.handle_event("log", {"message": "[СКАЧАН] строка журнала без подтверждённого результата"})
        self.task_finished(status="partial", skipped=800, errors=["Файл недоступен"])
        self.assertEqual(self.app.saved_files, 0)
        self.assertEqual(self.app.cycle_saved_files, 0)

    def test_progress_counts_new_and_replaced_files_without_duplicates_or_backwards_steps(self):
        self.start_run()
        self.progress(downloaded=2, updated=1, skipped=30)
        self.assertEqual(self.app.saved_files, 3)
        self.progress(downloaded=1, updated=1, skipped=29)
        self.assertEqual(self.app.saved_files, 3, "an older snapshot must not decrease the count")
        self.progress(index=1, downloaded=3, updated=2)
        self.assertEqual(self.app.saved_files, 8)
        self.progress(index=1, downloaded=3, updated=2)
        self.assertEqual(self.app.saved_files, 8, "duplicate progress must not count files twice")
        first = self.task_finished(downloaded=2, updated=1)
        second = self.task_finished(index=1, downloaded=3, updated=2)
        self.assertEqual(self.app.saved_files, 8)
        self.app.handle_event("finished", {"result": BatchResult(items=[first, second])})
        self.assertEqual(self.app.saved_files, 8, "final summary must not count the same files again")
        self.assertIn("8", self.app.saved_files_var.get())

    def test_task_report_completes_counter_when_last_progress_is_missing(self):
        self.start_run()
        self.progress(downloaded=1)
        self.task_finished(downloaded=4, updated=2)
        self.assertEqual(self.app.saved_files, 6)
        self.assertEqual(self.app.cycle_saved_files, 6)

    def test_final_result_completes_counter_when_progress_and_task_events_are_missing(self):
        self.start_run()
        item = TaskResult(0, self.app.tasks[0], "complete", Report(downloaded=4, updated=1))
        self.app.handle_event("finished", {"result": BatchResult(items=[item])})
        self.assertEqual(self.app.saved_files, 5)
        self.assertFalse(self.app.running)

    def test_next_cycle_resets_task_progress_but_accumulates_session_saved_files(self):
        self.start_run(repeat=True)
        self.app.handle_event("cycle_started", {"cycle": 1})
        self.progress(downloaded=2, updated=1)
        item = self.task_finished(downloaded=2, updated=1)
        self.app.handle_event("cycle_finished", {"cycle": 1, "result": BatchResult(items=[item])})
        self.assertTrue(self.app.running)
        self.app.handle_event("cycle_started", {"cycle": 2})
        self.assertEqual(self.app.current_cycle, 2)
        self.assertEqual(self.app.cycle_saved_files, 0)
        self.assertEqual(self.app.saved_files, 3)
        self.assertEqual(float(self.app.progress["value"]), 0)
        self.assertEqual(self.app.task_tree.set("task-0", "status"), "В очереди")
        self.progress(cycle=2, downloaded=1, updated=1, skipped=3)
        self.assertEqual(self.app.saved_files, 5)
        self.assertEqual(self.app.cycle_saved_files, 2)

    def test_stopping_between_cycles_keeps_form_busy_until_worker_finishes(self):
        self.start_run(repeat=True)
        self.app.handle_event("cycle_started", {"cycle": 1})
        item = self.task_finished(downloaded=2)
        result = BatchResult(items=[item])
        self.app.handle_event("cycle_finished", {"cycle": 1, "result": result})
        self.app.handle_event("cycle_wait", {"cycle": 1, "delay": 60})
        self.assertTrue(self.app.running)
        self.assertTrue(self.app.start_button.instate(["disabled"]))
        self.assertFalse(self.app.stop_button.instate(["disabled"]))
        self.assertIn("60", self.app.status_var.get())
        self.app.stop_download()
        self.app.runner.stop.assert_called_once_with()
        self.assertTrue(self.app.running)
        self.assertTrue(self.app.start_button.instate(["disabled"]))
        result.cancelled = True
        self.app.handle_event("finished", {"result": result})
        self.assertFalse(self.app.running)
        self.assertFalse(self.app.start_button.instate(["disabled"]))
        self.assertTrue(self.app.stop_button.instate(["disabled"]))
        self.assertEqual(self.app.saved_files, 2)

    def test_final_multi_cycle_summary_does_not_double_count_previous_rounds(self):
        self.start_run(repeat=True)
        self.app.handle_event("cycle_started", {"cycle": 1})
        first = self.task_finished(downloaded=2)
        self.app.handle_event("cycle_finished", {"cycle": 1, "result": BatchResult(items=[first])})
        self.app.handle_event("cycle_started", {"cycle": 2})
        second = self.task_finished(cycle=2, downloaded=1, updated=1)
        result = BatchResult(items=[second], cancelled=True, cycles=2,
                             previous_totals={"downloaded": 2})
        self.app.handle_event("finished", {"result": result})
        self.assertEqual(self.app.saved_files, 4)
        self.assertEqual(self.app.cycle_saved_files, 2)

    def test_new_run_resets_session_count_even_when_previous_run_had_errors(self):
        self.start_run()
        item = self.task_finished(downloaded=3, previews=2, status="partial", errors=["Файл недоступен"])
        self.app.handle_event("finished", {"result": BatchResult(items=[item])})
        self.assertEqual(self.app.saved_files, 3)
        self.assertEqual(self.app.saved_previews, 2)
        with patch.object(gui, "BatchRunner"), patch.object(gui.threading, "Thread"):
            self.app.start_download()
        self.assertTrue(self.app.running)
        self.assertEqual(self.app.saved_files, 0)
        self.assertEqual(self.app.cycle_saved_files, 0)
        self.assertEqual(self.app.saved_previews, 0)
        self.assertEqual(self.app.cycle_saved_previews, 0)
        self.assertEqual(self.app.previews_var.get(), "Превью: 0")
        self.assertIn("0", self.app.saved_files_var.get())

    def test_bad_repeat_pause_does_not_start_worker(self):
        self.app.add_tasks(["/Фото"])
        self.app.token_var.set("FAKE_REPEAT_GUI_TOKEN")
        self.app.repeat_var.set(True)
        self.app.repeat_delay_var.set("не число")
        with patch.object(gui, "BatchRunner") as factory:
            self.app.start_download()
        factory.assert_not_called()
        self.assertFalse(self.app.running)
        self.errors.assert_called_once()


if __name__ == "__main__":
    unittest.main()
