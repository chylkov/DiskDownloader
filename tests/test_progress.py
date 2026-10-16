"""Saved-file progress is emitted only after a verified, recorded save."""
from __future__ import annotations

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from yandex_disk.downloader import Downloader
from test_downloader import FakeAPI, FakeOpener, FakeResponse


class SavedProgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name) / "copy"

    def downloader(self, api, **options):
        return Downloader(api, self.output, workers=options.pop("workers", 1), retries=0,
                          opener=options.pop("opener", FakeOpener(api)), log=lambda _: None,
                          **options)

    def test_saved_snapshot_arrives_after_final_file_state_commit_and_partial_cleanup(self):
        content = b"verified and recorded"
        api = FakeAPI({"nested/file.txt": content})
        observations = []

        def progress(snapshot):
            target = self.output / "nested/file.txt"
            with closing(sqlite3.connect(self.output / ".disk-downloader/state.sqlite3")) as connection:
                row = connection.execute("SELECT path,size,sha256 FROM files").fetchone()
            report_lock_available = downloader.report_lock.acquire(blocking=False)
            if report_lock_available:
                downloader.report_lock.release()
            observations.append({
                "snapshot": snapshot,
                "content": target.read_bytes(),
                "row": row,
                "partials": list((self.output / ".disk-downloader/partials").iterdir()),
                "report_lock_available": report_lock_available,
            })

        downloader = self.downloader(api, on_progress=progress)
        report = downloader.run()
        self.assertTrue(report.complete)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0], {
            "snapshot": {"downloaded": 1, "updated": 0, "skipped": 0, "bytes_received": len(content), "previews": 0},
            "content": content,
            "row": ("nested/file.txt", len(content), hashlib.sha256(content).hexdigest()),
            "partials": [],
            "report_lock_available": True,
        })

    def test_failed_checksum_does_not_count_save_or_replace_previous_file(self):
        api = FakeAPI({"file.txt": b"old!"})
        self.assertTrue(self.downloader(api).run().complete)
        api.add_file("file.txt", b"new!", modified="2026-10-15T10:00:00Z")
        snapshots = []
        opener = FakeOpener(api, {"/file.txt": [FakeResponse(b"bad!")]})
        report = self.downloader(api, opener=opener, on_progress=snapshots.append).run()
        self.assertEqual(snapshots, [])
        self.assertEqual((report.downloaded, report.updated, len(report.errors)), (0, 0, 1))
        self.assertEqual((self.output / "file.txt").read_bytes(), b"old!")

    def test_unchanged_file_is_skipped_without_increasing_saved_progress(self):
        api = FakeAPI({"file.txt": b"already saved"})
        self.assertTrue(self.downloader(api).run().complete)
        snapshots = []
        opener = FakeOpener(api)
        report = self.downloader(api, opener=opener, on_progress=snapshots.append).run()
        self.assertEqual(snapshots, [])
        self.assertEqual((report.downloaded, report.updated, report.skipped), (0, 0, 1))
        self.assertEqual(opener.requests, [])

    def test_valid_replacement_increases_updated_counter_and_emits_saved_progress(self):
        api = FakeAPI({"file.txt": b"old!"})
        self.assertTrue(self.downloader(api).run().complete)
        api.add_file("file.txt", b"new!", modified="2026-10-15T10:00:00Z")
        snapshots = []
        report = self.downloader(api, on_progress=snapshots.append).run()
        self.assertTrue(report.complete)
        self.assertEqual(snapshots, [{"downloaded": 0, "updated": 1, "skipped": 0, "bytes_received": 4, "previews": 0}])
        self.assertEqual((self.output / "file.txt").read_bytes(), b"new!")

    def test_display_callback_exception_does_not_change_download_outcome(self):
        api = FakeAPI({"file.txt": b"saved"})
        snapshots = []

        def broken_progress(snapshot):
            snapshots.append(snapshot)
            raise RuntimeError("Window closed during progress update")

        report = self.downloader(api, on_progress=broken_progress).run()
        self.assertTrue(report.complete)
        self.assertEqual((report.downloaded, report.updated, report.errors), (1, 0, []))
        self.assertEqual((self.output / "file.txt").read_bytes(), b"saved")
        self.assertEqual(len(snapshots), 1)

    def test_parallel_downloads_emit_successful_save_count_without_losing_files(self):
        files = {f"nested/file-{index:02}.txt": f"content {index}".encode() for index in range(16)}
        api = FakeAPI(files)
        snapshots = []
        report = self.downloader(api, workers=4, on_progress=snapshots.append).run()
        self.assertTrue(report.complete)
        self.assertEqual(report.downloaded, len(files))
        self.assertEqual(len(snapshots), len(files))
        self.assertEqual(max(snapshot["downloaded"] for snapshot in snapshots), len(files))
        self.assertTrue(all(1 <= snapshot["downloaded"] <= len(files) for snapshot in snapshots))
        self.assertTrue(all(snapshot["updated"] == 0 for snapshot in snapshots))
        for relative, content in files.items():
            self.assertEqual((self.output / relative).read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
