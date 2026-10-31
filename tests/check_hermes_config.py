"""Optional integration check against real Hermes APIs, using disposable homes only."""
import argparse
import asyncio
import importlib.util
import os
from pathlib import Path
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="lan-config-test-") as home, tempfile.TemporaryDirectory(prefix="lan-desktop-test-") as desktop:
        os.environ["HERMES_HOME"] = home
        os.environ["HERMES_DESKTOP_USER_DATA_DIR"] = desktop
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        sys.path.insert(0, str(args.hermes_source.resolve()))
        from hermes_cli.config import load_config_readonly, save_config
        from agent.secret_scope import reset_secret_scope, set_secret_scope
        from hermes_constants import get_hermes_home
        import httpx
        from fastapi import FastAPI

        assert Path(get_hermes_home()).resolve() == Path(home).resolve(), "Hermes home isolation failed"

        path = Path(__file__).resolve().parents[1] / "dashboard/plugin_api.py"
        spec = importlib.util.spec_from_file_location("lan_config_integration", path)
        api = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = api
        spec.loader.exec_module(api)
        save_config({"plugins": {"entries": {"unrelated": {"settings": {"keep": "untouched"}}}}}, merge_existing=True)
        scope = set_secret_scope({}, profile_home=home)
        try:
            api.write_settings({**api.DEFAULTS, "host": "target.invalid", "health_url": "https://old.invalid/health"})
            api.write_token("SYNTHETIC-OLD")
            api.notify_settings_changed = lambda: None
            app = FastAPI()
            app.include_router(api.router)

            async def check():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    loaded = (await client.get("/settings")).json()
                    assert loaded["has_token"]
                    body = {"revision": loaded["revision"], "values": {**loaded["values"], "health_url": "https://new.invalid/health"}, "token": "SYNTHETIC-NEW"}
                    result = (await client.post("/settings", json=body)).json()
                    assert result["ok"], result
                    assert api.read_configuration()[2] == "SYNTHETIC-NEW"
                    assert api.read_configuration()[1]["health_url"] == "https://new.invalid/health"
                    assert load_config_readonly()["plugins"]["entries"]["unrelated"]["settings"]["keep"] == "untouched"
                    assert (await client.post("/settings", json=body)).json()["outcome"] == "conflict"
                    body = {"revision": result["settings"]["revision"], "values": {**result["settings"]["values"], "host": "a..b"}}
                    assert (await client.post("/settings", json=body)).json()["outcome"] == "invalid"
                    assert api.read_configuration()[1]["host"] == "target.invalid"
                    print("PASS: real Hermes config/secret persistence, revision conflicts, validation, unrelated settings preserved")

            asyncio.run(check())
        finally:
            reset_secret_scope(scope)


if __name__ == "__main__":
    main()
