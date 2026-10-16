"""Clipboard cleanup preserves private API headers and recovery identity."""
import unittest

from yandex_disk.api import YandexDiskAPI
from yandex_disk.token import TokenError


class APITokenTests(unittest.TestCase):
    def test_formatted_token_uses_original_source_identity(self):
        token = "y0_Original_Test_Token123"
        original = YandexDiskAPI(disk_path="/Фото", token=token)
        formatted = YandexDiskAPI(disk_path="/Фото", token="\ufeffOAuth " + token[:10] + "\u200b\n" + token[10:])
        self.assertEqual(formatted.source_id, original.source_id)
        self.assertEqual(formatted._token, token)

    def test_copied_verification_url_extracts_original_token(self):
        token = "y0_Original_Test_Token123"
        original = YandexDiskAPI(disk_path="/Фото", token=token)
        copied_url = "https://oauth.yandex.ru/verification_code#access_token=" + token + "&expires_in=31536000"
        formatted = YandexDiskAPI(disk_path="/Фото", token=copied_url)
        self.assertEqual(formatted.source_id, original.source_id)
        self.assertEqual(formatted._token, token)

    def test_invalid_token_error_explains_symbols_without_contents(self):
        invalid = "SECRET_TEST_Я"
        with self.assertRaises(TokenError) as raised:
            YandexDiskAPI(disk_path="/Фото", token=invalid)
        self.assertIn("U+042F", str(raised.exception))
        self.assertNotIn("SECRET_TEST", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
