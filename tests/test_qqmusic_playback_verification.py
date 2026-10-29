import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import main
from backend.db import Base
from backend.qqmusic import QQMusicClient, QQMusicLoginError

REAL_CLIENT = httpx.AsyncClient


class QQMusicPlaybackVerificationTests(unittest.IsolatedAsyncioTestCase):
    def client(self, purl="M800test.mp3?vkey=secret-vkey", content=b"ID3" + b"audio" * 20):
        client = QQMusicClient()
        client.set_cookie("musicid=123456; musickey=secret-key")
        client.search_with_keyword = AsyncMock(return_value={"req": {"data": {"body": {"song": {"list": [
            {"mid": "test", "name": "晴天", "singer": [{"name": "周杰伦"}], "pay": {"pay_play": 1}},
        ]}}}}})
        client.get_music_url = AsyncMock(return_value={"code": 0, "req_1": {"code": 0, "data": {"sip": ["https://stream.qqmusic.qq.com/"], "midurlinfo": [{"result": 0, "purl": purl, "opi30surl": "preview"}]}}})
        def handle(request):
            self.assertNotIn("cookie", request.headers)
            self.assertEqual("bytes=0-1023", request.headers["range"])
            return httpx.Response(206, content=content)
        return client, httpx.MockTransport(handle)

    async def test_full_address_is_probed_without_exposing_url_or_secrets(self):
        client, transport = self.client()
        with patch("backend.qqmusic.httpx.AsyncClient", side_effect=lambda **kwargs: REAL_CLIENT(transport=transport, **kwargs)), self.assertLogs("tsbot-backend", level="INFO") as logs:
            result = await client.verify_playback()
        self.assertEqual("passed", result["status"])
        self.assertEqual("晴天", result["song"])
        for sensitive in ("secret-vkey", "secret-key", "123456"):
            self.assertNotIn(sensitive, json.dumps(result) + "\n".join(logs.output))

    async def test_preview_is_not_accepted_as_a_success(self):
        client, _ = self.client(purl="")
        with patch("backend.qqmusic.httpx.AsyncClient") as network:
            result = await client.verify_playback()
        self.assertEqual("failed", result["status"])
        network.assert_not_called()

    async def test_html_instead_of_audio_is_not_accepted(self):
        client, transport = self.client(content=b"<html>Access denied</html>")
        with patch("backend.qqmusic.httpx.AsyncClient", side_effect=lambda **kwargs: REAL_CLIENT(transport=transport, **kwargs)):
            result = await client.verify_playback()
        self.assertEqual("failed", result["status"])

    async def test_network_failure_does_not_claim_the_cookie_is_invalid(self):
        client, _ = self.client()
        client.get_music_url.side_effect = httpx.ConnectError("secret-url")
        result = await client.verify_playback()
        self.assertEqual("error", result["status"])
        self.assertNotIn("secret-url", json.dumps(result))

    async def test_missing_music_credentials_is_rejected_before_song_requests(self):
        client, _ = self.client()
        client.set_cookie("qrsig=temporary")
        self.assertEqual("failed", (await client.verify_playback())["status"])
        client.search_with_keyword.assert_not_called()

    async def test_permission_denial_is_not_accepted(self):
        client, _ = self.client()
        client.get_music_url.return_value["req_1"]["data"]["midurlinfo"][0]["result"] = 104003
        with patch("backend.qqmusic.httpx.AsyncClient") as network:
            self.assertEqual("failed", (await client.verify_playback())["status"])
        network.assert_not_called()

    async def test_vkey_uses_media_mid_once_and_music_authentication(self):
        client = QQMusicClient()
        client.set_cookie("musicid=123; musickey=music-key")
        client._post = AsyncMock(side_effect=[{"detail": {"data": {"track_info": {"file": {"media_mid": "media-id"}}}}}, {}])
        await client.get_music_url("song-id")
        request = json.loads(client._post.await_args_list[1].args[1])
        self.assertEqual(["M800media-id.mp3"], request["req_1"]["param"]["filename"])
        self.assertEqual("music-key", request["comm"]["authst"])
        self.assertEqual("music.vkey.GetVkey", request["req_1"]["module"])


class QQMusicSavedVerificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_verification_is_bound_to_the_saved_cookie(self):
        main._save_qqmusic_login(self.session, "uin=1; qm_keyst=key", {"status": "passed", "message": "passed"})
        self.assertEqual("passed", main._saved_qqmusic_verification(self.session, "uin=1; qm_keyst=key")["status"])
        self.assertEqual("unverified", main._saved_qqmusic_verification(self.session, "uin=2; qm_keyst=other")["status"])
        self.assertNotIn("cookie_fingerprint", main._saved_qqmusic_verification(self.session, "uin=1; qm_keyst=key"))

    async def test_qr_confirm_saves_verdict_with_music_credentials(self):
        candidate = QQMusicClient()
        candidate.set_cookie("uin=123; qm_keyst=music-key")
        candidate.confirm_qr_login = AsyncMock(return_value={"ok": True})
        candidate.verify_playback = AsyncMock(return_value={"status": "passed", "message": "passed", "checked_at": 1})
        with patch.object(main, "_require_admin_token"), patch.object(main, "QQMusicClient", return_value=candidate), patch.object(main, "qqmusic", QQMusicClient()):
            result = await main.admin_qqmusic_qr_confirm(main.QQMusicQRConfirmRequest(auth_url="unused"), object(), self.session)
        self.assertTrue(result["admin_cookie_set"])
        self.assertEqual("passed", result["verification"]["status"])
        self.assertEqual("passed", main._saved_qqmusic_verification(self.session, candidate.get_cookie())["status"])
        self.assertNotIn("music-key", json.dumps(result))

    async def test_failed_qr_exchange_does_not_replace_saved_login(self):
        cookie = "uin=123; qm_keyst=old-music-key"
        main._save_qqmusic_login(self.session, cookie, {"status": "passed", "message": "passed"})
        candidate = QQMusicClient()
        candidate.confirm_qr_login = AsyncMock(side_effect=QQMusicLoginError("授权码缺失"))
        with patch.object(main, "_require_admin_token"), patch.object(main, "QQMusicClient", return_value=candidate):
            with self.assertRaises(main.HTTPException):
                await main.admin_qqmusic_qr_confirm(main.QQMusicQRConfirmRequest(auth_url="unused"), object(), self.session)
        self.assertEqual(cookie, main._get_admin_qqmusic_cookie(self.session))

    async def test_login_status_does_not_return_cookie_values(self):
        client = QQMusicClient()
        client.set_cookie("uin=123; qm_keyst=private-key")
        client.refresh_login = AsyncMock(return_value={"success": True, "message": "test"})
        with patch.object(main, "qqmusic", client):
            result = await main.qqmusic_login_status()
        self.assertNotIn("cookie", result)
        self.assertNotIn("private-key", json.dumps(result))
