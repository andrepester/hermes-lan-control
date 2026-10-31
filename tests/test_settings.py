"""Settings transactions: synthetic credentials, in-memory writes, no LAN traffic."""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from test_backend import api


class SecretScopeTests(unittest.TestCase):
    def test_waiting_request_refreshes_its_secret_scope_before_revision_check(self):
        snapshot = {api.TOKEN_KEY: "OLD-SNAPSHOT"}
        homes = []
        def refresh(home):
            homes.append(home)
            snapshot[api.TOKEN_KEY] = "ROTATED-TOKEN"
        modules = {
            "agent.secret_scope": SimpleNamespace(get_secret=snapshot.get, refresh_installed_secret_scope=refresh),
            "hermes_constants": SimpleNamespace(get_hermes_home=lambda: Path("/synthetic/profile-a")),
        }
        with patch.dict(sys.modules, modules):
            self.assertEqual(api.read_token(), "ROTATED-TOKEN")
        self.assertEqual(homes, [Path("/synthetic/profile-a")])


class SettingsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scope = "profile-a"
        self.raw = {**api.DEFAULTS, "host": "target.invalid", "health_url": "https://old.invalid/health"}
        self.token = "SYNTHETIC-OLD"
        self.writes, self.snapshots, self.probes, self.events = [], [], [], []

        def read():
            return self.scope, dict(self.raw), self.token

        def write(values):
            self.writes.append(dict(values))
            self.raw.update(values)
            self.snapshots.append((self.raw["health_url"], self.token))

        def token(value):
            self.token = value
            self.snapshots.append((self.raw["health_url"], self.token))

        def notify():
            self.assertFalse(self.service.states[self.scope].lock.locked())
            self.events.append((self.raw["health_url"], self.token))

        async def probe(settings):
            self.probes.append((settings.url, settings.token))
            return api.Outcome("unhealthy", "simulated", "down", "unhealthy" if settings.url else "disabled")

        self.service = api.Service(lambda: (self.scope, api.Settings.parse(self.raw, self.token)), probe, lambda _: self.fail("Unexpected UDP"))
        for name, replacement in [("read_configuration", read), ("write_settings", write), ("write_token", token),
                                  ("notify_settings_changed", notify),
                                  ("service", self.service)]:
            patcher = patch.object(api, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(api.router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    async def draft(self, **changes):
        loaded = (await self.client.get("/settings")).json()
        return {"revision": loaded["revision"], "values": {**loaded["values"], **changes}}

    async def save(self, body):
        response = await self.client.post("/settings", json=body)
        self.assertEqual(response.status_code, 200)
        return response.json()

    async def test_read_infers_host_without_returning_secret(self):
        self.raw["host"] = ""
        response = await self.client.get("/settings")
        self.assertTrue(response.json()["inferred_host"])
        self.assertEqual(response.json()["values"]["host"], "old.invalid")
        self.assertTrue(response.json()["has_token"])
        self.assertNotIn(self.token, response.text)

    async def test_endpoint_and_token_update_never_expose_mismatched_credentials(self):
        body = await self.draft(health_url="https://new.invalid/health")
        body["token"] = "SYNTHETIC-NEW"
        result = await self.save(body)
        self.assertTrue(result["ok"])
        self.assertNotIn("SYNTHETIC", json.dumps(result))
        for url, token in self.snapshots:
            self.assertFalse(url == "https://old.invalid/health" and token == "SYNTHETIC-NEW")
            self.assertFalse(url == "https://new.invalid/health" and token == "SYNTHETIC-OLD")
        self.assertEqual(self.events, [("https://new.invalid/health", "SYNTHETIC-NEW")])
        await self.service.status()
        self.assertEqual(self.probes, [("https://new.invalid/health", "SYNTHETIC-NEW")])

    async def test_two_queued_saves_with_same_revision_cannot_overwrite_each_other(self):
        a = await self.draft(display_name="Saved by A")
        b = await self.draft(host="b.invalid")
        state = self.service.states.setdefault(self.scope, api.State())
        await state.lock.acquire()
        first = asyncio.create_task(self.save(a))
        second = asyncio.create_task(self.save(b))
        await asyncio.sleep(0)
        state.lock.release()
        results = await asyncio.gather(first, second)
        self.assertEqual(sum(bool(result["ok"]) for result in results), 1)
        self.assertEqual(sum(result.get("outcome") == "conflict" for result in results), 1)
        self.assertEqual(self.raw["display_name"], "Saved by A")
        self.assertEqual(self.raw["host"], "target.invalid")
        self.assertEqual(len(self.events), 1)

    async def test_queued_token_only_rotation_also_conflicts(self):
        a, b = await self.draft(), await self.draft()
        a["token"], b["token"] = "FIRST-TOKEN", "SECOND-TOKEN"
        results = await asyncio.gather(self.save(a), self.save(b))
        self.assertEqual([result["ok"] for result in results], [True, False])
        self.assertEqual(self.token, "FIRST-TOKEN")

    async def test_wrong_profile_or_changed_config_rejects_old_revision(self):
        body = await self.draft()
        for change in ("profile", "config", "token"):
            with self.subTest(change=change):
                self.scope = "profile-b" if change == "profile" else "profile-a"
                if change == "config":
                    self.raw["host"] = "changed.invalid"
                if change == "token":
                    self.token = "ROTATED"
                result = await self.save(body)
                self.assertEqual(result["outcome"], "conflict")
        self.assertFalse(self.writes or self.events)

    async def test_invalid_runtime_settings_are_rejected_before_any_write(self):
        for changes in ({"host": "a..b"}, {"host": "0.0.0.0"}, {"host": ""},
                        {"display_name": "x" * 201}, {"health_url": "http://localhost:0"},
                        {"health_url": "http://a\\b"}, {"host": True}, {"interval": True},
                        {"timeout": 5}, {"wol_port": 1.5}, {"expected_status": "999"}):
            with self.subTest(changes=changes):
                result = await self.save(await self.draft(**changes))
                self.assertFalse(result["ok"])
                self.assertEqual(result["outcome"], "invalid")
                self.assertFalse(self.writes or self.events)

    async def test_validation_errors_keep_helpful_messages_without_echoing_values(self):
        cases = (
            ({"interval": "SYNTHETIC-SECRET"}, "interval: enter a number."),
            ({"timeout": 5}, "Timeout must be shorter than the check interval."),
            ({"host": "/synthetic/private/SYNTHETIC-SECRET"},
             "Enter an IP address or DNS hostname for the ping target."),
            ({"health_url": "https://user:SYNTHETIC-SECRET@private.invalid/health"},
             "Use an HTTP or HTTPS health URL without embedded credentials or a fragment."),
        )
        for changes, expected in cases:
            with self.subTest(changes=changes):
                body = await self.draft(**changes)
                body["token"] = "SYNTHETIC-NEW-TOKEN"
                response = await self.client.post("/settings", json=body)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"ok": False, "outcome": "invalid", "message": expected})
                self.assertNotIn("SYNTHETIC", response.text)
                self.assertNotIn("/synthetic/private", response.text)
        self.assertFalse(self.writes or self.events)

    async def test_config_errors_from_either_read_cannot_expose_internal_details(self):
        body = await self.draft()
        private = "SYNTHETIC-SECRET /synthetic/private/profile.env Traceback https://private.invalid/health"
        for failing_read in (1, 2):
            for arguments in ((private,), ("Enter a valid host to ping.", private)):
                with self.subTest(failing_read=failing_read, arguments=arguments):
                    reads = 0
                    def read():
                        nonlocal reads
                        reads += 1
                        if reads == failing_read:
                            raise api.ConfigError(*arguments)
                        return self.scope, dict(self.raw), self.token
                    with patch.object(api, "read_configuration", read):
                        response = await self.client.post("/settings", json=body)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), {
                        "ok": False, "outcome": "invalid", "message": "Plugin settings are invalid.",
                    })
                    self.assertNotIn("SYNTHETIC", response.text)
                    self.assertNotIn("/synthetic/private", response.text)
                    self.assertNotIn("Traceback", response.text)
        self.assertFalse(self.writes or self.events)

    async def test_unexpected_configuration_exceptions_use_fixed_http_errors(self):
        body = await self.draft()
        with patch.object(api, "read_configuration", side_effect=OSError(
                "SYNTHETIC-SECRET /synthetic/private/profile.env Traceback")):
            loaded = await self.client.get("/settings")
            saved = await self.client.post("/settings", json=body)
        self.assertEqual(loaded.status_code, 503)
        self.assertEqual(loaded.json(), {"detail": "Could not read settings."})
        self.assertEqual(saved.status_code, 503)
        self.assertEqual(saved.json(), {"detail": "Could not save settings."})
        self.assertFalse(self.writes or self.events)

    async def test_status_and_wol_validation_also_hide_unknown_config_error_details(self):
        private = "SYNTHETIC-SECRET /synthetic/private/profile.env Traceback"
        with patch.object(self.service, "load", side_effect=api.ConfigError(private)):
            response = await self.client.get("/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reason"], "Plugin settings are invalid.")
        self.assertFalse(response.json()["wake_allowed"])
        self.raw.update(wol_mac="02:00:00:00:00:10", wol_broadcast="192.0.2.255")
        with patch.object(api, "normalize_mac", side_effect=api.ConfigError(private)):
            response = await self.client.get("/status")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Plugin settings are invalid.", response.json()["reason"])
        self.assertFalse(response.json()["wake_allowed"])
        self.assertNotIn("SYNTHETIC", response.text)
        self.assertNotIn("/synthetic/private", response.text)
        self.assertNotIn("Traceback", response.text)

    async def test_optional_http_preserves_unused_fields_and_blank_token(self):
        self.raw["expected_status"] = "ignored-value"
        body = await self.draft(health_url="")
        del body["values"]["expected_status"]
        body["token"] = ""
        self.assertTrue((await self.save(body))["ok"])
        self.assertEqual(self.token, "SYNTHETIC-OLD")
        self.assertEqual(self.raw["expected_status"], "ignored-value")
        self.assertEqual(self.raw["health_url"], "")

    async def test_invalid_wol_does_not_prevent_saving_valid_monitoring_settings(self):
        result = await self.save(await self.draft(wol_mac="not-a-mac"))
        self.assertTrue(result["ok"])
        self.assertFalse((await self.service.status())["wake_allowed"])

    async def test_secret_input_is_never_echoed_in_validation_errors(self):
        for token in ("SECRET\r\nHEADER", "SECRET" * 2000, {"secret": "VALUE"}):
            body = await self.draft()
            body["token"] = token
            response = await self.client.post("/settings", json=body)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("SECRET", response.text)
        body = await self.draft()
        body.update(token="SECRET", key="OTHER_SECRET")
        response = await self.client.post("/settings", json=body)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("SECRET", response.text)
        self.assertFalse(self.writes)

    async def test_partial_secret_failure_leaves_http_paused_even_after_service_restart(self):
        body = await self.draft(health_url="https://new.invalid/health")
        body["token"] = "SYNTHETIC-NEW"
        def fail_after_persist(value):
            self.token = value
            raise OSError("PRIVATE-SECRET")
        with patch.object(api, "write_token", fail_after_persist):
            result = await self.save(body)
        self.assertEqual(result["outcome"], "save_error")
        self.assertTrue(result["http_paused"])
        self.assertNotIn("PRIVATE-SECRET", json.dumps(result))
        self.assertEqual(self.raw["health_url"], "")
        self.service.states.clear()
        await self.service.status()
        self.assertEqual(self.probes, [("", "")])
        body["revision"] = result["settings"]["revision"]
        self.assertTrue((await self.save(body))["ok"])

    async def test_partial_config_failures_never_pair_new_endpoint_with_old_token(self):
        original = api.write_settings
        for fail_at in (1, 2, 3):
            with self.subTest(fail_at=fail_at):
                self.raw["health_url"], self.token = "https://old.invalid/health", "SYNTHETIC-OLD"
                body = await self.draft(health_url="https://new.invalid/health")
                body["token"] = "SYNTHETIC-NEW"
                count = 0
                def fail_after_persist(values):
                    nonlocal count
                    count += 1
                    original(values)
                    if count == fail_at:
                        raise OSError("PRIVATE-SECRET")
                with patch.object(api, "write_settings", fail_after_persist):
                    result = await self.save(body)
                self.assertFalse(result["ok"])
                self.assertTrue(result["http_paused"])
                self.assertEqual(self.raw["health_url"], "")

    async def test_ineffective_token_write_cannot_enable_new_endpoint(self):
        body = await self.draft(health_url="https://new.invalid/health")
        body["token"] = "SYNTHETIC-NEW"
        with patch.object(api, "write_token", lambda _: None):
            result = await self.save(body)
        self.assertEqual(result["outcome"], "save_error")
        self.assertTrue(result["http_paused"])

    async def test_save_waiting_on_probe_times_out_without_writing(self):
        body = await self.draft()
        state = self.service.states.setdefault(self.scope, api.State())
        await state.lock.acquire()
        try:
            with patch.object(api, "REQUEST_BUDGET", 0.02):
                result = await self.save(body)
            self.assertEqual(result["outcome"], "timeout")
            self.assertFalse(self.writes)
        finally:
            state.lock.release()
