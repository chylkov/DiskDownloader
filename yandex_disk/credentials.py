"""OAuth storage in the macOS Keychain or Windows Credential Manager.

Traditional desktop Keychain items use the login Keychain's lock and access
control list.  Data-protection-only attributes such as kSecAttrAccessible are
not used: a Python command-line program does not have an app access group.
Windows uses an application-defined generic credential for the current user
on this computer. Neither backend uses shell commands or plaintext files.
"""
from __future__ import annotations

import ctypes
import hashlib
import sys
import threading

from .token import TokenError, normalize_token


SERVICE = "com.diskdownloader.yandex-disk"
_SUCCESS = 0
_NOT_FOUND = -25300
_DUPLICATE = -25299
_USER_CANCELLED = -128
_AUTH_FAILED = -25293
_INTERACTION_NOT_ALLOWED = -25308
_LOCK = threading.RLock()
_STORAGE_NAMES = {
    "darwin": "Связка ключей macOS",
    "win32": "Диспетчер учётных данных Windows",
}


class CredentialError(Exception):
    """A safe, user-facing failure that never includes the credential."""


def _validate_token(token: str) -> str:
    try:
        return normalize_token(token)
    except TokenError as error:
        raise CredentialError(str(error)) from None


def _check_status(status: int) -> None:
    if status == _SUCCESS:
        return
    if status == _USER_CANCELLED:
        message = "Доступ к Связке ключей отменён."
    elif status == _AUTH_FAILED:
        message = "Связка ключей не разрешила доступ к сохранённому токену."
    elif status == _INTERACTION_NOT_ALLOWED:
        message = "Связка ключей заблокирована или сейчас недоступна."
    else:
        message = "Не удалось выполнить операцию со Связкой ключей."
    raise CredentialError(message)


class TokenStore:
    """One project-scoped OAuth credential shared by the GUI and CLI.

    Pass the same canonical project directory as ``namespace`` in both entry
    points. Other platforms deliberately have no plaintext storage fallback.
    ``_backend`` is a private injection point for isolated tests.
    """

    def __init__(self, namespace: str, *, _backend=None):
        self.account = hashlib.sha256(str(namespace).encode("utf-8")).hexdigest()
        self.storage_name = _STORAGE_NAMES.get(sys.platform, "Системное хранилище токенов")
        self._backend = _backend
        if _backend is None and sys.platform in ("darwin", "win32"):
            try:
                self._backend = _MacKeychain() if sys.platform == "darwin" else _WindowsCredentials()
            except Exception:
                # Native library loading errors can include system paths; do not
                # propagate them into the GUI or implicitly fall back to files.
                self._backend = None
        backend_name = getattr(self._backend, "storage_name", None)
        if isinstance(backend_name, str):
            self.storage_name = backend_name
        self.available = self._backend is not None

    def _require_backend(self):
        if not self.available:
            raise CredentialError("Системное хранилище токенов недоступно. Сохранение поддерживается на macOS и Windows.")
        return self._backend

    def load(self) -> str | None:
        with _LOCK:
            backend = self._require_backend()
            try:
                token = backend.load(SERVICE, self.account)
                return None if token is None else _validate_token(token)
            except CredentialError:
                raise
            except Exception:
                raise CredentialError("Не удалось прочитать токен из системного хранилища.") from None

    def save(self, token: str) -> None:
        token = _validate_token(token)
        with _LOCK:
            backend = self._require_backend()
            try:
                backend.save(SERVICE, self.account, token.encode("ascii"))
            except CredentialError:
                raise
            except Exception:
                raise CredentialError("Не удалось сохранить токен в системном хранилище.") from None

    def delete(self) -> None:
        with _LOCK:
            backend = self._require_backend()
            try:
                backend.delete(SERVICE, self.account)
            except CredentialError:
                raise
            except Exception:
                raise CredentialError("Не удалось удалить токен из системного хранилища.") from None


class _DictionaryKeyCallbacks(ctypes.Structure):
    _fields_ = [("version", ctypes.c_long), ("retain", ctypes.c_void_p),
                ("release", ctypes.c_void_p), ("copyDescription", ctypes.c_void_p),
                ("equal", ctypes.c_void_p), ("hash", ctypes.c_void_p)]


class _DictionaryValueCallbacks(ctypes.Structure):
    _fields_ = [("version", ctypes.c_long), ("retain", ctypes.c_void_p),
                ("release", ctypes.c_void_p), ("copyDescription", ctypes.c_void_p),
                ("equal", ctypes.c_void_p)]


class _Frameworks:
    """Minimal, correctly typed Security/CoreFoundation C interface."""

    def __init__(self):
        self.cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        pointer = ctypes.c_void_p
        index = ctypes.c_long
        self._bind(self.cf, "CFStringCreateWithCString", [pointer, ctypes.c_char_p, ctypes.c_uint32], pointer)
        self._bind(self.cf, "CFDataCreate", [pointer, pointer, index], pointer)
        self._bind(self.cf, "CFDataGetLength", [pointer], index)
        self._bind(self.cf, "CFDataGetBytePtr", [pointer], pointer)
        self._bind(self.cf, "CFDataGetTypeID", [], ctypes.c_ulong)
        self._bind(self.cf, "CFGetTypeID", [pointer], ctypes.c_ulong)
        self._bind(self.cf, "CFDictionaryCreateMutable", [pointer, index, pointer, pointer], pointer)
        self._bind(self.cf, "CFDictionarySetValue", [pointer, pointer, pointer], None)
        self._bind(self.cf, "CFRelease", [pointer], None)
        self._bind(self.security, "SecItemCopyMatching", [pointer, ctypes.POINTER(pointer)], ctypes.c_int32)
        self._bind(self.security, "SecItemAdd", [pointer, ctypes.POINTER(pointer)], ctypes.c_int32)
        self._bind(self.security, "SecItemUpdate", [pointer, pointer], ctypes.c_int32)
        self._bind(self.security, "SecItemDelete", [pointer], ctypes.c_int32)
        self._keys = _DictionaryKeyCallbacks.in_dll(self.cf, "kCFTypeDictionaryKeyCallBacks")
        self._values = _DictionaryValueCallbacks.in_dll(self.cf, "kCFTypeDictionaryValueCallBacks")
        self._constants = {name: ctypes.c_void_p.in_dll(self.security, name).value for name in (
            "kSecClass", "kSecClassGenericPassword", "kSecAttrService", "kSecAttrAccount",
            "kSecAttrLabel", "kSecValueData", "kSecReturnData", "kSecMatchLimit", "kSecMatchLimitOne",
        )}
        self._constants["kCFBooleanTrue"] = ctypes.c_void_p.in_dll(self.cf, "kCFBooleanTrue").value

    @staticmethod
    def _bind(library, name, arguments, result):
        function = getattr(library, name)
        function.argtypes = arguments
        function.restype = result

    def constant(self, name):
        return self._constants[name]

    def scope(self):
        return _CFScope(self)

    def copy_matching(self, query):
        result = ctypes.c_void_p()
        status = self.security.SecItemCopyMatching(query, ctypes.byref(result))
        return status, result.value

    def add(self, attributes):
        return self.security.SecItemAdd(attributes, None)

    def update(self, query, attributes):
        return self.security.SecItemUpdate(query, attributes)

    def delete(self, query):
        return self.security.SecItemDelete(query)

    def read_data(self, reference) -> str:
        if not reference or self.cf.CFGetTypeID(reference) != self.cf.CFDataGetTypeID():
            raise CredentialError("Сохранённый токен имеет неверный формат.")
        length = self.cf.CFDataGetLength(reference)
        if not 0 < length <= 65536:
            raise CredentialError("Сохранённый токен имеет неверный формат.")
        pointer = self.cf.CFDataGetBytePtr(reference)
        if not pointer:
            raise CredentialError("Сохранённый токен имеет неверный формат.")
        try:
            return ctypes.string_at(pointer, length).decode("ascii")
        except UnicodeDecodeError:
            raise CredentialError("Сохранённый токен имеет неверный формат.") from None

    def release(self, reference):
        if reference:
            self.cf.CFRelease(reference)


class _CFScope:
    """Release every owned CF reference, also on partially built requests."""

    def __init__(self, native):
        self.native = native
        self.owned = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        for reference in reversed(self.owned):
            self.native.release(reference)
        self.owned.clear()

    def _own(self, reference):
        if not reference:
            raise CredentialError("Не удалось подготовить запрос к Связке ключей.")
        self.owned.append(reference)
        return reference

    def string(self, value: str):
        return self._own(self.native.cf.CFStringCreateWithCString(None, value.encode("utf-8"), 0x08000100))

    def data(self, value: bytes):
        # CFDataCreate copies the bytes, so this temporary buffer can be freed.
        buffer = ctypes.create_string_buffer(value)
        return self._own(self.native.cf.CFDataCreate(None, buffer, len(value)))

    def dictionary(self, values):
        native = self.native
        result = self._own(native.cf.CFDictionaryCreateMutable(
            None, 0, ctypes.byref(native._keys), ctypes.byref(native._values)))
        for key, value in values.items():
            native.cf.CFDictionarySetValue(result, key, value)
        return result


class _MacKeychain:
    storage_name = _STORAGE_NAMES["darwin"]

    def __init__(self, *, _native=None):
        self.native = _native if _native is not None else _Frameworks()

    def _identity(self, scope, service, account):
        c = self.native.constant
        return {
            c("kSecClass"): c("kSecClassGenericPassword"),
            c("kSecAttrService"): scope.string(service),
            c("kSecAttrAccount"): scope.string(account),
        }

    def load(self, service, account):
        native = self.native
        c = native.constant
        with native.scope() as scope:
            values = self._identity(scope, service, account)
            values.update({c("kSecReturnData"): c("kCFBooleanTrue"),
                           c("kSecMatchLimit"): c("kSecMatchLimitOne")})
            status, result = native.copy_matching(scope.dictionary(values))
            try:
                if status == _NOT_FOUND:
                    return None
                _check_status(status)
                return native.read_data(result)
            finally:
                native.release(result)

    def save(self, service, account, token):
        native = self.native
        c = native.constant
        with native.scope() as scope:
            identity = self._identity(scope, service, account)
            changes = {c("kSecValueData"): scope.data(token)}
            # Update first: an existing item keeps its user-approved ACL.
            query = scope.dictionary(identity)
            update = scope.dictionary(changes)
            status = native.update(query, update)
            if status == _NOT_FOUND:
                attributes = {**identity, **changes,
                              c("kSecAttrLabel"): scope.string("Disk Downloader — Яндекс Диск")}
                status = native.add(scope.dictionary(attributes))
                # Another process could have created this same item meanwhile.
                if status == _DUPLICATE:
                    status = native.update(query, update)
            _check_status(status)

    def delete(self, service, account):
        native = self.native
        with native.scope() as scope:
            status = native.delete(scope.dictionary(self._identity(scope, service, account)))
            if status != _NOT_FOUND:
                _check_status(status)


# Windows keeps DWORD and BOOL at 32 bits, including on 64-bit systems. Avoid
# ctypes.c_ulong/wintypes aliases whose size depends on the test host's ABI.
_DWORD = ctypes.c_uint32
_BOOL = ctypes.c_int32
_BYTE_POINTER = ctypes.POINTER(ctypes.c_ubyte)
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_CRED_MAX_BLOB_SIZE = 5 * 512
_WIN_NOT_FOUND = 1168


class _WinFileTime(ctypes.Structure):
    _fields_ = [("dwLowDateTime", _DWORD), ("dwHighDateTime", _DWORD)]


class _WinCredentialAttribute(ctypes.Structure):
    _fields_ = [("Keyword", ctypes.c_wchar_p), ("Flags", _DWORD),
                ("ValueSize", _DWORD), ("Value", _BYTE_POINTER)]


class _WinCredential(ctypes.Structure):
    _fields_ = [
        ("Flags", _DWORD), ("Type", _DWORD),
        ("TargetName", ctypes.c_wchar_p), ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _WinFileTime), ("CredentialBlobSize", _DWORD),
        ("CredentialBlob", _BYTE_POINTER), ("Persist", _DWORD),
        ("AttributeCount", _DWORD),
        ("Attributes", ctypes.POINTER(_WinCredentialAttribute)),
        ("TargetAlias", ctypes.c_wchar_p), ("UserName", ctypes.c_wchar_p),
    ]


_WinCredentialPointer = ctypes.POINTER(_WinCredential)


class _WinCredAPI:
    """Explicit Unicode WinCred entry points; loading makes no account calls."""

    def __init__(self, *, _library=None, _last_error=None):
        self.library = _library if _library is not None else ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self.last_error = _last_error if _last_error is not None else ctypes.get_last_error
        self._bind("CredReadW", [ctypes.c_wchar_p, _DWORD, _DWORD, ctypes.POINTER(_WinCredentialPointer)], _BOOL)
        self._bind("CredWriteW", [_WinCredentialPointer, _DWORD], _BOOL)
        self._bind("CredDeleteW", [ctypes.c_wchar_p, _DWORD, _DWORD], _BOOL)
        self._bind("CredFree", [ctypes.c_void_p], None)

    def _bind(self, name, arguments, result):
        function = getattr(self.library, name)
        function.argtypes = arguments
        function.restype = result


def _check_windows_status(status: int) -> None:
    if status == 5:
        message = "Диспетчер учётных данных Windows не разрешил доступ к токену."
    elif status == 1312:
        message = "Диспетчер учётных данных Windows недоступен в текущем сеансе."
    elif status == 1223:
        message = "Доступ к Диспетчеру учётных данных Windows отменён."
    else:
        message = "Не удалось выполнить операцию с Диспетчером учётных данных Windows."
    raise CredentialError(message)


class _WindowsCredentials:
    """A project-scoped CRED_TYPE_GENERIC item, never a domain password.

    Microsoft documents a 2560-byte blob limit. LOCAL_MACHINE persistence means
    this user's later logon sessions on this computer, not access by all users:
    https://learn.microsoft.com/windows/win32/api/wincred/ns-wincred-credentialw
    """

    storage_name = _STORAGE_NAMES["win32"]

    def __init__(self, *, _native=None):
        self.native = _native if _native is not None else _WinCredAPI()

    @staticmethod
    def _target(service: str, account: str) -> str:
        if any(not isinstance(value, str) or not value or "\0" in value for value in (service, account)):
            raise CredentialError("Не удалось определить место сохранения токена.")
        target = service + "/" + account
        if len(target) > 32767:
            raise CredentialError("Не удалось определить место сохранения токена.")
        return target

    def load(self, service, account):
        native = self.native
        target = self._target(service, account)
        result = _WinCredentialPointer()
        try:
            succeeded = native.library.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(result))
            if not succeeded:
                status = native.last_error()
                if status == _WIN_NOT_FOUND:
                    return None
                _check_windows_status(status)
            if not result:
                raise CredentialError("Сохранённый токен имеет неверный формат.")
            credential = result.contents
            if (credential.Type != _CRED_TYPE_GENERIC
                    or credential.TargetName is None
                    or credential.TargetName.casefold() != target.casefold()
                    or not 0 < credential.CredentialBlobSize <= _CRED_MAX_BLOB_SIZE
                    or not credential.CredentialBlob):
                raise CredentialError("Сохранённый токен имеет неверный формат.")
            try:
                return ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize).decode("ascii")
            except UnicodeDecodeError:
                raise CredentialError("Сохранённый токен имеет неверный формат.") from None
        finally:
            # CredRead allocates one block, including nested blob/string data.
            # Release that block exactly once, including all validation errors.
            if result:
                native.library.CredFree(result)

    def save(self, service, account, token):
        if (not isinstance(token, bytes) or not 0 < len(token) <= _CRED_MAX_BLOB_SIZE
                or any(not 33 <= byte <= 126 for byte in token)):
            raise CredentialError("Токен имеет неверный формат или превышает лимит хранилища Windows (2560 байт).")
        target = self._target(service, account)
        buffer = (ctypes.c_ubyte * len(token)).from_buffer_copy(token)
        credential = _WinCredential()
        credential.Type = _CRED_TYPE_GENERIC
        credential.TargetName = target
        credential.Comment = "DiskDownloader — Яндекс Диск"
        credential.CredentialBlobSize = len(token)
        credential.CredentialBlob = buffer
        credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = account
        try:
            if not self.native.library.CredWriteW(ctypes.byref(credential), 0):
                _check_windows_status(self.native.last_error())
        finally:
            ctypes.memset(buffer, 0, len(buffer))

    def delete(self, service, account):
        if not self.native.library.CredDeleteW(self._target(service, account), _CRED_TYPE_GENERIC, 0):
            status = self.native.last_error()
            if status != _WIN_NOT_FOUND:
                _check_windows_status(status)
