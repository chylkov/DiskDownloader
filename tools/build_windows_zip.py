"""Export a minimal GUI runtime for Windows or macOS without account data."""
from pathlib import Path
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED
import argparse


PROJECT = Path(__file__).resolve().parents[1]
COMMON_FILES = (
    "disk_downloader_gui.py",
    "yandex_disk/__init__.py", "yandex_disk/api.py", "yandex_disk/batch.py",
    "yandex_disk/credentials.py", "yandex_disk/downloader.py", "yandex_disk/token.py",
    "yandex_disk/ui_clipboard.py", "yandex_disk/ui_theme.py",
    "assets/app-icon-256.png", "assets/app-icon-hero.png",
)
PLATFORM_FILES = {
    "windows": ("run-gui.bat", "launch_windows.py", "bootstrap-python.ps1"),
    "macos": ("run-gui.command", "launch-macos.sh", "bootstrap-python-macos.sh"),
}
GUIDE_SOURCE = "docs/GUI.md"


def build(destination: Path, *, platform: str = "windows", project: Path = PROJECT) -> Path:
    if platform not in PLATFORM_FILES:
        raise ValueError("Поддерживаются платформы windows и macos.")
    project = Path(project)
    files = [(project / name, name) for name in COMMON_FILES + PLATFORM_FILES[platform]]
    files.append((project / GUIDE_SOURCE, "README.md"))
    # Validate before creating anything; never silently export an incomplete app.
    if any(not path.is_file() or path.is_symlink() for path, _name in files):
        raise ValueError("Не хватает файлов приложения или найден файл-ссылка.")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
        for path, archive_name in files:
            entry = ZipInfo.from_file(path, "DiskDownloader/" + archive_name)
            # Preserve double-click launchers on macOS even if the archive is
            # assembled on Windows, where chmod/executable bits are unavailable.
            entry.create_system = 3
            entry.external_attr = (0o100755 if path.suffix in {".command", ".sh", ".zsh"} else 0o100644) << 16
            archive.writestr(entry, path.read_bytes(), compress_type=ZIP_DEFLATED)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("windows", "macos"), default="windows")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    name = "DiskDownloader-macOS.zip" if arguments.platform == "macos" else "DiskDownloader-Windows.zip"
    print(build(arguments.output or PROJECT / "dist" / name, platform=arguments.platform))
