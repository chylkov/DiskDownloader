"""Exercise recovery behavior without connecting to Yandex or another network."""
from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.parse

from yandex_disk.api import ApiError, Resource
from yandex_disk.downloader import Downloader, RecoveryError, fingerprint, output_lock
from platform_helpers import symlink_or_skip


SIGNED_SECRET = "signed-download-secret"
TOKEN_SECRET = "oauth-secret-do-not-log"


class FakeAPI:
    """A changing remote tree, with independent failures for individual folders."""

    root_path = "/"

    def __init__(self, files=None, source_id="test-source"):
        self.source_id = source_id
        self._token = TOKEN_SECRET
        self.resources = {"/": Resource("Remote root", "/", "dir")}
        self.contents = {}
        self.list_failures = {}
        self.list_calls = []
        self.download_calls = []
        self.lock = threading.Lock()
        for relative, content in (files or {}).items():
            self.add_file(relative, content)

    def add_directory(self, relative):
        parts = PurePosixPath(relative).parts
        for length in range(1, len(parts) + 1):
            path = "/" + "/".join(parts[:length])
            self.resources[path] = Resource(parts[length - 1], path, "dir")

    def add_file(self, relative, content, *, sha256=True, md5=False, modified="2026-10-14T10:00:00Z"):
        relative = str(PurePosixPath(relative))
        parent = str(PurePosixPath(relative).parent)
        if parent != ".":
            self.add_directory(parent)
        path = "/" + relative
        self.contents[path] = content
        self.resources[path] = Resource(
            name=PurePosixPath(relative).name,
            path=path,
            kind="file",
            size=len(content),
            modified=modified,
            sha256=hashlib.sha256(content).hexdigest() if sha256 else None,
            md5=hashlib.md5(content).hexdigest() if md5 else None,
        )
        return self.resources[path]

    def get_resource(self, path):
        return self.resources[path]

    def iter_children(self, path):
        self.list_calls.append(path)
        children = sorted(
            (resource for resource in self.resources.values()
             if resource.path != path and str(PurePosixPath(resource.path).parent) == path),
            key=lambda resource: resource.name,
        )
        failure = self.list_failures.get(path)
        if isinstance(failure, Exception):
            raise failure
        for index, resource in enumerate(children):
            if isinstance(failure, int) and index == failure:
                raise ApiError("Папка временно недоступна", status=503, retryable=True)
            yield resource

    def download_url(self, path):
        with self.lock:
            self.download_calls.append(path)
        return "https://download.example/signed?" + urllib.parse.urlencode(
            {"secret": SIGNED_SECRET, "path": path}
        )


class FakeResponse:
    def __init__(self, body, status=200, headers=None, *, read_error=None, fail_after=None):
        self.status = status
        self.headers = {"Content-Length": str(len(body))}
        self.headers.update(headers or {})
        self.stream = io.BytesIO(body)
        self.read_error = read_error
        self.fail_after = fail_after

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stream.close()

    def read(self, size=-1):
        if self.read_error is not None and (
            self.fail_after is None or self.stream.tell() >= self.fail_after
        ):
            raise self.read_error
        if self.fail_after is not None:
            size = min(size, self.fail_after - self.stream.tell())
        return self.stream.read(size)


class FakeOpener:
    """Return valid range responses by default; scripts override particular opens."""

    def __init__(self, api, scripts=None):
        self.api = api
        self.scripts = {path: list(actions) for path, actions in (scripts or {}).items()}
        self.requests = []
        self.lock = threading.Lock()

    def __call__(self, request, timeout):
        path = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)["path"][0]
        with self.lock:
            self.requests.append(request)
            script = self.scripts.get(path)
            action = script.pop(0) if script else None
        if isinstance(action, Exception):
            raise action
        if action is not None:
            return action(request) if callable(action) else action
        content = self.api.contents[path]
        range_header = request.get_header("Range")
        if range_header:
            offset = int(range_header.removeprefix("bytes=").removesuffix("-"))
            return FakeResponse(
                content[offset:], status=206,
                headers={"Content-Range": f"bytes {offset}-{len(content) - 1}/{len(content)}"},
            )
        return FakeResponse(content)


class DownloaderRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.output = self.base / "recovered"
        self.logs = []

    def run_download(self, api, *, opener=None, verify=False):
        opener = opener or FakeOpener(api)
        report = Downloader(
            api, self.output, workers=3, retries=0, verify=verify,
            log=self.logs.append, opener=opener,
        ).run()
        return report, opener

    def partial_path(self, relative):
        key = hashlib.sha256(relative.encode()).hexdigest()
        return self.output / ".disk-downloader" / "partials" / f"{key}.part"

    def seed_partial(self, relative, resource, content):
        partial = self.partial_path(relative)
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(content)
        partial.with_suffix(".json").write_text(
            json.dumps({"path": relative, "fingerprint": fingerprint(resource)}), encoding="utf-8"
        )
        return partial

    def archive_contents(self):
        return [path.read_bytes() for path in self.output.glob(".disk-downloader/versions/**/*.bak")]

    def test_repeat_runs_discover_new_files_restore_missing_local_files_and_keep_vanished_remote_files(self):
        api = FakeAPI({"existing.txt": b"existing", "vanished.txt": b"keep me"})
        first, _ = self.run_download(api)
        self.assertTrue(first.complete)
        self.assertEqual(first.downloaded, 2)

        del api.resources["/vanished.txt"]
        api.add_file("restored/subfolder/фото.jpg", b"returned from recovery")
        api.add_file("restored/empty.txt", b"")
        api.add_directory("empty directory")
        (self.output / "existing.txt").unlink()

        second, _ = self.run_download(api)
        self.assertTrue(second.complete)
        self.assertEqual(second.downloaded, 3)
        self.assertEqual((self.output / "existing.txt").read_bytes(), b"existing")
        self.assertEqual((self.output / "vanished.txt").read_bytes(), b"keep me")
        self.assertEqual((self.output / "restored/subfolder/фото.jpg").read_bytes(), b"returned from recovery")
        self.assertEqual((self.output / "restored/empty.txt").read_bytes(), b"")
        self.assertTrue((self.output / "empty directory").is_dir())

        third, opener = self.run_download(api)
        self.assertTrue(third.complete)
        self.assertEqual(third.skipped, 3)
        self.assertEqual(third.downloaded, 0)
        self.assertEqual(opener.requests, [])
        self.assertEqual(api.list_calls.count("/restored/subfolder"), 2)

    def test_unavailable_subfolder_does_not_block_other_files_and_is_revisited(self):
        api = FakeAPI({"broken/later.txt": b"later", "healthy.txt": b"now"})
        api.list_failures["/broken"] = ApiError("Папка временно недоступна", status=503, retryable=True)
        first, _ = self.run_download(api)
        self.assertFalse(first.complete)
        self.assertEqual(first.downloaded, 1)
        self.assertEqual(first.errors[0]["path"], "broken")
        self.assertEqual((self.output / "healthy.txt").read_bytes(), b"now")

        api.list_failures.clear()
        second, _ = self.run_download(api)
        self.assertTrue(second.complete)
        self.assertEqual(second.downloaded, 1)
        self.assertEqual(second.skipped, 1)
        self.assertEqual((self.output / "broken/later.txt").read_bytes(), b"later")
        self.assertEqual(api.list_calls.count("/broken"), 2)

    def test_listing_failure_after_a_page_preserves_files_already_discovered(self):
        api = FakeAPI({"a-first.txt": b"first page", "z-later.txt": b"later page"})
        api.list_failures["/"] = 1
        first, _ = self.run_download(api)
        self.assertFalse(first.complete)
        self.assertEqual(first.downloaded, 1)
        self.assertEqual((self.output / "a-first.txt").read_bytes(), b"first page")

        api.list_failures.clear()
        second, _ = self.run_download(api)
        self.assertTrue(second.complete)
        self.assertEqual(second.skipped, 1)
        self.assertEqual(second.downloaded, 1)

    def test_short_stream_is_saved_and_next_run_resumes_the_correct_byte_range(self):
        content = b"0123456789"
        api = FakeAPI({"large.bin": content})
        opener = FakeOpener(api, {"/large.bin": [FakeResponse(content[:4], headers={"Content-Length": "10"})]})
        first, _ = self.run_download(api, opener=opener)
        self.assertFalse(first.complete)
        self.assertFalse((self.output / "large.bin").exists())
        self.assertEqual(self.partial_path("large.bin").read_bytes(), content[:4])

        second, resumed = self.run_download(api)
        self.assertTrue(second.complete)
        self.assertEqual(second.downloaded, 1)
        self.assertEqual(resumed.requests[0].get_header("Range"), "bytes=4-")
        self.assertEqual(resumed.requests[0].get_header("Accept-encoding"), "identity")
        self.assertEqual((self.output / "large.bin").read_bytes(), content)
        self.assertFalse(self.partial_path("large.bin").exists())
        self.assertFalse(self.partial_path("large.bin").with_suffix(".json").exists())

    def test_server_ignoring_range_restarts_instead_of_appending_a_second_copy(self):
        content = b"0123456789"
        api = FakeAPI({"file.bin": content})
        self.seed_partial("file.bin", api.resources["/file.bin"], content[:4])
        opener = FakeOpener(api, {"/file.bin": [FakeResponse(content, status=200)]})
        report, _ = self.run_download(api, opener=opener)
        self.assertTrue(report.complete)
        self.assertEqual(opener.requests[0].get_header("Range"), "bytes=4-")
        self.assertEqual((self.output / "file.bin").read_bytes(), content)

    def test_invalid_content_ranges_never_publish_or_keep_corrupted_partials(self):
        content = b"0123456789"
        invalid_ranges = ["bytes 3-9/10", "bytes 4-9/11", "bytes 4-3/10", "bytes 4-10/10", "items 4-9/10"]
        for index, invalid_range in enumerate(invalid_ranges):
            with self.subTest(content_range=invalid_range):
                relative = f"file-{index}.bin"
                api = FakeAPI({relative: content}, source_id="test-source")
                self.seed_partial(relative, api.resources["/" + relative], content[:4])
                opener = FakeOpener(api, {"/" + relative: [FakeResponse(content[4:], status=206, headers={"Content-Range": invalid_range})]})
                report, _ = self.run_download(api, opener=opener)
                self.assertFalse(report.complete)
                self.assertFalse((self.output / relative).exists())
                self.assertFalse(self.partial_path(relative).exists())

    def test_checksum_failure_keeps_last_good_copy_until_a_valid_replacement_arrives(self):
        api = FakeAPI({"important.txt": b"original"})
        self.assertTrue(self.run_download(api)[0].complete)
        api.add_file("important.txt", b"new copy")
        corrupt = FakeOpener(api, {"/important.txt": [FakeResponse(b"bad copy")]})
        failed, _ = self.run_download(api, opener=corrupt)
        self.assertFalse(failed.complete)
        self.assertEqual(failed.updated, 0)
        self.assertEqual((self.output / "important.txt").read_bytes(), b"original")
        self.assertEqual(self.archive_contents(), [])

        recovered, _ = self.run_download(api)
        self.assertTrue(recovered.complete)
        self.assertEqual(recovered.updated, 1)
        self.assertEqual((self.output / "important.txt").read_bytes(), b"new copy")
        self.assertEqual(self.archive_contents(), [])

    def test_repeated_remote_updates_overwrite_the_same_file_without_archiving(self):
        api = FakeAPI({"subfolder/file.bin": b"original"})
        target = self.output / "subfolder/file.bin"
        self.assertTrue(self.run_download(api)[0].complete)

        for content in (b"updated!", b"a longer third version"):
            with self.subTest(content=content):
                api.add_file("subfolder/file.bin", content)
                updated, opener = self.run_download(api)
                self.assertTrue(updated.complete)
                self.assertEqual(updated.updated, 1)
                self.assertEqual(updated.downloaded, 0)
                self.assertEqual(len(opener.requests), 1)
                self.assertIsNone(opener.requests[0].get_header("Range"))
                self.assertEqual(target.read_bytes(), content)
                self.assertFalse((self.output / ".disk-downloader/versions").exists())
                self.assertEqual(list(target.parent.iterdir()), [target])

                unchanged, skipped_opener = self.run_download(api)
                self.assertTrue(unchanged.complete)
                self.assertEqual(unchanged.skipped, 1)
                self.assertEqual(unchanged.updated, 0)
                self.assertEqual(unchanged.downloaded, 0)
                self.assertEqual(skipped_opener.requests, [])

    def test_failed_final_replace_keeps_original_and_retries_verified_partial(self):
        api = FakeAPI({"important.txt": b"original"})
        target = self.output / "important.txt"
        self.assertTrue(self.run_download(api)[0].complete)
        resource = api.add_file("important.txt", b"verified new copy")
        partial = self.partial_path("important.txt")
        real_replace = os.replace

        def fail_final_replace(source, destination):
            if Path(destination).resolve() == target.resolve():
                self.assertEqual(Path(source).read_bytes(), api.contents[resource.path])
                raise PermissionError(13, "Permission denied")
            return real_replace(source, destination)

        with mock.patch("yandex_disk.downloader.os.replace", side_effect=fail_final_replace):
            failed, _ = self.run_download(api)
        self.assertFalse(failed.complete)
        self.assertEqual(failed.updated, 0)
        self.assertEqual(failed.errors[0]["path"], "important.txt")
        self.assertEqual(target.read_bytes(), b"original")
        self.assertEqual(partial.read_bytes(), b"verified new copy")
        self.assertEqual(
            json.loads(partial.with_suffix(".json").read_text(encoding="utf-8")),
            {"path": "important.txt", "fingerprint": fingerprint(resource)},
        )
        self.assertEqual(self.archive_contents(), [])

        recovered, opener = self.run_download(api)
        self.assertTrue(recovered.complete)
        self.assertEqual(recovered.updated, 1)
        self.assertEqual(recovered.bytes_received, 0)
        self.assertEqual(opener.requests, [])
        self.assertEqual(target.read_bytes(), b"verified new copy")
        self.assertFalse(partial.exists())
        self.assertFalse(partial.with_suffix(".json").exists())
        self.assertEqual(self.archive_contents(), [])

        unchanged, opener = self.run_download(api)
        self.assertTrue(unchanged.complete)
        self.assertEqual(unchanged.skipped, 1)
        self.assertEqual(opener.requests, [])

    def test_changed_remote_version_discards_partial_even_when_file_size_is_unchanged(self):
        old, new = b"old-version", b"new-version"
        api = FakeAPI({"file.bin": old})
        self.seed_partial("file.bin", api.resources["/file.bin"], old[:4])
        api.add_file("file.bin", new)
        report, opener = self.run_download(api)
        self.assertTrue(report.complete)
        self.assertIsNone(opener.requests[0].get_header("Range"))
        self.assertEqual((self.output / "file.bin").read_bytes(), new)

    def test_verify_detects_corruption_even_when_size_and_mtime_are_unchanged(self):
        api = FakeAPI({"file.bin": b"good data"})
        self.assertTrue(self.run_download(api)[0].complete)
        target = self.output / "file.bin"
        previous_stat = target.stat()
        target.write_bytes(b"bad! data")
        os.utime(target, ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns))

        checked, _ = self.run_download(api, verify=True)
        self.assertTrue(checked.complete)
        self.assertEqual(checked.updated, 1)
        self.assertEqual(target.read_bytes(), b"good data")
        self.assertEqual(self.archive_contents(), [])

    def test_matching_existing_file_is_adopted_using_provider_checksum(self):
        self.output.mkdir()
        (self.output / "existing.bin").write_bytes(b"previous manual download")
        api = FakeAPI()
        api.add_file("existing.bin", b"previous manual download", sha256=False, md5=True)
        report, opener = self.run_download(api)
        self.assertTrue(report.complete)
        self.assertEqual(report.skipped, 1)
        self.assertEqual(opener.requests, [])

    @unittest.skipUnless(hasattr(os, "symlink"), "requires symbolic links")
    def test_local_file_and_directory_symlinks_cannot_redirect_recovery_writes(self):
        self.output.mkdir()
        outside = self.base / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_bytes(b"untouched")
        symlink_or_skip(self, self.output / "linked-file.txt", sentinel)
        symlink_or_skip(self, self.output / "linked-folder", outside, directory=True)
        api = FakeAPI({"linked-file.txt": b"replace", "linked-folder/sentinel.txt": b"replace", "healthy.txt": b"safe"})
        report, opener = self.run_download(api)
        self.assertFalse(report.complete)
        self.assertEqual(len(report.errors), 2)
        self.assertEqual(sentinel.read_bytes(), b"untouched")
        self.assertEqual((self.output / "healthy.txt").read_bytes(), b"safe")
        self.assertEqual(len(opener.requests), 1)

    def test_output_cannot_be_reused_for_a_different_source(self):
        original = FakeAPI({"file.txt": b"first"}, source_id="first-source")
        self.assertTrue(self.run_download(original)[0].complete)
        different = FakeAPI({"file.txt": b"second"}, source_id="second-source")
        with self.assertRaises(RecoveryError):
            self.run_download(different)
        self.assertEqual((self.output / "file.txt").read_bytes(), b"first")
        self.assertEqual(different.download_calls, [])

    def test_http_failure_logs_and_reports_never_include_signed_url_or_oauth(self):
        api = FakeAPI({"file.txt": b"content"})
        signed_url = api.download_url("/file.txt")
        error = urllib.error.HTTPError(signed_url, 503, TOKEN_SECRET, {}, None)
        report, opener = self.run_download(api, opener=FakeOpener(api, {"/file.txt": [error]}))
        self.assertFalse(report.complete)
        self.assertIn("503", report.errors[0]["error"])
        logged = "\n".join(self.logs) + json.dumps(report.errors, ensure_ascii=False)
        logged += (self.output / ".disk-downloader/events.log").read_text(encoding="utf-8")
        logged += (self.output / ".disk-downloader/last-run.json").read_text(encoding="utf-8")
        for secret in (SIGNED_SECRET, TOKEN_SECRET, signed_url):
            self.assertNotIn(secret, logged)
        self.assertIsNone(opener.requests[0].get_header("Authorization"))

    def test_a_second_run_is_rejected_while_the_output_is_locked(self):
        state_dir = self.output / ".disk-downloader"
        state_dir.mkdir(parents=True)
        api = FakeAPI({"file.txt": b"content"})
        with output_lock(state_dir):
            with self.assertRaises(RecoveryError):
                self.run_download(api)
        self.assertFalse((self.output / "file.txt").exists())
        self.assertTrue(self.run_download(api)[0].complete)

    def test_incomplete_chunked_http_stream_is_a_file_error_and_other_files_continue(self):
        api = FakeAPI({"broken.bin": b"0123456789", "healthy.txt": b"safe"})
        broken = FakeResponse(
            b"0123", headers={"Content-Length": "10"},
            read_error=http.client.IncompleteRead(b"45", 4), fail_after=4,
        )
        report, _ = self.run_download(api, opener=FakeOpener(api, {"/broken.bin": [broken]}))
        self.assertFalse(report.complete)
        self.assertEqual(report.errors[0]["path"], "broken.bin")
        self.assertEqual((self.output / "healthy.txt").read_bytes(), b"safe")
        self.assertFalse((self.output / "broken.bin").exists())
        self.assertEqual(self.partial_path("broken.bin").read_bytes(), b"012345")
        recovered, _ = self.run_download(api)
        self.assertTrue(recovered.complete)
        self.assertEqual((self.output / "broken.bin").read_bytes(), b"0123456789")

    def test_invalid_child_name_does_not_hide_later_valid_siblings(self):
        api = FakeAPI({"z-healthy.txt": b"safe"})
        api.resources["/a-bad"] = Resource("../outside.txt", "/a-bad", "file", size=3)
        report, _ = self.run_download(api)
        self.assertFalse(report.complete)
        self.assertEqual(report.downloaded, 1)
        self.assertEqual((self.output / "z-healthy.txt").read_bytes(), b"safe")
        self.assertFalse((self.base / "outside.txt").exists())

    def test_unicode_aliases_are_reported_before_concurrent_writes_can_overwrite_a_file(self):
        api = FakeAPI({"café.txt": b"composed", "cafe\u0301.txt": b"decomposed"})
        report, opener = self.run_download(api)
        self.assertFalse(report.complete)
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(report.downloaded, 1)
        self.assertEqual(len(opener.requests), 1)
        downloaded_path = urllib.parse.parse_qs(
            urllib.parse.urlsplit(opener.requests[0].full_url).query
        )["path"][0]
        self.assertEqual(
            (self.output / api.resources[downloaded_path].name).read_bytes(),
            api.contents[downloaded_path],
        )
        self.assertEqual(self.archive_contents(), [])

    def test_existing_local_name_in_other_case_is_reported_and_kept(self):
        self.output.mkdir(parents=True)
        (self.output / "Photo.JPG").write_bytes(b"local copy")
        if not (self.output / "photo.jpg").exists():
            self.skipTest("requires a case-insensitive filesystem")
        api = FakeAPI({"photo.jpg": b"remote", "other.txt": b"safe"})
        report, opener = self.run_download(api)
        self.assertEqual([error["path"] for error in report.errors], ["photo.jpg"])
        self.assertIn("Конфликт регистра", report.errors[0]["error"])
        self.assertEqual(len(opener.requests), 1)
        self.assertIn("Photo.JPG", os.listdir(self.output))
        self.assertEqual((self.output / "Photo.JPG").read_bytes(), b"local copy")
        self.assertEqual((self.output / "other.txt").read_bytes(), b"safe")

    def test_rerun_lists_each_local_directory_once_instead_of_once_per_file(self):
        api = FakeAPI({f"photos/IMG_{index:03d}.jpg": b"photo %d" % index for index in range(40)})
        self.assertTrue(self.run_download(api)[0].complete)
        with mock.patch("os.listdir", wraps=os.listdir) as listdir:
            report, opener = self.run_download(api)
        self.assertTrue(report.complete)
        self.assertEqual(report.skipped, 40)
        self.assertEqual(opener.requests, [])
        listed = [Path(call.args[0]).resolve() for call in listdir.call_args_list if call.args]
        self.assertEqual(listed.count((self.output / "photos").resolve()), 1)

    def test_remote_file_directory_type_changes_preserve_existing_local_contents(self):
        api = FakeAPI({"was-file": b"keep original", "was-directory/child.txt": b"keep child"})
        self.assertTrue(self.run_download(api)[0].complete)
        api.resources["/was-file"] = Resource("was-file", "/was-file", "dir")
        api.add_file("was-file/new-child.txt", b"new child")
        del api.resources["/was-directory/child.txt"]
        api.add_file("was-directory", b"replacement file")
        api.add_file("healthy.txt", b"safe")

        report, _ = self.run_download(api)
        self.assertFalse(report.complete)
        self.assertEqual(len(report.errors), 2)
        self.assertEqual(report.downloaded, 1)
        self.assertEqual((self.output / "was-file").read_bytes(), b"keep original")
        self.assertEqual((self.output / "was-directory/child.txt").read_bytes(), b"keep child")
        self.assertEqual((self.output / "healthy.txt").read_bytes(), b"safe")

    def test_unavailable_remote_root_keeps_previously_recovered_tree(self):
        api = FakeAPI({"subfolder/important.txt": b"safe copy"})
        self.assertTrue(self.run_download(api)[0].complete)
        with mock.patch.object(api, "get_resource", side_effect=ApiError("Источник временно недоступен", status=404)):
            report, opener = self.run_download(api)
        self.assertFalse(report.complete)
        self.assertEqual(report.errors[0]["path"], "/")
        self.assertEqual(opener.requests, [])
        self.assertEqual((self.output / "subfolder/important.txt").read_bytes(), b"safe copy")


if __name__ == "__main__":
    unittest.main()
