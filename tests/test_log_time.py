import importlib.util
import logging
import os
from pathlib import Path
import struct
import time
import unittest
from unittest.mock import patch

from backend.logger import TSBotFormatter, server_log_beijing_time

spec = importlib.util.spec_from_file_location("time_sync_worker", Path(__file__).resolve().parents[1] / "scripts/compose-updater.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class LogTimeTests(unittest.TestCase):
    def test_backend_log_time_is_beijing_regardless_of_process_timezone(self):
        original = os.environ.get("TZ")
        try:
            for zone in ("UTC", "America/New_York", "Asia/Shanghai"):
                os.environ["TZ"] = zone
                time.tzset()
                record = logging.LogRecord("test", logging.INFO, "test", 1, "message", (), None)
                record.created = 0
                self.assertEqual("[1970-01-01 08:00:00] [INFO] [backend] message", TSBotFormatter().format(record))
                record.created = 57600
                self.assertIn("[1970-01-02 00:00:00]", TSBotFormatter().format(record))
        finally:
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            time.tzset()

    def test_server_log_converts_utc_and_preserves_microseconds_and_message(self):
        self.assertEqual(
            "2026-10-31 07:59:59.123456|INFO|message with 2026-10-30 23:00:00 inside",
            server_log_beijing_time("2026-10-30 23:59:59.123456|INFO|message with 2026-10-30 23:00:00 inside"),
        )
        self.assertEqual("2026-10-30 08:00:00 INFO message", server_log_beijing_time("2026-10-30 00:00:00 INFO message"))
        self.assertEqual("message without a timestamp", server_log_beijing_time("message without a timestamp"))


class NetworkClockTests(unittest.TestCase):
    def response(self, packet: bytes, *, valid: bool = True) -> bytes:
        response = bytearray(48)
        response[0] = 0x24
        response[1] = 2 if valid else 0
        response[24:32] = packet[40:48]
        seconds = 1_700_000_000 + worker.NTP_UNIX_OFFSET
        response[32:48] = struct.pack("!IIII", seconds, 0, seconds, 2**30)
        return bytes(response)

    def test_one_sample_corrects_clock_drift_and_accounts_for_network_delay(self):
        with (
            patch.object(worker.socket, "socket") as socket,
            patch.object(worker.time, "time", return_value=1_699_999_400),
            patch.object(worker.time, "monotonic", side_effect=[100.0, 100.75]),
        ):
            connection = socket.return_value.__enter__.return_value
            connection.recv.side_effect = lambda size: self.response(connection.send.call_args.args[0])
            timestamp = worker.network_time()
        self.assertEqual(1_700_000_000.5, timestamp)
        connection.connect.assert_called_once_with((worker.NTP_SERVER, 123))
        connection.send.assert_called_once()
        connection.settimeout.assert_called_once_with(5)

    def test_unsynchronized_or_unmatched_ntp_reply_is_rejected_before_setting_time(self):
        for invalid in ("unsynchronized", "wrong-request", "truncated", "zero-time"):
            with (
                patch.object(worker.socket, "socket") as socket,
                patch.object(worker.time, "clock_settime", create=True) as set_clock,
            ):
                connection = socket.return_value.__enter__.return_value
                def reply(size):
                    response = bytearray(self.response(connection.send.call_args.args[0], valid=invalid != "unsynchronized"))
                    if invalid == "wrong-request":
                        response[24:32] = b"badnonce"
                    elif invalid == "truncated":
                        return bytes(response[:16])
                    elif invalid == "zero-time":
                        response[32:48] = bytes(16)
                    return bytes(response)
                connection.recv.side_effect = reply
                with self.assertRaises(ValueError):
                    worker.sync_host_time()
                set_clock.assert_not_called()

    def test_sync_sets_utc_epoch_once_and_formats_result_as_beijing(self):
        with (
            patch.object(worker, "network_time", return_value=0) as fetch,
            patch.object(worker.time, "clock_settime", create=True) as set_clock,
        ):
            result = worker.sync_host_time()
        fetch.assert_called_once()
        set_clock.assert_called_once_with(time.CLOCK_REALTIME, 0)
        self.assertEqual("1970-01-01 08:00:00", result)
