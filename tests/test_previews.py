"""Recover previews without ever treating them as verified original files."""
import base64
from contextlib import closing
import errno
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock
import urllib.error

from test_downloader import FakeAPI, FakeOpener, FakeResponse, SIGNED_SECRET, TOKEN_SECRET
from yandex_disk.downloader import Downloader, fingerprint, MAX_PREVIEW_BYTES
from platform_helpers import symlink_or_skip


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class PreviewRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name) / "copy"
        self.api = FakeAPI({"Фото/photo.jpg": b"original photo"})
        self.api.preview_url = Mock(return_value="https://downloader.disk.yandex.ru/preview/" + SIGNED_SECRET)
        self.api.open_preview = Mock(side_effect=lambda *args, **kwargs: FakeResponse(PNG, headers={"Content-Type": "image/png"}))
        self.logs = []

    @property
    def target(self):
        return self.output / "Фото/photo.jpg"

    def run_download(self, *, error=None, enabled=True, **options):
        error = error if error is not None else TimeoutError()
        opener = options.pop("opener", FakeOpener(self.api, {"/Фото/photo.jpg": [error]}))
        return Downloader(self.api, self.output, workers=1, retries=0, log=self.logs.append,
                          opener=opener, save_previews=enabled, **options).run()

    def test_fallback_is_optional_and_does_not_query_preview_when_disabled(self):
        report = self.run_download(enabled=False)
        self.assertFalse(report.complete)
        self.assertEqual(report.previews, 0)
        self.api.preview_url.assert_not_called()

    def test_success_is_separate_from_original_and_report_stays_partial(self):
        snapshots = []
        report = self.run_download(on_progress=snapshots.append)
        self.assertFalse(report.complete)
        self.assertEqual(len(report.errors), 1)
        self.assertEqual((report.downloaded, report.updated, report.skipped, report.previews), (0, 0, 0, 1))
        self.assertEqual(self.target.read_bytes(), PNG)
        self.assertFalse((self.output / ".disk-downloader/previews").exists())
        self.api.preview_url.assert_called_once_with("/Фото/photo.jpg", size="XXXL")
        with closing(sqlite3.connect(self.output / ".disk-downloader/state.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM files").fetchone()[0], 0)
            row = connection.execute("SELECT path,local_path,size,sha256 FROM previews").fetchone()
        self.assertEqual(row, ("Фото/photo.jpg", "Фото/photo.jpg", len(PNG), hashlib.sha256(PNG).hexdigest()))
        self.assertEqual(snapshots[0]["previews"], 1)
        self.assertEqual(snapshots[0]["downloaded"], 0)
        recorded = (self.output / ".disk-downloader/events.log").read_text()
        for secret in (SIGNED_SECRET, TOKEN_SECRET):
            self.assertNotIn(secret, recorded)

    def test_next_run_still_tries_original_but_skips_saved_preview(self):
        self.assertEqual(self.run_download().previews, 1)
        second = self.run_download()
        self.assertEqual(second.previews, 0)
        self.assertEqual(len(self.api.download_calls), 2)
        self.assertEqual(self.api.preview_url.call_count, 1)
        self.assertEqual(self.api.open_preview.call_count, 1)
        self.assertIn("[ПРЕВЬЮ ЕСТЬ] Фото/photo.jpg", self.logs)

    def test_recovered_original_replaces_preview_even_without_optional_sha_check(self):
        self.run_download()
        report = self.run_download(opener=FakeOpener(self.api))
        self.assertTrue(report.complete)
        self.assertEqual((report.downloaded, report.updated, report.previews), (0, 1, 0))
        self.assertEqual((self.output / "Фото/photo.jpg").read_bytes(), b"original photo")
        self.assertEqual(self.target.read_bytes(), b"original photo")
        self.assertEqual(self.api.preview_url.call_count, 1)
        with closing(sqlite3.connect(self.output / ".disk-downloader/state.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM previews").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM files").fetchone()[0], 1)
        again = self.run_download(opener=FakeOpener(self.api))
        self.assertEqual((again.skipped, again.previews), (1, 0))

    def test_changed_original_or_missing_local_preview_refreshes_cache(self):
        self.run_download()
        self.api.add_file("Фото/photo.jpg", b"changed original")
        self.assertEqual(self.run_download().previews, 1)
        self.target.unlink()
        self.assertEqual(self.run_download().previews, 1)
        self.assertEqual(self.api.open_preview.call_count, 3)

    def test_original_partial_is_preserved(self):
        relative = "Фото/photo.jpg"
        partial = self.output / ".disk-downloader/partials" / (hashlib.sha256(relative.encode()).hexdigest() + ".part")
        partial.parent.mkdir(parents=True)
        partial.write_bytes(b"ori")
        partial.with_suffix(".json").write_text(json.dumps({"path": relative, "fingerprint": fingerprint(self.api.resources["/" + relative])}))
        self.assertEqual(self.run_download().previews, 1)
        self.assertEqual(partial.read_bytes(), b"ori")

    def test_local_disk_failure_does_not_trigger_more_writes_for_preview(self):
        report = self.run_download(error=OSError(errno.ENOSPC, "disk full"))
        self.assertEqual(report.previews, 0)
        self.api.preview_url.assert_not_called()

    def test_forbidden_signed_original_link_can_still_have_an_authorized_preview(self):
        error = urllib.error.HTTPError("https://download.example/" + SIGNED_SECRET, 403, "unavailable", {}, None)
        report = self.run_download(error=error)
        self.assertEqual(report.previews, 1)
        self.assertEqual(self.target.read_bytes(), PNG)
        self.assertNotIn(SIGNED_SECRET, "\n".join(self.logs))

    def test_preview_is_only_requested_after_original_retries_are_exhausted(self):
        opener = FakeOpener(self.api, {"/Фото/photo.jpg": [TimeoutError(), TimeoutError(), TimeoutError()]})
        downloader = Downloader(self.api, self.output, workers=1, retries=2, opener=opener,
                                save_previews=True, log=self.logs.append)
        downloader.stop.wait = Mock(return_value=False)
        report = downloader.run()
        self.assertEqual(len(opener.requests), 3)
        self.assertEqual(report.previews, 1)
        self.api.preview_url.assert_called_once()

    def test_original_success_on_retry_never_requests_preview(self):
        opener = FakeOpener(self.api, {"/Фото/photo.jpg": [TimeoutError(), TimeoutError()]})
        downloader = Downloader(self.api, self.output, workers=1, retries=2, opener=opener,
                                save_previews=True, log=self.logs.append)
        downloader.stop.wait = Mock(return_value=False)
        report = downloader.run()
        self.assertEqual((report.downloaded, report.previews), (1, 0))
        self.assertTrue(report.complete)
        self.api.preview_url.assert_not_called()

    def test_no_preview_or_failed_preview_does_not_block_other_files(self):
        self.api.add_file("available.txt", b"available")
        self.api.preview_url.return_value = None
        report = self.run_download()
        self.assertEqual((report.downloaded, report.previews), (1, 0))
        self.api.open_preview.assert_not_called()
        self.api.preview_url.return_value = "https://downloader.disk.yandex.ru/preview/" + SIGNED_SECRET
        self.api.open_preview.side_effect = urllib.error.URLError("https://secret.example/" + SIGNED_SECRET)
        report = self.run_download()
        self.assertEqual((report.skipped, report.previews), (1, 0))
        self.assertNotIn(SIGNED_SECRET, "\n".join(self.logs))

    def test_html_truncated_or_oversized_preview_never_replaces_saved_image(self):
        self.run_download()
        self.api.add_file("Фото/photo.jpg", b"new original")
        responses = [
            FakeResponse(b"<html>error</html>", headers={"Content-Type": "text/html"}),
            FakeResponse(PNG[:-10], headers={"Content-Type": "image/png"}),
            FakeResponse(PNG, headers={"Content-Length": str(len(PNG) + 1)}),
            FakeResponse(PNG, headers={"Content-Length": str(MAX_PREVIEW_BYTES + 1)}),
            FakeResponse(PNG, headers={"Content-Type": "image/jpeg"}),
            FakeResponse(PNG, status=206),
        ]
        for response in responses:
            with self.subTest(headers=response.headers, status=response.status):
                self.api.open_preview.side_effect = None
                self.api.open_preview.return_value = response
                self.assertEqual(self.run_download().previews, 0)
                self.assertEqual(self.target.read_bytes(), PNG)
                self.assertEqual(list(self.output.glob(".disk-downloader/partials/.preview-*.part")), [])

    def test_cancellation_cleans_preview_temporary_without_marking_original_complete(self):
        stop = threading.Event()
        response = FakeResponse(PNG)
        read = response.read
        def cancelling_read(size):
            data = read(size)
            stop.set()
            return data
        response.read = cancelling_read
        self.api.open_preview.side_effect = None
        self.api.open_preview.return_value = response
        report = self.run_download(stop_event=stop)
        self.assertTrue(report.interrupted)
        self.assertEqual(report.previews, 0)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.output.glob(".disk-downloader/partials/.preview-*.part")), [])

    def test_preview_names_cannot_conflict_with_other_source_folders(self):
        self.api.add_file("Фото/photo.jpg.preview.png/nested.jpg", b"second original")
        opener = FakeOpener(self.api, {path: [TimeoutError()] for path in self.api.contents})
        report = self.run_download(opener=opener)
        self.assertEqual(report.previews, 2)
        self.assertEqual(self.target.read_bytes(), PNG)
        self.assertEqual((self.output / "Фото/photo.jpg.preview.png/nested.jpg").read_bytes(), PNG)

    def test_symlink_at_original_path_is_rejected_without_touching_destination(self):
        self.run_download()
        other = self.output.parent / "elsewhere.jpg"
        other.write_bytes(b"keep")
        self.target.unlink()
        symlink_or_skip(self, self.target, other)
        self.assertEqual(self.run_download().previews, 0)
        self.assertEqual(other.read_bytes(), b"keep")
        self.assertEqual(self.api.preview_url.call_count, 1)

    def test_previous_full_original_is_never_downgraded_to_preview(self):
        self.assertTrue(self.run_download(opener=FakeOpener(self.api)).complete)
        self.api.add_file("Фото/photo.jpg", b"new version currently unavailable")
        report = self.run_download()
        self.assertEqual(report.previews, 0)
        self.assertEqual(self.target.read_bytes(), b"original photo")
        self.api.preview_url.assert_not_called()

    def test_untracked_local_file_is_never_overwritten_by_preview(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"user file")
        report = self.run_download()
        self.assertEqual(report.previews, 0)
        self.assertEqual(self.target.read_bytes(), b"user file")
        self.api.preview_url.assert_not_called()

    def test_manually_replaced_preview_is_not_downgraded_again(self):
        self.run_download()
        self.target.write_bytes(b"a full copy supplied by the user")
        report = self.run_download()
        self.assertEqual(report.previews, 0)
        self.assertEqual(self.target.read_bytes(), b"a full copy supplied by the user")
        self.assertEqual(self.api.preview_url.call_count, 1)

    def test_file_created_during_preview_transfer_is_kept(self):
        def created_during_open(*args, **kwargs):
            self.target.write_bytes(b"concurrent local copy")
            return FakeResponse(PNG, headers={"Content-Type": "image/png"})
        self.api.open_preview.side_effect = created_during_open
        report = self.run_download()
        self.assertEqual(report.previews, 0)
        self.assertEqual(self.target.read_bytes(), b"concurrent local copy")
