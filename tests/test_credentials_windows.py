"""WinCred ABI and fake-native lifecycle tests; never touch user credentials."""

from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from yandex_disk.credentials import (
    CredentialError, SERVICE, TokenStore, _BOOL, _CRED_MAX_BLOB_SIZE,
    _CRED_PERSIST_LOCAL_MACHINE, _CRED_TYPE_GENERIC, _DWORD,
    _WinCredential, _WinCredentialAttribute, _WinCredentialPointer,
    _WinCredAPI, _WinFileTime, _WindowsCredentials,
)


TOKEN = "dummy-windows-oauth-token-123456789"


class FakeWinCred:
    """Exercise real ctypes pointers and structs, with an in-memory fake DLL."""

    def __init__(self):
        self.values = {}
        self.error = 0
        self.fail_status = None
        self.read_mutator = None
        self.empty_read = False
        self.allocations = {}
        self.freed = []
        self.written = []
        self.write_structs = []
        self.reads = []
        self.deletes = []
        self.library = Mock()
        self.library.CredReadW = Mock(side_effect=self.read)
        self.library.CredWriteW = Mock(side_effect=self.write)
        self.library.CredDeleteW = Mock(side_effect=self.delete)
        self.library.CredFree = Mock(side_effect=self.free)
        self.native = _WinCredAPI(_library=self.library, _last_error=lambda: self.error)

    def read(self, target, kind, flags, output):
        self.reads.append((target, kind, flags))
        if self.fail_status is not None:
            self.error = self.fail_status
            return 0
        if target not in self.values:
            self.error = 1168
            return 0
        if self.empty_read:
            return 1
        data = self.values[target]
        buffer = (ctypes.c_ubyte * max(1, len(data)))()
        if data:
            ctypes.memmove(buffer, data, len(data))
        credential = _WinCredential()
        credential.TargetName = target
        credential.Type = _CRED_TYPE_GENERIC
        credential.CredentialBlobSize = len(data)
        credential.CredentialBlob = buffer
        if self.read_mutator:
            self.read_mutator(credential)
        pointer = ctypes.pointer(credential)
        self.allocations[ctypes.addressof(credential)] = (credential, buffer)
        ctypes.cast(output, ctypes.POINTER(_WinCredentialPointer))[0] = pointer
        return 1

    def write(self, pointer, flags):
        credential = ctypes.cast(pointer, _WinCredentialPointer).contents
        self.written.append({
            "target": credential.TargetName, "type": credential.Type,
            "user": credential.UserName, "persist": credential.Persist,
            "flags": credential.Flags, "call_flags": flags,
            "blob_size": credential.CredentialBlobSize,
            "blob": ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize),
            "attributes": credential.AttributeCount, "has_attributes": bool(credential.Attributes),
            "alias": credential.TargetAlias,
        })
        # Keep the original ctypes object alive to inspect buffer clearing.
        self.write_structs.append(pointer._obj)
        if self.fail_status is not None:
            self.error = self.fail_status
            return 0
        self.values[credential.TargetName] = self.written[-1]["blob"]
        return 1

    def delete(self, target, kind, flags):
        self.deletes.append((target, kind, flags))
        if self.fail_status is not None:
            self.error = self.fail_status
            return 0
        if target not in self.values:
            self.error = 1168
            return 0
        del self.values[target]
        return 1

    def free(self, pointer):
        address = ctypes.addressof(pointer.contents)
        self.freed.append(address)
        del self.allocations[address]


class WinCredABITests(unittest.TestCase):
    def test_win32_scalar_sizes_are_fixed_on_non_windows_test_hosts(self):
        self.assertEqual(ctypes.sizeof(_DWORD), 4)
        self.assertEqual(ctypes.sizeof(_BOOL), 4)
        self.assertEqual(ctypes.sizeof(_WinFileTime), 8)

    def test_credential_layout_matches_windows_32_or_64_bit_abi(self):
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            expected = {"Flags": 0, "Type": 4, "TargetName": 8, "Comment": 16,
                        "LastWritten": 24, "CredentialBlobSize": 32, "CredentialBlob": 40,
                        "Persist": 48, "AttributeCount": 52, "Attributes": 56,
                        "TargetAlias": 64, "UserName": 72}
            size, attribute_size = 80, 24
        else:
            expected = {"Flags": 0, "Type": 4, "TargetName": 8, "Comment": 12,
                        "LastWritten": 16, "CredentialBlobSize": 24, "CredentialBlob": 28,
                        "Persist": 32, "AttributeCount": 36, "Attributes": 40,
                        "TargetAlias": 44, "UserName": 48}
            size, attribute_size = 52, 16
        self.assertEqual(ctypes.sizeof(_WinCredential), size)
        self.assertEqual(ctypes.sizeof(_WinCredentialAttribute), attribute_size)
        self.assertEqual({name: getattr(_WinCredential, name).offset for name in expected}, expected)

    def test_unicode_function_signatures_and_last_error_capture_are_explicit(self):
        fake = FakeWinCred()
        self.assertEqual(fake.library.CredReadW.argtypes,
                         [ctypes.c_wchar_p, _DWORD, _DWORD, ctypes.POINTER(_WinCredentialPointer)])
        self.assertEqual(fake.library.CredWriteW.argtypes, [_WinCredentialPointer, _DWORD])
        self.assertEqual(fake.library.CredDeleteW.argtypes, [ctypes.c_wchar_p, _DWORD, _DWORD])
        self.assertEqual(fake.library.CredFree.argtypes, [ctypes.c_void_p])
        self.assertIs(fake.library.CredReadW.restype, _BOOL)
        self.assertIs(fake.library.CredWriteW.restype, _BOOL)
        self.assertIs(fake.library.CredDeleteW.restype, _BOOL)
        self.assertIsNone(fake.library.CredFree.restype)
        with patch.object(ctypes, "WinDLL", return_value=fake.library, create=True) as factory, \
                patch.object(ctypes, "get_last_error", return_value=5, create=True) as last_error:
            native = _WinCredAPI()
            factory.assert_called_once_with("Advapi32.dll", use_last_error=True)
            self.assertEqual(native.last_error(), 5)
            last_error.assert_called_once_with()


class WindowsCredentialTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeWinCred()
        self.backend = _WindowsCredentials(_native=self.fake.native)
        self.store = TokenStore("C:/Users/Test/Проект", _backend=self.backend)
        self.target = SERVICE + "/" + self.store.account

    def assert_freed(self):
        self.assertFalse(self.fake.allocations)

    def test_project_namespace_and_service_are_the_only_identity(self):
        self.assertEqual(self.store.account, hashlib.sha256("C:/Users/Test/Проект".encode()).hexdigest())
        self.assertEqual(self.store.storage_name, "Диспетчер учётных данных Windows")
        self.store.save(TOKEN)
        saved = self.fake.written[-1]
        self.assertEqual(saved["target"], self.target)
        self.assertEqual(saved["user"], self.store.account)
        self.assertNotIn(TOKEN, saved["target"])
        self.assertEqual(saved["blob"], TOKEN.encode("ascii"))
        self.assertEqual(saved["blob_size"], len(TOKEN))
        self.assertEqual(saved["type"], _CRED_TYPE_GENERIC)
        self.assertEqual(saved["persist"], _CRED_PERSIST_LOCAL_MACHINE)
        self.assertEqual((saved["flags"], saved["call_flags"], saved["attributes"]), (0, 0, 0))
        self.assertFalse(saved["has_attributes"])
        self.assertIsNone(saved["alias"])

    def test_load_save_update_and_delete_lifecycle_is_scoped_and_idempotent(self):
        self.assertIsNone(self.store.load())
        self.store.save(TOKEN)
        self.assertEqual(self.store.load(), TOKEN)
        another = TokenStore("another project", _backend=self.backend)
        self.assertIsNone(another.load())
        another.delete()
        self.assertEqual(self.store.load(), TOKEN)
        self.store.save(TOKEN + "-updated")
        self.assertEqual(self.store.load(), TOKEN + "-updated")
        self.store.delete()
        self.assertIsNone(self.store.load())
        self.store.delete()
        self.assertTrue(all(kind == 1 and flags == 0 for _, kind, flags in self.fake.reads + self.fake.deletes))
        self.assert_freed()

    def test_clipboard_wrapping_is_normalized_before_native_call(self):
        self.store.save("OAuth \ufeff" + TOKEN[:12] + "\n\u200b" + TOKEN[12:])
        self.assertEqual(self.fake.written[-1]["blob"], TOKEN.encode())
        self.assertEqual(self.store.load(), TOKEN)
        self.assert_freed()

    def test_native_write_buffer_is_cleared_on_success_and_failure(self):
        for status in (None, 5):
            self.fake.fail_status = status
            if status is None:
                self.store.save(TOKEN)
            else:
                with self.assertRaises(CredentialError):
                    self.store.save(TOKEN)
            credential = self.fake.write_structs[-1]
            self.assertEqual(ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize), b"\0" * len(TOKEN))

    def test_blob_limit_is_checked_before_native_write(self):
        self.store.save("x" * _CRED_MAX_BLOB_SIZE)
        self.assertEqual(len(self.fake.written[-1]["blob"]), 2560)
        self.assertEqual(self.store.load(), "x" * _CRED_MAX_BLOB_SIZE)
        self.fake.library.CredWriteW.reset_mock()
        for invalid in ("x" * 2561, "", "test\0secret", "secret\x7f", "секрет"):
            with self.subTest(length=len(invalid)), self.assertRaises(CredentialError) as caught:
                self.store.save(invalid)
            self.assertNotIn("secret", str(caught.exception))
        self.fake.library.CredWriteW.assert_not_called()

    def test_oversized_empty_null_blob_and_wrong_identity_are_rejected_and_freed(self):
        self.fake.values[self.target] = TOKEN.encode()
        mutators = [
            lambda item: setattr(item, "CredentialBlobSize", 0),
            lambda item: setattr(item, "CredentialBlobSize", 2561),
            lambda item: setattr(item, "CredentialBlob", None),
            lambda item: setattr(item, "Type", 2),
            lambda item: setattr(item, "TargetName", "other-account"),
            lambda item: setattr(item, "TargetName", None),
        ]
        for mutate in mutators:
            self.fake.read_mutator = mutate
            with self.subTest(mutate=mutate), self.assertRaises(CredentialError) as caught:
                self.store.load()
            self.assertNotIn(TOKEN, str(caught.exception))
            self.assertFalse(self.fake.allocations)
        self.assertEqual(self.fake.library.CredFree.call_count, len(mutators))

    def test_non_ascii_and_invalid_stored_tokens_are_rejected_and_freed(self):
        for value in (b"secret\xff", b"secret\0", b"", b"****"):
            self.fake.values[self.target] = value
            with self.subTest(value=value), self.assertRaises(CredentialError) as caught:
                self.store.load()
            self.assertNotIn("secret", str(caught.exception))
            self.assertFalse(self.fake.allocations)
        self.assertEqual(self.fake.library.CredFree.call_count, 4)

    def test_success_with_null_credential_pointer_is_rejected(self):
        self.fake.values[self.target] = TOKEN.encode()
        self.fake.empty_read = True
        with self.assertRaises(CredentialError):
            self.store.load()
        self.fake.library.CredFree.assert_not_called()

    def test_read_allocation_is_freed_even_if_native_call_raises_after_allocation(self):
        self.fake.values[self.target] = TOKEN.encode()
        def failing_read(*args):
            self.fake.read(*args)
            raise RuntimeError(TOKEN)
        self.fake.library.CredReadW.side_effect = failing_read
        with self.assertRaises(CredentialError) as caught:
            self.store.load()
        self.assertNotIn(TOKEN, str(caught.exception))
        self.fake.library.CredFree.assert_called_once()
        self.assertFalse(self.fake.allocations)

    def test_native_error_codes_are_safe_for_all_operations(self):
        for status in (5, 1312, 1223, 87, 99999):
            self.fake.fail_status = status
            for action in (self.store.load, lambda: self.store.save(TOKEN), self.store.delete):
                with self.subTest(status=status), self.assertRaises(CredentialError) as caught:
                    action()
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertIn("Windows", str(caught.exception))
            self.assertFalse(self.fake.allocations)

    def test_unexpected_native_exception_never_leaks_token(self):
        for function, action in ((self.fake.library.CredReadW, self.store.load),
                                 (self.fake.library.CredWriteW, lambda: self.store.save(TOKEN)),
                                 (self.fake.library.CredDeleteW, self.store.delete)):
            function.side_effect = RuntimeError(TOKEN)
            with self.assertRaises(CredentialError) as caught:
                action()
            self.assertNotIn(TOKEN, str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)

    def test_windows_auto_selects_backend_without_opening_keychain(self):
        with patch("yandex_disk.credentials.sys.platform", "win32"), \
                patch("yandex_disk.credentials._WindowsCredentials", return_value=self.backend) as windows, \
                patch("yandex_disk.credentials._MacKeychain") as mac:
            store = TokenStore("project")
        self.assertTrue(store.available)
        self.assertEqual(store.storage_name, "Диспетчер учётных данных Windows")
        windows.assert_called_once_with()
        mac.assert_not_called()

    def test_missing_windows_dll_has_no_plaintext_fallback_or_secret_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("yandex_disk.credentials.sys.platform", "win32"), \
                    patch("yandex_disk.credentials._WindowsCredentials", side_effect=OSError(TOKEN)):
                store = TokenStore(directory)
            self.assertFalse(store.available)
            self.assertEqual(store.storage_name, "Диспетчер учётных данных Windows")
            for action in (store.load, lambda: store.save(TOKEN), store.delete):
                with self.assertRaises(CredentialError) as caught:
                    action()
                self.assertNotIn(TOKEN, str(caught.exception))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_storage_name_on_macos_and_unsupported_platforms_is_public_text(self):
        for platform, expected in (("darwin", "Связка ключей macOS"), ("linux", "Системное хранилище токенов")):
            with patch("yandex_disk.credentials.sys.platform", platform):
                store = TokenStore("project", _backend=Mock())
            self.assertEqual(store.storage_name, expected)


@unittest.skipUnless(sys.platform == "win32" and os.environ.get("DISK_DOWNLOADER_WINDOWS_CREDENTIAL_TESTS") == "1",
                     "Opt-in Windows Credential Manager test with an isolated dummy item")
class NativeWindowsCredentialTests(unittest.TestCase):
    def test_dummy_item_persists_updates_and_deletes_without_other_credentials(self):
        # Only one UUID-scoped application item is touched; no enumeration and
        # no real OAuth values. Explicit opt-in is required for all native calls.
        namespace = "DiskDownloader-WinCred-native-test-" + uuid.uuid4().hex
        first = TokenStore(namespace)
        second = TokenStore(namespace)
        self.assertTrue(first.available)
        try:
            self.assertIsNone(first.load())
            first.save(TOKEN)
            self.assertEqual(second.load(), TOKEN)
            second.save(TOKEN + "-updated")
            self.assertEqual(first.load(), TOKEN + "-updated")
            first.delete()
            self.assertIsNone(second.load())
            second.delete()
        finally:
            first.delete()


if __name__ == "__main__":
    unittest.main()
