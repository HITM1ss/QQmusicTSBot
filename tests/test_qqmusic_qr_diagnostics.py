import unittest
from unittest.mock import AsyncMock, patch

import httpx

from backend import main
from backend.qqmusic import QQMusicClient


class QQMusicQRDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_logs_missing_music_key_without_secrets(self) -> None:
        client = QQMusicClient()
        client._session_cookies = {"qrsig": "secret-qr-value", "uin": "123456"}
        response = httpx.Response(302, headers={"location": "https://example.test/"}, request=httpx.Request("GET", "https://example.test/"))
        transport = AsyncMock()
        transport.__aenter__.return_value.get.return_value = response
        with (
            patch("backend.qqmusic.httpx.AsyncClient", return_value=transport),
            patch.object(client, "_fetch_qqmusic_cookies", new_callable=AsyncMock),
            self.assertLogs("tsbot-backend", level="INFO") as logs,
        ):
            await client.confirm_qr_login("https://example.test/?token=secret-url-value")

        output = "\n".join(logs.output)
        self.assertIn("HTTP 302", output)
        self.assertIn("未获取到音乐密钥", output)
        for sensitive in ("secret-qr-value", "secret-url-value", "123456"):
            self.assertNotIn(sensitive, output)

    async def test_post_check_forwards_parameters(self) -> None:
        request = main.QQMusicQRCheckRequest(qr_key="qr", ptqrtoken="token", pt_login_sig="sig")
        with patch.object(main, "qqmusic_qr_check", new_callable=AsyncMock, return_value={"status": "waiting"}) as check:
            result = await main.qqmusic_qr_check_post(request)
        check.assert_awaited_once_with("qr", "token", "sig")
        self.assertEqual({"status": "waiting"}, result)

    async def test_confirm_failure_logs_exception_type_without_details(self) -> None:
        request = main.QQMusicQRConfirmRequest(auth_url="https://example.test/?secret=hidden")
        with (
            patch.object(main, "_require_admin_token"),
            patch.object(main.qqmusic, "confirm_qr_login", new_callable=AsyncMock, side_effect=RuntimeError("secret-details")),
            self.assertLogs("tsbot-backend", level="WARNING") as logs,
        ):
            with self.assertRaises(main.HTTPException):
                await main.admin_qqmusic_qr_confirm(request, object(), object())
        output = "\n".join(logs.output)
        self.assertIn("RuntimeError", output)
        self.assertNotIn("secret-details", output)
        self.assertNotIn("hidden", output)
