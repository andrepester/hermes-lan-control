"""Offline Git lifecycle through a pinned Hermes client, PM worker and desktop reconciler.

Only tool acquisition and the application dependency fixture are substituted,
following Hermes' own plugin_worker_support test approach. Git, admission scan,
PM resolution/publication, config/secret APIs and desktop filesystem code are real.
No gateway or Electron process is launched.
"""
import argparse
import asyncio
from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def install_guards(root):
    """Fail on Python network I/O or writes outside this disposable test tree."""
    root = Path(root).resolve()

    def writable(value, dir_fd=-1, *, follow=True):
        if isinstance(value, (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(value))
            if path == Path(os.devnull):
                return
            if not path.is_absolute() and dir_fd not in (None, -1):
                if sys.platform == "darwin":
                    import fcntl
                    parent = os.fsdecode(fcntl.fcntl(dir_fd, 50, bytes(1024)).split(b"\0", 1)[0])
                else:
                    parent = os.readlink(f"/proc/self/fd/{dir_fd}")
                path = Path(parent) / path
            resolved = path.resolve() if follow else path.parent.resolve() / path.name
            require(resolved.is_relative_to(root),
                    "Lifecycle test attempted a write outside its temporary root")

    def audit(event, args):
        if event in {"socket.connect", "socket.sendto", "socket.bind"}:
            raise AssertionError("Network I/O is forbidden in the offline lifecycle test")
        if event == "open":
            _, mode, flags = args
            if (mode and any(char in mode for char in "wax+")) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                writable(args[0])
        elif event in {"os.remove", "os.rmdir"}:
            writable(args[0], args[1], follow=False)
        elif event in {"os.mkdir", "os.chmod"}:
            writable(args[0], args[2])
        elif event == "os.utime":
            writable(args[0], args[3])
        elif event == "os.truncate":
            writable(args[0])
        elif event in {"os.rename", "os.link"}:
            writable(args[0], args[2], follow=False)
            writable(args[1], args[3], follow=False)
        elif event == "os.symlink":
            writable(args[1], args[2], follow=False)

    sys.addaudithook(audit)


def run(command, *, cwd, env=None, timeout=90):
    result = subprocess.run([str(item) for item in command], cwd=cwd, env=env,
                            capture_output=True, text=True, timeout=timeout)
    require(result.returncode == 0, f"Lifecycle subprocess failed: {result.stderr}\n{result.stdout}")
    return result.stdout.strip()


def git(repo, *arguments):
    return run(["git", "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false", *arguments], cwd=repo)


def load_backend(path):
    spec = importlib.util.spec_from_file_location("lifecycle_backend", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def exercise(source, root):
    home, core = root / "home", root / "core"
    home.mkdir()
    core.mkdir()
    scratch = root / "tmp"
    scratch.mkdir()
    inherited = {key: value for key, value in os.environ.items() if key in {
        "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
        "LANG", "LC_ALL", "LC_CTYPE",
    }}
    os.environ.clear()
    os.environ.update(inherited)
    os.environ.update({
        "HERMES_HOME": str(home), "HERMES_DESKTOP_USER_DATA_DIR": str(root / "desktop-data"),
        "HERMES_RUNTIME_DIR": str(root / "store"), "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "UV_CACHE_DIR": str(root / "uv-cache"), "UV_OFFLINE": "1", "UV_NO_MANAGED_PYTHON": "1",
        "XDG_CONFIG_HOME": str(root / "config"), "XDG_CONFIG_DIRS": str(root / "config"),
        "PYTHONDONTWRITEBYTECODE": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_ALLOW_PROTOCOL": "file", "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Lifecycle fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Lifecycle fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "TMPDIR": str(scratch), "TMP": str(scratch), "TEMP": str(scratch),
    })
    tempfile.tempdir = str(scratch)
    sys.dont_write_bytecode = True
    install_guards(root)
    # Test the rejection paths without attempting a write or sending traffic.
    for event, args in (("open", (str(ROOT / "guard-probe"), "w", os.O_WRONLY)),
                        ("socket.connect", (None, ("127.0.0.1", 9)))):
        try:
            sys.audit(event, *args)
        except AssertionError:
            pass
        else:
            raise AssertionError(f"Isolation guard failed for {event}")
    sys.path.insert(0, str(source))
    import hermes_yaml as yaml
    from hermes_constants import get_hermes_home, get_default_hermes_root
    from hermes_cli import plugins_cmd as pc
    from hermes_cli.config import load_config_readonly, save_config
    from hermes_cli.subcommands.plugins import build_plugins_parser
    from pm import client, paths

    require(get_hermes_home().resolve() == home and get_default_hermes_root().resolve() == home,
            "Hermes home isolation failed")
    driver_uv = Path(sys.executable).parent / "uv"
    uv = str(driver_uv) if driver_uv.is_file() else shutil.which("uv")
    require(uv, "Install tests/lifecycle-requirements.txt to provide uv")
    # This plugin declares no additional Python dependencies. Keep the offline
    # core fixture empty instead of provisioning the full LLM/provider stack.
    (core / "pyproject.toml").write_text(
        '[project]\nname="lan-lifecycle-core"\nversion="1"\nrequires-python=">=3.14"\n'
        'dependencies=[]\n[tool.uv]\npackage=false\nno-index=true\n', encoding="utf-8")
    run([uv, "lock", "--python", sys._base_executable], cwd=core)

    def worker_command(worker, **kwargs):
        # Same test seam as upstream plugin_worker_support: use the prepared
        # test interpreter/uv, retaining the real JSON worker and publisher.
        code = (
            "import sys, runpy; from pathlib import Path; "
            f"sys.path[:0] = {[str(source), str(ROOT / 'tests')]!r}; "
            "from check_hermes_lifecycle import install_guards; "
            f"install_guards({str(root)!r}); import pm._uv; "
            f"pm._uv._toolchain = lambda **kwargs: (Path({uv!r}), Path({sys._base_executable!r})); "
            f"runpy.run_path({str(worker)!r}, run_name='__main__')"
        )
        return [sys.executable, "-I", "-B", "-c", code]

    def command(*argv):
        parser = argparse.ArgumentParser()
        build_plugins_parser(parser.add_subparsers(), cmd_plugins=pc.plugins_command)
        pc.plugins_command(parser.parse_args(["plugins", *argv]))

    def desktop(phase):
        output = run(["node", ROOT / "tests/check_desktop_lifecycle.mjs", source, home, phase], cwd=root)
        print(output, flush=True)

    origin = root / "plugin-origin"
    origin.mkdir()
    sys.path.insert(0, str(ROOT / "scripts"))
    from check_package import PACKAGE_FILES
    for name in PACKAGE_FILES:
        target = origin / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    git(origin, "init", "-q", "-b", "main")
    git(origin, "add", ".")
    git(origin, "commit", "-qm", "Lifecycle fixture baseline")
    baseline = git(origin, "rev-parse", "HEAD")
    source_url = origin.as_uri()
    installed = home / "plugins/lan-control"
    with ExitStack() as stack:
        stack.enter_context(patch.object(paths, "repo_root", lambda: core))
        stack.enter_context(patch.object(paths, "lockfile_path", lambda: root / "lock.json"))
        stack.enter_context(patch.object(client, "runtime_command", worker_command))
        require(not client.is_runtime(), "Lifecycle test must exercise the real PM worker boundary")
        command("install", source_url, "--no-enable")
        require(pc._read_install_metadata()["lan-control"]["revision"] == baseline, "Wrong installed revision")
        require("lan-control" not in pc._get_enabled_set(), "Installation must remain opt-in")
        desktop("installed")

        # Exercise the real admission/selection transaction without contacting
        # an existing gateway or starting any background services.
        require(pc._activate_key("lan-control", enable=True), "Activation did not change selection")
        require("lan-control" in pc._get_enabled_set(), "Activation was not persisted")
        require(any(row[0] == "lan-control" and Path(row[4]).resolve() == installed
                    for row in pc._discover_all_plugins()), "Installed plugin was not discovered")
        require((installed / "dashboard/manifest.json").is_file(), "Backend manifest missing")
        api = load_backend(installed / "dashboard/plugin_api.py")
        from agent.secret_scope import set_secret_scope, reset_secret_scope
        scope = set_secret_scope({}, profile_home=str(home))
        stack.callback(reset_secret_scope, scope)
        stack.enter_context(patch.object(api, "notify_settings_changed", lambda: None))
        save_config({"plugins": {"entries": {"unrelated": {"settings": {"keep": "untouched"}}}}}, merge_existing=True)
        settings = {**api.DEFAULTS, "host": "127.0.0.1", "health_url": "https://health.invalid/health"}
        api.write_settings(settings)
        api.write_token("SYNTHETIC-LIFECYCLE-TOKEN")
        (installed / "local-state.txt").write_text("keep user data", encoding="utf-8")
        before = api.read_configuration()

        async def probe_backend():
            from fastapi import FastAPI
            import httpx
            app = FastAPI()
            app.include_router(api.router)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
                loaded = await http.get("/settings")
                require(loaded.status_code == 200 and loaded.json()["has_token"], "Installed backend settings route failed")
                require("SYNTHETIC-LIFECYCLE-TOKEN" not in loaded.text, "Settings API leaked the synthetic token")
        asyncio.run(probe_backend())
        print("PASS: Git install, default-off discovery, activation and installed backend route", flush=True)

        # A fixture-only revision changes both manifests and desktop bytes.
        # The actual plugin repository and its index are never committed.
        manifest = yaml.safe_load((origin / "plugin.yaml").read_text(encoding="utf-8"))
        manifest["version"] = "999.0.0-lifecycle"
        (origin / "plugin.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
        backend = json.loads((origin / "dashboard/manifest.json").read_text(encoding="utf-8"))
        backend["version"] = manifest["version"]
        (origin / "dashboard/manifest.json").write_text(json.dumps(backend), encoding="utf-8")
        with (origin / "desktop/plugin.js").open("a", encoding="utf-8") as output:
            output.write("\n// Synthetic lifecycle update marker.\n")
        git(origin, "add", ".")
        git(origin, "commit", "-qm", "Lifecycle fixture update")
        revision = git(origin, "rev-parse", "HEAD")
        command("update", "lan-control")
        require(pc._read_install_metadata()["lan-control"]["revision"] == revision, "Update did not record new revision")
        require(json.loads((installed / "dashboard/manifest.json").read_text())["version"] == manifest["version"],
                "Backend files were not updated")
        require(api.read_configuration() == before, "Update changed persisted settings or secret")
        require("lan-control" in pc._get_enabled_set(), "Update lost activation")
        require((installed / "local-state.txt").read_text() == "keep user data", "Update lost untracked user data")
        api = load_backend(installed / "dashboard/plugin_api.py")
        require(api.read_configuration() == before, "Updated backend could not read the preserved settings/token")
        asyncio.run(probe_backend())
        desktop("updated")

        # Also exercise the immutable-SHA installation path used by releases.
        command("install", source_url, "--force", "--ref", revision, "--no-enable", "--yes-deps")
        record = pc._read_install_metadata()["lan-control"]
        require(record["pinned"] and record["revision"] == revision, "Exact-SHA reinstall was not pinned")
        refusal = io.StringIO()
        with redirect_stdout(refusal):
            try:
                command("update", "lan-control")
            except SystemExit as exc:
                require(exc.code == 1, "Pinned update returned an unexpected status")
            else:
                raise AssertionError("Mutable update must refuse a pinned plugin")
        require("pinned" in refusal.getvalue() and revision in refusal.getvalue(),
                "Mutable update failed for a reason other than the exact-SHA pin")
        require(pc._read_install_metadata()["lan-control"] == record, "Refused update changed its install record")
        require(api.read_configuration() == before, "Pinned reinstall changed settings or secret")
        print("PASS: real update, preserved settings/token/user data, exact-SHA reinstall and pin refusal", flush=True)

        require(pc._activate_key("lan-control", enable=False), "Deactivation did not change selection")
        require("lan-control" not in pc._get_enabled_set(), "Deactivation was not persisted")
        command("remove", "lan-control")
        require(not installed.exists(), "Plugin directory survived removal")
        require("lan-control" not in pc._read_install_metadata(), "Install metadata survived removal")
        config = load_config_readonly()["plugins"]
        require("lan-control" not in config.get("enabled", []) + config.get("disabled", []), "Stale selection after removal")
        require("lan-control" not in config.get("entries", {}), "Plugin settings survived removal")
        require(config["entries"]["unrelated"]["settings"]["keep"] == "untouched", "Removal changed unrelated settings")
        # Hermes deliberately retains env-backed secrets on plugin removal;
        # test the documented separate explicit deletion as well.
        require(api.read_token() == "SYNTHETIC-LIFECYCLE-TOKEN", "Removal unexpectedly changed the stored token")
        from hermes_cli.config import remove_env_value
        require(remove_env_value(api.TOKEN_KEY), "Explicit token cleanup failed")
        require(not api.read_token(), "Synthetic token survived explicit cleanup")
        desktop("removed")
        print("PASS: deactivation/removal and metadata cleanup; unrelated settings preserved", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", type=Path, required=True)
    args = parser.parse_args()
    source = args.hermes_source.resolve()
    expected = (ROOT / "tests/hermes-revision.txt").read_text().strip()
    require(git(source, "rev-parse", "HEAD") == expected, "Hermes source does not match the reviewed test pin")
    require(not git(source, "status", "--porcelain", "--untracked-files=no"), "Hermes source has tracked modifications")
    with tempfile.TemporaryDirectory(prefix="lan-lifecycle-") as temporary:
        exercise(source, Path(temporary).resolve())
    print(f"PASS: offline lifecycle against Hermes {expected}", flush=True)


if __name__ == "__main__":
    main()
