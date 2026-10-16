"""Windows path rules and Tcl discovery, without requiring a Windows host."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import disk_downloader_gui as gui
import yandex_disk.downloader as recovery
from test_downloader import FakeAPI, FakeOpener


class WindowsNameTests(unittest.TestCase):
    def test_device_names_streams_and_aliases_are_rejected(self):
        invalid = ["CON", "nul.txt", "COM1.jpg", "LPT9", "COM¹", "LPT².txt", "COM³ .jpg",
                   "CONIN$", "CONOUT$.png", "photo.jpg:secret", "photo.", "photo ", "a?b.jpg"]
        for name in invalid:
            with self.subTest(name=name), patch.object(recovery.os, "name", "nt"):
                with self.assertRaises(recovery.RecoveryError):
                    recovery.valid_name(name)

    def test_regular_unicode_names_are_preserved(self):
        for name in ["Фото 2026.jpg", "COM10.jpg", "Курсы", "preview.jpg", "com¹-photo.jpg"]:
            with self.subTest(name=name), patch.object(recovery.os, "name", "nt"):
                self.assertEqual(recovery.valid_name(name), name)


class WindowsJunctionTests(unittest.TestCase):
    def test_junction_is_a_redirect_even_when_it_is_not_a_symlink(self):
        path = Mock()
        path.is_symlink.return_value = False
        path.lstat.return_value = SimpleNamespace(st_reparse_tag=0xA0000003)
        with patch.object(recovery.os, "name", "nt"):
            self.assertTrue(recovery.redirected_path(path))
        path.lstat.return_value = SimpleNamespace(st_reparse_tag=0x9000001A)
        with patch.object(recovery.os, "name", "nt"):
            self.assertFalse(recovery.redirected_path(path))

    def test_missing_path_is_not_a_junction(self):
        path = Mock()
        path.is_symlink.return_value = False
        path.lstat.side_effect = FileNotFoundError()
        with patch.object(recovery.os, "name", "nt"):
            self.assertFalse(recovery.redirected_path(path))

    @unittest.skipUnless(sys.platform == "win32", "requires a real Windows filesystem")
    def test_real_junction_cannot_redirect_downloads_outside_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            output, outside = base / "copy", base / "outside"
            output.mkdir()
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_bytes(b"keep original")
            junction = output / "redirect"
            result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                                    capture_output=True)
            if result.returncode:
                self.skipTest("Windows environment could not create a temporary junction")
            try:
                api = FakeAPI({"redirect/sentinel.txt": b"wrong", "good.txt": b"good"})
                report = recovery.Downloader(api, output, opener=FakeOpener(api), retries=0, log=lambda _: None).run()
                self.assertFalse(report.complete)
                self.assertEqual(sentinel.read_bytes(), b"keep original")
                self.assertEqual((output / "good.txt").read_bytes(), b"good")
            finally:
                junction.rmdir()


class WindowsTclDiscoveryTests(unittest.TestCase):
    def test_tcl_layout_of_official_windows_python_is_found(self):
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            for folder, marker in [("tcl8.6", "init.tcl"), ("tk8.6", "tk.tcl")]:
                directory = prefix / "tcl" / folder
                directory.mkdir(parents=True)
                (directory / marker).touch()
            with patch.dict(os.environ, {}, clear=True), patch.object(gui.sys, "prefix", str(prefix)), \
                    patch.object(gui.sys, "base_prefix", str(prefix)):
                gui.configure_tk_runtime()
                self.assertEqual(os.environ["TCL_LIBRARY"], str(prefix / "tcl/tcl8.6"))
                self.assertEqual(os.environ["TK_LIBRARY"], str(prefix / "tcl/tk8.6"))
