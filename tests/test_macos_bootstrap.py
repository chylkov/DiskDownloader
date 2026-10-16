"""Native shell bootstrap flows with fixture packages; never install Python."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
BOOTSTRAP = PROJECT / "bootstrap-python-macos.sh"


@unittest.skipUnless(sys.platform == "darwin", "Requires native macOS shell tools")
class MacBootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="Диск bootstrap & ")
        self.addCleanup(temporary.cleanup)
        self.fixture = Path(temporary.name)
        self.events = self.fixture / "events"

    def shell(self, body, *, input="", **variables):
        environment = os.environ.copy()
        environment.update(TEST_ROOT=str(self.fixture), TEST_EVENTS=str(self.events),
                           TMPDIR=str(self.fixture) + "/")
        environment.update({key: str(value) for key, value in variables.items()})
        return subprocess.run(
            ["/bin/zsh", "-f", "-c", 'source "$1"\nshift\n' + body,
             "bootstrap-fixture", str(BOOTSTRAP)],
            cwd=self.fixture, env=environment, input=input, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )

    def scenario(self, **variables):
        return self.shell(r'''
typeset -gi runtime_calls=0 conflict_calls=0
disk_bootstrap_supported() { return ${TEST_SUPPORTED:-0}; }
disk_bootstrap_runtime_ok() {
  (( runtime_calls += 1 ))
  if (( runtime_calls == 1 )); then return ${TEST_INITIAL_RUNTIME:-1}; fi
  print -r -- postcheck >> "$TEST_EVENTS"
  return ${TEST_POST_RUNTIME:-0}
}
disk_bootstrap_conflict() {
  (( conflict_calls += 1 ))
  if (( conflict_calls == 1 )); then return ${TEST_CONFLICT:-1}; fi
  return ${TEST_LATE_CONFLICT:-1}
}
disk_bootstrap_consent() {
  print -r -- consent >> "$TEST_EVENTS"
  return ${TEST_CONSENT:-0}
}
disk_bootstrap_download() {
  print -r -- download >> "$TEST_EVENTS"
  print -r -- fixture > "$1"
  return ${TEST_DOWNLOAD:-0}
}
disk_bootstrap_verify() {
  print -r -- verify >> "$TEST_EVENTS"
  return ${TEST_VERIFY:-0}
}
disk_bootstrap_install() {
  print -r -- install >> "$TEST_EVENTS"
  [[ -f "$1" && -f "$2" ]] || return 99
  return ${TEST_INSTALL:-0}
}
disk_bootstrap_certificates() {
  print -r -- certificates >> "$TEST_EVENTS"
  return ${TEST_CERTIFICATES:-0}
}
disk_bootstrap_main
bootstrap_result=$?
exit "$bootstrap_result"
''', **variables)

    def operations(self):
        return self.events.read_text().splitlines() if self.events.exists() else []

    def assert_clean(self):
        self.assertEqual(list(self.fixture.glob("disk-downloader-python.*")), [])

    def test_sourcing_never_downloads_or_prompts(self):
        result = self.shell("print -r -- sourced")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "sourced\n")
        self.assertEqual(list(self.fixture.iterdir()), [])

    def test_existing_runtime_has_no_installation_side_effects(self):
        result = self.scenario(TEST_INITIAL_RUNTIME=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.operations(), [])

    def test_unsupported_os_and_existing_installation_prevent_download(self):
        for options in ({"TEST_SUPPORTED": 1}, {"TEST_CONFLICT": 0}):
            with self.subTest(options=options):
                result = self.scenario(**options)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(self.operations(), [])

    def test_declined_installation_never_downloads(self):
        result = self.scenario(TEST_CONSENT=1)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.operations(), ["consent"])
        self.assert_clean()

    def test_installation_checks_integrity_then_certificates_and_runtime(self):
        result = self.scenario()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.operations(), ["consent", "download", "verify", "install", "certificates", "postcheck"])
        self.assert_clean()

    def test_failures_stop_later_steps_and_always_remove_own_download(self):
        cases = (
            ("TEST_DOWNLOAD", ["consent", "download"]),
            ("TEST_VERIFY", ["consent", "download", "verify"]),
            ("TEST_INSTALL", ["consent", "download", "verify", "install"]),
            ("TEST_CERTIFICATES", ["consent", "download", "verify", "install", "certificates"]),
            ("TEST_POST_RUNTIME", ["consent", "download", "verify", "install", "certificates", "postcheck"]),
        )
        for option, expected in cases:
            with self.subTest(option=option):
                self.events.unlink(missing_ok=True)
                result = self.scenario(**{option: 1})
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(self.operations(), expected)
                self.assert_clean()

    def test_installation_appearing_during_download_is_protected(self):
        result = self.scenario(TEST_LATE_CONFLICT=0)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.operations(), ["consent", "download", "verify"])
        self.assert_clean()

    def test_consent_accepts_yes_and_defaults_to_no(self):
        for answer in ("Y", "y", "yes", "Д", "да"):
            with self.subTest(answer=answer):
                result = self.shell("disk_bootstrap_consent", input=answer + "\n")
                self.assertEqual(result.returncode, 0, result.stderr)
        for answer in ("", "N", "unexpected"):
            with self.subTest(answer=answer):
                result = self.shell("disk_bootstrap_consent", input=answer + "\n")
                self.assertNotEqual(result.returncode, 0)

    def test_existing_framework_and_dangling_application_link_are_protected(self):
        body = r'''
disk_bootstrap_python_path() { print -r -- "$TEST_ROOT/framework/bin/python3"; }
disk_bootstrap_applications_path() { print -r -- "$TEST_ROOT/applications"; }
disk_bootstrap_conflict
'''
        self.assertNotEqual(self.shell(body).returncode, 0)
        (self.fixture / "framework").mkdir()
        self.assertEqual(self.shell(body).returncode, 0)
        (self.fixture / "framework").rmdir()
        (self.fixture / "applications").symlink_to(self.fixture / "missing")
        self.assertEqual(self.shell(body).returncode, 0)

    def test_runtime_with_no_ca_is_rejected_even_when_certificate_directory_exists(self):
        driver = self.fixture / "runtime driver.py"
        driver.write_text(
            'import sys\nfrom types import SimpleNamespace\n'
            'sys.modules["ssl"] = SimpleNamespace(\n'
            '  create_default_context=lambda: SimpleNamespace(cert_store_stats=lambda: {"x509_ca": 0}),\n'
            '  get_default_verify_paths=lambda: SimpleNamespace(capath=sys.argv[1]))\n'
            'code = sys.argv[sys.argv.index("-c") + 1]\n'
            'exec(compile(code, "<bootstrap-runtime-probe>", "exec"))\n', encoding="utf-8")
        runtime = self.fixture / "fake Python"
        runtime.write_text("#!/bin/zsh\nexec " + " ".join(shlex.quote(item) for item in (
            sys.executable, "-I", str(driver), str(self.fixture))) + ' "$@"\n', encoding="utf-8")
        runtime.chmod(0o755)
        result = self.shell(r'''
disk_bootstrap_python_path() { print -r -- "$TEST_ROOT/fake Python"; }
disk_bootstrap_runtime_ok
''')
        self.assertNotEqual(result.returncode, 0)

    def test_real_hash_and_exact_publisher_signature_are_required(self):
        package = self.fixture / "test & 100% !.pkg"
        package.write_bytes(b"Harmless fixture, never installed")
        expected = hashlib.sha256(package.read_bytes()).hexdigest()
        body = r'''
disk_bootstrap_expected_hash() { print -r -- "$TEST_HASH"; }
disk_bootstrap_signature() {
  print -r -- signature >> "$TEST_EVENTS"
  print -r -- "$TEST_SIGNATURE"
  return ${TEST_SIGNATURE_EXIT:-0}
}
disk_bootstrap_gatekeeper() {
  print -r -- gatekeeper >> "$TEST_EVENTS"
  return ${TEST_GATEKEEPER:-0}
}
disk_bootstrap_verify "$TEST_ROOT/test & 100% !.pkg"
'''
        valid = "  1. Developer ID Installer: Python Software Foundation (BMM5U3QVKW)"
        cases = (
            ({"TEST_HASH": "0" * 64}, False, []),
            ({"TEST_SIGNATURE": valid + " Evil"}, False, ["signature"]),
            ({"TEST_SIGNATURE": valid.replace("BMM5U3QVKW", "WRONGTEAM")}, False, ["signature"]),
            ({"TEST_SIGNATURE_EXIT": 1}, False, ["signature"]),
            ({"TEST_GATEKEEPER": 1}, False, ["signature", "gatekeeper"]),
            ({}, True, ["signature", "gatekeeper"]),
        )
        for options, valid_package, events in cases:
            with self.subTest(options=options):
                self.events.unlink(missing_ok=True)
                result = self.shell(body, **{"TEST_HASH": expected, "TEST_SIGNATURE": valid, **options})
                self.assertEqual(result.returncode == 0, valid_package, result.stderr)
                self.assertEqual(self.operations(), events)

    def test_installer_choices_preserve_shell_profiles_and_unix_commands(self):
        target = self.fixture / "choices.plist"
        result = self.shell('disk_bootstrap_choices "$TEST_ROOT/choices.plist"')
        self.assertEqual(result.returncode, 0, result.stderr)
        choices = plistlib.loads(target.read_bytes())
        self.assertEqual({item["choiceIdentifier"] for item in choices}, {
            "org.python.Python.PythonUnixTools-3.14",
            "org.python.Python.PythonProfileChanges-3.14",
        })
        self.assertTrue(all(item["choiceAttribute"] == "selected" and item["attributeSetting"] == 0 for item in choices))

    def test_native_authorization_script_compiles_without_running(self):
        source = BOOTSTRAP.read_text()
        apple_script = source.split("<<'APPLESCRIPT'\n", 1)[1].split("\nAPPLESCRIPT", 1)[0]
        result = subprocess.run(
            ["/usr/bin/osacompile", "-o", str(self.fixture / "test.scpt"), "-"],
            input=apple_script, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
