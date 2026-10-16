"""Normalize common OAuth clipboard formats without exposing credentials."""
from __future__ import annotations

from collections import Counter
import re
from urllib.parse import parse_qs, urlsplit


class TokenError(ValueError):
    """A safe explanation of invalid token input, without its contents."""


_FORMATTING = frozenset(("\ufeff", "\u200b", "\u2060"))
_MASK_CHARACTERS = frozenset(".*•●∙·◦▪█")
_PREFIX = re.compile(r"^(?:Authorization\s*:\s*)?OAuth\s+", re.IGNORECASE)
_OAUTH_HOSTS = frozenset(("oauth.yandex.ru", "oauth.yandex.com"))


def _compact(value: str) -> str:
    return "".join(character for character in value
                   if not character.isspace() and character not in _FORMATTING)


def _parameter_token(parameters: str) -> str:
    try:
        # A literal '+' is part of some bearer tokens, never a token separator.
        # Preserve it even when a copied fragment omitted percent-encoding.
        values = parse_qs(parameters.replace("+", "%2B"), keep_blank_values=True,
                          encoding="utf-8", errors="strict").get("access_token", [])
    except (ValueError, UnicodeError):
        raise TokenError("Не удалось прочитать токен из ссылки. Скопируйте сам токен на странице Яндекса.") from None
    if not values:
        raise TokenError("В ссылке нет access_token. Скопируйте сам токен на странице Яндекса.")
    if len(values) != 1:
        raise TokenError("В ссылке несколько значений токена. Скопируйте сам токен на странице Яндекса.")
    return values[0]


def normalize_token(value: str) -> str:
    """Return one printable ASCII token from text, an OAuth prefix or URL.

    OAuth tokens have no whitespace. Clipboard line wrapping, Unicode spaces,
    BOM, zero-width space and word joiner can therefore be removed safely.
    Other characters are never silently translated or stripped. Errors name
    invalid code points and their counts, but never include a token excerpt.
    """
    if not isinstance(value, str):
        raise TokenError("Токен должен быть строкой.")
    value = "".join(character for character in value if character not in _FORMATTING).strip()
    value = _PREFIX.sub("", value, count=1)
    value = _compact(value)

    if value.lower().startswith(("https://", "http://")):
        try:
            url = urlsplit(value)
            allowed = (url.scheme == "https" and url.hostname in _OAUTH_HOSTS
                       and url.path.rstrip("/") == "/verification_code"
                       and url.username is None and url.password is None and url.port in (None, 443))
        except ValueError:
            allowed = False
        if not allowed:
            raise TokenError("Нужен сам токен или ссылка со страницы получения токена OAuth Яндекса.")
        value = _parameter_token("&".join(part for part in (url.query, url.fragment) if part))
    elif value.startswith("access_token="):
        value = _parameter_token(value)

    value = _compact(value)
    if not value:
        raise TokenError("Токен не указан. Скопируйте его на странице Яндекса.")
    if len(value) > 65536:
        raise TokenError("Вставленный текст слишком длинный для токена. Скопируйте только токен.")
    if all(character in _MASK_CHARACTERS for character in value):
        raise TokenError("Скопирован скрытый токен (точки или звёздочки). На странице Яндекса нажмите кнопку копирования токена.")
    if "\"" in value or "'" in value:
        raise TokenError("Токен содержит кавычки. Скопируйте значение без кавычек.")
    invalid = Counter(ord(character) for character in value if not 33 <= ord(character) <= 126)
    if invalid:
        groups = sorted(invalid.items())
        description = ", ".join(f"U+{codepoint:04X} ({count})" for codepoint, count in groups[:4])
        if len(groups) > 4:
            description += f", и ещё {len(groups) - 4} типов"
        raise TokenError("Токен содержит недопустимые символы: " + description + ". Скопируйте токен кнопкой на странице Яндекса.")
    return value
