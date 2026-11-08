"""Lokale Tests: Mock-UDP, Mock-HTTP und ausschließlich Loopback-HTTP."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch, MagicMock

import httpx
from fastapi import FastAPI

PATH = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
spec = importlib.util.spec_from_file_location("lan_control_api", PATH)
api = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = api
spec.loader.exec_module(api)


def settings(**changes):
    raw = {"health_url": "http://localhost:12345/health", "wol_mac": "02:00:00:00:00:01",
           "wol_broadcast": "192.0.2.255"}
    raw.update(changes)
    return api.Settings.parse(raw)


class ValidationTests(unittest.TestCase):
    def test_defaults_and_label(self):
        value = settings()
        self.assertEqual((value.label, value.interval, value.timeout, value.expected, value.port),
                         ("localhost", 60, 2, 200, 9))
        self.assertEqual(settings(display_name=" Test ").label, "Test")

    def test_explicit_intervals_are_preserved(self):
        self.assertEqual(api.DEFAULTS["interval"], 60)
        self.assertEqual(api.Settings("demo", "target.invalid", "").interval, 60)
        for interval in (2, 5, 60, 300):
            with self.subTest(interval=interval):
                self.assertEqual(settings(interval=interval, timeout=1).interval, interval)
        self.assertEqual(settings(timeout=30).timeout, 30)

    def test_invalid_health_settings(self):
        for change in ({"health_url": ""}, {"health_url": "file:///tmp/a"},
                       {"health_url": "ftp://localhost"}, {"health_url": "http://u:p@localhost"},
                       {"health_url": "http://localhost/#token"}, {"health_url": "http://localhost:70000"},
                       {"health_url": "http://[broken"}, {"health_url": "http://localhost:0"},
                       {"health_url": "http://a\nb"}, {"health_url": "http://a\\b"},
                       {"interval": True}, {"interval": float("nan")}, {"interval": 301},
                       {"interval": 5, "timeout": 5}, {"timeout": 0}, {"expected_status": 200.5},
                       {"expected_status": 600}, {"display_name": "x" * 201}):
            with self.subTest(change=change), self.assertRaises(api.ConfigError):
                settings(**change)

    def test_invalid_wol_keeps_health(self):
        for change in ({"wol_mac": ""}, {"wol_mac": "no"}, {"wol_mac": "ff:ff:ff:ff:ff:ff"},
                       {"wol_mac": "00:00:00:00:00:00"}, {"wol_mac": "02-00:00:00:00:01"},
                       {"wol_broadcast": "example.invalid"}, {"wol_broadcast": "::1"},
                       {"wol_broadcast": "127.0.0.1"}, {"wol_broadcast": "224.0.0.1"},
                       {"wol_port": 0}, {"wol_port": True}, {"wol_port": 65536}):
            with self.subTest(change=change):
                self.assertTrue(settings(**change).wol_error)

    def test_mac_and_packet(self):
        self.assertEqual(api.normalize_mac("020000000001"), "02:00:00:00:00:01")
        self.assertEqual(api.normalize_mac("02-00-00-00-00-01"), "02:00:00:00:00:01")
        with patch.object(api.socket, "socket") as constructor:
            sock = constructor.return_value.__enter__.return_value
            api.send_magic_packet(settings())
            packet, target = sock.sendto.call_args.args
            self.assertEqual(packet, b"\xff" * 6 + bytes.fromhex("020000000001") * 16)
            self.assertEqual(len(packet), 102)
            self.assertEqual(target, ("192.0.2.255", 9))
            sock.setsockopt.assert_called_once_with(api.socket.SOL_SOCKET, api.socket.SO_BROADCAST, 1)

    def test_secret_repr_and_invalid_header(self):
        secret = "TEST-ONLY-SECRET"
        value = api.Settings.parse({"health_url": "http://localhost/?private=yes"}, secret)
        self.assertNotIn(secret, repr(value))
        self.assertNotIn("private=yes", repr(value))
        with self.assertRaises(api.ConfigError):
            api.Settings.parse({"health_url": "http://localhost"}, "x\r\ny")


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = settings()
        self.scope = "profile-a"
        self.now = 100.0
        self.result = ("unhealthy", "Simulierter Fehler.")
        self.checks = 0
        self.sent = []
        async def check(config):
            self.checks += 1
            await asyncio.sleep(0)
            return api.Outcome(*self.result, host_status="up" if self.result[0] == "healthy" else "down" if self.result[0] == "unhealthy" else "unknown")
        self.service = api.Service(lambda: (self.scope, self.config), check,
                                   self.sent.append, lambda: self.now)

    async def test_cache_shared_across_windows(self):
        values = await asyncio.gather(*(self.service.status() for _ in range(12)))
        self.assertEqual(self.checks, 1)
        self.assertTrue(all(value == values[0] for value in values))
        self.now += 59.9
        await self.service.status()
        self.assertEqual(self.checks, 1)
        self.now += 0.1
        await self.service.status()
        self.assertEqual(self.checks, 2)

    async def test_double_click_cooldown_and_recheck(self):
        status = await self.service.status()
        replies = await asyncio.gather(*(self.service.wake(status["revision"]) for _ in range(12)))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(sum(reply["outcome"] == "sent" for reply in replies), 1)
        self.assertEqual(self.checks, 2)
        self.assertTrue(all(not reply["state"]["wake_allowed"] for reply in replies))
        self.now += 11
        fresh = await self.service.status()
        self.assertEqual((await self.service.wake(fresh["revision"]))["outcome"], "sent")

    async def test_manual_recheck_bypasses_cache_without_sending_or_resetting_cooldown(self):
        status = await self.service.status()
        await self.service.wake(status["revision"])
        self.assertEqual(self.checks, 2)
        self.result = ("healthy", "Frisch.")
        self.assertEqual((await self.service.status())["status"], "unhealthy")
        fresh = await self.service.status(force=True)
        self.assertEqual(fresh["status"], "healthy")
        self.assertEqual(fresh["cooldown_remaining"], 10)
        self.assertEqual(self.checks, 3)
        self.assertEqual(len(self.sent), 1)

    async def test_recheck_route_runs_fresh_probe_and_never_sends(self):
        app = FastAPI()
        app.include_router(api.router)
        with patch.object(api, "service", self.service):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                await client.get("/status")
                self.result = ("healthy", "Frisch.")
                response = await client.post("/recheck")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "healthy")
                self.assertEqual(self.checks, 2)
                self.assertFalse(self.sent)

    async def test_post_wake_rechecks_bypass_long_cache_and_preserve_normal_interval(self):
        for interval in (60, 300):
            with self.subTest(interval=interval):
                self.config = settings(interval=interval)
                self.result = ("unhealthy", "Simulated down")
                app = FastAPI()
                app.include_router(api.router)
                with patch.object(api, "service", self.service):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                        snapshot = (await client.get("/status")).json()
                        before_checks, before_sent = self.checks, len(self.sent)
                        wake = (await client.post("/wake", json={"revision": snapshot["revision"], "expires_at": snapshot["wake_expires_at"]})).json()
                        self.assertEqual(wake["outcome"], "sent")
                        self.assertEqual(self.checks, before_checks + 1)
                        for elapsed in (0, 5, 5):
                            self.now += elapsed
                            await client.post("/recheck")
                        self.assertEqual(self.checks, before_checks + 4)
                        self.result = ("healthy", "Fresh ping response")
                        self.now += 5
                        self.assertEqual((await client.get("/status")).json()["host_status"], "down")
                        fresh = (await client.post("/recheck")).json()
                        self.assertEqual(fresh["host_status"], "up")
                        self.assertEqual(fresh["next_check_in"], interval)
                        self.assertEqual(self.config.interval, interval)
                        self.assertEqual(len(self.sent), before_sent + 1)

    async def test_becomes_healthy_before_wake(self):
        status = await self.service.status()
        self.result = ("healthy", "Gesund.")
        reply = await self.service.wake(status["revision"])
        self.assertEqual(reply["state"]["status"], "healthy")
        self.assertFalse(self.sent)

    async def test_auth_missing_wol_unknown_and_stale_block(self):
        for state in ("healthy", "auth_error", "backend_error"):
            self.result = (state, "Test")
            self.now += 61
            status = await self.service.status()
            await self.service.wake(status["revision"])
        self.result = ("unhealthy", "Test")
        self.config = settings(wol_mac="")
        status = await self.service.status()
        await self.service.wake(status["revision"])
        self.config = settings()
        status = await self.service.status()
        self.now += 64
        reply = await self.service.wake(status["revision"])
        self.assertEqual(reply["state"]["status"], "stale")
        self.assertFalse(self.sent)
        self.scope = "new-profile"
        await self.service.wake(status["revision"])
        self.assertFalse(self.sent)

    async def test_profile_and_config_switch_reject_old_revision(self):
        old = await self.service.status()
        self.scope = "profile-b"
        fresh = await self.service.status()
        self.assertNotEqual(old["revision"], fresh["revision"])
        await self.service.wake(old["revision"])
        self.config = settings(wol_mac="02:00:00:00:00:02")
        await self.service.wake(fresh["revision"])
        self.assertFalse(self.sent)

    async def test_config_switch_during_recheck(self):
        status = await self.service.status()
        async def changed(config):
            self.config = settings(wol_mac="02:00:00:00:00:02")
            return api.Outcome("unhealthy", "Test", "down")
        self.service.check = changed
        reply = await self.service.wake(status["revision"])
        self.assertEqual(reply["state"]["status"], "unknown")
        self.assertFalse(self.sent)

    async def test_invalid_then_restored_settings_drop_cached_green(self):
        self.result = ("healthy", "Test")
        await self.service.status()
        original = self.service.load
        self.service.load = lambda: (_ for _ in ()).throw(api.ConfigError("Health-URL fehlt."))
        self.assertEqual((await self.service.status())["status"], "config_error")
        self.service.load = original
        self.result = ("unhealthy", "Test")
        self.assertEqual((await self.service.status())["status"], "unhealthy")

    async def test_errors_are_sanitized_and_send_failure_has_cooldown(self):
        status = await self.service.status()
        self.service.send = MagicMock(side_effect=OSError("TEST-ONLY-SECRET"))
        reply = await self.service.wake(status["revision"])
        self.assertEqual(reply["outcome"], "send_error")
        self.assertFalse(reply["state"]["wake_allowed"])
        self.assertNotIn("TEST-ONLY-SECRET", json.dumps(reply))
        self.service.load = MagicMock(side_effect=RuntimeError("TEST-ONLY-SECRET"))
        self.assertNotIn("TEST-ONLY-SECRET", json.dumps(await self.service.status()))

    async def test_routes_reject_arbitrary_url_and_extra_fields(self):
        app = FastAPI()
        app.include_router(api.router, prefix="/api/plugins/lan-control")
        with patch.object(api, "service", self.service):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                status = (await client.get("/api/plugins/lan-control/status")).json()
                response = await client.post("/api/plugins/lan-control/wake", json={"revision": status["revision"], "expires_at": status["wake_expires_at"], "url": "http://evil.invalid"})
                self.assertEqual(response.status_code, 422)
                self.assertEqual((await client.post("/api/plugins/lan-control/wake", json={})).status_code, 422)
                self.assertEqual((await client.post("/api/plugins/lan-control/wake", json={"revision": status["revision"]})).status_code, 422)
                self.assertEqual((await client.get("/api/plugins/lan-control/probe")).status_code, 404)
                response = await client.post("/api/plugins/lan-control/wake", json={"revision": status["revision"], "expires_at": status["wake_expires_at"]})
                self.assertEqual(response.json()["outcome"], "sent")


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_loopback_wall_clock_timeout(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                time.sleep(0.4)
                self.send_response(200)
                self.end_headers()
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = settings(health_url=f"http://127.0.0.1:{server.server_port}/", timeout=0.2)
            started = time.monotonic()
            result = await api.http_probe(config)
            self.assertEqual(result[0], "unhealthy")
            self.assertIn("timed out", result[1])
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join()

    async def test_loopback_status_auth_redirect_and_no_body(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                code = int(self.path.strip("/"))
                self.send_response(code)
                if code == 302:
                    self.send_header("Location", "/200")
                self.send_header("Content-Length", "999999")
                self.end_headers()
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for code, expected in [(200, "healthy"), (503, "unhealthy"), (401, "auth_error"), (403, "auth_error"), (302, "unhealthy")]:
                config = api.Settings.parse({"health_url": f"http://127.0.0.1:{server.server_port}/{code}"}, "TEST-ONLY-SECRET")
                result = await api.http_probe(config)
                self.assertEqual(result[0], expected)
                self.assertNotIn("TEST-ONLY-SECRET", repr(result))
            self.assertEqual(len(seen), 5)
            self.assertTrue(all(header == "Bearer TEST-ONLY-SECRET" for _, header in seen))
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join()

    async def test_transport_timeout_tls_options_and_unexpected_errors(self):
        for error, expected in [(httpx.ConnectError("secret"), "unhealthy"),
                                (httpx.ReadTimeout("secret"), "unhealthy"),
                                (RuntimeError("secret"), "backend_error")]:
            with patch.object(api.httpx, "AsyncClient") as constructor:
                client = constructor.return_value.__aenter__.return_value
                client.stream = MagicMock(side_effect=error)
                result = await api.http_probe(settings())
                self.assertEqual(result[0], expected)
                self.assertNotIn("secret", repr(result))
                self.assertEqual(constructor.call_args.kwargs,
                                 {"verify": True, "follow_redirects": False, "trust_env": False, "timeout": 2.0})


if __name__ == "__main__":
    unittest.main()
