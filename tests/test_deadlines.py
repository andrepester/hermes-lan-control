"""Bound queued actions and reject disconnected wake requests; all sends are mocked."""
import asyncio
import socket
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import uvicorn
from fastapi import FastAPI

from test_backend import api, settings


class DeadlineTests(unittest.IsolatedAsyncioTestCase):
    def service(self, check, clock=None):
        self.sent = []
        options = {"clock": clock} if clock else {}
        return api.Service(lambda: ("deadline-test", settings(host="target.invalid", health_url="")),
                           check, self.sent.append, **options)

    async def test_queue_and_probe_share_one_budget(self):
        checks = 0
        started = asyncio.Event()
        async def check(_):
            nonlocal checks
            checks += 1
            if checks > 1:
                started.set()
                await asyncio.sleep(0.04)
            return api.Outcome("unhealthy", "simulated", "down")
        service = self.service(check)
        state = await service.status()
        with patch.object(api, "REQUEST_BUDGET", 0.06):
            poll = asyncio.create_task(service.status(force=True))
            await started.wait()
            result = await service.wake(state["revision"])
            await poll
        self.assertEqual(result["outcome"], "blocked")
        self.assertFalse(result["state"]["wake_allowed"])
        self.assertFalse(self.sent)
        self.assertFalse(service.states["deadline-test"].lock.locked())
        self.assertEqual(service.states["deadline-test"].cooldown_until, 0)

    async def test_queued_status_and_wake_expire_without_running_a_probe(self):
        check = AsyncMock(return_value=api.Outcome("unhealthy", "simulated", "down"))
        service = self.service(check)
        state = await service.status()
        lock = service.states["deadline-test"].lock
        await lock.acquire()
        try:
            with patch.object(api, "REQUEST_BUDGET", 0.02):
                status, wake = await asyncio.gather(service.status(force=True), service.wake(state["revision"]))
            self.assertEqual(status["status"], "backend_error")
            self.assertEqual(wake["outcome"], "blocked")
            self.assertEqual(check.await_count, 1)
            self.assertFalse(self.sent)
        finally:
            lock.release()

    async def test_explicit_clock_guard_blocks_send_after_event_loop_overrun(self):
        now = [0.0]
        async def check(_):
            return api.Outcome("unhealthy", "simulated", "down")
        service = self.service(check, lambda: now[0])
        state = await service.status()
        async def overrun(_):
            now[0] += api.REQUEST_BUDGET + 1
            return api.Outcome("unhealthy", "simulated", "down")
        service.check = overrun
        result = await service.wake(state["revision"])
        self.assertEqual(result["outcome"], "blocked")
        self.assertFalse(self.sent)

    async def test_disconnected_client_cannot_send_after_fresh_probe(self):
        service = self.service(AsyncMock(return_value=api.Outcome("unhealthy", "simulated", "down")))
        state = await service.status()
        disconnected = AsyncMock(return_value=True)
        result = await service.wake(state["revision"], disconnected=disconnected)
        self.assertEqual(result["outcome"], "blocked")
        disconnected.assert_awaited_once()
        self.assertFalse(self.sent)

    async def test_expiry_also_blocks_requests_buffered_before_handler_start(self):
        now = [100.0]
        check = AsyncMock(return_value=api.Outcome("unhealthy", "simulated", "down"))
        sent = []
        service = api.Service(lambda: ("buffer-test", settings(host="target.invalid", health_url="", interval=300)),
                              check, sent.append, lambda: now[0])
        state = await service.status()
        now[0] += api.REQUEST_BUDGET + 1
        result = await service.wake(state["revision"], expires_at=state["wake_expires_at"])
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(result["state"]["status"], "backend_error")
        self.assertFalse(sent)
        self.assertEqual(check.await_count, 1)
        fresh = await service.status()  # Renew the action expiry without an extra network probe.
        self.assertEqual(check.await_count, 1)
        self.assertEqual((await service.wake(fresh["revision"], expires_at=fresh["wake_expires_at"]))["outcome"], "sent")

    async def test_loopback_client_timeout_does_not_send_later(self):
        slow = False
        async def check(_):
            if slow:
                await asyncio.sleep(0.1)
            return api.Outcome("unhealthy", "simulated", "down")
        service = self.service(check)
        app = FastAPI()
        app.include_router(api.router)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False, lifespan="off"))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            while not server.started:
                if task.done():
                    await task
                    self.fail("Loopback server did not start")
                await asyncio.sleep(0.01)
            with patch.object(api, "service", service):
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", trust_env=False) as client:
                    state = (await client.get("/status")).json()
                    slow = True
                    with self.assertRaises(httpx.ReadTimeout):
                        await client.post("/wake", json={"revision": state["revision"], "expires_at": state["wake_expires_at"]}, timeout=0.03)
                    await asyncio.sleep(0.15)
                    self.assertFalse(self.sent)
                    self.assertEqual(service.states["deadline-test"].cooldown_until, 0)
        finally:
            server.should_exit = True
            await task
            sock.close()
