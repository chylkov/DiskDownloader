"""Continuous folder recovery rescans complete queues and remains stoppable."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from yandex_disk.batch import (
    BatchResult, BatchRunner, BatchSettings, ConfigurationError, FolderTask,
    load_settings, save_settings,
)
from yandex_disk.downloader import Downloader, RecoveryError, Report
from test_downloader import FakeAPI, FakeOpener, FakeResponse


class RepeatSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.config = self.base / "download-gui.json"

    def test_repeat_options_roundtrip_without_credentials_and_preserve_v1(self):
        settings = BatchSettings(
            [FolderTask("/Фото", str(self.base / "photos"))],
            repeat=True, repeat_delay=12.5,
        )
        settings.token = "must-never-be-persisted"
        save_settings(self.config, settings)
        content = self.config.read_text(encoding="utf-8")
        self.assertNotIn("token", content)
        self.assertNotIn(settings.token, content)
        self.assertEqual(json.loads(content)["version"], 1)
        self.assertEqual(load_settings(self.config), settings)
        self.assertFalse((self.base / "photos").exists())

    def test_previous_v1_and_cli_watch_do_not_enable_repeating(self):
        self.config.write_text(json.dumps({"version": 1, "tasks": []}))
        old = load_settings(self.config)
        self.assertFalse(old.repeat)
        self.assertEqual(old.repeat_delay, 60)
        self.config.unlink()
        legacy = self.base / "download.json"
        legacy.write_text(json.dumps({
            "disk_path": "/Фото", "output": "photos", "watch": 1800,
        }))
        imported = load_settings(self.config, legacy)
        self.assertFalse(imported.repeat)
        self.assertEqual(imported.repeat_delay, 60)

    def test_repeat_options_require_strict_bool_and_finite_bounded_delay(self):
        for value in (1, 0, "true", None):
            with self.subTest(repeat=value), self.assertRaises(ConfigurationError):
                BatchSettings(repeat=value)
        for value in (True, False, None, "60", 0, -1, 86401, float("nan"), float("inf")):
            with self.subTest(delay=value), self.assertRaises(ConfigurationError):
                BatchSettings(repeat_delay=value)
        for value in (1, 86400, 12.5):
            self.assertEqual(BatchSettings(repeat_delay=value).repeat_delay, value)

    def test_save_revalidates_mutated_repeat_options(self):
        settings = BatchSettings()
        settings.repeat_delay = 0
        with self.assertRaises(ConfigurationError):
            save_settings(self.config, settings)
        self.assertFalse(self.config.exists())


class RepeatRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.tasks = [
            FolderTask("/one", str(self.base / "one")),
            FolderTask("/two", str(self.base / "two")),
        ]
        self.events = []

    def test_all_folders_rescan_with_fresh_instances_and_cumulative_totals(self):
        apis = []
        downloaders = []
        runs = []
        reports = []

        def emit(kind, payload):
            self.events.append((kind, payload))
            if kind == "cycle_finished" and payload["cycle"] == 2:
                runner.stop()

        def api_factory(**options):
            api = SimpleNamespace(**options)
            apis.append(api)
            return api

        def downloader_factory(api, output, **options):
            def run():
                runs.append(api.disk_path)
                report = Report(downloaded=1, updated=2, skipped=3, previews=6, directories=4, bytes_received=5)
                reports.append(report)
                options["on_progress"]({"downloaded": 1, "updated": 2, "skipped": 3,
                                        "previews": 6, "bytes_received": 5})
                return report
            downloader = SimpleNamespace(stop=options["stop_event"], run=run)
            downloaders.append(downloader)
            return downloader

        settings = BatchSettings(self.tasks, repeat=True, repeat_delay=12.5)
        runner = BatchRunner(settings, "private-token", emit, api_factory, downloader_factory)
        # GUI edits must not alter the active run's repeat settings or task list.
        settings.repeat = False
        settings.repeat_delay = 99
        settings.tasks.clear()
        with patch.object(runner._stop, "wait", return_value=False) as wait:
            result = runner.run()
        self.assertEqual(runs, ["disk:/one", "disk:/two"] * 2)
        self.assertEqual(len({id(api) for api in apis}), 4)
        self.assertEqual(len({id(downloader) for downloader in downloaders}), 4)
        self.assertEqual([item.report for item in result.items], reports[-2:])
        self.assertEqual(len(result.items), 2)
        self.assertEqual(result.cycles, 2)
        self.assertEqual(result.totals, {
            "downloaded": 4, "updated": 8, "skipped": 12, "previews": 24, "directories": 16,
            "bytes_received": 20, "errors": 0,
        })
        self.assertTrue(result.cancelled)
        wait.assert_called_once_with(12.5)
        kinds = [kind for kind, _ in self.events]
        self.assertEqual(kinds.count("finished"), 1)
        self.assertEqual(kinds.count("cycle_started"), 2)
        self.assertEqual(kinds.count("cycle_finished"), 2)
        self.assertEqual(kinds.count("cycle_wait"), 1)
        progress = [payload for kind, payload in self.events if kind == "progress"]
        self.assertEqual([(item["cycle"], item["index"]) for item in progress], [(1, 0), (1, 1), (2, 0), (2, 1)])
        self.assertEqual([item["previews"] for item in progress], [6] * 4)
        self.assertEqual(result.previous_totals["previews"], 12)
        self.assertEqual(result.previews, 24)
        self.assertEqual([payload["result"].downloaded for kind, payload in self.events if kind == "cycle_finished"], [2, 2])

    def test_failed_and_partial_folders_do_not_end_repeating_or_skip_healthy_folder(self):
        runs = []

        def emit(kind, payload):
            if kind == "cycle_finished" and payload["cycle"] == 3:
                runner.stop()

        def factory(api, output, **options):
            def run():
                runs.append(api.disk_path)
                if api.disk_path == "disk:/one":
                    raise RecoveryError("Временно недоступно")
                return Report(downloaded=1, errors=[{"path": "missing", "error": "unavailable"}])
            return SimpleNamespace(stop=options["stop_event"], run=run)

        runner = BatchRunner(BatchSettings(self.tasks, repeat=True), "token", emit,
                             lambda **options: SimpleNamespace(**options), factory)
        with patch.object(runner._stop, "wait", return_value=False):
            result = runner.run()
        self.assertEqual(runs, ["disk:/one", "disk:/two"] * 3)
        self.assertEqual([item.status for item in result.items], ["failed", "partial"])
        self.assertEqual((result.downloaded, result.errors, result.cycles), (3, 6, 3))

    def test_next_cycle_resumes_partial_file_and_finds_newly_available_file(self):
        content = b"0123456789"
        apis = []
        openers = []

        def emit(kind, payload):
            self.events.append((kind, payload))
            if kind == "cycle_finished" and payload["cycle"] == 2:
                runner.stop()

        def api_factory(**options):
            files = {"large.bin": content}
            if apis:
                files["restored/new.txt"] = b"newly available"
            api = FakeAPI(files, source_id="stable-private-folder")
            apis.append(api)
            return api

        def downloader_factory(api, output, **options):
            responses = ({"/large.bin": [FakeResponse(content[:4], headers={"Content-Length": "10"})]}
                         if not openers else None)
            opener = FakeOpener(api, responses)
            openers.append(opener)
            return Downloader(api, output, opener=opener, **options)

        runner = BatchRunner(
            BatchSettings(self.tasks[:1], workers=1, retries=0, repeat=True), "token", emit,
            api_factory, downloader_factory,
        )
        with patch.object(runner._stop, "wait", return_value=False):
            result = runner.run()
        self.assertEqual(len(apis), 2)
        self.assertEqual((self.base / "one/large.bin").read_bytes(), content)
        self.assertEqual((self.base / "one/restored/new.txt").read_bytes(), b"newly available")
        self.assertTrue(any(request.get_header("Range") == "bytes=4-" for request in openers[1].requests))
        self.assertEqual((result.downloaded, result.errors, len(result.items)), (2, 1, 1))
        self.assertEqual(result.items[0].status, "complete")
        self.assertEqual([payload["result"].items[0].status for kind, payload in self.events
                          if kind == "cycle_finished"], ["partial", "complete"])

    def test_wait_is_cancelled_immediately_without_starting_another_cycle(self):
        entered_wait = threading.Event()
        completed = []
        runs = []

        def emit(kind, payload):
            self.events.append((kind, payload))
            if kind == "cycle_wait":
                entered_wait.set()

        def factory(api, output, **options):
            runs.append(api.disk_path)
            return SimpleNamespace(stop=options["stop_event"], run=lambda: Report(downloaded=1))

        runner = BatchRunner(BatchSettings(self.tasks, repeat=True, repeat_delay=86400), "token", emit,
                             lambda **options: SimpleNamespace(**options), factory)
        thread = threading.Thread(target=lambda: completed.append(runner.run()), daemon=True)
        thread.start()
        self.addCleanup(runner.stop)
        self.assertTrue(entered_wait.wait(2))
        runner.stop()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(runs, ["disk:/one", "disk:/two"])
        self.assertEqual(completed[0].cycles, 1)
        self.assertEqual(completed[0].downloaded, 2)
        self.assertTrue(completed[0].cancelled)
        self.assertEqual([kind for kind, _ in self.events].count("finished"), 1)

    def test_stop_from_cycle_started_callback_prevents_network_and_remaining_tasks(self):
        def emit(kind, payload):
            self.events.append((kind, payload))
            if kind == "cycle_started":
                runner.stop()

        def forbidden(**options):
            self.fail("No API may start after cycle_started callback stops the run")

        runner = BatchRunner(BatchSettings(self.tasks, repeat=True), "token", emit, forbidden)
        result = runner.run()
        self.assertTrue(result.cancelled)
        self.assertEqual([item.status for item in result.items], ["not_started", "not_started"])
        self.assertEqual(list(self.base.iterdir()), [])

    def test_stop_from_cycle_wait_callback_does_not_start_or_wait_again(self):
        def emit(kind, payload):
            self.events.append((kind, payload))
            if kind == "cycle_wait":
                runner.stop()

        def factory(api, output, **options):
            return SimpleNamespace(stop=options["stop_event"], run=lambda: Report(downloaded=1))

        runner = BatchRunner(BatchSettings(self.tasks, repeat=True), "token", emit,
                             lambda **options: SimpleNamespace(**options), factory)
        with patch.object(runner._stop, "wait", side_effect=AssertionError("Must stop before waiting")):
            result = runner.run()
        self.assertEqual(result.cycles, 1)
        self.assertEqual(result.downloaded, 2)
        self.assertTrue(result.cancelled)

    def test_empty_or_all_disabled_plans_finish_without_waiting_or_looping(self):
        for tasks in ([], [FolderTask("/one", str(self.base / "one"), enabled=False)]):
            with self.subTest(tasks=tasks):
                events = []
                runner = BatchRunner(BatchSettings(tasks, repeat=True), "token",
                                     lambda kind, payload: events.append((kind, payload)))
                with patch.object(runner._stop, "wait", side_effect=AssertionError("Empty queue must not wait")):
                    result = runner.run()
                self.assertEqual(result.items, [])
                self.assertEqual(result.cycles, 0)
                self.assertFalse(result.cancelled)
                self.assertEqual([kind for kind, _ in events], ["finished"])

    def test_many_cycles_keep_only_latest_items_and_aggregate_earlier_totals(self):
        def emit(kind, payload):
            if kind == "cycle_finished" and payload["cycle"] == 40:
                runner.stop()

        def factory(api, output, **options):
            return SimpleNamespace(stop=options["stop_event"], run=lambda: Report(downloaded=1))

        runner = BatchRunner(BatchSettings(self.tasks, repeat=True), "token", emit,
                             lambda **options: SimpleNamespace(**options), factory)
        with patch.object(runner._stop, "wait", return_value=False):
            result = runner.run()
        self.assertEqual((result.cycles, len(result.items), result.downloaded), (40, 2, 80))
        self.assertEqual(result.previous_totals["downloaded"], 78)

    def test_result_previous_totals_are_isolated_between_instances(self):
        first = BatchResult()
        first.previous_totals["downloaded"] = 7
        self.assertEqual(first.downloaded, 7)
        self.assertEqual(BatchResult().downloaded, 0)


if __name__ == "__main__":
    unittest.main()
