"""Private previews authenticate only the documented host, never signed URLs."""

from __future__ import annotations

import io
import json
import threading
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from yandex_disk.api import ApiError, YandexDiskAPI, _NoMetadataRedirect


TRUSTED = "https://downloader.disk.yandex.ru/preview/signed-test?size=XXXL"
TOKEN = "test-preview-token"


class PreviewAPITests(unittest.TestCase):
    def client(self, public=False):
        args = {"public_url": "https://disk.yandex.ru/d/test"} if public else {"disk_path": "/Photos", "token": TOKEN}
        client = YandexDiskAPI(**args, retries=0)
        client._opener = Mock()
        client._preview_opener = Mock()
        return client

    def metadata(self, client, data):
        client._opener.open.return_value = io.BytesIO(json.dumps(data).encode())

    def requests(self, client):
        return [call.args[0] for call in client._preview_opener.open.call_args_list]

    def redirect(self, url):
        return HTTPError(TRUSTED, 302, "private-signed-test", {"Location": url}, io.BytesIO())

    def test_preview_requested_lazily_with_largest_named_size_and_no_crop(self):
        client = self.client()
        client._opener.open.assert_not_called()
        self.metadata(client, {"preview": TRUSTED})
        self.assertEqual(client.preview_url("disk:/Photos/one.jpg"), TRUSTED)
        request = client._opener.open.call_args.args[0]
        self.assertEqual(urlsplit(request.full_url).path, "/v1/disk/resources")
        self.assertEqual(request.get_header("Authorization"), "OAuth " + TOKEN)
        self.assertEqual(parse_qs(urlsplit(request.full_url).query), {
            "path": ["disk:/Photos/one.jpg"], "limit": ["0"], "fields": ["preview"],
            "preview_size": ["XXXL"], "preview_crop": ["false"],
        })

    def test_public_metadata_and_preview_request_have_no_oauth(self):
        client = self.client(public=True)
        self.metadata(client, {"preview": TRUSTED})
        self.assertEqual(client.preview_url("/one.jpg", "XL"), TRUSTED)
        request = client._opener.open.call_args.args[0]
        self.assertEqual(urlsplit(request.full_url).path, "/v1/disk/public/resources")
        self.assertEqual(parse_qs(urlsplit(request.full_url).query)["public_key"], ["https://disk.yandex.ru/d/test"])
        self.assertFalse(request.has_header("Authorization"))
        client._preview_opener.open.return_value = io.BytesIO(b"image")
        with client.open_preview(TRUSTED):
            pass
        self.assertFalse(self.requests(client)[0].has_header("Authorization"))

    def test_missing_preview_is_normal(self):
        for data in ({}, {"preview": None}):
            client = self.client()
            self.metadata(client, data)
            self.assertIsNone(client.preview_url("disk:/Photos/file.bin"))
            client._preview_opener.open.assert_not_called()

    def test_malformed_preview_links_are_safe_errors(self):
        for url in ("", 7, "http://example.test/signed-test", "https://user:pass@example.test/signed-test",
                    "https://example.test:bad/signed-test", "https://example.test/a\nb", "https://example.test/a\x7fb",
                    "https://example.test/a b", "https://example.test\\@downloader.disk.yandex.ru/preview/x"):
            with self.subTest(url=url):
                client = self.client()
                self.metadata(client, {"preview": url})
                with self.assertRaises(ApiError) as caught:
                    client.preview_url("disk:/Photos/one.jpg")
                self.assertNotIn("signed-test", str(caught.exception))
                client._preview_opener.open.assert_not_called()

    def test_invalid_size_does_not_send_request(self):
        client = self.client()
        with self.assertRaises(ValueError):
            client.preview_url("disk:/Photos/one.jpg", "secret-size")
        client._opener.open.assert_not_called()

    def test_private_preview_authorizes_trusted_endpoint_and_closes_response(self):
        client = self.client()
        body = io.BytesIO(b"image")
        client._preview_opener.open.return_value = body
        with client.open_preview(TRUSTED, timeout=7) as response:
            self.assertEqual(response.read(), b"image")
        self.assertTrue(body.closed)
        request = self.requests(client)[0]
        self.assertEqual(request.get_header("Authorization"), "OAuth " + TOKEN)
        self.assertEqual(request.get_header("Accept"), "image/*")
        self.assertEqual(client._preview_opener.open.call_args.kwargs["timeout"], 7)

    def test_oauth_is_not_sent_to_untrusted_or_non_preview_destinations(self):
        urls = (
            "https://signed.example.test/image", "https://downloader.disk.yandex.ru.evil.test/preview/x",
            "https://other.yandex.ru/preview/x", "https://downloader.disk.yandex.ru:444/preview/x",
            "https://downloader.disk.yandex.ru/disk/x", "https://downloader.disk.yandex.ru/preview-evil/x",
        )
        for url in urls:
            with self.subTest(url=url):
                client = self.client()
                client._preview_opener.open.return_value = io.BytesIO(b"image")
                with client.open_preview(url):
                    pass
                self.assertFalse(self.requests(client)[0].has_header("Authorization"))

    def test_same_origin_preview_redirect_keeps_authorization(self):
        client = self.client()
        redirect = self.redirect("/preview/second")
        client._preview_opener.open.side_effect = [redirect, io.BytesIO(b"image")]
        with client.open_preview(TRUSTED):
            pass
        self.assertTrue(redirect.fp.closed)
        self.assertTrue(all(req.get_header("Authorization") == "OAuth " + TOKEN for req in self.requests(client)))

    def test_cross_origin_redirect_strips_oauth_permanently(self):
        client = self.client()
        client._preview_opener.open.side_effect = [
            self.redirect("https://signed.example.test/image"), self.redirect(TRUSTED), io.BytesIO(b"image"),
        ]
        with client.open_preview(TRUSTED):
            pass
        requests = self.requests(client)
        self.assertEqual(requests[0].get_header("Authorization"), "OAuth " + TOKEN)
        self.assertFalse(requests[1].has_header("Authorization"))
        self.assertFalse(requests[2].has_header("Authorization"))

    def test_redirect_leaving_preview_path_does_not_authorize_other_endpoint(self):
        client = self.client()
        client._preview_opener.open.side_effect = [self.redirect("/disk/one"), io.BytesIO(b"image")]
        with client.open_preview(TRUSTED):
            pass
        self.assertFalse(self.requests(client)[1].has_header("Authorization"))

    def test_insecure_malformed_and_control_character_redirects_are_rejected(self):
        for target in ("http://example.test/image", "https://user:pass@example.test/x", "https://example.test:bad/x", "\nhttps://example.test/x", ""):
            with self.subTest(target=target):
                client = self.client()
                redirect = self.redirect(target)
                client._preview_opener.open.side_effect = redirect
                with self.assertRaises(ApiError):
                    with client.open_preview(TRUSTED):
                        self.fail("Invalid redirect opened")
                self.assertTrue(redirect.fp.closed)
                self.assertEqual(client._preview_opener.open.call_count, 1)

    def test_redirect_count_is_bounded(self):
        client = self.client()
        client._preview_opener.open.side_effect = [self.redirect(TRUSTED) for _ in range(7)]
        with self.assertRaises(ApiError):
            with client.open_preview(TRUSTED):
                pass
        self.assertEqual(client._preview_opener.open.call_count, 6)

    def test_open_errors_do_not_leak_signed_links_or_upstream_bodies(self):
        for error in (HTTPError(TRUSTED, 403, TOKEN, {}, io.BytesIO(TOKEN.encode())), URLError(TRUSTED + TOKEN), OSError(TOKEN)):
            with self.subTest(error=type(error).__name__):
                client = self.client()
                client._preview_opener.open.side_effect = error
                with self.assertRaises(ApiError) as caught:
                    with client.open_preview(TRUSTED):
                        pass
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertNotIn(TRUSTED, str(caught.exception))
                if isinstance(error, HTTPError):
                    self.assertEqual(caught.exception.status, 403)
                    self.assertTrue(error.fp.closed)

    def test_response_closes_when_caller_rejects_content(self):
        client = self.client()
        body = io.BytesIO(b"not-an-image")
        client._preview_opener.open.return_value = body
        with self.assertRaises(ValueError):
            with client.open_preview(TRUSTED):
                raise ValueError("content invalid")
        self.assertTrue(body.closed)

    def test_bad_timeout_and_insecure_url_do_not_open_request(self):
        for url, timeout in ((TRUSTED, 0), (TRUSTED, float("nan")), (TRUSTED, "secret"), ("http://example.test/image", 10)):
            client = self.client()
            with self.assertRaises((ValueError, ApiError)):
                with client.open_preview(url, timeout=timeout):
                    pass
            client._preview_opener.open.assert_not_called()

    def test_cancelled_preview_does_not_send_request(self):
        client = self.client()
        client.stop_event = threading.Event()
        client.stop_event.set()
        with self.assertRaises(ApiError):
            with client.open_preview(TRUSTED):
                pass
        client._preview_opener.open.assert_not_called()

    def test_preview_opener_disables_automatic_redirects(self):
        client = YandexDiskAPI(disk_path="/Photos", token=TOKEN)
        self.assertTrue(any(isinstance(handler, _NoMetadataRedirect) for handler in client._preview_opener.handlers))


if __name__ == "__main__":
    unittest.main()
