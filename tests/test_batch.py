"""Folder plans preserve existing recovery state and isolate queued failures."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from yandex_disk.api import YandexDiskAPI
from yandex_disk.batch import (
    BatchRunner, BatchSettings, ConfigurationError, FolderTask,
    load_settings, save_settings,
)
from yandex_disk.downloader import Downloader, RecoveryError, Report, output_lock
from test_downloader import FakeAPI, FakeOpener, FakeResponse


TOKEN = "private-oauth-token-do-not-save"


class BatchSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.config = self.base / "download-gui.json"
        self.legacy = self.base / "download.json"

    def test_imports_existing_private_plan_without_changing_it_or_creating_files(self):
        content = {
            "disk_path": "ФОТО", "output": "./копия", "workers": 1,
            "retries": 5, "timeout": 120, "verify": True, "watch": 1800,
        }
        original = json.dumps(content, ensure_ascii=False).encode()
        self.legacy.write_bytes(original)
        settings = load_settings(self.config, self.legacy)
        self.assertEqual(settings.tasks, [FolderTask("disk:/ФОТО", str(self.base / "копия"))])
        self.assertEqual((settings.workers, settings.retries, settings.timeout, settings.verify), (1, 5, 120, True))
        self.assertEqual(self.legacy.read_bytes(), original)
        self.assertFalse(self.config.exists())
        self.assertFalse((self.base / "копия").exists())
        expected = YandexDiskAPI(disk_path="ФОТО", token=TOKEN)
        imported = YandexDiskAPI(disk_path=settings.tasks[0].disk_path, token=TOKEN)
        self.assertEqual(expected.source_id, imported.source_id)

    def test_new_plan_takes_precedence_and_resolves_relative_outputs_at_config(self):
        self.legacy.write_text("invalid old settings", encoding="utf-8")
        self.config.write_text(json.dumps({"version": 1, "tasks": [
            {"disk_path": "/Архив/../Фото", "output": "../result", "enabled": False},
        ]}), encoding="utf-8")
        settings = load_settings(self.config, self.legacy)
        self.assertEqual(settings.tasks[0], FolderTask("disk:/Фото", str(self.base.parent / "result"), False))

    def test_public_cli_plan_is_not_silently_converted_to_private_folder(self):
        self.legacy.write_text(json.dumps({"public_url": "https://disk.yandex.ru/d/public", "output": "copy"}))
        self.assertEqual(load_settings(self.config, self.legacy).tasks, [])

    def test_strict_schema_rejects_tokens_unknown_fields_and_invalid_types(self):
        valid = {"version": 1, "tasks": [{"disk_path": "/Фото", "output": "copy"}]}
        invalid = [
            {**valid, "token": TOKEN},
            {**valid, "oauth_token": TOKEN},
            {**valid, "other": "unexpected"},
            {**valid, "version": True},
            {**valid, "tasks": "not a list"},
            {**valid, "tasks": [{**valid["tasks"][0], "access_token": TOKEN}]},
            {**valid, "tasks": [{**valid["tasks"][0], "enabled": 1}]},
            {**valid, "workers": True},
            {**valid, "retries": 11},
            {**valid, "timeout": float("nan")},
            {**valid, "verify": "true"},
            {**valid, "save_previews": "true"},
            {**valid, "save_previews": 1},
        ]
        for content in invalid:
            with self.subTest(content=list(content)):
                self.config.write_text(json.dumps(content), encoding="utf-8")
                with self.assertRaises(ConfigurationError) as raised:
                    load_settings(self.config)
                self.assertNotIn(TOKEN, str(raised.exception))
        self.config.unlink()
        self.legacy.write_text(json.dumps({"disk_path": "/Фото", "output": "copy", "YANDEX_DISK_TOKEN": TOKEN}))
        with self.assertRaises(ConfigurationError):
            load_settings(self.config, self.legacy)

    def test_same_nested_and_symlinked_destinations_are_rejected_before_writes(self):
        first = self.base / "copy"
        for second in (first, first / "child", self.base):
            with self.subTest(second=second):
                with self.assertRaises(ConfigurationError):
                    BatchSettings([FolderTask("/one", str(first)), FolderTask("/two", str(second))])
        first.mkdir()
        alias = self.base / "alias"
        from platform_helpers import symlink_or_skip
        symlink_or_skip(self, alias, first, directory=True)
        with self.assertRaises(ConfigurationError):
            BatchSettings([FolderTask("/one", str(first)), FolderTask("/two", str(alias))])
        self.assertFalse((first / ".disk-downloader").exists())

    def test_disabled_rows_can_share_destination_but_reenabling_requires_distinct_targets(self):
        first = FolderTask("/one", str(self.base / "copy"))
        disabled = FolderTask("/two", first.output, False)
        save_settings(self.config, BatchSettings([first, disabled]))
        self.assertEqual(load_settings(self.config).tasks, [first, disabled])
        with self.assertRaises(ConfigurationError):
            BatchSettings([first, FolderTask(disabled.disk_path, disabled.output)])

    def test_rejects_non_private_or_controlled_paths_and_requires_absolute_local_destination(self):
        for source in ("https://disk.yandex.ru/d/public", "app:/Фото", "/Фото\n", ""):
            with self.subTest(source=source):
                with self.assertRaises(ConfigurationError):
                    FolderTask(source, str(self.base / "copy"))
        with self.assertRaises(ConfigurationError):
            FolderTask("/Фото", "relative-copy")

    def test_symlink_loop_in_destination_is_a_safe_configuration_error(self):
        first, second = self.base / "first", self.base / "second"
        from platform_helpers import symlink_or_skip
        symlink_or_skip(self, first, second)
        symlink_or_skip(self, second, first)
        with self.assertRaises(ConfigurationError):
            FolderTask("/Фото", str(first / "copy"))
        self.config.write_text(json.dumps({"version": 1, "tasks": [
            {"disk_path": "/Фото", "output": "first/copy"},
        ]}))
        with self.assertRaises(ConfigurationError):
            load_settings(self.config)

    def test_saves_owner_only_plan_without_credentials_or_creating_destination(self):
        output = self.base / "copy"
        settings = BatchSettings([FolderTask("/Фото", str(output))])
        settings.token = TOKEN
        save_settings(self.config, settings)
        contents = self.config.read_text(encoding="utf-8")
        self.assertNotIn(TOKEN, contents)
        self.assertNotIn("token", contents)
        self.assertEqual(set(json.loads(contents)), {"version", "tasks", "workers", "retries", "timeout", "verify"})
        if os.name != "nt":
            self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertFalse(output.exists())
        self.assertEqual(sorted(p.name for p in self.base.iterdir()), ["download-gui.json"])
        self.assertEqual(load_settings(self.config), BatchSettings([FolderTask("/Фото", str(output))]))

    def test_failed_atomic_save_keeps_previous_plan_and_cleans_temporary_file(self):
        save_settings(self.config, BatchSettings())
        previous = self.config.read_bytes()
        with patch("yandex_disk.batch.os.replace", side_effect=OSError("simulated disk error")):
            with self.assertRaises(OSError):
                save_settings(self.config, BatchSettings([FolderTask("/Фото", str(self.base / "copy"))]))
        self.assertEqual(self.config.read_bytes(), previous)
        self.assertEqual([p.name for p in self.base.iterdir()], ["download-gui.json"])

    def test_preview_choice_roundtrips_in_v1_and_can_be_turned_off(self):
        settings = BatchSettings([FolderTask("/Фото", str(self.base / "photos"))],
                                 save_previews=True)
        save_settings(self.config, settings)
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 1)
        self.assertIs(payload["save_previews"], True)
        self.assertEqual(load_settings(self.config), settings)
        settings.save_previews = False
        save_settings(self.config, settings)
        self.assertNotIn("save_previews", json.loads(self.config.read_text(encoding="utf-8")))
        self.assertFalse(load_settings(self.config).save_previews)

    def test_preview_default_and_old_positional_settings_remain_compatible(self):
        self.assertFalse(load_settings(self.config, self.legacy).save_previews)
        self.config.write_text(json.dumps({"version": 1, "tasks": []}), encoding="utf-8")
        self.assertFalse(load_settings(self.config).save_previews)
        settings = BatchSettings([], 2, 1, 90, True, True, 12.5)
        self.assertFalse(settings.save_previews)
        self.assertEqual((settings.workers, settings.retries, settings.timeout,
                          settings.verify, settings.repeat, settings.repeat_delay),
                         (2, 1, 90, True, True, 12.5))
        self.config.unlink()
        self.legacy.write_text(json.dumps({"disk_path": "/Фото", "output": "photos"}), encoding="utf-8")
        self.assertFalse(load_settings(self.config, self.legacy).save_previews)
        self.legacy.write_text(json.dumps({"disk_path": "/Фото", "output": "photos",
                                           "save_previews": True}), encoding="utf-8")
        self.assertTrue(load_settings(self.config, self.legacy).save_previews)

    def test_save_revalidates_mutated_preview_choice(self):
        settings = BatchSettings()
        settings.save_previews = "true"
        with self.assertRaises(ConfigurationError):
            save_settings(self.config, settings)
        self.assertFalse(self.config.exists())


class BatchRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.events = []
        self.settings = BatchSettings([
            FolderTask("/one", str(self.base / "one")),
            FolderTask("/two", str(self.base / "two")),
            FolderTask("/three", str(self.base / "three")),
        ], workers=1, retries=0)

    def emit(self, kind, payload):
        self.events.append((kind, payload))

    def make_api(self, **options):
        return SimpleNamespace(**options)

    def test_partial_or_failed_task_does_not_block_remaining_and_results_keep_indices(self):
        runs = []

        def factory(api, output, **options):
            def run():
                runs.append(api.disk_path)
                if api.disk_path == "disk:/one":
                    return Report(downloaded=1, errors=[{"path": "broken", "error": "temporarily unavailable"}])
                if api.disk_path == "disk:/two":
                    raise RecoveryError("Другой процесс уже использует папку")
                return Report(downloaded=2, skipped=3, bytes_received=40)
            return SimpleNamespace(stop=options["stop_event"], run=run)

        runner = BatchRunner(self.settings, TOKEN, self.emit, self.make_api, factory)
        result = runner.run()
        self.assertEqual(runs, ["disk:/one", "disk:/two", "disk:/three"])
        self.assertEqual([item.status for item in result.items], ["partial", "failed", "complete"])
        self.assertEqual([item.index for item in result.items], [0, 1, 2])
        self.assertEqual(result.totals, {"downloaded": 3, "updated": 0, "skipped": 3, "previews": 0,
                                         "directories": 0, "bytes_received": 40, "errors": 2})
        self.assertFalse(result.cancelled)
        self.assertEqual(self.events[-1], ("finished", {"result": result}))

    def test_preview_choice_is_snapshotted_forwarded_and_reported_separately(self):
        self.settings.save_previews = True
        options_seen = []

        def factory(api, output, **options):
            options_seen.append(options)

            def run():
                options["on_progress"]({"downloaded": 1, "previews": 2})
                return Report(downloaded=1, previews=2,
                              errors=[{"path": "missing.jpg", "error": "Original unavailable"}])

            return SimpleNamespace(stop=options["stop_event"], run=run)

        runner = BatchRunner(self.settings, TOKEN, self.emit, self.make_api, factory)
        self.settings.save_previews = False
        result = runner.run()
        self.assertTrue(all(options["save_previews"] is True for options in options_seen))
        self.assertEqual((result.downloaded, result.updated, result.previews), (3, 0, 6))
        self.assertEqual([item.status for item in result.items], ["partial"] * 3)
        progress = [payload for kind, payload in self.events if kind == "progress"]
        self.assertEqual([payload["previews"] for payload in progress], [2, 2, 2])

    def test_stopping_before_run_does_not_construct_api_or_create_destinations(self):
        def forbidden(**options):
            self.fail("API must not be constructed after stopping")
        runner = BatchRunner(self.settings, TOKEN, self.emit, forbidden)
        runner.stop()
        result = runner.run()
        self.assertEqual([item.status for item in result.items], ["not_started"] * 3)
        self.assertTrue(result.cancelled)
        self.assertEqual([kind for kind, _ in self.events], ["finished"])
        self.assertEqual(list(self.base.iterdir()), [])

    def test_stop_from_task_started_event_skips_api_and_remaining_tasks(self):
        def emit(kind, payload):
            self.emit(kind, payload)
            if kind == "task_started":
                runner.stop()
        def forbidden(**options):
            self.fail("No network/write must start after cancellation in started callback")
        runner = BatchRunner(self.settings, TOKEN, emit, forbidden)
        result = runner.run()
        self.assertEqual([item.status for item in result.items], ["cancelled", "not_started", "not_started"])
        self.assertEqual(list(self.base.iterdir()), [])

    def test_stop_during_downloader_construction_does_not_call_run(self):
        apis = []
        def api_factory(**options):
            apis.append(options["disk_path"])
            return self.make_api(**options)
        def downloader_factory(api, output, **options):
            runner.stop()
            self.assertTrue(api.stop_event.is_set())
            self.assertTrue(options["stop_event"].is_set())
            def forbidden():
                self.fail("Downloader.run must be skipped after construction races with stop")
            return SimpleNamespace(stop=options["stop_event"], run=forbidden)
        runner = BatchRunner(self.settings, TOKEN, self.emit, api_factory, downloader_factory)
        result = runner.run()
        self.assertEqual(apis, ["disk:/one"])
        self.assertEqual([item.status for item in result.items], ["cancelled", "not_started", "not_started"])
        self.assertFalse((self.base / "one").exists())

    def test_stop_between_tasks_prevents_the_next_folder_from_starting(self):
        starts = []
        def emit(kind, payload):
            self.emit(kind, payload)
            if kind == "task_finished":
                runner.stop()
        def factory(api, output, **options):
            starts.append(api.disk_path)
            return SimpleNamespace(stop=options["stop_event"], run=lambda: Report(downloaded=1))
        runner = BatchRunner(self.settings, TOKEN, emit, self.make_api, factory)
        result = runner.run()
        self.assertEqual(starts, ["disk:/one"])
        self.assertEqual([item.status for item in result.items], ["complete", "not_started", "not_started"])

    def test_stop_cancels_real_active_api_wait_and_skips_other_destinations(self):
        entered = threading.Event()
        completed = []
        api = FakeAPI()
        def waiting_resource(path):
            entered.set()
            if not api.stop_event.wait(2):
                raise AssertionError("Active API did not receive cancellation")
            raise RecoveryError("Операция остановлена")
        api.get_resource = waiting_resource
        runner = BatchRunner(self.settings, TOKEN, self.emit, lambda **options: api)
        thread = threading.Thread(target=lambda: completed.append(runner.run()), daemon=True)
        thread.start()
        self.assertTrue(entered.wait(2))
        runner.stop()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        result = completed[0]
        self.assertTrue(result.cancelled)
        self.assertEqual([item.status for item in result.items], ["cancelled", "not_started", "not_started"])
        self.assertTrue(result.items[0].report.interrupted)
        self.assertFalse((self.base / "two").exists())
        self.assertFalse((self.base / "three").exists())

    def test_failures_and_logs_are_sanitized_without_persisting_oauth(self):
        def factory(api, output, **options):
            options["log"]("accidental token in callback: " + TOKEN)
            raise RecoveryError("accidental token in error: " + TOKEN)
        result = BatchRunner(self.settings, TOKEN, self.emit, self.make_api, factory).run()
        self.assertEqual([item.status for item in result.items], ["failed"] * 3)
        self.assertNotIn(TOKEN, repr(self.events))
        self.assertNotIn(TOKEN, repr(result))
        self.assertEqual(list(self.base.iterdir()), [])

    def test_callback_failure_does_not_leave_queue_running_or_block_other_tasks(self):
        calls = []
        def broken_callback(kind, payload):
            raise RuntimeError("closed UI")
        def factory(api, output, **options):
            calls.append(api.disk_path)
            return SimpleNamespace(stop=options["stop_event"], run=lambda: Report())
        result = BatchRunner(self.settings, TOKEN, broken_callback, self.make_api, factory).run()
        self.assertEqual(len(calls), 3)
        self.assertEqual([item.status for item in result.items], ["complete"] * 3)

    def test_existing_cli_backup_is_reused_and_new_files_are_discovered(self):
        legacy = self.base / "download.json"
        output = self.base / "saved"
        legacy.write_text(json.dumps({"disk_path": "ФОТО", "output": str(output), "retries": 0, "workers": 1}))
        source_id = YandexDiskAPI(disk_path="ФОТО", token=TOKEN).source_id
        api = FakeAPI({"old.txt": b"saved before form"}, source_id=source_id)
        first = Downloader(api, output, workers=1, retries=0, opener=FakeOpener(api), log=lambda _: None).run()
        self.assertEqual(first.downloaded, 1)
        settings = load_settings(self.base / "download-gui.json", legacy)
        api.add_file("new/found.txt", b"recovered later")
        requests = FakeOpener(api)
        def downloader_factory(source, target, **options):
            return Downloader(source, target, opener=requests, **options)
        result = BatchRunner(settings, TOKEN, self.emit, lambda **options: api, downloader_factory).run()
        self.assertEqual(result.items[0].status, "complete")
        self.assertEqual((result.downloaded, result.skipped), (1, 1))
        self.assertEqual((output / "old.txt").read_bytes(), b"saved before form")
        self.assertEqual((output / "new/found.txt").read_bytes(), b"recovered later")
        self.assertEqual(len(requests.requests), 1)
        self.assertEqual(settings.tasks[0].output, str(output.resolve()))
        self.assertNotIn(TOKEN, (output / ".disk-downloader/events.log").read_text())

    def test_partial_transfer_is_resumed_by_next_batch_run(self):
        settings = BatchSettings([self.settings.tasks[0]], workers=1, retries=0)
        content = b"0123456789"
        api = FakeAPI({"large.bin": content})
        first_opener = FakeOpener(api, {"/large.bin": [FakeResponse(content[:4], headers={"Content-Length": "10"})]})
        def factory(source, target, **options):
            return Downloader(source, target, opener=first_opener, **options)
        first = BatchRunner(settings, TOKEN, self.emit, lambda **options: api, factory).run()
        self.assertEqual(first.items[0].status, "partial")
        second_opener = FakeOpener(api)
        def resumed_factory(source, target, **options):
            return Downloader(source, target, opener=second_opener, **options)
        second = BatchRunner(settings, TOKEN, self.emit, lambda **options: api, resumed_factory).run()
        self.assertEqual(second.items[0].status, "complete")
        self.assertEqual(second_opener.requests[0].get_header("Range"), "bytes=4-")
        self.assertEqual((Path(settings.tasks[0].output) / "large.bin").read_bytes(), content)

    def test_output_locked_by_running_cli_does_not_block_other_selected_folder(self):
        settings = BatchSettings(self.settings.tasks[:2], workers=1, retries=0)
        apis = {task.disk_path: FakeAPI({"file.txt": task.disk_path.encode()}, source_id=task.disk_path) for task in settings.tasks}
        def factory(source, target, **options):
            return Downloader(source, target, opener=FakeOpener(source), **options)
        state = Path(settings.tasks[0].output) / ".disk-downloader"
        state.mkdir(parents=True)
        with output_lock(state):
            result = BatchRunner(settings, TOKEN, self.emit, lambda **options: apis[options["disk_path"]], factory).run()
        self.assertEqual([item.status for item in result.items], ["failed", "complete"])
        self.assertFalse((Path(settings.tasks[0].output) / "file.txt").exists())
        self.assertEqual((Path(settings.tasks[1].output) / "file.txt").read_bytes(), b"disk:/two")


if __name__ == "__main__":
    unittest.main()
