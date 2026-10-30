from datetime import datetime, timedelta
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import main
from backend.auth import GUEST_COOKIE, SESSION_COOKIE, create_session, hash_password
from backend.db import Base, get_session
from backend.models import AdminCredential, GuestSession, HistoryItem


class GuestAccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        with self.sessions() as session:
            session.add(AdminCredential(id=1, username="admin", password_hash=hash_password("admin-test-password"), must_change_password=False))
            session.commit()

        def session_override():
            with self.sessions() as session:
                yield session

        main.app.dependency_overrides[get_session] = session_override
        self.session_patch = patch.object(main, "new_session", self.sessions)
        self.session_patch.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://testserver")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.session_patch.stop()
        main.app.dependency_overrides.clear()
        self.engine.dispose()

    async def enter_guest(self) -> dict:
        response = await self.client.post("/auth/guest", json={})
        self.assertEqual(200, response.status_code, response.text)
        payload = response.json()
        if len(response.headers.get_list("set-cookie")) > 1:
            self.assertIn("HttpOnly", response.headers.get_list("set-cookie")[0])
        self.client.headers["X-CSRF-Token"] = payload["csrf_token"]
        return payload

    async def enter_admin(self, *, must_change: bool = False) -> str:
        with self.sessions() as session:
            credential = session.get(AdminCredential, 1)
            credential.must_change_password = must_change
            session.commit()
            token, row = create_session(session, credential)
            csrf = row.csrf_token
        self.client.cookies.set(SESSION_COOKIE, token)
        self.client.headers["X-CSRF-Token"] = csrf
        return token

    async def test_anonymous_requires_entry_but_login_and_branding_remain_public(self) -> None:
        for path in ("/queue", "/history", "/admin/settings", "/qqmusic/search/songs?keywords=test"):
            response = await self.client.get(path)
            self.assertEqual(401, response.status_code, path)
        self.assertEqual("none", (await self.client.get("/auth/status")).json()["role"])
        self.assertEqual(200, (await self.client.get("/config/public")).status_code)

    async def test_guest_session_survives_reload_and_is_hashed_in_database(self) -> None:
        payload = await self.enter_guest()
        self.assertEqual("guest", payload["role"])
        self.assertTrue(payload["authenticated"])
        self.assertNotIn(SESSION_COOKIE, self.client.cookies)
        cookie = self.client.cookies.get(GUEST_COOKIE)
        with self.sessions() as session:
            row = session.scalar(select(GuestSession))
            self.assertNotEqual(cookie, row.token_hash)
        status = (await self.client.get("/auth/status")).json()
        self.assertEqual("guest", status["role"])
        self.assertEqual(payload["csrf_token"], status["csrf_token"])
        await self.enter_guest()
        with self.sessions() as session:
            self.assertEqual(1, len(session.scalars(select(GuestSession)).all()))

    async def test_guest_can_search_read_queue_and_request_a_song(self) -> None:
        await self.enter_guest()
        with patch.object(main.qqmusic, "search_songs_simple", AsyncMock(return_value=[{"mid": "song-mid", "name": "test-song"}])):
            response = await self.client.get("/qqmusic/search/songs?keywords=test")
        self.assertEqual(200, response.status_code)
        self.assertEqual("song-mid", response.json()["songs"][0]["mid"])
        self.assertEqual([], (await self.client.get("/queue")).json())
        with patch.object(main, "_enqueue_qqmusic_song", AsyncMock(return_value=(42, False))) as enqueue:
            response = await self.client.post("/queue/qqmusic", json={"song_mid": "song-mid", "title": "test-song", "play_now": False})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(42, response.json()["id"])
        self.assertFalse(enqueue.call_args.kwargs["play_now"])
        self.assertEqual("web_guest", enqueue.call_args.kwargs["requested_by"])

    async def test_guest_writes_require_their_own_csrf_token(self) -> None:
        await self.enter_guest()
        self.client.headers.pop("X-CSRF-Token")
        with patch.object(main, "_enqueue_qqmusic_song", AsyncMock()) as enqueue:
            response = await self.client.post("/queue/qqmusic", json={"song_mid": "song-mid", "title": "test-song"})
        self.assertEqual(403, response.status_code)
        enqueue.assert_not_awaited()

    async def test_guest_cannot_promote_their_token_to_an_administrator(self) -> None:
        await self.enter_guest()
        token = self.client.cookies.get(GUEST_COOKIE)
        self.client.cookies.clear()
        self.client.cookies.set(SESSION_COOKIE, token)
        self.assertEqual("none", (await self.client.get("/auth/status")).json()["role"])
        self.assertEqual(401, (await self.client.get("/admin/settings")).status_code)

    async def test_guest_request_still_requires_saved_music_authorization(self) -> None:
        await self.enter_guest()
        with patch.object(main.qqmusic, "get_music_url_simple", AsyncMock()) as get_url:
            response = await self.client.post("/queue/qqmusic", json={"song_mid": "test", "title": "test"})
        self.assertEqual(400, response.status_code)
        self.assertEqual([], (await self.client.get("/queue")).json())
        get_url.assert_not_awaited()

    async def test_guest_cannot_manage_settings_authorization_updates_or_playback(self) -> None:
        await self.enter_guest()
        operations = [
            ("GET", "/admin/settings", None),
            ("GET", "/admin/logs/bot", None),
            ("GET", "/admin/version", None),
            ("GET", "/admin/update/check", None),
            ("POST", "/admin/update", {"version": "v9.9.9"}),
            ("PUT", "/admin/settings", {"values": {}}),
            ("DELETE", "/admin/qqmusic/cookie", None),
            ("POST", "/qqmusic/login/cookie", {"cookie": "test-only"}),
            ("GET", "/qqmusic/login/qr/key", None),
            ("POST", "/qqmusic/login/qr/check", {"qr_key": "test", "ptqrtoken": "test"}),
            ("POST", "/qqmusic/login/refresh", {}),
            ("GET", "/qqmusic/user/info", None),
            ("GET", "/qqmusic/user/playlists", None),
            ("GET", "/qqmusic/song/test/url", None),
            ("POST", "/voice/play", {}),
            ("POST", "/voice/pause", {}),
            ("POST", "/voice/next", {}),
            ("PUT", "/voice/volume", {"volume_percent": 1}),
            ("PUT", "/voice/fx", {"pan": 0}),
            ("POST", "/queue", {"track_id": "qqmusic:test", "title": "test", "source_url": "https://example.test/audio"}),
            ("DELETE", "/queue", None),
            ("DELETE", "/queue/1", None),
            ("POST", "/queue/1/play", {}),
            ("POST", "/queue/1/prioritize", {}),
            ("POST", "/auth/change-password", {"current_password": "test", "new_password": "new-test-password"}),
        ]
        with patch.object(main.qqmusic, "set_cookie") as set_cookie:
            for method, path, body in operations:
                response = await self.client.request(method, path, json=body)
                self.assertEqual(403, response.status_code, (method, path, response.text))
        set_cookie.assert_not_called()

    async def test_guest_cannot_interrupt_playback_via_enqueue_or_history(self) -> None:
        await self.enter_guest()
        with patch.object(main, "_enqueue_qqmusic_song", AsyncMock()) as enqueue:
            response = await self.client.post("/queue/qqmusic", json={"song_mid": "test", "title": "test", "play_now": True})
        self.assertEqual(403, response.status_code)
        enqueue.assert_not_awaited()
        self.assertEqual(403, (await self.client.post("/history/1/replay")).status_code)
        with self.sessions() as session:
            session.add(HistoryItem(id=1, track_id="qqmusic:test", title="test", source_url=""))
            session.commit()
        with patch.object(main, "_replay_history_item", AsyncMock(return_value={"ok": True})) as replay:
            response = await self.client.post("/history/1/replay?play_now=false")
        self.assertEqual(200, response.status_code)
        self.assertFalse(replay.call_args.kwargs["play_now"])

    async def test_expired_forged_and_logged_out_guest_sessions_cannot_be_used(self) -> None:
        await self.enter_guest()
        old_cookie = self.client.cookies.get(GUEST_COOKIE)
        self.assertEqual(200, (await self.client.post("/auth/logout")).status_code)
        self.assertEqual("none", (await self.client.get("/auth/status")).json()["role"])
        self.client.cookies.set(GUEST_COOKIE, old_cookie)
        self.assertEqual(401, (await self.client.get("/queue")).status_code)
        self.client.cookies.clear()
        await self.enter_guest()
        with self.sessions() as session:
            row = session.scalar(select(GuestSession))
            row.expires_at = datetime.now() - timedelta(seconds=1)
            session.commit()
        self.assertEqual(401, (await self.client.get("/queue")).status_code)
        self.client.cookies.clear()
        self.client.cookies.set(GUEST_COOKIE, "forged-token")
        self.assertEqual(401, (await self.client.get("/queue")).status_code)

    async def test_admin_retains_control_and_still_requires_csrf(self) -> None:
        await self.enter_admin()
        self.assertEqual("admin", (await self.client.get("/auth/status")).json()["role"])
        self.assertEqual(200, (await self.client.get("/admin/settings")).status_code)
        with patch.object(main.voice, "pause", AsyncMock()), patch.object(main, "_mark_playback_paused", AsyncMock()):
            self.assertEqual(200, (await self.client.post("/voice/pause")).status_code)
        self.client.headers.pop("X-CSRF-Token")
        self.assertEqual(403, (await self.client.post("/voice/pause")).status_code)

    async def test_admin_first_password_change_is_not_bypassed(self) -> None:
        await self.enter_admin(must_change=True)
        self.assertTrue((await self.client.get("/auth/status")).json()["must_change_password"])
        self.assertEqual(403, (await self.client.get("/queue")).status_code)
        self.assertEqual(200, (await self.client.post("/auth/logout")).status_code)

    async def test_admin_can_complete_required_password_change(self) -> None:
        old_token = await self.enter_admin(must_change=True)
        with patch.object(main, "remove_initial_password_file"):
            response = await self.client.post("/auth/change-password", json={
                "current_password": "admin-test-password", "new_password": "new-admin-test-password",
            })
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("admin", response.json()["role"])
        self.assertFalse(response.json()["must_change_password"])
        self.client.cookies.clear()
        self.client.cookies.set(SESSION_COOKIE, old_token)
        self.assertEqual(401, (await self.client.get("/admin/settings")).status_code)

    async def test_guest_can_upgrade_to_admin_and_old_guest_token_is_invalidated(self) -> None:
        await self.enter_guest()
        cookie = self.client.cookies.get(GUEST_COOKIE)
        response = await self.client.post("/auth/login", json={"username": "admin", "password": "admin-test-password"})
        self.assertEqual(200, response.status_code)
        self.assertEqual("admin", response.json()["role"])
        self.assertEqual("admin", (await self.client.get("/auth/status")).json()["role"])
        self.client.cookies.clear()
        self.client.cookies.set(GUEST_COOKIE, cookie)
        self.assertEqual(401, (await self.client.get("/queue")).status_code)

    async def test_external_api_cannot_bypass_guest_permissions_without_configured_token(self) -> None:
        await self.enter_guest()
        with patch.object(main.settings, "api_token", ""), patch.object(main.settings, "api_tokens", ""):
            self.assertEqual(401, (await self.client.post("/external/player/action", json={"action": "pause"})).status_code)
        with patch.object(main.settings, "api_token", "external-test-token"), patch.object(main.settings, "api_tokens", ""), patch.object(main, "voice_pause", AsyncMock(return_value={"ok": True})) as pause:
            self.assertEqual(401, (await self.client.post("/external/player/action", json={"action": "pause"})).status_code)
            for headers in ({"Authorization": "Bearer external-test-token"}, {"x-api-token": "external-test-token"}):
                response = await self.client.post("/external/player/action", json={"action": "pause"}, headers=headers)
                self.assertEqual(200, response.status_code)
            self.assertEqual(2, pause.await_count)


if __name__ == "__main__":
    unittest.main()
