import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from backend import main
from backend.qqmusic import QQMusicClient, QQMusicLoginError

AUTH_URL = "https://ssl.ptlogin2.graph.qq.com/check_sig?uin=123456&ptsigx=secret-signature"
REAL_CLIENT = httpx.AsyncClient


class QQMusicQRDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def transport(self, complete=True):
        def handle(request):
            if request.url.path == "/check_sig":
                return httpx.Response(302, headers={"set-cookie": "p_skey=secret-pskey; Path=/", "location": "https://graph.qq.com/oauth2.0/login_jump"})
            if request.url.path == "/oauth2.0/authorize":
                self.assertIn(b"client_id=100497308", request.content)
                return httpx.Response(302, headers={"location": "https://y.qq.com/portal/wx_redirect.html?code=secret-code"})
            data = {"musicid": 123456, "musickey": "secret-musickey"} if complete else {}
            body = json.loads(request.content)
            self.assertEqual("QQConnectLogin.LoginServer", body["req_0"]["module"])
            return httpx.Response(200, json={"code": 0, "req_0": {"code": 0, "data": data}})
        return httpx.MockTransport(handle)

    async def test_confirmation_exchanges_music_credentials_without_logging_secrets(self):
        client = QQMusicClient()
        client._session_cookies = {"qrsig": "secret-qr-value"}
        transport = self.transport()
        with patch("backend.qqmusic.httpx.AsyncClient", side_effect=lambda **kwargs: REAL_CLIENT(transport=transport, **kwargs)), self.assertLogs("tsbot-backend", level="INFO") as logs:
            await client.confirm_qr_login(AUTH_URL)
        self.assertIn("secret-musickey", client.get_cookie())
        self.assertNotIn("qrsig", client.get_cookie())
        self.assertEqual("123456", client.get_uin())
        output = "\n".join(logs.output)
        for sensitive in ("123456", "secret-qr-value", "secret-code", "secret-musickey", "secret-pskey", "secret-signature"):
            self.assertNotIn(sensitive, output)

    async def test_incomplete_exchange_preserves_previous_cookie(self):
        client = QQMusicClient()
        client.set_cookie("uin=999; qm_keyst=old-key")
        transport = self.transport(complete=False)
        with patch("backend.qqmusic.httpx.AsyncClient", side_effect=lambda **kwargs: REAL_CLIENT(transport=transport, **kwargs)):
            with self.assertRaises(QQMusicLoginError):
                await client.confirm_qr_login(AUTH_URL)
        self.assertEqual("uin=999; qm_keyst=old-key", client.get_cookie())

    async def test_invalid_callback_never_receives_session_cookies(self):
        with patch("backend.qqmusic.httpx.AsyncClient") as network:
            with self.assertRaises(QQMusicLoginError):
                await QQMusicClient().confirm_qr_login("https://example.test/?uin=123&ptsigx=sig")
        network.assert_not_called()

    async def test_post_check_forwards_parameters(self):
        request = main.QQMusicQRCheckRequest(qr_key="qr", ptqrtoken="token", pt_login_sig="sig")
        with patch.object(main, "qqmusic_qr_check", new_callable=AsyncMock, return_value={"status": "waiting"}) as check:
            result = await main.qqmusic_qr_check_post(request)
        check.assert_awaited_once_with("qr", "token", "sig")
        self.assertEqual({"status": "waiting"}, result)

    async def test_confirm_failure_logs_exception_type_without_details(self):
        candidate = QQMusicClient()
        candidate.confirm_qr_login = AsyncMock(side_effect=RuntimeError("secret-details"))
        with patch.object(main, "_require_admin_token"), patch.object(main, "QQMusicClient", return_value=candidate), self.assertLogs("tsbot-backend", level="WARNING") as logs:
            with self.assertRaises(main.HTTPException):
                await main.admin_qqmusic_qr_confirm(main.QQMusicQRConfirmRequest(auth_url=AUTH_URL), object(), object())
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertNotIn("secret-details", "\n".join(logs.output))
