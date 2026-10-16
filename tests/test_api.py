from __future__ import annotations

from email.message import Message
import io
import json
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from yandex_disk.api import ApiError, Resource, YandexDiskAPI, _NoMetadataRedirect


def response(data):
    return io.BytesIO(json.dumps(data).encode())


def file(name="file.txt", prefix="/"):
    return {"name": name, "path": prefix + name, "type": "file", "size": 7, "modified": "2026-10-14T12:00:00+00:00"}


def directory(items, total=None, offset=0):
    return {
        "name": "folder",
        "path": "/",
        "type": "dir",
        "_embedded": {"items": items, "total": len(items) if total is None else total, "offset": offset},
    }


class ApiTests(unittest.TestCase):
    def public(self, **kwargs):
        client = YandexDiskAPI(public_url="https://disk.yandex.ru/d/example", **kwargs)
        client._opener = Mock()
        return client

    def test_public_resource_request_never_has_oauth(self):
        client = self.public(token="test-secret")
        client._opener.open.return_value = response(file())
        self.assertEqual(client.get_resource("/").size, 7)
        request = client._opener.open.call_args.args[0]
        self.assertFalse(request.has_header("Authorization"))
        self.assertEqual(urlsplit(request.full_url).path, "/v1/disk/public/resources")
        self.assertEqual(parse_qs(urlsplit(request.full_url).query)["public_key"], ["https://disk.yandex.ru/d/example"])

    def test_private_resource_request_normalizes_path_and_sends_header(self):
        client = YandexDiskAPI(disk_path="/Фото//2026/", token="test-secret")
        client._opener = Mock()
        client._opener.open.return_value = response({"name": "2026", "path": client.root_path, "type": "dir"})
        self.assertEqual(client.root_path, "disk:/Фото/2026")
        client.get_resource(client.root_path)
        request = client._opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "OAuth test-secret")
        self.assertEqual(parse_qs(urlsplit(request.full_url).query)["path"], ["disk:/Фото/2026"])

    def test_private_source_is_bound_to_token_without_revealing_it(self):
        one = YandexDiskAPI(disk_path="Photos", token="token-one")
        same = YandexDiskAPI(disk_path="disk:/Photos/", token="token-one")
        another = YandexDiskAPI(disk_path="Photos", token="token-two")
        self.assertEqual(one.source_id, same.source_id)
        self.assertNotEqual(one.source_id, another.source_id)
        self.assertNotIn("token", one.source_id)

    def test_pagination_advances_by_received_count_and_sorts(self):
        client = self.public()
        client._opener.open.side_effect = [
            response(directory([file("b"), file("a")], total=3)),
            response(directory([file("c")], total=3, offset=2)),
        ]
        self.assertEqual([item.name for item in client.iter_children("/")], ["a", "b", "c"])
        requests = [call.args[0] for call in client._opener.open.call_args_list]
        self.assertEqual([parse_qs(urlsplit(req.full_url).query)["offset"][0] for req in requests], ["0", "2"])
        self.assertTrue(all(parse_qs(urlsplit(req.full_url).query)["sort"] == ["name"] for req in requests))

    def test_empty_page_does_not_silently_mark_remaining_files_complete(self):
        client = self.public()
        client._opener.open.side_effect = [
            response(directory([file()], total=2)),
            response(directory([], total=2, offset=1)),
        ]
        iterator = client.iter_children("/")
        self.assertEqual(next(iterator).name, "file.txt")
        with self.assertRaises(ApiError) as caught:
            next(iterator)
        self.assertTrue(caught.exception.retryable)

    def test_duplicate_paths_across_pages_fail(self):
        client = self.public()
        client._opener.open.side_effect = [
            response(directory([file()], total=2)),
            response(directory([file()], total=2, offset=1)),
        ]
        iterator = client.iter_children("/")
        next(iterator)
        with self.assertRaises(ApiError):
            next(iterator)

    def test_changed_directory_total_fails(self):
        client = self.public()
        client._opener.open.side_effect = [
            response(directory([file()], total=2)),
            response(directory([file("other")], total=3, offset=1)),
        ]
        with self.assertRaises(ApiError):
            list(client.iter_children("/"))

    def test_damaged_metadata_does_not_block_valid_siblings_or_later_pages(self):
        client = self.public()
        invalid = {"name": "bad", "path": "/bad", "type": "file", "size": 7}
        client._opener.open.side_effect = [
            response(directory([invalid, file("a")], total=4)),
            response(directory([file("b"), file("c")], total=4, offset=2)),
        ]
        iterator = client.iter_children("/")
        self.assertEqual(next(iterator).name, "a")
        self.assertEqual(next(iterator).name, "b")
        self.assertEqual(next(iterator).name, "c")
        with self.assertRaises(ApiError) as caught:
            next(iterator)
        self.assertTrue(caught.exception.retryable)
        self.assertIn("Пропущено 1", str(caught.exception))
        self.assertEqual(client._opener.open.call_count, 2)

    def test_page_with_only_damaged_items_still_advances_to_later_page(self):
        client = self.public()
        client._opener.open.side_effect = [
            response(directory([None, {"name": "secret-token"}], total=3)),
            response(directory([file("rescued")], total=3, offset=2)),
        ]
        iterator = client.iter_children("/")
        self.assertEqual(next(iterator).name, "rescued")
        with self.assertRaises(ApiError) as caught:
            next(iterator)
        self.assertIn("Пропущено 2", str(caught.exception))
        self.assertNotIn("secret-token", str(caught.exception))

    def test_http_errors_are_sanitized_and_notfound_is_not_retried(self):
        client = self.public()
        secret = "https://downloader.example/secret-token"
        client._opener.open.side_effect = HTTPError(secret, 404, secret, {}, io.BytesIO(secret.encode()))
        with self.assertRaises(ApiError) as caught:
            client.get_resource("/")
        self.assertEqual(caught.exception.status, 404)
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(client._opener.open.call_count, 1)

    @patch("yandex_disk.api.time.sleep")
    def test_rate_limit_retries_with_bounded_retry_after(self, sleep):
        client = self.public(retries=1)
        headers = Message()
        headers["Retry-After"] = "100000"
        client._opener.open.side_effect = [HTTPError("private", 429, "secret", headers, io.BytesIO()), response(file())]
        client.get_resource("/")
        sleep.assert_called_once_with(60.0)

    @patch("yandex_disk.api.time.sleep")
    def test_network_retries_are_bounded_and_logs_have_no_exception_text(self, sleep):
        logs = []
        client = self.public(retries=2, log=logs.append)
        client._opener.open.side_effect = URLError("secret-token https://signed.example/secret")
        with self.assertRaises(ApiError) as caught:
            client.get_resource("/")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(client._opener.open.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertNotIn("secret", str(caught.exception) + " ".join(logs))

    def test_download_requires_https_get(self):
        for data in (
            {"href": "http://example.com/file", "method": "GET"},
            {"href": "https://secret@example.com/file", "method": "GET"},
            {"href": "https://example.com/file", "method": "PUT"},
            {"href": "https://example.com/file", "method": "GET", "templated": True},
        ):
            with self.subTest(data=data):
                client = self.public()
                client._opener.open.return_value = response(data)
                with self.assertRaises(ApiError):
                    client.download_url("/file")
        client = self.public()
        client._opener.open.return_value = response({"href": "https://example.com/file", "method": "GET", "templated": False})
        self.assertEqual(client.download_url("/file"), "https://example.com/file")

    def test_metadata_redirects_are_disabled(self):
        self.assertIsNone(_NoMetadataRedirect().redirect_request(None, None, 302, "", {}, "https://other-host/"))

    def test_invalid_json_shape_fails(self):
        for data in ([], None, "secret"):
            client = self.public()
            client._opener.open.return_value = response(data)
            with self.assertRaises(ApiError):
                client.get_resource("/")

    def test_invalid_sizes_and_hashes_fail(self):
        for value in (-1, True, "7", None):
            data = file()
            data["size"] = value
            with self.subTest(size=value), self.assertRaises(ApiError):
                Resource.from_json(data)
        for key, value in (("md5", "secret"), ("sha256", "a" * 63)):
            data = file()
            data[key] = value
            with self.subTest(key=key), self.assertRaises(ApiError):
                Resource.from_json(data)
        data = file()
        data["md5"] = "A" * 32
        self.assertEqual(Resource.from_json(data).md5, "a" * 32)

    def test_file_without_version_identity_is_rejected(self):
        data = file()
        del data["modified"]
        with self.assertRaises(ApiError):
            Resource.from_json(data)

    def test_cancellation_stops_before_request(self):
        client = self.public()
        client.stop_event = threading.Event()
        client.stop_event.set()
        with self.assertRaises(ApiError):
            client.get_resource("/")
        client._opener.open.assert_not_called()

    def test_cancellation_can_interrupt_retry_delay(self):
        client = self.public()
        client.stop_event = Mock()
        client.stop_event.is_set.return_value = False
        client.stop_event.wait.return_value = True
        client._opener.open.side_effect = URLError("unavailable")
        with self.assertRaises(ApiError):
            client.get_resource("/")
        self.assertEqual(client._opener.open.call_count, 1)
        client.stop_event.wait.assert_called_once_with(1.0)


if __name__ == "__main__":
    unittest.main()
