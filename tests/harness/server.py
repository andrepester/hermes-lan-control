"""Loopback-Harness: echter Plugin-Router und HTTP, immer abgefangener UDP-Versand."""
import argparse
import asyncio
from dataclasses import replace
import importlib.util
from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
import uvicorn

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("lan_control_api", ROOT.parents[1] / "dashboard/plugin_api.py")
api = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = api
spec.loader.exec_module(api)


def build_app(port):
    app = FastAPI()
    demo = {"mode": "unhealthy", "ping": "down", "health_on": True, "long_label": False,
            "profile": "demo-a", "checks": 0, "ping_checks": 0, "packets": 0, "token_writes": 0}
    configurations, tokens = {}, {}

    def raw_settings(profile):
        return configurations.setdefault(profile, {**api.DEFAULTS, "host": "target.invalid",
            "health_url": f"http://127.0.0.1:{port}/health", "display_name": "demo",
            "wol_mac": "02:00:00:00:00:01", "wol_broadcast": "192.0.2.255"})

    def read():
        raw = dict(raw_settings(demo["profile"]))
        if demo["mode"] == "config":
            raw.update(host="", health_url="")
        return demo["profile"], raw, tokens.get(demo["profile"], "")

    def load():
        profile, raw, token = read()
        return profile, api.Settings.parse(raw, token)

    async def ping(_settings):
        demo["ping_checks"] += 1
        return demo["ping"], {"up": "The host responds to ping (simulated).", "down": "The host does not respond to ping (simulated).", "error": "Ping failed locally (simulated)."}[demo["ping"]]

    async def http(settings):
        # Auch beliebige im Testformular eingegebene URLs verlassen nie Loopback.
        return await api.http_probe(replace(settings, url=f"http://127.0.0.1:{port}/health"))

    async def check(settings):
        return await api.probe(settings, ping=ping, http=http)

    def write_token(value):
        tokens[demo["profile"]] = value
        demo["token_writes"] += 1

    def capture(_settings):
        demo["packets"] += 1

    def write_settings(values):
        raw_settings(demo["profile"]).update(values)

    api.read_configuration, api.write_token = read, write_token
    api.write_settings = write_settings
    api.notify_settings_changed = lambda: None
    api.service = api.Service(load=load, check=check, send=capture)
    app.include_router(api.router, prefix="/api/plugins/lan-control")

    @app.get("/health")
    async def health():
        demo["checks"] += 1
        if demo["mode"] == "timeout":
            await asyncio.sleep(3)
        return Response(status_code={"healthy": 200, "auth": 401}.get(demo["mode"], 503))

    class DemoRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")
        mode: str
        ping: str
        health_on: bool
        long_label: bool
        profile: str

    @app.post("/demo")
    def set_demo(request: DemoRequest):
        demo.update(request.model_dump())
        raw = raw_settings(demo["profile"])
        raw["display_name"] = "Sehr langer neutraler Anzeigename für die Statusleiste" if demo["long_label"] else "demo"
        raw["health_url"] = f"http://127.0.0.1:{port}/health" if demo["health_on"] else ""
        for state in api.service.states.values():
            state.checked = None
        return {"ok": True}

    @app.get("/demo")
    def get_demo():
        return {key: demo[key] for key in ("checks", "ping_checks", "packets", "token_writes")}

    @app.get("/")
    def index():
        return FileResponse(ROOT / "index.html")

    app.mount("/dist", StaticFiles(directory=ROOT / "dist"), name="dist")
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    print(f"Nur Loopback: http://127.0.0.1:{args.port}; UDP-Versand vollständig abgefangen.", flush=True)
    uvicorn.run(build_app(args.port), host="127.0.0.1", port=args.port, access_log=False)
