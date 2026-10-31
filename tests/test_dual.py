import asyncio
import unittest
from unittest.mock import patch, AsyncMock, MagicMock

from test_backend import api, settings


class DualTests(unittest.IsolatedAsyncioTestCase):
    async def test_matrix_and_wol_uses_only_confirmed_host_state(self):
        for host, http, colour, allowed in [
            ("up", "healthy", "healthy", False), ("up", "unhealthy", "degraded", False),
            ("down", "healthy", "degraded", True), ("down", "unhealthy", "unhealthy", True),
            ("down", "auth_error", "unhealthy", True), ("up", "auth_error", "degraded", False),
            ("error", "healthy", "backend_error", False), ("down", "backend_error", "backend_error", False),
        ]:
            with self.subTest(host=host, http=http):
                result = api.combine_checks((host, "Host"), (http, "HTTP"))
                self.assertEqual(result.status, colour)
                check, sent = AsyncMock(return_value=result), []
                service = api.Service(lambda: ("scope", settings()), check, sent.append)
                state = await service.status()
                self.assertEqual(state["wake_allowed"], allowed)
                await service.wake(state["revision"])
                self.assertEqual(len(sent), int(allowed))

    async def test_optional_http_disabled_and_concurrent_checks(self):
        http = AsyncMock()
        result = await api.probe(settings(host="target.invalid", health_url="", expected_status="garbage"),
                                 ping=AsyncMock(return_value=("up", "ok")), http=http)
        self.assertEqual(result.status, "healthy")
        http.assert_not_called()
        started = set()
        ready = asyncio.Event()
        async def check(name, result):
            started.add(name)
            if len(started) == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 0.2)
            return result
        result = await api.probe(settings(), ping=lambda _: check("ping", ("up", "ok")),
                                 http=lambda _: check("http", ("unhealthy", "failed")))
        self.assertEqual(result.status, "degraded")

    async def test_host_recovers_before_wake_http_can_stay_unhealthy(self):
        check = AsyncMock(side_effect=[api.Outcome("unhealthy", "down", "down", "unhealthy"),
                                       api.Outcome("degraded", "host up", "up", "unhealthy")])
        send = MagicMock()
        service = api.Service(lambda: ("scope", settings()), check, send)
        state = await service.status()
        self.assertEqual((await service.wake(state["revision"]))["outcome"], "blocked")
        send.assert_not_called()

    def test_optional_configuration_and_explicit_host_migration(self):
        self.assertEqual(settings().host, "localhost")
        self.assertEqual(settings(host="different.invalid").host, "different.invalid")
        self.assertEqual(settings(expected_status="").expected, 200)
        self.assertEqual(settings(expected_status="   ").expected, 200)
        self.assertEqual(settings(expected_status="204").expected, 204)
        value = api.Settings.parse({"host": "target.invalid", "health_url": "", "expected_status": {"bad": True}}, "bad\r\nsecret")
        self.assertEqual((value.host, value.token, value.expected), ("target.invalid", "", 200))
        for host in ("", "-c", "a b", "http://host", "foo/bar", "127.0.0.999", "a..b", "a.-b", "0.0.0.0", "224.0.0.1"):
            with self.subTest(host=host), self.assertRaises(api.ConfigError):
                settings(host=host, health_url="")

    def test_ping_only_runtime_does_not_consult_secret_storage(self):
        with patch.object(api, "read_profile_settings", return_value=("scope", {"host": "target.invalid", "health_url": "  "})), patch.object(api, "read_configuration", side_effect=AssertionError("Must not read token")):
            scope, value = api.load_runtime()
        self.assertEqual((scope, value.token, value.url), ("scope", "", ""))


class FakeProcess:
    def __init__(self, code=0, stderr=b"", pending=False, stdout=b""):
        self.returncode = None
        self.code, self.stderr, self.pending, self.stdout = code, stderr, pending, stdout
        self.killed, self.reaped = False, False
        self.finished = asyncio.Event()
        if not pending:
            self.finished.set()

    async def communicate(self):
        await self.finished.wait()
        self.returncode = -9 if self.killed else self.code
        self.reaped = True
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True
        self.finished.set()

    async def wait(self):
        await self.finished.wait()
        self.reaped = True


class PingTests(unittest.IsolatedAsyncioTestCase):
    async def run_fake(self, process, system="Darwin", resolve=None):
        spawn = AsyncMock(return_value=process)
        with patch.object(api.shutil, "which", return_value="/test/ping"):
            result = await api.ping_probe(settings(timeout=0.2), system=system, spawn=spawn,
                                          resolve=resolve or AsyncMock(return_value="127.0.0.1"))
        return result, spawn

    async def test_return_codes_and_permission_errors(self):
        for system, code, error, expected in [("Darwin", 0, b"", "up"), ("Darwin", 2, b"", "down"),
                                             ("Linux", 1, b"", "down"), ("Linux", 2, b"permission denied", "error"),
                                             ("Darwin", 77, b"secret", "error"), ("Windows", 1, b"", "error")]:
            result, spawn = await self.run_fake(FakeProcess(code, error), system)
            self.assertEqual(result[0], expected)
            self.assertNotIn("secret", repr(result))
            self.assertNotIn("shell", spawn.call_args.kwargs)
            self.assertEqual(spawn.call_args.args[-1], "127.0.0.1")

    async def test_windows_echo_requires_numeric_reply_evidence_from_target(self):
        examples = [
            ("127.0.0.1", b"Reply from 127.0.0.1: bytes=32 time<1ms TTL=128"),
            ("127.0.0.1", "Antwort von 127.0.0.1: Bytes=32 Zeit<1ms TTL=128".encode("cp850")),
            ("127.0.0.1", "127.0.0.1 からの応答: バイト数 =32 時間 <1ms TTL=128".encode("cp932")),
            ("::1", b"Reply from ::1: time<1ms"),
            ("2001:db8::1", b"Reply from 2001:0DB8:0:0:0:0:0:1: time=12ms"),
            ("::1", "Ответ от ::1: время<1мс".encode("cp866")),
            ("::1", "::1 からの応答: 時間 <1ms".encode("cp932")),
        ]
        for address, stdout in examples:
            with self.subTest(address=address, stdout=stdout):
                result, _ = await self.run_fake(FakeProcess(stdout=stdout), "Windows",
                                                resolve=AsyncMock(return_value=address))
                self.assertEqual(result[0], "up")

    async def test_windows_icmp_errors_with_exit_zero_are_not_echo_success(self):
        examples = [
            b"Reply from 127.0.0.2: Destination host unreachable.",
            b"Reply from 127.0.0.1: TTL expired in transit.",
            "Antwort von 127.0.0.2: Zielhost nicht erreichbar.".encode("cp850"),
            "Antwort von 127.0.0.1: Gültigkeitsdauer bei der Übertragung überschritten.".encode("cp850"),
            "127.0.0.2 からの応答: 宛先ホストに到達できません。".encode("cp932"),
        ]
        for stdout in examples:
            for code in (0, 1):
                with self.subTest(stdout=stdout, code=code):
                    result, _ = await self.run_fake(FakeProcess(code, stdout=stdout), "Windows")
                    self.assertEqual(result[0], "down")
                    self.assertEqual(api.combine_checks(result).status, "unhealthy")
        result, _ = await self.run_fake(
            FakeProcess(stdout=b"Reply from 2001:db8::2: TTL expired in transit."),
            "Windows", resolve=AsyncMock(return_value="2001:db8::1"))
        self.assertEqual(result[0], "down")

    async def test_windows_local_errors_and_summary_without_echo_fail_closed(self):
        for stdout, stderr in [
            (b"", b""),
            (b"PING: transmit failed. General failure.", b""),
            (b"PING: transmit failed. Network unreachable.", b""),
            ("PING: Fehler bei der Übertragung. Allgemeiner Fehler.".encode("cp850"), b""),
            (b"PING: Zeitgeber konnte nicht gestartet werden.", b""),
            (b"Ping statistics for 127.0.0.1:\r\n    Packets: Sent = 1, Received = 1, Lost = 0 (0% loss)", b""),
            (b"Reply from 127.0.0.1: bytes=32 time<1ms TTL=128", b"permission denied"),
        ]:
            for code in (0, 1):
                with self.subTest(stdout=stdout, stderr=stderr, code=code):
                    result, _ = await self.run_fake(FakeProcess(code, stderr=stderr, stdout=stdout), "Windows")
                    self.assertEqual(result[0], "error")
                    self.assertEqual(api.combine_checks(result).status, "backend_error")

    async def test_windows_request_timeouts_remain_confirmed_no_reply(self):
        for stdout in [b"Request timed out.", "Zeitüberschreitung der Anforderung.".encode("cp850")]:
            with self.subTest(stdout=stdout):
                result, _ = await self.run_fake(FakeProcess(1, stdout=stdout), "Windows")
                self.assertEqual(result[0], "down")

    async def test_timeout_kills_and_reaps(self):
        process = FakeProcess(pending=True)
        result, _ = await self.run_fake(process)
        self.assertEqual(result[0], "down")
        self.assertTrue(process.killed and process.reaped)

    async def test_cancellation_kills_and_reaps(self):
        process = FakeProcess(pending=True)
        with patch.object(api.shutil, "which", return_value="/test/ping"):
            task = asyncio.create_task(api.ping_probe(settings(), spawn=AsyncMock(return_value=process),
                                                      resolve=AsyncMock(return_value="127.0.0.1")))
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(process.killed and process.reaped)

    async def test_failed_pipe_reader_still_kills_and_reaps(self):
        process = FakeProcess(pending=True)
        process.communicate = AsyncMock(side_effect=OSError("PRIVATE-SECRET"))
        result, _ = await self.run_fake(process)
        self.assertEqual(result[0], "error")
        self.assertTrue(process.killed and process.reaped)
        self.assertNotIn("PRIVATE-SECRET", repr(result))

    async def test_missing_binary_dns_failure_and_dns_timeout_fail_closed(self):
        with patch.object(api.shutil, "which", return_value=None):
            result = await api.ping_probe(settings(), resolve=AsyncMock(return_value="127.0.0.1"))
        self.assertEqual(result[0], "error")
        result, spawn = await self.run_fake(FakeProcess(), resolve=AsyncMock(side_effect=OSError("secret")))
        self.assertEqual(result[0], "error")
        spawn.assert_not_called()
        async def slow(_):
            await asyncio.sleep(1)
        result, spawn = await self.run_fake(FakeProcess(), resolve=slow)
        self.assertEqual(result[0], "error")
        spawn.assert_not_called()

    def test_platform_argument_vectors_ipv6(self):
        with patch.object(api.shutil, "which", side_effect=lambda value: "/test/" + value):
            self.assertEqual(api.ping_command("::1", 2, "Darwin"), ["/test/ping6", "-n", "-c", "1", "::1"])
            self.assertEqual(api.ping_command("127.0.0.1", 2, "Linux"), ["/test/ping", "-4", "-n", "-c", "1", "127.0.0.1"])
            self.assertIn("2000", api.ping_command("127.0.0.1", 2, "Windows"))
