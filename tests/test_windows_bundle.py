"""Minimal GUI archives contain only their runtime, never account or dev files."""
from pathlib import Path
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

from tools.build_windows_zip import build


COMMON_RUNTIME = {
    "disk_downloader_gui.py", "README.md",
    "yandex_disk/__init__.py", "yandex_disk/api.py", "yandex_disk/batch.py",
    "yandex_disk/credentials.py", "yandex_disk/downloader.py", "yandex_disk/token.py",
    "yandex_disk/ui_clipboard.py", "yandex_disk/ui_theme.py",
    "assets/app-icon-256.png", "assets/app-icon-hero.png",
}
PLATFORM_RUNTIME = {
    "windows": {"run-gui.bat", "launch_windows.py", "bootstrap-python.ps1"},
    "macos": {"run-gui.command", "launch-macos.sh", "bootstrap-python-macos.sh"},
}


class WindowsBundleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.project = self.base / "Исходники & GUI runtime"
        self.project.mkdir()
        self.source = Path(__file__).resolve().parents[1]
        sources = ((COMMON_RUNTIME - {"README.md"})
                   | PLATFORM_RUNTIME["windows"] | PLATFORM_RUNTIME["macos"])
        for filename in sources:
            target = self.project / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.source / filename, target)
        self.guide = b"GUI USER GUIDE: use the launch button; no developer instructions.\n"
        (self.project / "docs").mkdir()
        (self.project / "docs/GUI.md").write_bytes(self.guide)
        (self.project / "README.md").write_bytes(b"DEVELOPER README MUST NOT BE PACKED\n")
        # Poison fixture extras so any accidentally broadened search is caught.
        for filename in (
            "disk_downloader.py", "run.command", "run.bat", "download.json", "download-gui.json", ".env",
            ".venv/bin/python3", "recovered/private-photo.jpg", ".disk-downloader/state.sqlite3",
            ".disk-downloader/events.log", "__pycache__/private.pyc", "yandex_disk/dev_helper.py",
            "assets/pic.jpg", "assets/icon-prompt.txt", "tests/test_private.py",
            "tools/private_tool.py", ".github/workflows/private.yml",
        ):
            path = self.project / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"PRIVATE_FIXTURE_SENTINEL_NOT_REAL_DATA\n")

    def expected(self, platform):
        return {"DiskDownloader/" + name for name in COMMON_RUNTIME | PLATFORM_RUNTIME[platform]}

    def test_each_platform_has_exactly_its_fifteen_runtime_files_and_gui_guide(self):
        for platform in PLATFORM_RUNTIME:
            with self.subTest(platform=platform):
                target = build(self.base / (platform + " build.zip"), platform=platform, project=self.project)
                with ZipFile(target) as archive:
                    self.assertEqual(set(archive.namelist()), self.expected(platform))
                    self.assertEqual(len(archive.infolist()), 15)
                    self.assertEqual(archive.read("DiskDownloader/README.md"), self.guide)
                    self.assertIsNone(archive.testzip())
                    for entry in archive.infolist():
                        self.assertNotIn(b"PRIVATE_FIXTURE_SENTINEL", archive.read(entry))
                        self.assertNotIn(b"DEVELOPER README MUST NOT BE PACKED", archive.read(entry))
                    if platform == "windows":
                        self.assertIn(b"\r\n", archive.read("DiskDownloader/run-gui.bat"))

    def test_default_build_targets_windows_and_has_no_cli_or_development_files(self):
        target = build(self.base / "default.zip", project=self.project)
        with ZipFile(target) as archive:
            self.assertEqual(set(archive.namelist()), self.expected("windows"))
            parts = {part for name in archive.namelist() for part in Path(name).parts}
            excluded = {"disk_downloader.py", "run.bat", "run.command", "tests", "tools", ".github",
                        "docs", ".venv", ".env", "download.json", "download-gui.json", ".disk-downloader",
                        "recovered", "__pycache__"}
            self.assertFalse(excluded.intersection(parts))
            self.assertTrue(all(not name.endswith((".command", ".sh")) for name in archive.namelist()))

    def test_archive_modes_are_normalized_and_macos_shebangs_remain_usable(self):
        for platform in PLATFORM_RUNTIME:
            with self.subTest(platform=platform):
                for filename in PLATFORM_RUNTIME[platform]:
                    (self.project / filename).chmod(0o600)
                target = build(self.base / (platform + " modes.zip"), platform=platform, project=self.project)
                with ZipFile(target) as archive:
                    for entry in archive.infolist():
                        self.assertEqual(entry.create_system, 3)
                        mode = entry.external_attr >> 16
                        self.assertTrue(stat.S_ISREG(mode))
                        expected_mode = 0o755 if Path(entry.filename).suffix in {".command", ".sh"} else 0o644
                        self.assertEqual(stat.S_IMODE(mode), expected_mode, entry.filename)
                        if expected_mode == 0o755:
                            shebang = archive.read(entry).partition(b"\n")[0]
                            self.assertEqual(shebang.split()[0], b"#!/bin/zsh")
                            self.assertNotIn(b"\r", shebang)

    def test_common_command_passes_platform_through_to_the_builder(self):
        builder = self.project / "tools/build_windows_zip.py"
        shutil.copyfile(self.source / "tools/build_windows_zip.py", builder)
        for platform in PLATFORM_RUNTIME:
            with self.subTest(platform=platform):
                target = self.base / (platform + " CLI build.zip")
                result = subprocess.run([sys.executable, str(builder), "--platform", platform, "--output", str(target)],
                                        cwd=self.base, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                with ZipFile(target) as archive:
                    self.assertEqual(set(archive.namelist()), self.expected(platform))
                    self.assertIsNone(archive.testzip())

    def test_incomplete_runtime_is_rejected_before_overwriting_an_archive(self):
        target = self.base / "previous.zip"
        target.write_bytes(b"PREVIOUS_ARCHIVE_FIXTURE")
        (self.project / "yandex_disk/api.py").unlink()
        with self.assertRaises(ValueError):
            build(target, project=self.project)
        self.assertEqual(target.read_bytes(), b"PREVIOUS_ARCHIVE_FIXTURE")

    def test_unknown_platform_is_rejected_without_creating_output(self):
        target = self.base / "unsupported.zip"
        with self.assertRaises(ValueError):
            build(target, platform="unknown", project=self.project)
        self.assertFalse(target.exists())

    def test_extracted_runtime_imports_the_complete_gui_without_cli_or_a_tk_window(self):
        code = (
            "from pathlib import Path; import sys; "
            "runtime = Path(sys.argv[1]); sys.path.insert(0, str(runtime)); "
            "import disk_downloader_gui as gui; import tkinter; "
            "assert Path(gui.__file__).parent == runtime; "
            "assert tkinter._default_root is None; "
            "assert not (runtime / 'disk_downloader.py').exists(); "
            "modules = [m for n, m in sys.modules.items() if n == 'yandex_disk' or n.startswith('yandex_disk.')]; "
            "assert len(modules) == 8; "
            "assert all(Path(m.__file__).is_relative_to(runtime) for m in modules)"
        )
        environment = os.environ.copy()
        environment["YANDEX_DISK_TOKEN"] = ""
        for platform in PLATFORM_RUNTIME:
            with self.subTest(platform=platform):
                target = build(self.base / (platform + " imports.zip"), platform=platform, project=self.project)
                destination = self.base / (platform + " extracted")
                with ZipFile(target) as archive:
                    archive.extractall(destination)
                runtime = destination / "DiskDownloader"
                result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", code, str(runtime)],
                                        cwd=destination, env=environment, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
