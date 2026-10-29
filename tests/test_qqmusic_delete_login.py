import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import main
from backend.db import Base
from backend.models import Secret
from backend.qqmusic import QQMusicClient


class QQMusicDeleteLoginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine)()
        self.client = QQMusicClient()
        self.client.set_cookie("uin=12345; qm_keyst=test")
        self.client._session_cookies = {"qrsig": "test"}
        self.session.add(Secret(key="qqmusic_cookie", value="encrypted"))
        self.session.add(Secret(key="other_secret", value="keep"))
        self.session.commit()

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    async def test_delete_clears_saved_and_in_memory_login_and_is_idempotent(self) -> None:
        with patch.object(main, "_require_admin_token") as require_admin, patch.object(main, "qqmusic", self.client):
            result = await main.admin_qqmusic_delete_cookie(object(), self.session)
            repeated = await main.admin_qqmusic_delete_cookie(object(), self.session)

        self.assertEqual({"ok": True, "admin_cookie_set": False}, result)
        self.assertEqual(result, repeated)
        self.assertEqual(2, require_admin.call_count)
        self.assertIsNone(self.session.get(Secret, "qqmusic_cookie"))
        self.assertIsNotNone(self.session.get(Secret, "other_secret"))
        self.assertEqual("", self.client.get_cookie())
        self.assertEqual("", self.client.get_uin())
        self.assertEqual({}, self.client._session_cookies)

    async def test_unauthorized_request_does_not_clear_login(self) -> None:
        with patch.object(main, "_require_admin_token", side_effect=HTTPException(status_code=401)):
            with self.assertRaises(HTTPException):
                await main.admin_qqmusic_delete_cookie(object(), self.session)

        self.assertIsNotNone(self.session.get(Secret, "qqmusic_cookie"))
        self.assertTrue(self.client.get_cookie())
