"""Native zsh launcher scenarios with fake Python, Tk, SSL, and bootstrap.

The real downloader, certificates, installers, account, and Apple Python stub are
never executed. Candidate enumeration and the managed path are fixture-scoped.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
import unittest


DRIVER = r'''
import json, os, pathlib, re, sys
import ssl as real_ssl
from types import SimpleNamespace

bootstrap = sys.argv[1] == "--bootstrap"
settings_path = pathlib.Path(sys.argv[2] if bootstrap else sys.argv[1])
settings = json.loads(settings_path.read_text(encoding="utf-8"))
arguments = sys.argv[3:]
invocation = None if bootstrap else sys.argv[2]

def record(kind, **values):
    event = {"kind": kind, "name": settings.get("name"), **values}
    with open(settings["events"], "a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")

if bootstrap:
    record("bootstrap", args=arguments)
    if settings["exit"] == 0 and settings["install"]:
        target = pathlib.Path(settings["managed_settings"])
        managed = json.loads(target.read_text(encoding="utf-8"))
        managed.update(python_version=[3, 14, 8], tk_version="8.6.17", ca_count=1, probe_exit=None)
        target.write_text(json.dumps(managed), encoding="utf-8")
    raise SystemExit(settings["exit"])

if "-c" not in arguments:
    record("app", executable=invocation, script=arguments[2], args=arguments[3:], cwd=os.getcwd())
    raise SystemExit(settings["app_exit"])

mode = arguments[-1]
record("probe", mode=mode)
if settings.get("probe_exit") is not None:
    raise SystemExit(settings["probe_exit"])

class CertificateContext:
    def __init__(self):
        self.context = real_ssl.SSLContext(real_ssl.PROTOCOL_TLS_CLIENT)
    def cert_store_stats(self):
        stats = self.context.cert_store_stats()
        stats["x509_ca"] += settings["ca_count"]
        return stats
    def load_verify_locations(self, *, cafile):
        self.context.load_verify_locations(cafile=cafile)

sys.modules["ssl"] = SimpleNamespace(
    create_default_context=CertificateContext,
    get_default_verify_paths=lambda: SimpleNamespace(capath=settings.get("capath")),
    SSLError=real_ssl.SSLError,
)

class FakeWindow:
    def __init__(self):
        record("tk")
        if settings.get("tk_broken"):
            raise RuntimeError("FAKE TK ERROR")
        self.tk = self
    def withdraw(self): pass
    def update_idletasks(self): pass
    def destroy(self): pass
    def call(self, *args):
        if args == ("package", "provide", "Tk"):
            return settings["tk_version"]
        if args == ("info", "patchlevel"):
            return settings["tcl_version"]
        if args[:2] == ("package", "vcompare"):
            versions = [tuple(int(item) for item in value.split(".")) for value in args[2:]]
            return (versions[0] > versions[1]) - (versions[0] < versions[1])
        raise ValueError("unexpected fake Tcl command")

sys.modules["tkinter"] = None if settings.get("tk_missing") else SimpleNamespace(Tk=FakeWindow)
sys.version_info = tuple(settings["python_version"])
code = arguments[arguments.index("-c") + 1]
sys.argv = [invocation, mode]
try:
    exec(compile(code, "<launcher-probe>", "exec"), {"__name__": "__main__"})
except SystemExit:
    raise
except Exception:
    raise SystemExit(1)
'''


@unittest.skipUnless(sys.platform == "darwin" and Path("/bin/zsh").is_file(), "requires macOS zsh")
class MacOSLauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.project = self.base / "Диск & Архив! 100% (копия)"
        self.project.mkdir()
        self.source = Path(__file__).resolve().parent.parent
        self.helper = self.project / "launch-macos.sh"
        shutil.copyfile(self.source / "launch-macos.sh", self.helper)
        for script in ("disk_downloader.py", "disk_downloader_gui.py"):
            (self.project / script).write_text("# fixture only, never executed\n", encoding="utf-8")
        self.events_path = self.base / "events.jsonl"
        self.driver = self.base / "fake interpreter.py"
        self.driver.write_text(DRIVER, encoding="utf-8")
        self.zdotdir = self.base / "empty zsh configuration"
        self.zdotdir.mkdir()
        self.environment = os.environ.copy()
        self.environment.update(ZDOTDIR=str(self.zdotdir), YANDEX_DISK_TOKEN="")
        for key in ("PYTHONHOME", "PYTHONPATH", "TCL_LIBRARY", "TK_LIBRARY"):
            self.environment.pop(key, None)
        self.managed, self.managed_settings = self.interpreter("managed", probe_exit=1)
        self.bootstrap(exit_code=17)

    def interpreter(self, name, *, executable=None, **values):
        executable = executable or self.base / (name + " & интерпретатор") / "python3"
        executable.parent.mkdir(parents=True, exist_ok=True)
        settings_path = executable.parent / (name + " settings.json")
        settings = {
            "name": name, "events": str(self.events_path), "python_version": [3, 12, 0],
            "tk_version": "8.6.14", "tcl_version": "8.6.14", "ca_count": 1,
            "app_exit": 0, "probe_exit": None, **values,
        }
        settings_path.write_text(json.dumps(settings), encoding="utf-8")
        executable.write_text(
            "#!/bin/zsh\nexec " + " ".join(shlex.quote(value) for value in (
                sys.executable, "-I", "-X", "utf8", str(self.driver), str(settings_path))) + ' "$0" "$@"\n',
            encoding="utf-8",
        )
        executable.chmod(0o755)
        return executable, settings_path

    def bootstrap(self, *, exit_code=0, install=True):
        settings = self.base / "bootstrap settings.json"
        settings.write_text(json.dumps({"events": str(self.events_path), "exit": exit_code,
                                         "install": install, "managed_settings": str(self.managed_settings)}), encoding="utf-8")
        script = self.project / "bootstrap-python-macos.sh"
        script.write_text(
            "#!/bin/zsh\nexec " + " ".join(shlex.quote(value) for value in (
                sys.executable, "-I", "-X", "utf8", str(self.driver), "--bootstrap", str(settings))) + ' "$@"\n',
            encoding="utf-8",
        )

    def events(self, kind=None):
        rows = ([json.loads(line) for line in self.events_path.read_text(encoding="utf-8").splitlines()]
                if self.events_path.exists() else [])
        return [row for row in rows if kind is None or row["kind"] == kind]

    def run_launcher(self, candidates, *, mode="gui", arguments=()):
        harness = self.base / "launch harness.zsh"
        harness.write_text(
            "source " + shlex.quote(str(self.helper)) + "\n"
            "disk_macos_candidates() { reply=(" + " ".join(shlex.quote(str(item)) for item in candidates) + ") }\n"
            "disk_macos_managed_python() { print -r -- " + shlex.quote(str(self.managed)) + "; }\n"
            'disk_macos_launch "$@"\nexit $?\n', encoding="utf-8")
        return subprocess.run(["/bin/zsh", "-f", str(harness), mode, str(self.project), *arguments],
                              cwd=self.base, env=self.environment, input="\n", capture_output=True,
                              text=True, encoding="utf-8", timeout=15)

    def test_valid_venv_symlink_keeps_its_invocation_path_and_literal_app_arguments(self):
        target, _ = self.interpreter("base")
        alias = self.project / ".venv" / "bin" / "python3"
        alias.parent.mkdir(parents=True)
        alias.symlink_to(target)
        unused, _ = self.interpreter("unused")
        arguments = ["--output", 'Копия & $(touch SHOULD_NOT_EXIST) ! 100% "кавычки"', "--disk-path", "/Фото"]
        result = self.run_launcher([alias, unused], arguments=arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        app = self.events("app")[0]
        self.assertEqual(app["executable"], str(alias))
        self.assertEqual(app["args"], arguments)
        self.assertEqual(app["cwd"], str(self.project))
        self.assertFalse((self.project / "SHOULD_NOT_EXIST").exists())
        self.assertEqual([row["name"] for row in self.events("probe")], ["base"])
        self.assertEqual(self.events("bootstrap"), [])

    def test_old_or_broken_venv_falls_back_without_changing_it(self):
        for values in ({"python_version": [3, 9, 20]}, {"tk_broken": True}):
            with self.subTest(values=values):
                old, settings = self.interpreter("old", executable=self.project / ".venv" / "bin" / "python3", **values)
                old_bytes, settings_bytes = old.read_bytes(), settings.read_bytes()
                good, _ = self.interpreter("good")
                result = self.run_launcher([old, good])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(old.read_bytes(), old_bytes)
                self.assertEqual(settings.read_bytes(), settings_bytes)
                self.assertEqual(self.events("app")[-1]["name"], "good")

    def test_gui_rejects_old_tk_even_with_newer_tcl_and_accepts_tk_9(self):
        for version in ("8.6.13", "9.0.2"):
            with self.subTest(version=version):
                old, _ = self.interpreter("old Tk", tk_version="8.6.12", tcl_version="8.6.17")
                good, _ = self.interpreter("new Tk " + version, tk_version=version, tcl_version="8.6.12")
                result = self.run_launcher([old, good])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.events("app")[-1]["name"], "new Tk " + version)

    def test_cli_needs_no_tk(self):
        python, _ = self.interpreter("no Tk", tk_missing=True)
        result = self.run_launcher([python], mode="cli")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events("tk"), [])

    def test_empty_certificate_store_is_rejected_before_the_app_starts(self):
        bad, _ = self.interpreter("no CA", ca_count=0)
        good, _ = self.interpreter("valid CA")
        result = self.run_launcher([bad, good], mode="cli")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events("app")[0]["name"], "valid CA")

    def test_empty_lazy_openssl_certificate_directory_is_rejected(self):
        directory = self.base / "CA directory"
        directory.mkdir()
        python, _ = self.interpreter("lazy CA", ca_count=0, capath=str(directory))
        good, _ = self.interpreter("valid CA")
        result = self.run_launcher([python, good], mode="cli")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events("app")[0]["name"], "valid CA")
        self.assertEqual(self.events("bootstrap"), [])

    def public_ca_pem(self):
        # Export one public trust anchor already present on the test machine;
        # certificates contain no private key, and SSL performs no networking.
        certificates = ssl.create_default_context().get_ca_certs(binary_form=True)
        if not certificates:
            self.skipTest("test Python has no public CA available for an offline fixture")
        return ssl.DER_cert_to_PEM_cert(certificates[0])

    def test_invalid_hashed_certificate_file_is_rejected(self):
        directory = self.base / "invalid CA directory"
        directory.mkdir()
        (directory / "1234abcd.0").write_text("not a PEM certificate", encoding="ascii")
        bad, _ = self.interpreter("invalid lazy CA", ca_count=0, capath=str(directory))
        good, _ = self.interpreter("valid CA")
        result = self.run_launcher([bad, good], mode="cli")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events("app")[0]["name"], "valid CA")

    def test_unhashed_certificate_does_not_make_a_lazy_directory_usable(self):
        directory = self.base / "unhashed CA directory"
        directory.mkdir()
        (directory / "certificate.pem").write_text(self.public_ca_pem(), encoding="ascii")
        bad, _ = self.interpreter("unhashed lazy CA", ca_count=0, capath=str(directory))
        good, _ = self.interpreter("valid CA")
        result = self.run_launcher([bad, good], mode="cli")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events("app")[0]["name"], "valid CA")

    def test_real_valid_hashed_ca_is_loaded_before_accepting_a_lazy_directory(self):
        directory = self.base / "valid CA directory"
        directory.mkdir()
        (directory / "1234ABcd.0").write_text(self.public_ca_pem(), encoding="ascii")
        python, _ = self.interpreter("real lazy CA", ca_count=0, capath=str(directory))
        result = self.run_launcher([python], mode="cli")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events("app")[0]["name"], "real lazy CA")
        self.assertEqual(self.events("bootstrap"), [])

    def test_bootstrap_runs_once_without_app_arguments_then_checks_the_result(self):
        self.bootstrap()
        invalid, _ = self.interpreter("invalid", tk_version="8.6.12")
        arguments = ["--output", "Копия & Архив!", "--disk-path", "/Фото"]
        result = self.run_launcher([invalid], arguments=arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.events("bootstrap")), 1)
        self.assertEqual(self.events("bootstrap")[0]["args"], [])
        self.assertEqual(self.events("app")[0]["name"], "managed")
        self.assertEqual(self.events("app")[0]["args"], arguments)
        self.assertEqual([row["name"] for row in self.events("probe")], ["invalid", "managed"])

    def test_declined_or_failed_bootstrap_preserves_status_and_does_not_start_app(self):
        for status in (2, 19, 130):
            with self.subTest(status=status):
                self.bootstrap(exit_code=status)
                result = self.run_launcher([])
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual(self.events("app"), [])

    def test_failed_post_install_probe_does_not_restart_bootstrap(self):
        self.bootstrap(install=False)
        result = self.run_launcher([])
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(self.events("bootstrap")), 1)
        self.assertEqual(self.events("app"), [])

    def test_application_failure_does_not_trigger_installation_or_another_runtime(self):
        python, _ = self.interpreter("app failure", app_exit=23)
        unused, _ = self.interpreter("unused")
        result = self.run_launcher([python, unused])
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(self.events("bootstrap"), [])
        self.assertEqual([row["name"] for row in self.events("probe")], ["app failure"])

    def test_interrupting_a_probe_does_not_trigger_installation(self):
        python, _ = self.interpreter("interrupted", probe_exit=130)
        result = self.run_launcher([python])
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual(self.events("bootstrap"), [])

    def test_missing_app_is_reported_before_runtime_probes_or_bootstrap(self):
        (self.project / "disk_downloader_gui.py").unlink()
        result = self.run_launcher([])
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("целиком", result.stderr)
        self.assertEqual(self.events(), [])

    def test_sourcing_helper_does_not_launch_or_install_anything(self):
        result = subprocess.run(["/bin/zsh", "-f", "-c", "source " + shlex.quote(str(self.helper)) + "; print READY"],
                                env=self.environment, capture_output=True, text=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout), (0, "READY\n"), result.stderr)
        self.assertEqual(self.events(), [])

    def test_executed_helper_rejects_missing_or_unknown_mode_before_runtime_search(self):
        for arguments in ([], ["PRIVATE_UNKNOWN_MODE"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run(["/bin/zsh", "-f", str(self.helper), *arguments],
                                        env=self.environment, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertNotIn("PRIVATE", result.stderr)
                self.assertEqual(self.events(), [])

    def test_system_stub_and_multiple_symlink_aliases_are_recognized_without_execution(self):
        alias = self.base / "system alias"
        alias.symlink_to("/usr/bin/python3")
        second = self.base / "second alias"
        second.symlink_to(alias)
        expression = "source " + shlex.quote(str(self.helper)) + '; disk_macos_is_system_stub "$1"'
        for candidate in ("/usr/bin/python3", str(alias), str(second)):
            result = subprocess.run(["/bin/zsh", "-f", "-c", expression, "fixture", candidate],
                                    env=self.environment, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(), [])

    def test_both_wrappers_forward_mode_arguments_working_directory_and_exit_status(self):
        recorder = self.base / "wrapper recorder.py"
        observed = self.base / "wrapper result.json"
        recorder.write_text(
            "import json, os, pathlib, sys\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({'args': sys.argv[2:], 'cwd': os.getcwd()}), encoding='utf-8')\n"
            "raise SystemExit(7)\n", encoding="utf-8")
        # Separate fixture for wrapper behavior: no candidate discovery is run.
        self.helper.write_text("#!/bin/zsh\nexec " + " ".join(shlex.quote(value) for value in (
            sys.executable, "-I", str(recorder), str(observed))) + ' "$@"\n', encoding="utf-8")
        arguments = ["--output", 'Копия & Архив! $(touch SHOULD_NOT_EXIST) "кавычки"']
        for filename, mode in (("run-gui.command", "gui"), ("run.command", "cli")):
            shutil.copyfile(self.source / filename, self.project / filename)
            result = subprocess.run(["/bin/zsh", "-f", str(self.project / filename), *arguments],
                                    cwd=self.base, env=self.environment, input="\n", capture_output=True,
                                    text=True, encoding="utf-8", timeout=5)
            self.assertEqual(result.returncode, 7, result.stderr)
            self.assertEqual(json.loads(observed.read_text(encoding="utf-8")),
                             {"args": [mode, *arguments], "cwd": str(self.project)})
            self.assertFalse((self.project / "SHOULD_NOT_EXIST").exists())


if __name__ == "__main__":
    unittest.main()
