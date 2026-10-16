"""Keychain status handling, credential privacy, and native CF ownership."""
from __future__ import annotations

import ctypes
import hashlib
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from yandex_disk.credentials import (
    CredentialError, SERVICE, TokenStore, _CFScope, _Frameworks, _MacKeychain,
)


TOKEN = "fake-test-oauth-token-123456789"


class FakeScope:
    def __init__(self, native):
        self.native = native

    def __enter__(self):
        self.native.scopes_open += 1
        return self

    def __exit__(self, *_):
        self.native.scopes_open -= 1

    def string(self, value):
        return value

    def data(self, value):
        return value

    def dictionary(self, values):
        return values.copy()


class FakeFrameworks:
    def __init__(self):
        self.update_statuses = [-25300]
        self.add_status = 0
        self.load_status = 0
        self.load_value = TOKEN.encode("ascii")
        self.delete_status = 0
        self.calls = []
        self.released = []
        self.scopes_open = 0

    def constant(self, name):
        return name

    def scope(self):
        return FakeScope(self)

    def copy_matching(self, query):
        self.calls.append(("load", query))
        return self.load_status, self.load_value

    def add(self, attributes):
        self.calls.append(("add", attributes))
        return self.add_status

    def update(self, query, attributes):
        self.calls.append(("update", query, attributes))
        return self.update_statuses.pop(0)

    def delete(self, query):
        self.calls.append(("delete", query))
        return self.delete_status

    def read_data(self, reference):
        if reference is None:
            raise CredentialError("Сохранённый токен имеет неверный формат.")
        return reference.decode("ascii")

    def release(self, reference):
        self.released.append(reference)


class TokenStoreTests(unittest.TestCase):
    def setUp(self):
        self.native = FakeFrameworks()
        self.store = TokenStore("/canonical/project", _backend=_MacKeychain(_native=self.native))

    def test_account_is_project_hash_and_shared_across_instances(self):
        other = TokenStore("/canonical/project", _backend=Mock())
        self.assertEqual(self.store.account, other.account)
        self.assertEqual(self.store.account, hashlib.sha256(b"/canonical/project").hexdigest())
        self.assertNotEqual(other.account, TokenStore("/other/project", _backend=Mock()).account)
        self.assertTrue(self.store.available)

    def test_unsupported_platform_does_not_create_files_or_start_frameworks(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("yandex_disk.credentials.sys.platform", "linux"), \
                    patch("yandex_disk.credentials._MacKeychain") as factory:
                store = TokenStore(directory)
                self.assertFalse(store.available)
                for operation in (store.load, lambda: store.save(TOKEN), store.delete):
                    with self.assertRaises(CredentialError) as raised:
                        operation()
                    self.assertNotIn(TOKEN, str(raised.exception))
                factory.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_missing_framework_is_unavailable_without_exposing_backend_failure(self):
        with patch("yandex_disk.credentials.sys.platform", "darwin"), \
                patch("yandex_disk.credentials._MacKeychain", side_effect=OSError(TOKEN)):
            store = TokenStore("project")
        self.assertFalse(store.available)
        with self.assertRaises(CredentialError) as raised:
            store.load()
        self.assertNotIn(TOKEN, str(raised.exception))

    def test_save_strips_surrounding_whitespace_but_rejects_unsafe_content(self):
        self.store.save(" \n" + TOKEN + "\t ")
        self.assertEqual(self.native.calls[-1][1]["kSecValueData"], TOKEN.encode("ascii"))
        self.native.calls.clear()
        for invalid in ("", " \n ", "a\x00b", "a\x7fb", "токен", "x" * 65537, None):
            with self.subTest(length=len(invalid) if isinstance(invalid, str) else None):
                with self.assertRaises(CredentialError):
                    self.store.save(invalid)
        self.assertEqual(self.native.calls, [])

    def test_wrapped_clipboard_token_is_normalized_before_keychain_storage(self):
        wrapped = "OAuth \ufeff" + TOKEN[:10] + "\n\u200b\u00a0" + TOKEN[10:]
        self.store.save(wrapped)
        self.assertEqual(self.native.calls[-1][1]["kSecValueData"], TOKEN.encode("ascii"))
        for invalid in ("••••", "****", TOKEN + "я"):
            with self.assertRaises(CredentialError) as raised:
                self.store.save(invalid)
            self.assertNotIn(TOKEN, str(raised.exception))

    def test_load_returns_missing_or_valid_credential(self):
        self.assertEqual(self.store.load(), TOKEN)
        self.native.load_status = -25300
        self.native.load_value = None
        self.assertIsNone(self.store.load())
        query = self.native.calls[0][1]
        self.assertEqual(query["kSecAttrService"], SERVICE)
        self.assertEqual(query["kSecAttrAccount"], self.store.account)
        self.assertEqual(query["kSecClass"], "kSecClassGenericPassword")
        self.assertEqual(query["kSecReturnData"], "kCFBooleanTrue")
        self.assertEqual(query["kSecMatchLimit"], "kSecMatchLimitOne")
        self.assertNotIn("kSecValueData", query)

    def test_invalid_stored_data_is_rejected_without_secret_in_error(self):
        for value in (None, b"", b"bad\x00value", b"secret\xff"):
            self.native.load_value = value
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(CredentialError) as raised:
                    self.store.load()
                self.assertNotIn("secret", str(raised.exception))
                self.assertEqual(self.native.scopes_open, 0)
                self.assertEqual(self.native.released[-1], value)

    def test_unexpected_backend_exceptions_do_not_reveal_credential(self):
        backend = Mock()
        store = TokenStore("project", _backend=backend)
        for name, action in (("load", store.load), ("save", lambda: store.save(TOKEN)), ("delete", store.delete)):
            getattr(backend, name).side_effect = RuntimeError(TOKEN)
            with self.subTest(operation=name):
                with self.assertRaises(CredentialError) as raised:
                    action()
                self.assertNotIn(TOKEN, str(raised.exception))
                self.assertTrue(raised.exception.__suppress_context__)

    def test_shared_lock_serializes_calls_from_worker_threads(self):
        entered, release, second_entered = threading.Event(), threading.Event(), threading.Event()
        backend = Mock()
        def load(*_):
            entered.set()
            self.assertTrue(release.wait(2))
            return TOKEN
        def save(*_):
            second_entered.set()
        backend.load = load
        backend.save = save
        first = TokenStore("project", _backend=backend)
        second = TokenStore("project", _backend=backend)
        failures = []
        def run(action):
            try:
                action()
            except Exception as error:
                failures.append(error)
        read_thread = threading.Thread(target=run, args=(first.load,))
        save_thread = threading.Thread(target=run, args=(lambda: second.save(TOKEN),))
        read_thread.start()
        self.assertTrue(entered.wait(2))
        save_thread.start()
        self.assertFalse(second_entered.wait(0.05))
        release.set()
        read_thread.join(2)
        save_thread.join(2)
        self.assertTrue(second_entered.is_set())
        self.assertFalse(failures)


class KeychainStatusTests(unittest.TestCase):
    def setUp(self):
        self.native = FakeFrameworks()
        self.store = TokenStore("project", _backend=_MacKeychain(_native=self.native))

    def test_save_adds_only_missing_item_and_preserves_desktop_acl(self):
        self.store.save(TOKEN)
        self.assertEqual([call[0] for call in self.native.calls], ["update", "add"])
        attributes = self.native.calls[1][1]
        self.assertEqual(attributes["kSecAttrService"], SERVICE)
        self.assertEqual(attributes["kSecAttrAccount"], self.store.account)
        self.assertEqual(attributes["kSecValueData"], TOKEN.encode("ascii"))
        self.assertNotIn("kSecAttrAccessible", attributes)
        self.assertNotIn("kSecAttrSynchronizable", attributes)
        self.assertNotIn("kSecAttrAccess", attributes)
        self.assertEqual(self.native.scopes_open, 0)

    def test_save_updates_existing_item_without_deletion_or_recreation(self):
        self.native.update_statuses = [0]
        self.store.save(TOKEN)
        self.assertEqual([call[0] for call in self.native.calls], ["update"])
        _, query, changes = self.native.calls[0]
        self.assertEqual(set(changes), {"kSecValueData"})
        self.assertNotIn("kSecReturnData", query)

    def test_duplicate_created_by_another_process_is_updated(self):
        self.native.add_status = -25299
        self.native.update_statuses = [-25300, 0]
        self.store.save(TOKEN)
        self.assertEqual([call[0] for call in self.native.calls], ["update", "add", "update"])

    def test_delete_is_scoped_and_idempotent(self):
        for status in (0, -25300):
            self.native.delete_status = status
            self.store.delete()
        _, query = self.native.calls[-1]
        self.assertEqual(set(query), {"kSecClass", "kSecAttrService", "kSecAttrAccount"})
        self.assertEqual(query["kSecAttrAccount"], self.store.account)

    def test_cancel_denied_locked_and_unknown_failures_are_safe_and_release_result(self):
        for status in (-128, -25293, -25308, -99999):
            for operation in ("load", "save", "delete"):
                with self.subTest(status=status, operation=operation):
                    self.native.calls.clear()
                    self.native.load_status = status
                    self.native.update_statuses = [status]
                    self.native.delete_status = status
                    action = lambda: self.store.save(TOKEN) if operation == "save" else getattr(self.store, operation)()
                    with self.assertRaises(CredentialError) as raised:
                        action()
                    self.assertNotIn(TOKEN, str(raised.exception))
                    self.assertEqual(self.native.scopes_open, 0)
                    if operation == "load":
                        self.assertEqual(self.native.released[-1], TOKEN.encode("ascii"))
                    if operation == "save":
                        self.assertEqual([call[0] for call in self.native.calls], ["update"])

    def test_failed_creation_does_not_mask_cancellation_or_retry(self):
        self.native.add_status = -128
        with self.assertRaises(CredentialError) as raised:
            self.store.save(TOKEN)
        self.assertIn("отменён", str(raised.exception))
        self.assertEqual([call[0] for call in self.native.calls], ["update", "add"])


class CFOwnershipTests(unittest.TestCase):
    def test_partial_allocations_are_freed_on_failed_creation(self):
        native = Mock()
        native.cf.CFStringCreateWithCString.side_effect = [1234, None]
        with self.assertRaises(CredentialError):
            with _CFScope(native) as scope:
                scope.string("service")
                scope.string("account")
        native.release.assert_called_once_with(1234)

    def test_allocations_are_released_in_reverse_order_on_exception(self):
        native = Mock()
        native.cf.CFStringCreateWithCString.side_effect = [11, 22]
        scope = _CFScope(native)
        with self.assertRaises(RuntimeError):
            with scope:
                scope.string("service")
                scope.string("account")
                raise RuntimeError("simulated failure")
        self.assertEqual([call.args[0] for call in native.release.call_args_list], [22, 11])
        self.assertEqual(scope.owned, [])

    @unittest.skipUnless(sys.platform == "darwin", "Native CoreFoundation is macOS-only")
    def test_native_cf_strings_data_and_dictionary_without_accessing_keychain(self):
        # Exercise the actual C ABI and retain/release callbacks with fake data.
        # SecItem functions are never called, so this touches no user keychain.
        native = _Frameworks()
        native._bind(native.cf, "CFDictionaryGetValue", [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_void_p)
        with native.scope() as scope:
            data = scope.data(TOKEN.encode("ascii"))
            self.assertEqual(native.read_data(data), TOKEN)
            name = scope.string("Тестовый токен")
            key = native.constant("kSecValueData")
            dictionary = scope.dictionary({key: data, native.constant("kSecAttrLabel"): name})
            self.assertEqual(native.cf.CFDictionaryGetValue(dictionary, key), data)
            with self.assertRaises(CredentialError):
                native.read_data(name)
        self.assertEqual(scope.owned, [])


if __name__ == "__main__":
    unittest.main()
