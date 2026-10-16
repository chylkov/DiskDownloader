"""Normalize clipboard wrapping and diagnose errors without printing secrets."""
from __future__ import annotations

from urllib.parse import quote
import unittest

from yandex_disk.token import TokenError, normalize_token


TOKEN = "y0_Test_abcdefghijklmnopqrstuvwxyz-0123456789"


class NormalizeTokenTests(unittest.TestCase):
    def test_plain_token_and_wrapped_unicode_whitespace_are_normalized(self):
        variants = (
            TOKEN, "  " + TOKEN + "\r\n",
            TOKEN[:10] + "\n\t " + TOKEN[10:],
            "\ufeff" + TOKEN[:8] + "\u200b\u2060" + TOKEN[8:] + "\ufeff",
            TOKEN[:6] + "\u00a0\u202f\u3000" + TOKEN[6:],
        )
        for index, variant in enumerate(variants):
            with self.subTest(variant=index):
                self.assertEqual(normalize_token(variant), TOKEN)

    def test_oauth_authorization_prefix_is_removed_before_compacting(self):
        for prefix in ("OAuth ", "oauth \n ", "Authorization: OAuth ", "authorization : oauth\t"):
            self.assertEqual(normalize_token(prefix + TOKEN), TOKEN)
        self.assertEqual(normalize_token("OAuthRealToken"), "OAuthRealToken")

    def test_yandex_verification_url_fragment_or_query_is_decoded(self):
        for host in ("oauth.yandex.ru", "oauth.yandex.com"):
            for separator in ("#", "?"):
                value = f"https://{host}/verification_code{separator}access_token={quote(TOKEN)}&token_type=bearer&expires_in=31536000"
                self.assertEqual(normalize_token(value), TOKEN)
        encoded = "https://oauth.yandex.ru:443/verification_code/#access_token=y0%5FTest%2D123"
        self.assertEqual(normalize_token(encoded), "y0_Test-123")

    def test_parameter_form_and_percent_encoded_whitespace(self):
        self.assertEqual(normalize_token("access_token=" + TOKEN + "&expires_in=1&token_type=bearer"), TOKEN)
        self.assertEqual(normalize_token("access_token=y0%5FTest%0A123%C2%A0"), "y0_Test123")

    def test_literal_and_encoded_plus_preserve_the_same_credential(self):
        token = "test+base64/value=="
        for encoded in (token, "test%2Bbase64%2Fvalue%3D%3D"):
            self.assertEqual(normalize_token("access_token=" + encoded + "&expires_in=1"), token)

    def test_masked_placeholder_diagnosis(self):
        for mask in ("....", "****", "••••", "●●●●", "·•.*", "▪█◦∙"):
            with self.subTest(mask_type=ord(mask[0])):
                with self.assertRaises(TokenError) as raised:
                    normalize_token(mask)
                self.assertIn("скрытый токен", str(raised.exception))
                self.assertIn("копирования", str(raised.exception))

    def test_visible_cyrillic_is_rejected_with_safe_codepoint_count(self):
        value = TOKEN[:5] + "аа" + TOKEN[5:] + "б"
        with self.assertRaises(TokenError) as raised:
            normalize_token(value)
        error = str(raised.exception)
        self.assertIn("U+0430 (2)", error)
        self.assertIn("U+0431 (1)", error)
        self.assertNotIn(TOKEN[:5], error)
        self.assertNotIn(value, error)

    def test_controls_visible_joiners_and_quotes_are_not_silently_removed(self):
        for unwanted in ("\x00", "\x7f", "\u200d", "\u200c", "\u202e", '"', "'"):
            with self.subTest(codepoint=ord(unwanted)):
                with self.assertRaises(TokenError) as raised:
                    normalize_token(TOKEN + unwanted)
                self.assertNotIn(TOKEN, str(raised.exception))

    def test_empty_invalid_type_and_long_input_are_distinct_and_safe(self):
        for value, expected in (("\ufeff\u200b \t\n", "не указан"), (None, "строкой"), ("x" * 65537, "слишком длинный")):
            with self.assertRaises(TokenError) as raised:
                normalize_token(value)
            self.assertIn(expected, str(raised.exception))

    def test_unknown_or_unsafe_url_is_rejected_without_echoing_it(self):
        for prefix in ("http://oauth.yandex.ru", "https://example.com", "https://oauth.yandex.ru.evil.example", "https://user@oauth.yandex.ru", "https://oauth.yandex.ru:444"):
            value = prefix + "/verification_code#access_token=" + TOKEN
            with self.assertRaises(TokenError) as raised:
                normalize_token(value)
            self.assertNotIn(TOKEN, str(raised.exception))
            self.assertNotIn(prefix, str(raised.exception))

    def test_missing_duplicate_empty_and_invalid_encoded_parameters_are_safe(self):
        for parameters, expected in (("expires_in=1", "нет access_token"),
                                     ("access_token=&expires_in=1", "не указан"),
                                     ("access_token=" + TOKEN + "&access_token=another", "несколько"),
                                     ("access_token=%FF", "Не удалось")):
            with self.subTest(kind=expected):
                with self.assertRaises(TokenError) as raised:
                    normalize_token("https://oauth.yandex.ru/verification_code#" + parameters)
                self.assertIn(expected, str(raised.exception))
                self.assertNotIn(TOKEN, str(raised.exception))

    def test_duplicate_token_across_query_and_fragment_is_rejected(self):
        with self.assertRaises(TokenError):
            normalize_token("https://oauth.yandex.ru/verification_code?access_token=first#access_token=second")

    def test_diagnostics_are_bounded_even_for_many_invalid_character_types(self):
        with self.assertRaises(TokenError) as raised:
            normalize_token("".join(chr(0x0400 + index) for index in range(500)))
        self.assertLess(len(str(raised.exception)), 300)
        self.assertIn("ещё", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
