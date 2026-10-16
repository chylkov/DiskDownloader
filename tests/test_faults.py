"""Local fault injection must never abandon healthy sibling files."""

import errno
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from yandex_disk.downloader import Downloader, State
from test_downloader import FakeAPI, FakeOpener


class FaultIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "recovered"
        self.files = {name + ".txt": name.encode() for name in ("a", "b", "c", "d")}
        self.api = FakeAPI(self.files)

    def run_download(self):
        return Downloader(
            self.api, self.output, workers=1, retries=0,
            log=lambda message: None, opener=FakeOpener(self.api),
        ).run()

    def assert_all_files_saved(self):
        for name, content in self.files.items():
            self.assertEqual((self.output / name).read_bytes(), content)

    def test_one_sqlite_write_failure_keeps_all_healthy_siblings_running(self):
        original_save = State.save

        def failing_save(state, relative, *args):
            if relative == "a.txt":
                raise sqlite3.OperationalError("sensitive database error detail")
            return original_save(state, relative, *args)

        with patch.object(State, "save", failing_save):
            report = self.run_download()

        self.assert_all_files_saved()
        self.assertFalse(report.complete)
        self.assertEqual(report.downloaded, 3)
        self.assertEqual([entry["path"] for entry in report.errors], ["a.txt"])
        self.assertIn("OperationalError", report.errors[0]["error"])
        self.assertNotIn("sensitive", report.errors[0]["error"])
        saved_report = json.loads((self.output / ".disk-downloader/last-run.json").read_text())
        self.assertEqual(saved_report["errors"], report.errors)

        # The final copy was already valid before its state write failed. The
        # next launch verifies and adopts it without another network transfer.
        repeated = self.run_download()
        self.assertTrue(repeated.complete)
        self.assertEqual(repeated.skipped, 4)
        self.assertEqual(len(self.api.download_calls), 4)

    def test_failed_journal_write_is_reported_once_without_stopping_transfers(self):
        original_open = Path.open

        class BrokenJournal(io.StringIO):
            def write(self, value):
                raise OSError(errno.ENOSPC, "No space left on device")

        def opening(path, *args, **kwargs):
            if path.name == "events.log":
                return BrokenJournal()
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", opening):
            report = self.run_download()

        self.assert_all_files_saved()
        self.assertEqual(report.downloaded, 4)
        self.assertFalse(report.complete)
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(report.errors[0]["path"], ".disk-downloader/events.log")
        saved_report = json.loads((self.output / ".disk-downloader/last-run.json").read_text())
        self.assertEqual(saved_report["errors"], report.errors)


if __name__ == "__main__":
    unittest.main()
