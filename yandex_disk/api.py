"""Small read-only Yandex Disk REST client using only the standard library.

The client intentionally does not expose upstream error bodies: those can
contain signed download links. OAuth is sent only to the fixed metadata API
origin and the documented private preview endpoint. Redirects never forward
OAuth to another origin.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import http.client
import json
import math
import posixpath
import re
import threading
import time
from typing import Any, Callable, Iterator, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from .token import normalize_token


API_BASE = "https://cloud-api.yandex.net/v1/disk"
PAGE_SIZE = 1000
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_RETRY_DELAY = 60.0
MAX_PREVIEW_REDIRECTS = 5


class ApiError(Exception):
    """A safe API error, without access tokens, URLs, or upstream bodies."""

    def __init__(
        self, message: str, status: int | None = None, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


def _invalid_response(message: str) -> ApiError:
    return ApiError(f"Некорректный ответ Яндекс Диска: {message}.", retryable=True)


@dataclass(frozen=True)
class Resource:
    name: str
    path: str
    kind: str
    size: int | None = None
    modified: str | None = None
    md5: str | None = None
    sha256: str | None = None

    @classmethod
    def from_json(cls, data: Any) -> Resource:
        if not isinstance(data, dict):
            raise _invalid_response("метаданные должны быть объектом")
        name, path, kind = data.get("name"), data.get("path"), data.get("type")
        if not isinstance(name, str) or not name or "\0" in name:
            raise _invalid_response("отсутствует имя ресурса")
        if not isinstance(path, str) or not path or "\0" in path:
            raise _invalid_response("отсутствует путь ресурса")
        if kind not in ("file", "dir"):
            raise _invalid_response("неизвестный тип ресурса")
        size = data.get("size")
        if size is not None and (type(size) is not int or size < 0):
            raise _invalid_response("неверный размер файла")
        if kind == "file" and size is None:
            raise _invalid_response("отсутствует размер файла")
        modified = data.get("modified")
        if modified is not None and (not isinstance(modified, str) or not modified):
            raise _invalid_response("неверное время изменения")
        hashes: dict[str, str | None] = {}
        for key, length in (("md5", 32), ("sha256", 64)):
            value = data.get(key)
            if value is not None:
                if not isinstance(value, str) or re.fullmatch(
                    rf"[0-9a-fA-F]{{{length}}}", value
                ) is None:
                    raise _invalid_response("неверная контрольная сумма")
                value = value.lower()
            hashes[key] = value
        if kind == "file" and not (modified or hashes["md5"] or hashes["sha256"]):
            raise _invalid_response("отсутствуют признаки версии файла")
        return cls(name, path, kind, size, modified, hashes["md5"], hashes["sha256"])


class _NoMetadataRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib otherwise copies Authorization to a redirect destination.
        return None


def _preview_location(value: Any):
    """Validate links without ever including signed URLs in an exception."""
    if not isinstance(value, str) or not value:
        raise _invalid_response("неверная ссылка на превью")
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
            and "\\" not in value
        )
        parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise _invalid_response("неверная ссылка на превью")
    return parsed


def _authenticated_preview(parsed) -> bool:
    # The documented private preview endpoint requires OAuth. Never authorize
    # arbitrary signed links, Yandex subdomain suffix matches, or other ports.
    return (
        parsed.hostname == "downloader.disk.yandex.ru"
        and parsed.port in (None, 443)
        and parsed.path.startswith("/preview/")
    )


def _normalized_disk_path(path: str) -> str:
    if not isinstance(path, str) or not path.strip() or "\0" in path:
        raise ValueError("Укажите путь к папке на Яндекс Диске.")
    path = path.strip()
    if path.startswith("disk:"):
        path = path[5:]
    elif re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", path):
        raise ValueError("Поддерживаются пути disk:/ или пути от корня Диска.")
    return "disk:" + posixpath.normpath("/" + path.lstrip("/"))


class YandexDiskAPI:
    def __init__(
        self,
        public_url: str | None = None,
        disk_path: str | None = None,
        token: str | None = None,
        timeout: float = 60,
        retries: int = 3,
        log: Callable[[str], None] | None = None,
    ) -> None:
        if (public_url is None) == (disk_path is None):
            raise ValueError("Укажите публичную ссылку или путь к личной папке.")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Таймаут должен быть положительным числом.")
        if type(retries) is not int or not 0 <= retries <= 20:
            raise ValueError("Число повторов должно быть целым числом от 0 до 20.")
        if public_url is not None:
            if not isinstance(public_url, str) or not public_url.strip() or "\0" in public_url:
                raise ValueError("Укажите непустую публичную ссылку.")
            self._public_url = public_url.strip()
            self._root_path = "/"
            self._token = None
            identity = "public:" + self._public_url
        else:
            token = normalize_token(token)
            self._public_url = None
            self._root_path = _normalized_disk_path(disk_path)  # type: ignore[arg-type]
            self._token = token
            # Account-independent paths must never share backup state. Binding
            # to a one-way token fingerprint avoids requiring disk.info during
            # an outage, at the cost of a new identity after token rotation.
            token_fingerprint = hashlib.sha256(self._token.encode("utf-8")).hexdigest()
            identity = "private:" + token_fingerprint + ":" + self._root_path
        self._source_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        self.timeout = float(timeout)
        self.retries = retries
        self._log = log or (lambda message: None)
        self._opener = build_opener(_NoMetadataRedirect())
        self._preview_opener = build_opener(_NoMetadataRedirect())
        self.stop_event: threading.Event | None = None

    @property
    def root_path(self) -> str:
        return self._root_path

    @property
    def source_id(self) -> str:
        return self._source_id

    def _path_params(self, path: str) -> dict[str, str]:
        if not isinstance(path, str) or not path or "\0" in path:
            raise ValueError("Укажите непустой путь ресурса.")
        if self._public_url is not None:
            return {"public_key": self._public_url, "path": path}
        return {"path": path}

    def _endpoint(self, download: bool = False) -> str:
        prefix = "/public" if self._public_url is not None else ""
        return prefix + "/resources" + ("/download" if download else "")

    def get_resource(self, path: str) -> Resource:
        params = self._path_params(path)
        params["limit"] = "0"
        return Resource.from_json(self._request_json(self._endpoint(), params))

    def iter_children(self, path: str) -> Iterator[Resource]:
        """Page through a directory; previously yielded pages survive a later error."""
        offset = 0
        total: int | None = None
        seen: set[str] = set()
        invalid_count = 0

        def finish_listing() -> None:
            if invalid_count:
                raise ApiError(
                    f"Пропущено {invalid_count} элементов с некорректными метаданными; "
                    "повторите запуск.",
                    retryable=True,
                )

        while True:
            params = self._path_params(path)
            params.update(limit=str(PAGE_SIZE), offset=str(offset), sort="name")
            data = self._request_json(self._endpoint(), params)
            parent = Resource.from_json(data)
            if parent.kind != "dir":
                raise ApiError("Ресурс не является папкой.")
            embedded = data.get("_embedded")
            if not isinstance(embedded, dict) or not isinstance(embedded.get("items"), list):
                raise _invalid_response("отсутствует список содержимого папки")
            page_total = embedded.get("total")
            if type(page_total) is not int or page_total < 0:
                raise _invalid_response("отсутствует число элементов папки")
            if total is not None and page_total != total:
                raise _invalid_response("содержимое папки изменилось во время обхода")
            total = page_total
            reported_offset = embedded.get("offset", offset)
            if type(reported_offset) is not int or reported_offset != offset:
                raise _invalid_response("неверное смещение страницы")
            raw_items = embedded["items"]
            if len(raw_items) > PAGE_SIZE or offset + len(raw_items) > total:
                raise _invalid_response("несогласованное число элементов папки")
            if not raw_items:
                if offset < total:
                    raise _invalid_response("пустая страница до конца списка")
                finish_listing()
                return
            # Damaged metadata for one item must not block rescue of its valid
            # siblings or later pages. Report the directory as incomplete once
            # all retrievable items have been yielded.
            children = []
            for item in raw_items:
                try:
                    children.append(Resource.from_json(item))
                except ApiError:
                    invalid_count += 1
            # Sort locally as well as remotely, for deterministic processing.
            children.sort(key=lambda resource: (resource.name, resource.path))
            for child in children:
                if child.path in seen:
                    raise _invalid_response("повторный путь в списке папки")
                seen.add(child.path)
                yield child
            # Advance by the number actually received, not the requested limit.
            offset += len(raw_items)
            if offset >= total:
                finish_listing()
                return

    def download_url(self, path: str) -> str:
        data = self._request_json(self._endpoint(download=True), self._path_params(path))
        href = data.get("href")
        if not isinstance(href, str) or not href:
            raise _invalid_response("отсутствует ссылка для скачивания")
        try:
            parsed = urlsplit(href)
            valid = parsed.scheme == "https" and bool(parsed.hostname)
            valid = valid and parsed.username is None and parsed.password is None
            valid = valid and not any(ord(char) < 32 for char in href)
            # Accessing port also validates malformed host/port combinations.
            parsed.port
        except ValueError:
            valid = False
        if not valid or data.get("method") != "GET" or data.get("templated", False) is not False:
            raise _invalid_response("неверная ссылка для скачивания")
        return href

    def preview_url(self, path: str, size: str = "XXXL") -> str | None:
        """Request a fresh preview only when an original could not be saved.

        Preview metadata is deliberately absent from Resource and its version
        identity: expiring signed links must not invalidate completed originals.
        """
        if size not in ("S", "M", "L", "XL", "XXL", "XXXL"):
            raise ValueError("Неизвестный размер превью.")
        params = self._path_params(path)
        params.update(limit="0", fields="preview", preview_size=size, preview_crop="false")
        data = self._request_json(self._endpoint(), params)
        href = data.get("preview")
        if href is None:
            return None
        _preview_location(href)
        return href

    @contextmanager
    def open_preview(self, url: str, *, timeout: float | None = None) -> Iterator[Any]:
        """Open a preview with tightly scoped OAuth and sanitized open errors.

        Redirects are handled explicitly because urllib would otherwise copy
        Authorization across hosts. Once a redirect leaves the trusted preview
        origin, OAuth is permanently removed, even if a later hop returns there.
        The caller owns streaming limits, content validation, and cancellation
        while reading the yielded response.
        """
        parsed = _preview_location(url)
        actual_timeout = self.timeout if timeout is None else timeout
        if (
            not isinstance(actual_timeout, (int, float))
            or not math.isfinite(actual_timeout)
            or actual_timeout <= 0
        ):
            raise ValueError("Таймаут должен быть положительным числом.")
        authorized = self._token is not None and _authenticated_preview(parsed)
        for hop in range(MAX_PREVIEW_REDIRECTS + 1):
            if self.stop_event is not None and self.stop_event.is_set():
                raise ApiError("Операция остановлена.")
            headers = {"Accept": "image/*", "User-Agent": "DiskDownloader/0.1"}
            if authorized:
                headers["Authorization"] = "OAuth " + self._token  # type: ignore[operator]
            request = Request(url, headers=headers, method="GET")
            try:
                response = self._preview_opener.open(request, timeout=float(actual_timeout))
            except HTTPError as exc:
                status = exc.code
                if status in (301, 302, 303, 307, 308):
                    location = exc.headers.get("Location") if exc.headers is not None else None
                    exc.close()
                    if hop >= MAX_PREVIEW_REDIRECTS or not isinstance(location, str) or not location:
                        raise _invalid_response("слишком много перенаправлений или отсутствует адрес превью") from None
                    # Validate before urljoin, which can silently remove control
                    # characters from a malicious redirect target.
                    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in location):
                        raise _invalid_response("неверная ссылка на превью") from None
                    try:
                        next_url = urljoin(url, location)
                    except ValueError:
                        raise _invalid_response("неверная ссылка на превью") from None
                    next_parsed = _preview_location(next_url)
                    authorized = authorized and _authenticated_preview(next_parsed)
                    url, parsed = next_url, next_parsed
                    continue
                exc.close()
                raise ApiError(
                    f"Превью недоступно (HTTP {status}).",
                    status=status,
                    retryable=status == 429 or 500 <= status <= 599,
                ) from None
            except (URLError, OSError, http.client.HTTPException):
                raise ApiError("Не удалось получить превью Яндекс Диска.", retryable=True) from None
            try:
                yield response
            finally:
                response.close()
            return
        raise AssertionError("unreachable")

    @staticmethod
    def _retry_delay(headers: Mapping[str, str] | None, attempt: int) -> float:
        default = min(2.0 ** attempt, MAX_RETRY_DELAY)
        if headers is None:
            return default
        raw = headers.get("Retry-After")
        if raw is None:
            return default
        try:
            delay = float(raw)
            if not math.isfinite(delay):
                return default
        except (TypeError, ValueError):
            try:
                date = parsedate_to_datetime(raw)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                delay = (date - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return default
        return min(max(delay, 0.0), MAX_RETRY_DELAY)

    def _request_json(self, endpoint: str, params: Mapping[str, str]) -> dict[str, Any]:
        url = API_BASE + endpoint + "?" + urlencode(params)
        headers = {"Accept": "application/json", "User-Agent": "DiskDownloader/0.1"}
        if self._token is not None:
            headers["Authorization"] = "OAuth " + self._token
        request = Request(url, headers=headers, method="GET")
        for attempt in range(self.retries + 1):
            if self.stop_event is not None and self.stop_event.is_set():
                raise ApiError("Операция остановлена.")
            retry_headers = None
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    body = response.read(MAX_JSON_BYTES + 1)
                if len(body) > MAX_JSON_BYTES:
                    raise _invalid_response("слишком большой ответ API")
                try:
                    data = json.loads(body)
                except (ValueError, UnicodeError):
                    raise _invalid_response("ожидался JSON") from None
                if not isinstance(data, dict):
                    raise _invalid_response("ожидался объект JSON")
                return data
            except HTTPError as exc:
                status = exc.code
                retryable = status == 429 or 500 <= status <= 599
                retry_headers = exc.headers
                exc.close()
                explanations = {
                    401: "проверьте OAuth-токен",
                    403: "нет доступа или разрешения на скачивание",
                    404: "ресурс отсутствует или временно недоступен",
                    429: "превышен лимит запросов",
                }
                explanation = explanations.get(status, "запрос не выполнен")
                error = ApiError(
                    f"Яндекс Диск: HTTP {status}, {explanation}.",
                    status=status,
                    retryable=retryable,
                )
            except (URLError, OSError, http.client.HTTPException):
                error = ApiError("Не удалось получить ответ от Яндекс Диска.", retryable=True)
            if not error.retryable or attempt >= self.retries:
                raise error from None
            delay = self._retry_delay(retry_headers, attempt)
            self._log(f"API временно недоступен; повтор через {delay:g} с.")
            if self.stop_event is not None:
                if self.stop_event.wait(delay):
                    raise ApiError("Операция остановлена.")
            else:
                time.sleep(delay)
        raise AssertionError("unreachable")
