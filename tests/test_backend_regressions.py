import asyncio
import unittest
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend import main
from backend import serverquery
from backend.db import Base


class AdminCacheTests(unittest.TestCase):
    def test_cache_status_uses_the_generic_empty_cache_shape(self) -> None:
        with patch.object(main, "require_admin"):
            result = main.admin_cache_status(object(), object())

        self.assertEqual(0, result["total_size_bytes"])
        self.assertEqual(0, result["total_file_count"])
        self.assertEqual({"size_bytes": 0, "file_count": 0}, result["cache"])
        self.assertEqual({}, result["memory_entries"])

    def test_clear_cache_is_a_safe_no_op_without_media_caching(self) -> None:
        with (
            patch.object(main, "require_admin", return_value=(object(), object())),
            patch.object(main, "require_csrf"),
        ):
            result = main.admin_clear_cache(object(), object())

        self.assertTrue(result["ok"])
        self.assertEqual(0, result["removed_files"])
        self.assertEqual(0, result["removed_bytes"])
        self.assertEqual(0, result["total_size_bytes"])


class AdminLogTests(unittest.IsolatedAsyncioTestCase):
    def test_clear_bot_logs_preserves_open_append_writers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend_log = root / "backend.log"
            voice_log = root / "voice.log"
            untouched = root / "other.txt"
            backend_log.write_text("old backend history\n")
            voice_log.write_text("old voice history\n")
            untouched.write_text("preserve this file")
            inodes = (backend_log.stat().st_ino, voice_log.stat().st_ino)
            with (
                backend_log.open("ab") as backend_writer,
                voice_log.open("ab") as voice_writer,
                patch.object(main, "require_admin", return_value=(object(), object())),
                patch.object(main, "require_csrf"),
                patch.object(main.settings, "log_file", str(backend_log)),
                patch.object(main, "_voice_log_file_path", return_value=voice_log),
            ):
                self.assertTrue(main.admin_clear_logs("bot", object(), object())["ok"])
                self.assertEqual(b"", backend_log.read_bytes())
                self.assertEqual(b"", voice_log.read_bytes())
                self.assertEqual(0, main._bot_log_payload(200)["line_count"])
                backend_writer.write(b"new backend log\n")
                backend_writer.flush()
                voice_writer.write(b"new voice log\n")
                voice_writer.flush()
            self.assertEqual(b"new backend log\n", backend_log.read_bytes())
            self.assertEqual(b"new voice log\n", voice_log.read_bytes())
            self.assertEqual(inodes, (backend_log.stat().st_ino, voice_log.stat().st_ino))
            self.assertEqual("preserve this file", untouched.read_text())

    def test_clear_missing_logs_succeeds_without_creating_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend_log = Path(directory) / "backend.log"
            voice_log = Path(directory) / "voice.log"
            with (
                patch.object(main, "require_admin", return_value=(object(), object())),
                patch.object(main, "require_csrf"),
                patch.object(main.settings, "log_file", str(backend_log)),
                patch.object(main, "_voice_log_file_path", return_value=voice_log),
            ):
                self.assertTrue(main.admin_clear_logs("bot", object(), object())["ok"])
            self.assertFalse(backend_log.exists())
            self.assertFalse(voice_log.exists())

    def test_clear_remote_or_unknown_logs_does_not_touch_files_or_query_server(self) -> None:
        with (
            patch.object(main, "require_admin", return_value=(object(), object())),
            patch.object(main, "require_csrf"),
            patch.object(main, "_voice_log_file_path") as voice_path,
            patch.object(main, "query_server_logs", AsyncMock()) as query_logs,
        ):
            for source, status in (("teamspeak", 400), ("unknown", 404)):
                with self.assertRaises(HTTPException) as raised:
                    main.admin_clear_logs(source, object(), object())
                self.assertEqual(status, raised.exception.status_code)
        voice_path.assert_not_called()
        query_logs.assert_not_awaited()

    def test_clear_log_write_failure_is_reported_without_exposing_paths(self) -> None:
        with (
            patch.object(main, "require_admin", return_value=(object(), object())),
            patch.object(main, "require_csrf"),
            patch.object(Path, "open", side_effect=PermissionError("private-log-path")),
            patch.object(main.logger, "warning"),
        ):
            with self.assertRaises(HTTPException) as raised:
                main.admin_clear_logs("bot", object(), object())
        self.assertEqual(500, raised.exception.status_code)
        self.assertNotIn("private-log-path", raised.exception.detail)

    def test_bot_log_payload_combines_components_and_redacts_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backend_log = root / "backend.log"
            voice_log = root / "voice.log"
            backend_log.write_text(
                "[INFO] token=secret-token initial_password=another-secret\n"
                "首次启动管理员账号：admin，初始密码：temporary-password，同时写入文件\n",
                encoding="utf-8",
            )
            voice_log.write_text("[INFO] [voice] ts3 connected\n", encoding="utf-8")

            with (
                patch.object(main.settings, "log_file", str(backend_log)),
                patch.object(main, "_voice_log_file_path", return_value=voice_log),
            ):
                result = main._bot_log_payload(200)

        self.assertIn("===== 后端 =====", result["content"])
        self.assertIn("===== Voice =====", result["content"])
        self.assertIn("token=[已隐藏]", result["content"])
        self.assertIn("初始密码：[已隐藏]", result["content"])
        self.assertNotIn("secret-token", result["content"])
        self.assertNotIn("another-secret", result["content"])
        self.assertNotIn("temporary-password", result["content"])

    async def test_teamspeak_log_endpoint_uses_serverquery_and_redacts_content(self) -> None:
        session = unittest.mock.Mock()
        with (
            patch.object(main, "require_admin"),
            patch.object(main, "_serverquery_log_config", return_value=unittest.mock.Mock()),
            patch.object(
                main,
                "query_server_logs",
                AsyncMock(return_value=["2026-08-23 10:15:00.123456|INFO|token=secret-token"]),
            ) as query_logs,
        ):
            result = await main.admin_logs("teamspeak", object(), lines=200, session=session)

        self.assertEqual("teamspeak", result["source"])
        self.assertEqual(1, result["line_count"])
        self.assertIn("token=[已隐藏]", result["content"])
        self.assertIn("2026-08-23 18:15:00.123456|INFO|", result["content"])
        query_logs.assert_awaited_once()

    def test_serverquery_logview_rows_are_unescaped_and_chronological(self) -> None:
        entries = serverquery.parse_log_lines([
            "notifyserverlog last_pos=12 file_size=42 l=2026-08-23\\s10:02:00\\pINFO\\pnewest|l=2026-08-23\\s10:01:00\\pWARN\\polder",
        ])

        self.assertEqual(
            [
                "2026-08-23 10:01:00|WARN|older",
                "2026-08-23 10:02:00|INFO|newest",
            ],
            entries,
        )

    async def test_serverquery_logview_logs_in_selects_server_and_reads_entries(self) -> None:
        class FakeWriter:
            def __init__(self) -> None:
                self.commands: list[str] = []
                self.closed = False

            def write(self, data: bytes) -> None:
                self.commands.append(data.decode("utf-8").strip())

            async def drain(self) -> None:
                return None

            def close(self) -> None:
                self.closed = True

            async def wait_closed(self) -> None:
                return None

        reader = asyncio.StreamReader()
        reader.feed_data(
            b"TS3\nWelcome to the TeamSpeak 3 ServerQuery interface\n\n"
            b"error id=0 msg=ok\n"
            b"error id=0 msg=ok\n"
            b"notifyserverlog last_pos=10 file_size=100 "
            b"l=2026-08-23\\s10:02:00\\pINFO\\pconnected|"
            b"l=2026-08-23\\s10:01:00\\pWARN\\pretrying\n"
            b"error id=0 msg=ok\n"
        )
        reader.feed_eof()
        writer = FakeWriter()

        with patch.object(serverquery.asyncio, "open_connection", AsyncMock(return_value=(reader, writer))):
            entries = await serverquery.query_server_logs(
                serverquery.ServerQueryConfig(
                    host="127.0.0.1",
                    port=10011,
                    username="server admin",
                    password="pa ss|/word",
                    virtual_server_port=9987,
                ),
                lines=200,
            )

        self.assertEqual(
            [
                "login client_login_name=server\\sadmin client_login_password=pa\\sss\\p\\/word",
                "use port=9987",
                "logview lines=200 begin_pos=0 reverse=1 instance=0",
            ],
            writer.commands,
        )
        self.assertTrue(writer.closed)
        self.assertEqual(
            [
                "2026-08-23 10:01:00|WARN|retrying",
                "2026-08-23 10:02:00|INFO|connected",
            ],
            entries,
        )

class QQMusicNormalizationTests(unittest.TestCase):
    def test_playlist_payload_uses_songname_and_songmid_fields(self) -> None:
        normalized = main._normalize_qqmusic_song(
            {
                "songmid": "002r2KrX1JWd1pg",
                "songname": "测试歌曲",
                "singer": [{"name": "测试歌手"}],
                "albumname": "测试专辑",
                "albummid": "003albummid",
                "interval": 210,
            }
        )

        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertEqual("002r2KrX1JWd1pg", normalized["song_mid"])
        self.assertEqual("测试歌曲", normalized["title"])
        self.assertEqual("测试歌手", normalized["artist"])
        self.assertEqual("测试专辑", normalized["album"])
        self.assertEqual(210000, normalized["duration_ms"])


class TsChatCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        main._ts_playlist_results.clear()
    async def test_playlist_search_then_select_enqueues_qqmusic_tracks(self) -> None:
        search_result = {
            "req": {
                "data": {
                    "body": {
                        "diss": {
                            "list": [
                                {
                                    "dissid": "123",
                                    "dissname": "测试歌单",
                                    "creator": {"nick": "创建者"},
                                    "song_count": 2,
                                }
                            ]
                        }
                    }
                }
            }
        }
        tracks = [
            {"songmid": "mid001", "name": "歌曲一", "singer": [{"name": "歌手一"}], "album": {"mid": "alb001", "name": "专辑一"}, "interval": 180},
            {"songmid": "mid002", "name": "歌曲二", "singer": [{"name": "歌手二"}], "album": {"mid": "alb002", "name": "专辑二"}, "interval": 200},
        ]

        with (
            patch.object(main.qqmusic, "search_with_keyword", AsyncMock(return_value=search_result)) as search,
            patch.object(main, "_load_qqmusic_playlist_tracks", AsyncMock(return_value=("测试歌单", tracks))),
            patch.object(main, "_enqueue_qqmusic_song", AsyncMock(return_value=(1, False))) as enqueue,
            patch.object(main.voice, "get_status", AsyncMock(return_value=SimpleNamespace(state="STATE_PLAYING"))),
            patch.object(main.voice, "send_notice", AsyncMock()) as notice,
        ):
            await main._handle_chat_command("Alice", "playlist 测试", invoker_unique_id="alice-uid")
            await main._handle_chat_command("Alice", "select 1", invoker_unique_id="alice-uid")

        search.assert_awaited_once_with("测试", search_type=3, result_num=5, page_num=1)
        self.assertEqual(2, enqueue.await_count)
        self.assertEqual("mid001", enqueue.await_args_list[0].kwargs["song_mid"])
        self.assertEqual("mid002", enqueue.await_args_list[1].kwargs["song_mid"])
        self.assertIn("使用 select <编号>", notice.await_args_list[0].args[0])
        self.assertIn("已从歌单《测试歌单》加入 2 首歌曲", notice.await_args_list[1].args[0])

    async def test_point_song_prioritizes_the_qqmusic_request(self) -> None:
        songs = [{
            "songmid": "mid12345",
            "name": "测试歌曲",
            "singer": [{"name": "测试歌手"}],
            "album": {"mid": "alb123", "name": "测试专辑"},
            "interval": 210,
        }]

        with (
            patch.object(main.qqmusic, "search_songs_simple", AsyncMock(return_value=songs)) as search,
            patch.object(main, "_enqueue_qqmusic_song", AsyncMock(return_value=(7, False))) as enqueue,
            patch.object(main.voice, "get_status", AsyncMock(return_value=SimpleNamespace(state="STATE_PLAYING"))),
            patch.object(main.voice, "send_notice", AsyncMock()) as notice,
        ):
            await main._handle_chat_command("Alice", "点歌 测试歌曲")

        search.assert_awaited_once_with("测试歌曲", limit=1, page=1)
        enqueue.assert_awaited_once_with(
            song_mid="mid12345",
            title="测试歌曲",
            artist="测试歌手",
            play_now=False,
            requested_by="Alice",
            prioritize=True,
            quality="320",
            album_mid="alb123",
            album="测试专辑",
            artwork_url="https://y.gtimg.cn/music/photo_new/T002R300x300M000alb123.jpg",
            duration_ms=210000,
        )
        self.assertIn("已置顶，将在下一首播放", notice.await_args.args[0])

    async def test_search_uses_qqmusic(self) -> None:
        songs = [{
            "songmid": "mid12345",
            "name": "测试歌曲",
            "singer": [{"name": "测试歌手"}],
        }]

        with (
            patch.object(main.qqmusic, "search_songs_simple", AsyncMock(return_value=songs)) as search,
            patch.object(main.voice, "send_notice", AsyncMock()) as notice,
        ):
            await main._handle_chat_command("Alice", "搜索 测试歌曲")

        search.assert_awaited_once_with("测试歌曲", limit=5, page=1)
        self.assertIn("QQ 音乐搜索结果", notice.await_args.args[0])

    async def test_direct_qqmusic_mid_skips_search(self) -> None:
        with (
            patch.object(main.qqmusic, "search_songs_simple", AsyncMock()) as search,
            patch.object(main, "_enqueue_qqmusic_song", AsyncMock(return_value=(8, False))) as enqueue,
            patch.object(main.voice, "get_status", AsyncMock(return_value=SimpleNamespace(state="STATE_PLAYING"))),
            patch.object(main.voice, "send_notice", AsyncMock()),
        ):
            await main._handle_chat_command("Alice", "add 003aAYrm3GE0Ac")

        search.assert_not_awaited()
        enqueue.assert_awaited_once_with(
            song_mid="003aAYrm3GE0Ac",
            title="003aAYrm3GE0Ac",
            artist="",
            play_now=False,
            requested_by="Alice",
            prioritize=False,
            quality="320",
            album_mid="",
            album="",
            artwork_url="",
            duration_ms=None,
        )

    async def test_point_song_reports_missing_qqmusic_authorization(self) -> None:
        with (
            patch.object(
                main.qqmusic,
                "search_songs_simple",
                AsyncMock(return_value=[{"songmid": "mid12345", "name": "测试歌曲"}]),
            ),
            patch.object(
                main,
                "_enqueue_qqmusic_song",
                AsyncMock(side_effect=HTTPException(status_code=400, detail="admin qqmusic cookie not set")),
            ),
            patch.object(main.voice, "send_notice", AsyncMock()) as notice,
        ):
            await main._handle_chat_command("Alice", "点歌 测试歌曲")

        self.assertIn("QQ 音乐后台授权未配置", notice.await_args.args[0])


class ExternalQQMusicDefaultTests(unittest.IsolatedAsyncioTestCase):
    def test_queue_request_defaults_to_qqmusic(self) -> None:
        self.assertEqual("qqmusic", main.ExternalQueueRequest().source)

    async def test_queue_treats_an_empty_source_as_qqmusic(self) -> None:
        request = main.ExternalQueueRequest(source="", song_mid="mid12345", title="测试歌曲")

        with patch.object(main, "_enqueue_qqmusic_song", AsyncMock(return_value=(9, False))) as enqueue:
            result = await main.external_add_queue(request)

        enqueue.assert_awaited_once_with(
            song_mid="mid12345",
            title="测试歌曲",
            artist="",
            play_now=False,
            requested_by="external_api",
            quality="320",
            album_mid="",
            album="",
            artwork_url="",
            duration_ms=None,
        )
        self.assertEqual("qqmusic", result["source"])

    async def test_queue_accepts_qqmusic_mid_without_title(self) -> None:
        request = main.ExternalQueueRequest(source="qqmusic", song_mid="mid12345")

        with patch.object(main, "_enqueue_qqmusic_song", AsyncMock(return_value=(10, False))) as enqueue:
            result = await main.external_add_queue(request)

        self.assertEqual("mid12345", enqueue.await_args.kwargs["title"])
        self.assertEqual("mid12345", result["track"]["title"])

    async def test_search_defaults_to_qqmusic(self) -> None:
        songs = [{
            "songmid": "mid12345",
            "name": "测试歌曲",
            "singer": [{"name": "测试歌手"}],
        }]

        with (
            patch.object(main.qqmusic, "search_songs_simple", AsyncMock(return_value=songs)) as search,
        ):
            result = await main.external_search("测试歌曲")

        search.assert_awaited_once_with("测试歌曲", limit=20, page=1)
        self.assertEqual("qqmusic", result["source"])
        self.assertEqual("mid12345", result["items"][0]["song_mid"])

    async def test_playlist_search_endpoint_uses_qqmusic(self) -> None:
        raw = {
            "req": {
                "data": {
                    "body": {
                        "diss": {
                            "list": [{"dissid": "123", "dissname": "测试歌单"}]
                        }
                    }
                }
            }
        }
        with patch.object(main.qqmusic, "search_with_keyword", AsyncMock(return_value=raw)) as search:
            result = await main.qqmusic_search_playlists("测试", limit=5, page=2)

        search.assert_awaited_once_with("测试", search_type=3, result_num=5, page_num=2)
        self.assertEqual("测试歌单", result["playlists"][0]["name"])
        self.assertEqual(2, result["page"])

    async def test_play_without_argument_plays_first_queue_item(self) -> None:
        row = SimpleNamespace(id=42, title="队首歌曲", artist="歌手")
        session = unittest.mock.Mock()
        session.execute.return_value.scalars.return_value.first.return_value = row

        with (
            patch.object(main, "new_session", return_value=session),
            patch.object(main, "_play_queue_item_internal", AsyncMock(return_value=True)) as play_item,
            patch.object(main.voice, "send_notice", AsyncMock()) as notice,
        ):
            await main._handle_chat_command("Bob", "play")

        session.close.assert_called_once_with()
        play_item.assert_awaited_once_with(42, requested_by="Bob")
        self.assertIn("已播放队列第一首: 队首歌曲 - 歌手", notice.await_args.args[0])

    async def test_random_and_order_commands_switch_shuffle_mode(self) -> None:
        with (
            patch.object(main, "_set_shuffle_enabled", AsyncMock(return_value={"ok": True})) as shuffle,
            patch.object(main.voice, "get_status", AsyncMock(return_value=SimpleNamespace(state="STATE_PLAYING"))),
            patch.object(main.voice, "send_notice", AsyncMock()) as notice,
        ):
            await main._handle_chat_command("Carol", "随机播放")
            await main._handle_chat_command("Carol", "顺序播放")

        self.assertEqual([True, False], [call.args[0] for call in shuffle.await_args_list])
        self.assertIn("已切换为随机播放", notice.await_args_list[0].args[0])
        self.assertIn("已切换为顺序播放", notice.await_args_list[1].args[0])

    async def test_clear_command_clears_queue_and_playback_state(self) -> None:
        count_result = unittest.mock.Mock()
        count_result.scalar.return_value = 3
        session = unittest.mock.Mock()
        session.execute.side_effect = [count_result, unittest.mock.Mock()]

        with (
            patch.object(main, "new_session", return_value=session),
            patch.object(main, "_shuffle_queue", [1, 2, 3]),
            patch.object(main, "_current_shuffle_index", 1),
            patch.object(main, "_invalidate_play_requests", AsyncMock()) as invalidate,
            patch.object(main, "_set_now_playing_queue_item", AsyncMock()) as clear_now_playing,
            patch.object(main, "_schedule_ts_description_update"),
            patch.object(main.voice, "stop", AsyncMock()) as stop,
            patch.object(main.voice, "send_notice", AsyncMock()) as notice,
        ):
            await main._handle_chat_command("Dave", "清空")

            self.assertEqual([], main._shuffle_queue)
            self.assertEqual(-1, main._current_shuffle_index)

        session.commit.assert_called_once_with()
        session.close.assert_called_once_with()
        invalidate.assert_awaited_once_with()
        clear_now_playing.assert_awaited_once_with(None)
        stop.assert_awaited_once_with()
        self.assertIn("已清空播放队列（3 首）", notice.await_args.args[0])

    def test_playlist_results_are_isolated_by_unique_id_and_expire(self) -> None:
        alice_key = main._ts_playlist_result_key(invoker_unique_id="alice-uid", invoker_name="SameName")
        bob_key = main._ts_playlist_result_key(invoker_unique_id="bob-uid", invoker_name="SameName")
        playlists = [{"id": "123", "name": "测试", "creator": "", "track_count": "1"}]

        with patch.object(main.time, "monotonic", return_value=100.0):
            main._remember_ts_playlist_results(alice_key, playlists)

        with patch.object(main.time, "monotonic", return_value=101.0):
            self.assertEqual(playlists, main._get_ts_playlist_results(alice_key))
            self.assertEqual([], main._get_ts_playlist_results(bob_key))

        with patch.object(main.time, "monotonic", return_value=100.0 + main._TS_PLAYLIST_RESULTS_TTL_S):
            self.assertEqual([], main._get_ts_playlist_results(alice_key))

    def test_qqmusic_playlist_search_normalizes_cover_and_creator(self) -> None:
        raw = {
            "req": {
                "data": {
                    "body": {
                        "diss": {
                            "list": [
                                {
                                    "dissid": "123456",
                                    "dissname": "热门歌单",
                                    "creator": {"nick": "创建者"},
                                    "song_count": 12,
                                    "logo": "//y.gtimg.cn/cover.jpg",
                                    "listennum": 3456,
                                }
                            ]
                        }
                    }
                }
            }
        }

        self.assertEqual(
            [{
                "id": "123456",
                "name": "热门歌单",
                "creator": "创建者",
                "track_count": "12",
                "cover_url": "https://y.gtimg.cn/cover.jpg",
                "play_count": "3456",
            }],
            main._extract_qqmusic_playlist_search_items(raw),
        )


class QQMusicQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_song_defers_url_lookup_until_playback(self) -> None:
        session = unittest.mock.Mock()
        session.execute.return_value.scalar.return_value = None
        with (
            patch.object(main, "new_session", return_value=session),
            patch.object(
                main,
                "QueueItem",
                side_effect=lambda **kwargs: SimpleNamespace(id=17, **kwargs),
            ) as queue_item,
            patch.object(main, "_get_admin_qqmusic_cookie", return_value="qq-cookie"),
            patch.object(main.qqmusic, "set_cookie") as set_cookie,
            patch.object(main.qqmusic, "get_music_url_simple", AsyncMock()) as get_url,
            patch.object(main, "_schedule_ts_description_update"),
        ):
            result = await main._enqueue_qqmusic_song(
                song_mid="003aAYrm3GE0Ac",
                title="测试歌曲",
                artist="测试歌手",
                album="测试专辑",
                artwork_url="https://example.test/cover.jpg",
                play_now=False,
                requested_by="Alice",
                quality="320",
                album_mid="alb123",
                duration_ms=210000,
            )

        self.assertEqual((17, False), result)
        set_cookie.assert_called_once_with("qq-cookie")
        get_url.assert_not_awaited()
        created = queue_item.call_args.kwargs
        self.assertEqual(1, created["queue_position"])
        self.assertEqual("__qqmusic_quality__:320", created["source_url"])
        self.assertEqual("测试专辑", created["album"])
        self.assertEqual("https://example.test/cover.jpg", created["cover_url"])
        session.close.assert_called_once_with()

    async def test_prioritized_song_is_queued_after_the_current_song(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        session_factory = sessionmaker(bind=engine)
        Base.metadata.create_all(engine)
        seed_session = session_factory()
        try:
            current = main.QueueItem(
                track_id="qqmusic:current",
                queue_position=1,
                title="当前歌曲",
                source_url="current-source",
            )
            waiting_one = main.QueueItem(
                track_id="qqmusic:waiting-one",
                queue_position=2,
                title="等待歌曲一",
                source_url="waiting-one-source",
            )
            waiting_two = main.QueueItem(
                track_id="qqmusic:waiting-two",
                queue_position=3,
                title="等待歌曲二",
                source_url="waiting-two-source",
            )
            seed_session.add_all([current, waiting_one, waiting_two])
            seed_session.commit()
            current_id = int(current.id)
        finally:
            seed_session.close()

        try:
            with (
                patch.object(main, "new_session", side_effect=session_factory),
                patch.object(main, "_get_admin_qqmusic_cookie", return_value="qq-cookie"),
                patch.object(main.qqmusic, "set_cookie"),
                patch.object(main, "_schedule_ts_description_update"),
                patch.object(main, "_current_queue_item_id", current_id),
                patch.object(main, "_pending_queue_item_id", None),
            ):
                await main._enqueue_qqmusic_song(
                    song_mid="priority-mid",
                    title="置顶歌曲",
                    artist="测试歌手",
                    play_now=False,
                    requested_by="Alice",
                    prioritize=True,
                )

            check_session = session_factory()
            try:
                rows = check_session.execute(
                    select(main.QueueItem).order_by(main.QueueItem.queue_position.asc())
                ).scalars().all()
            finally:
                check_session.close()

            self.assertEqual(["当前歌曲", "置顶歌曲", "等待歌曲一", "等待歌曲二"], [row.title for row in rows])
            self.assertEqual([0, 1, 2, 3], [row.queue_position for row in rows])
        finally:
            engine.dispose()

    async def test_web_prioritize_moves_an_existing_song_after_the_current_song(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        session_factory = sessionmaker(bind=engine)
        Base.metadata.create_all(engine)
        seed_session = session_factory()
        try:
            current = main.QueueItem(
                track_id="qqmusic:current",
                queue_position=1,
                title="当前歌曲",
                source_url="current-source",
            )
            waiting_one = main.QueueItem(
                track_id="qqmusic:waiting-one",
                queue_position=2,
                title="等待歌曲一",
                source_url="waiting-one-source",
            )
            target = main.QueueItem(
                track_id="qqmusic:target",
                queue_position=3,
                title="置顶歌曲",
                source_url="target-source",
            )
            waiting_two = main.QueueItem(
                track_id="qqmusic:waiting-two",
                queue_position=4,
                title="等待歌曲二",
                source_url="waiting-two-source",
            )
            seed_session.add_all([current, waiting_one, target, waiting_two])
            seed_session.commit()
            current_id = int(current.id)
            target_id = int(target.id)
        finally:
            seed_session.close()

        action_session = session_factory()
        try:
            with (
                patch.object(main, "_current_queue_item_id", current_id),
                patch.object(main, "_pending_queue_item_id", None),
                patch.object(main, "_shuffle_enabled", False),
                patch.object(main, "_schedule_ts_description_update") as update_description,
            ):
                result = await main.prioritize_queue_item(target_id, session=action_session)

            self.assertEqual({"ok": True, "id": target_id, "action": "prioritized"}, result)
            update_description.assert_called_once_with()
        finally:
            action_session.close()

        check_session = session_factory()
        try:
            rows = check_session.execute(
                select(main.QueueItem).order_by(main.QueueItem.queue_position.asc())
            ).scalars().all()
        finally:
            check_session.close()

        self.assertEqual(
            ["当前歌曲", "置顶歌曲", "等待歌曲一", "等待歌曲二"],
            [row.title for row in rows],
        )
        self.assertEqual([0, 1, 2, 3], [row.queue_position for row in rows])
        engine.dispose()

    async def test_prioritized_existing_song_follows_current_song_in_shuffle_mode(self) -> None:
        with (
            patch.object(main, "_shuffle_enabled", True),
            patch.object(main, "_shuffle_queue", [1, 2, 3, 4]),
            patch.object(main, "_current_shuffle_index", 1),
        ):
            main._place_item_next_in_shuffle_queue(item_id=1, active_item_id=2)

            self.assertEqual([2, 1, 3, 4], main._shuffle_queue)
            self.assertEqual(0, main._current_shuffle_index)

    async def test_auto_play_uses_the_persisted_queue_position(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        session_factory = sessionmaker(bind=engine)
        Base.metadata.create_all(engine)
        seed_session = session_factory()
        try:
            regular = main.QueueItem(
                track_id="qqmusic:regular",
                queue_position=2,
                title="普通歌曲",
                source_url="regular-source",
            )
            prioritized = main.QueueItem(
                track_id="qqmusic:priority",
                queue_position=1,
                title="置顶歌曲",
                source_url="priority-source",
            )
            seed_session.add_all([regular, prioritized])
            seed_session.commit()
            prioritized_id = int(prioritized.id)
        finally:
            seed_session.close()

        try:
            with (
                patch.object(main, "new_session", side_effect=session_factory),
                patch.object(main, "_shuffle_enabled", False),
                patch.object(main, "_repeat_mode", "none"),
                patch.object(main, "_current_queue_item_id", None),
                patch.object(main, "_play_queue_item_internal", AsyncMock(return_value=True)) as play_item,
            ):
                await main._auto_play_next_from_queue()

            play_item.assert_awaited_once_with(prioritized_id, requested_by="auto")
        finally:
            engine.dispose()

    async def test_playing_queued_song_refreshes_qqmusic_url(self) -> None:
        queued = SimpleNamespace(
            id=17,
            track_id="qqmusic:003aAYrm3GE0Ac",
            title="测试歌曲",
            artist="测试歌手",
            album="测试专辑",
            duration=210000,
            cover_url="https://example.test/cover.jpg",
            source_url="__qqmusic_quality__:128",
        )
        session = unittest.mock.Mock()
        session.get.return_value = queued

        with (
            patch.object(main, "new_session", return_value=session),
            patch.object(main, "_begin_play_request", AsyncMock(return_value=1)),
            patch.object(main, "_is_play_request_current", AsyncMock(return_value=True)),
            patch.object(main, "_clear_pending_queue_item_if_match", AsyncMock()),
            patch.object(main, "_get_admin_qqmusic_cookie", return_value="qq-cookie"),
            patch.object(main.qqmusic, "set_cookie") as set_cookie,
            patch.object(main.qqmusic, "get_music_url_simple", AsyncMock(return_value="https://cdn.example.test/fresh.mp3")) as get_url,
            patch.object(main, "_set_now_playing_queue_item", AsyncMock()) as set_now_playing,
            patch.object(main.voice, "play", AsyncMock()) as play,
            patch.object(main, "HistoryItem", side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
        ):
            result = await main._play_queue_item_internal(17, requested_by="Alice")

        self.assertTrue(result)
        set_cookie.assert_called_once_with("qq-cookie")
        get_url.assert_awaited_once_with("003aAYrm3GE0Ac", "128")
        self.assertEqual("__qqmusic_quality__:128|https://cdn.example.test/fresh.mp3", queued.source_url)
        set_now_playing.assert_awaited_once_with(
            17,
            "https://cdn.example.test/fresh.mp3",
            duration_ms=210000,
            artist="测试歌手",
            album="测试专辑",
            artwork_url="https://example.test/cover.jpg",
        )
        play.assert_awaited_once_with(
            source_url="https://cdn.example.test/fresh.mp3",
            title="测试歌曲",
            requested_by="Alice",
            notice="",
        )
        session.close.assert_called_once_with()

    def test_blank_qqmusic_cookie_is_not_configured(self) -> None:
        session = unittest.mock.Mock()
        session.get.return_value = unittest.mock.Mock(value="encrypted-empty-cookie")

        with patch.object(main, "decrypt_text", return_value="  "):
            with self.assertRaises(HTTPException) as raised:
                main._get_admin_qqmusic_cookie(session)

        self.assertEqual(400, raised.exception.status_code)


class PlaybackCompletionTests(unittest.IsolatedAsyncioTestCase):
    def test_only_permanent_playback_failures_are_auto_skipped(self) -> None:
        unavailable = HTTPException(
            status_code=404,
            detail="无法获取 QQ 音乐播放链接，可能需要 VIP 会员或该歌曲不可用",
        )

        self.assertTrue(main._should_auto_skip_unplayable_queue_item(unavailable))
        self.assertTrue(
            main._should_auto_skip_unplayable_queue_item(
                HTTPException(status_code=402, detail="需要 VIP")
            )
        )
        self.assertTrue(
            main._should_auto_skip_unplayable_queue_item(
                HTTPException(status_code=403, detail="无版权")
            )
        )
        self.assertFalse(
            main._should_auto_skip_unplayable_queue_item(
                HTTPException(status_code=400, detail="admin qqmusic cookie not set")
            )
        )
        self.assertFalse(
            main._should_auto_skip_unplayable_queue_item(
                HTTPException(status_code=409, detail="playback request superseded")
            )
        )
        self.assertFalse(
            main._should_auto_skip_unplayable_queue_item(
                HTTPException(status_code=502, detail="upstream temporarily unavailable")
            )
        )

    async def test_auto_play_skips_unavailable_qqmusic_item_and_continues(self) -> None:
        session = unittest.mock.Mock()
        session.execute.return_value.scalars.return_value.first.return_value = SimpleNamespace(id=42)
        error = HTTPException(
            status_code=404,
            detail="无法获取 QQ 音乐播放链接，可能需要 VIP 会员或该歌曲不可用",
        )

        with (
            patch.object(main, "_shuffle_enabled", False),
            patch.object(main, "_repeat_mode", "none"),
            patch.object(main, "_current_queue_item_id", None),
            patch.object(main, "new_session", return_value=session),
            patch.object(main, "_play_queue_item_internal", AsyncMock(side_effect=error)),
            patch.object(main, "_skip_unplayable_queue_item", AsyncMock()) as skip_item,
        ):
            await main._auto_play_next_from_queue()

        skip_item.assert_awaited_once_with(42, reason=str(error.detail))
        session.close.assert_called_once_with()

    async def test_skip_unplayable_item_deletes_it_notifies_and_plays_next(self) -> None:
        with (
            patch.object(main, "_delete_queue_item", AsyncMock()) as delete_item,
            patch.object(main.voice, "send_notice", AsyncMock()) as send_notice,
            patch.object(main, "_auto_play_next_from_queue", AsyncMock()) as play_next,
        ):
            await main._skip_unplayable_queue_item(
                11,
                reason="  无法获取 QQ 音乐播放链接，可能需要 VIP 会员或该歌曲不可用  ",
            )

        delete_item.assert_awaited_once_with(11)
        send_notice.assert_awaited_once_with(
            "无法播放，已跳过: #11\n无法获取 QQ 音乐播放链接，可能需要 VIP 会员或该歌曲不可用\n将播放下一首",
            target_mode=2,
        )
        play_next.assert_awaited_once_with(start_after_id=11)

    async def test_web_play_keeps_missing_queue_item_as_not_found(self) -> None:
        session = unittest.mock.Mock()
        session.get.return_value = None

        with (
            patch.object(main, "_play_queue_item_internal", AsyncMock(return_value=False)),
            patch.object(main, "_skip_unplayable_queue_item", AsyncMock()) as skip_item,
            self.assertRaises(HTTPException) as raised,
        ):
            await main.play_queue_item(99, session=session)

        self.assertEqual(404, raised.exception.status_code)
        self.assertEqual("queue item not found", raised.exception.detail)
        skip_item.assert_not_awaited()

    async def test_repeat_one_replays_finished_item_without_deleting_it(self) -> None:
        with (
            patch.object(main, "_repeat_mode", "one"),
            patch.object(main, "_take_now_playing_if_match", AsyncMock(return_value=7)),
            patch.object(main, "_play_queue_item_internal", AsyncMock(return_value=True)) as replay,
            patch.object(main, "_delete_queue_item", AsyncMock()) as delete_item,
            patch.object(main, "_auto_play_next_from_queue", AsyncMock()) as play_next,
        ):
            await main._handle_playback_finished("source")

        replay.assert_awaited_once_with(7, requested_by="auto")
        delete_item.assert_not_awaited()
        play_next.assert_not_awaited()

    async def test_repeat_one_skips_song_that_becomes_unavailable(self) -> None:
        error = HTTPException(
            status_code=404,
            detail="无法获取 QQ 音乐播放链接，可能需要 VIP 会员或该歌曲不可用",
        )

        with (
            patch.object(main, "_repeat_mode", "one"),
            patch.object(main, "_take_now_playing_if_match", AsyncMock(return_value=7)),
            patch.object(main, "_play_queue_item_internal", AsyncMock(side_effect=error)),
            patch.object(main, "_skip_unplayable_queue_item", AsyncMock()) as skip_item,
        ):
            await main._handle_playback_finished("source")

        skip_item.assert_awaited_once_with(7, reason=str(error.detail))

    async def test_normal_completion_deletes_item_and_plays_next(self) -> None:
        with (
            patch.object(main, "_repeat_mode", "none"),
            patch.object(main, "_take_now_playing_if_match", AsyncMock(return_value=8)),
            patch.object(main, "_play_queue_item_internal", AsyncMock()) as replay,
            patch.object(main, "_delete_queue_item", AsyncMock()) as delete_item,
            patch.object(main, "_auto_play_next_from_queue", AsyncMock()) as play_next,
        ):
            await main._handle_playback_finished("source")

        replay.assert_not_awaited()
        delete_item.assert_awaited_once_with(8)
        play_next.assert_awaited_once_with()


class VoiceStatusFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_voice_service_returns_offline_status(self) -> None:
        with (
            patch.object(main.voice, "get_status", AsyncMock(side_effect=RuntimeError("offline"))),
            patch.object(main, "_current_queue_item_id", None),
        ):
            result = await main.voice_status()

        self.assertFalse(result["voice_connected"])
        self.assertEqual("idle", result["state"])
        self.assertEqual("", result["now_playing_title"])


if __name__ == "__main__":
    unittest.main()
