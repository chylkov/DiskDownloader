"""Exercise urllib's real streaming behavior against a local HTTP fixture."""
import hashlib
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from yandex_disk.api import Resource
from yandex_disk.downloader import Downloader


class TransportTests(unittest.TestCase):
    def test_real_urllib_disconnect_then_resume_without_oauth(self):
        payload = b"recovery-data-" * 10000
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(dict(self.headers))
                offset = int(self.headers.get("Range", "bytes=0-")[6:-1])
                self.send_response(206 if offset else 200)
                self.send_header("Content-Length", str(len(payload) - offset))
                if offset:
                    self.send_header("Content-Range", f"bytes {offset}-{len(payload)-1}/{len(payload)}")
                self.send_header("Connection", "close")
                self.end_headers()
                # First response breaks while announcing the complete file size.
                content = payload[offset:] if len(requests) > 1 else payload[:4321]
                self.wfile.write(content)
                self.wfile.flush()
                self.close_connection = True

            def log_message(self, *args):
                pass

        resource = Resource("file.bin", "disk:/Archive/file.bin", "file", len(payload),
                            sha256=hashlib.sha256(payload).hexdigest())

        class API:
            root_path = "disk:/Archive"
            source_id = "transport-fixture"

            def get_resource(self, path):
                return Resource("Archive", self.root_path, "dir")

            def iter_children(self, path):
                yield resource

            def download_url(self, path):
                return "https://download.example/file?secret=signed-link"

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            def local_opener(request, *, timeout):
                # Only the fixture maps an HTTPS provider link to loopback HTTP.
                forwarded = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/file", headers=dict(request.header_items()))
                return urllib.request.urlopen(forwarded, timeout=timeout)

            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory)
                first = Downloader(API(), output, opener=local_opener, retries=0, log=lambda _: None).run()
                self.assertEqual(len(first.errors), 1)
                self.assertFalse((output / "file.bin").exists())
                partial = next((output / ".disk-downloader/partials").glob("*.part"))
                self.assertEqual(partial.read_bytes(), payload[:4321])
                second = Downloader(API(), output, opener=local_opener, retries=0, log=lambda _: None).run()
                self.assertTrue(second.complete)
                self.assertEqual((output / "file.bin").read_bytes(), payload)
                self.assertEqual(requests[1]["Range"], "bytes=4321-")
                self.assertTrue(all("Authorization" not in request for request in requests))
                self.assertTrue(all(request.get("Accept-Encoding") == "identity" for request in requests))
                self.assertNotIn("signed-link", (output / ".disk-downloader/events.log").read_text())
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
