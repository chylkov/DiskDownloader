"""Opt-in macOS credential lifecycle in an isolated temporary Keychain.

Run with DISK_DOWNLOADER_KEYCHAIN_TESTS=1. All SecItem queries are explicitly
restricted to the temporary Keychain; no real OAuth token is involved. No
default-Keychain or search-list setters are called. Apple documents private
temporary Keychains and its implementation excludes private paths from the
search list:
https://developer.apple.com/documentation/security/seckeychaincreate(_:_:_:_:_:_:)
https://github.com/apple-oss-distributions/Security/blob/main/OSX/libsecurity_keychain/lib/StorageManager.cpp
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import secrets
import sys
import tempfile
import unittest

from yandex_disk.credentials import TokenStore, _Frameworks, _MacKeychain


class _ArrayCallbacks(ctypes.Structure):
    _fields_ = [("version", ctypes.c_long), ("retain", ctypes.c_void_p),
                ("release", ctypes.c_void_p), ("copyDescription", ctypes.c_void_p),
                ("equal", ctypes.c_void_p)]


class _IsolatedFrameworks(_Frameworks):
    """Production SecItem/CF calls with a mandatory explicit test target."""

    def __init__(self, keychain):
        super().__init__()
        pointer = ctypes.c_void_p
        self._bind(self.cf, "CFArrayCreate", [pointer, pointer, ctypes.c_long, pointer], pointer)
        self._bind(self.cf, "CFDictionaryGetValue", [pointer, pointer], pointer)
        self.keychain = keychain
        callbacks = _ArrayCallbacks.in_dll(self.cf, "kCFTypeArrayCallBacks")
        references = (pointer * 1)(keychain)
        self.search_list = self.cf.CFArrayCreate(None, references, 1, ctypes.byref(callbacks))
        if not self.search_list:
            raise RuntimeError("Could not create isolated Keychain query scope")
        for name in ("kSecUseKeychain", "kSecMatchSearchList", "kSecUseAuthenticationUI", "kSecUseAuthenticationUIFail"):
            self._constants[name] = pointer.in_dll(self.security, name).value

    def _target(self, query, *, adding=False):
        key = self.constant("kSecUseKeychain" if adding else "kSecMatchSearchList")
        value = self.keychain if adding else self.search_list
        self.cf.CFDictionarySetValue(query, key, value)
        self.cf.CFDictionarySetValue(query, self.constant("kSecUseAuthenticationUI"),
                                     self.constant("kSecUseAuthenticationUIFail"))
        if self.cf.CFDictionaryGetValue(query, key) != value:
            raise RuntimeError("Refusing an unscoped Keychain test operation")

    def copy_matching(self, query):
        self._target(query)
        return super().copy_matching(query)

    def add(self, query):
        self._target(query, adding=True)
        return super().add(query)

    def update(self, query, attributes):
        self._target(query)
        return super().update(query, attributes)

    def delete(self, query):
        self._target(query)
        return super().delete(query)


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("DISK_DOWNLOADER_KEYCHAIN_TESTS") == "1",
                     "Opt-in isolated macOS Keychain test")
class NativeKeychainTests(unittest.TestCase):
    def test_persists_loads_updates_and_deletes_only_in_private_keychain(self):
        native = _Frameworks()
        pointer = ctypes.c_void_p
        status = ctypes.c_int32
        native._bind(native.security, "SecKeychainCreate",
                     [ctypes.c_char_p, ctypes.c_uint32, pointer, ctypes.c_ubyte, pointer, ctypes.POINTER(pointer)], status)
        native._bind(native.security, "SecKeychainUnlock",
                     [pointer, ctypes.c_uint32, pointer, ctypes.c_ubyte], status)
        native._bind(native.security, "SecKeychainDelete", [pointer], status)
        native._bind(native.security, "SecKeychainCopyDefault", [ctypes.POINTER(pointer)], status)
        native._bind(native.security, "SecKeychainCopySearchList", [ctypes.POINTER(pointer)], status)
        native._bind(native.cf, "CFEqual", [pointer, pointer], ctypes.c_ubyte)

        def snapshot():
            default, search_list = pointer(), pointer()
            try:
                self.assertEqual(native.security.SecKeychainCopyDefault(ctypes.byref(default)), 0)
                self.assertEqual(native.security.SecKeychainCopySearchList(ctypes.byref(search_list)), 0)
                return default.value, search_list.value
            except Exception:
                native.release(default.value)
                native.release(search_list.value)
                raise

        def assert_unchanged(before):
            after = snapshot()
            try:
                self.assertTrue(native.cf.CFEqual(before[0], after[0]), "Default Keychain changed")
                self.assertTrue(native.cf.CFEqual(before[1], after[1]), "Default search list changed")
            finally:
                for reference in after:
                    native.release(reference)

        before = snapshot()
        keychain = pointer()
        scoped_native = None
        try:
            with tempfile.TemporaryDirectory(prefix="disk-downloader-keychain-") as directory:
                path = Path(directory) / "isolated-test.keychain"
                password = secrets.token_hex(24).encode("ascii")
                buffer = ctypes.create_string_buffer(password)
                created = native.security.SecKeychainCreate(
                    os.fsencode(path), len(password), buffer, 0, None, ctypes.byref(keychain))
                self.assertEqual(created, 0, f"Isolated Keychain creation failed: status {created}")
                try:
                    self.assertEqual(native.security.SecKeychainUnlock(keychain, len(password), buffer, 1), 0)
                    assert_unchanged(before)
                    scoped_native = _IsolatedFrameworks(keychain.value)
                    namespace = str(Path(directory) / "project")
                    first = TokenStore(namespace, _backend=_MacKeychain(_native=scoped_native))
                    second = TokenStore(namespace, _backend=_MacKeychain(_native=scoped_native))
                    another = TokenStore(namespace + "-other", _backend=_MacKeychain(_native=scoped_native))
                    dummy_token = "dummy-native-test-token-" + secrets.token_hex(12)
                    updated_token = "dummy-native-test-updated-" + secrets.token_hex(12)
                    self.assertIsNone(first.load())
                    first.save("OAuth \ufeff" + dummy_token[:15] + "\n\u200b" + dummy_token[15:])
                    self.assertEqual(second.load(), dummy_token)
                    self.assertIsNone(another.load())
                    second.save(updated_token)
                    self.assertEqual(first.load(), updated_token)
                    another.delete()
                    self.assertEqual(second.load(), updated_token)
                    first.delete()
                    self.assertIsNone(second.load())
                    second.delete()
                    assert_unchanged(before)
                finally:
                    if scoped_native is not None:
                        scoped_native.release(scoped_native.search_list)
                    self.assertEqual(native.security.SecKeychainDelete(keychain), 0)
                    native.release(keychain.value)
                    keychain = pointer()
            assert_unchanged(before)
        finally:
            if keychain.value:
                native.security.SecKeychainDelete(keychain)
                native.release(keychain.value)
            for reference in before:
                native.release(reference)


if __name__ == "__main__":
    unittest.main()
