"""Profilgebundene Health-Prüfung und explizites Wake-on-LAN ohne Shell."""

from __future__ import annotations

import asyncio
import ipaddress
import hashlib
import hmac
import json
import math
import os
import platform
import re
import secrets
import shutil
import socket
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter()
COOLDOWN = 10.0
# Includes lock contention; the desktop transport allows another three seconds.
REQUEST_BUDGET = 32.0
TOKEN_KEY = "LAN_CONTROL_BEARER_TOKEN"
DEFAULTS = {"display_name": "", "host": "", "wol_mac": "", "wol_broadcast": "", "wol_port": 9,
            "interval": 60, "timeout": 2, "health_url": "", "expected_status": ""}
_revision_key = secrets.token_bytes(32)


class ConfigError(ValueError):
    """Configuration validation failed; exception arguments remain internal."""


# Public responses select a trusted catalog entry, never exception text. Keep
# field names tied to our schema rather than a request's keys or input values.
PUBLIC_CONFIG_ERRORS = (
    "Plugin settings are invalid.",
    "Enter a valid Wake-on-LAN MAC address.",
    "Wake-on-LAN requires a unicast MAC address for a single device.",
    "Use an HTTP or HTTPS health URL without embedded credentials or a fragment.",
    "Display name must be no longer than 200 characters.",
    "Timeout must be shorter than the check interval.",
    "Bearer token contains invalid characters.",
    "Set both the MAC address and broadcast address to enable Wake-on-LAN.",
    "Broadcast address must be an IPv4 address.",
    "This IPv4 address cannot be used as a Wake-on-LAN destination.",
    "Enter a valid host to ping.",
    "Ping requires the address of a single host.",
    "Enter an IP address or DNS hostname for the ping target.",
    "Enter a valid hostname for the ping target.",
    "Some settings could not be saved. Check managed configuration.",
    "Ping is not supported on this operating system.",
    "The ping program is not installed on the backend.",
    "Set a health URL before saving a token.",
    "The saved token is not active for this profile.",
    *(f"{key}: {reason}" for key in DEFAULTS for reason in (
        "enter a number.", "value is outside the allowed range.",
        "enter a whole number.", "invalid text.",
    )),
)


def public_config_error(error):
    for message in PUBLIC_CONFIG_ERRORS:
        if error.args == (message,):
            return message
    return "Plugin settings are invalid."


def number(raw, key, default, low, high, integer=False):
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{key}: enter a number.")
    if not math.isfinite(value) or not low <= value <= high:
        raise ConfigError(f"{key}: value is outside the allowed range.")
    if integer and int(value) != value:
        raise ConfigError(f"{key}: enter a whole number.")
    return int(value) if integer else float(value)


def string(raw, key):
    value = raw.get(key, "")
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ConfigError(f"{key}: invalid text.")
    return value.strip()


def normalize_mac(value):
    if not re.fullmatch(r"(?:[0-9a-fA-F]{12}|(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}|(?:[0-9a-fA-F]{2}-){5}[0-9a-fA-F]{2})", value):
        raise ConfigError("Enter a valid Wake-on-LAN MAC address.")
    compact = value.replace(":", "").replace("-", "").lower()
    octets = bytes.fromhex(compact)
    if not any(octets) or octets[0] & 1:
        raise ConfigError("Wake-on-LAN requires a unicast MAC address for a single device.")
    return ":".join(compact[i:i + 2] for i in range(0, 12, 2))


@dataclass(frozen=True)
class Settings:
    label: str
    host: str
    url: str = field(repr=False)
    expected: int = 200
    interval: float = 60.0
    timeout: float = 2.0
    token: str = field(default="", repr=False)
    mac: str = ""
    broadcast: str = ""
    port: int = 9
    wol_error: str = ""

    @classmethod
    def parse(cls, raw, token=""):
        if not isinstance(raw, dict):
            raise ConfigError("Plugin settings are invalid.")
        url = string(raw, "health_url")
        host = string(raw, "host")
        try:
            parts = urlsplit(url)
            valid = (parts.scheme in ("http", "https") and parts.hostname
                     and parts.username is None and parts.password is None
                     and not parts.fragment and parts.port != 0
                     and not any(c.isspace() for c in url) and "\\" not in url)
        except ValueError:
            valid = False
        if url and not valid:
            raise ConfigError("Use an HTTP or HTTPS health URL without embedded credentials or a fragment.")
        host = normalize_host(host or (parts.hostname if url else ""))
        label = string(raw, "display_name") or host
        if len(label) > 200:
            raise ConfigError("Display name must be no longer than 200 characters.")
        interval = number(raw, "interval", DEFAULTS["interval"], 2, 300)
        timeout = number(raw, "timeout", 2, 0.2, 30)
        if timeout >= interval:
            raise ConfigError("Timeout must be shorter than the check interval.")
        if not url:
            token = ""
        if not isinstance(token, str) or any(ord(c) < 32 or ord(c) > 126 for c in token):
            raise ConfigError("Bearer token contains invalid characters.")
        expected = 200
        if url:
            value = raw.get("expected_status", "")
            if isinstance(value, str):
                value = value.strip()
            if value is None or value == "":
                value = 200
            if isinstance(value, str) and re.fullmatch(r"\d{3}", value):
                value = int(value)
            expected = number({"expected_status": value}, "expected_status", 200, 100, 599, integer=True)
        mac, broadcast, port, wol_error = "", "", 9, ""
        try:
            mac, broadcast = string(raw, "wol_mac"), string(raw, "wol_broadcast")
            port = number(raw, "wol_port", 9, 1, 65535, integer=True)
            if not mac or not broadcast:
                raise ConfigError("Set both the MAC address and broadcast address to enable Wake-on-LAN.")
            mac = normalize_mac(mac)
            try:
                address = ipaddress.IPv4Address(broadcast)
            except ipaddress.AddressValueError:
                raise ConfigError("Broadcast address must be an IPv4 address.") from None
            if address.is_multicast or address.is_unspecified or address.is_loopback:
                raise ConfigError("This IPv4 address cannot be used as a Wake-on-LAN destination.")
            broadcast = str(address)
        except ConfigError as exc:
            wol_error = public_config_error(exc)
        return cls(label, host, url, expected, interval, timeout, token, mac, broadcast, port, wol_error)


def normalize_host(value):
    if not value or len(value) > 253 or any(c.isspace() for c in value):
        raise ConfigError("Enter a valid host to ping.")
    try:
        address = ipaddress.ip_address(value)
        if address.is_multicast or address.is_unspecified or str(address) == "255.255.255.255":
            raise ConfigError("Ping requires the address of a single host.")
        return str(address)
    except ValueError as exc:
        if isinstance(exc, ConfigError):
            raise
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.?", value):
        raise ConfigError("Enter an IP address or DNS hostname for the ping target.")
    if all(c in "0123456789." for c in value) or any(not part or len(part) > 63 or part.startswith("-") or part.endswith("-") for part in value.rstrip(".").split(".")):
        raise ConfigError("Enter a valid hostname for the ping target.")
    return value.lower()


def read_profile_settings():
    """Hermes setzt Home und Secret-Scope je eingehender Profilanfrage."""
    from hermes_cli.config import load_config_readonly
    from hermes_constants import hermes_home_key

    config = load_config_readonly()
    raw = config.get("plugins", {}).get("entries", {}).get("lan-control", {}).get("settings", {})
    return hermes_home_key(), raw


def read_configuration():
    scope, raw = read_profile_settings()
    return scope, raw, read_token()


def read_token():
    from agent.secret_scope import get_secret, refresh_installed_secret_scope
    from hermes_constants import get_hermes_home
    # A request waiting for the profile lock may carry a secret-scope snapshot
    # from before another request rotated the token. Re-read through Hermes.
    refresh_installed_secret_scope(get_hermes_home())
    return get_secret(TOKEN_KEY) or ""


def configuration_revision(scope, raw, token):
    payload = json.dumps([scope, raw, token], sort_keys=True, default=str).encode()
    return hmac.new(_revision_key, payload, hashlib.sha256).hexdigest()


def settings_payload(scope, raw, token):
    if not isinstance(raw, dict):
        raise ConfigError("Plugin settings are invalid.")
    values = {key: raw.get(key, default) for key, default in DEFAULTS.items()}
    values["expected_status"] = str(values["expected_status"] or "")
    inferred = False
    if not values["host"] and values["health_url"]:
        try:
            values["host"] = normalize_host(urlsplit(values["health_url"]).hostname or "")
            inferred = True
        except (ValueError, TypeError):
            pass
    return {"values": values, "has_token": bool(token), "inferred_host": inferred,
            "revision": configuration_revision(scope, raw, token)}


def write_token(value):
    from hermes_cli.config import save_env_value_secure
    save_env_value_secure(TOKEN_KEY, value)


def write_settings(values):
    """Use Hermes' public, profile-scoped config writer, preserving unrelated settings."""
    from hermes_cli.config import is_managed, save_config
    if is_managed():
        raise PermissionError("Settings are managed.")
    save_config({"plugins": {"entries": {"lan-control": {"settings": values}}}},
                preserve_keys={("plugins", "entries", "lan-control", "settings", key) for key in values},
                merge_existing=True)
    # Administrator-managed keys may be retained by Hermes rather than overwritten.
    _, actual = read_profile_settings()
    if any(actual.get(key) != value for key, value in values.items()):
        raise ConfigError("Some settings could not be saved. Check managed configuration.")


def notify_settings_changed():
    from hermes_cli.plugin_events import broadcast_plugin_event
    broadcast_plugin_event("lan-control", "settings.changed", {"written": list(DEFAULTS)})


def load_runtime():
    scope, raw = read_profile_settings()
    token = ""
    url = raw.get("health_url") if isinstance(raw, dict) else None
    if isinstance(url, str) and url.strip():
        token = read_token()
    return scope, Settings.parse(raw, token)


async def http_probe(settings):
    headers = {"Authorization": f"Bearer {settings.token}"} if settings.token else {}
    try:
        # Keine Proxys aus der Umgebung, keine Redirects und kein Antwortkörper.
        async with httpx.AsyncClient(verify=True, follow_redirects=False, trust_env=False,
                                    timeout=settings.timeout) as client:
            async def request():
                async with client.stream("GET", settings.url, headers=headers) as response:
                    return response.status_code
            status = await asyncio.wait_for(request(), timeout=settings.timeout)
        if status in (401, 403):
            return "auth_error", "The health endpoint rejected authentication."
        if status == settings.expected:
            return "healthy", "The health endpoint returned the expected HTTP status."
        return "unhealthy", f"The health endpoint returned HTTP {status}; expected {settings.expected}."
    except (httpx.TimeoutException, TimeoutError):
        return "unhealthy", "The health check timed out."
    except httpx.TransportError:
        return "unhealthy", "Cannot reach the health endpoint. Check the connection and TLS certificate."
    except Exception:
        # Auch unerwartete Fehler niemals als schlafenden Rechner interpretieren.
        return "backend_error", "The health check could not be completed."


async def resolve_host(host):
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        results = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_DGRAM)
        if not results:
            raise OSError("DNS resolution failed")
        # IPv4 bevorzugen; ein aufgelöstes IPv6-Ziel ist ebenfalls zulässig.
        results.sort(key=lambda row: row[0] != socket.AF_INET)
        return results[0][4][0]


def ping_command(address, timeout, system):
    ipv6 = ipaddress.ip_address(address).version == 6
    if system == "Darwin":
        name, args = ("ping6" if ipv6 else "ping"), ["-n", "-c", "1"]
    elif system == "Linux":
        name, args = "ping", ["-6" if ipv6 else "-4", "-n", "-c", "1"]
    elif system == "Windows":
        name, args = "ping", ["-6" if ipv6 else "-4", "-n", "1", "-w", str(max(1, math.ceil(timeout * 1000)))]
    else:
        raise ConfigError("Ping is not supported on this operating system.")
    binary = shutil.which(name)
    if not binary:
        raise ConfigError("The ping program is not installed on the backend.")
    return [binary, *args, address]


def windows_ping_reply(stdout, address):
    """Read per-packet numeric fields, without decoding localized OEM text.

    IPv4 echo replies include TTL=<number>; IPv6 replies include a measured
    round-trip time. Error replies have neither. Headers/statistics have no
    nonempty reply payload after the numeric responder address and colon.
    """
    target = ipaddress.ip_address(address)
    reply = False
    for line in stdout.splitlines():
        match = re.search(
            rb"(?<![0-9a-fA-F:.])([0-9a-fA-F:.]+(?:%[0-9]+)?)"
            rb"(?::\s+|\s+[^:]*:\s+)(.+)$", line)
        if not match:
            continue
        try:
            responder = ipaddress.ip_address(match[1].decode("ascii"))
        except ValueError:
            continue
        reply = True
        payload = match[2]
        evidence = (re.search(rb"\bTTL\s*=\s*[0-9]+\b", payload, re.I)
                    if target.version == 4 else
                    re.search(rb"[=<]\s*[0-9]+(?:[.,][0-9]+)?\s*[^\s0-9=<>]+", payload))
        if responder == target and evidence:
            return "up"
    return "down" if reply else "error"


async def ping_probe(settings, *, system=None, spawn=None, resolve=None):
    """Ein Paket ohne Shell; DNS, Start und Antwort teilen eine Deadline."""
    system = system or platform.system()
    spawn = spawn or asyncio.create_subprocess_exec
    resolve = resolve or resolve_host
    process, communication = None, None
    try:
        async with asyncio.timeout(settings.timeout):
            address = normalize_host(await resolve(settings.host))
            command = ping_command(address, settings.timeout, system)
            process = await spawn(*command, stdout=asyncio.subprocess.PIPE,
                                  stderr=asyncio.subprocess.PIPE,
                                  env={**os.environ, "LC_ALL": "C", "LANG": "C"})
            communication = asyncio.create_task(process.communicate())
            stdout, stderr = await asyncio.shield(communication)
            if system == "Windows" and process.returncode in (0, 1) and not stderr.strip():
                result = windows_ping_reply(stdout, address)
                if result == "up" and process.returncode == 0:
                    return "up", "The host responds to ping."
                if result == "down":
                    return "down", "The host does not respond to ping."
            elif system != "Windows" and process.returncode == 0:
                return "up", "The host responds to ping."
            # macOS: 2 = keine Antwort, Linux: 1; alle anderen Codes sind lokale Fehler.
            no_reply = 2 if system == "Darwin" else 1
            if process.returncode == no_reply and not stderr.strip():
                if system != "Windows" or any(word in stdout.lower() for word in (
                        b"request timed out", b"berschreitung der anforderung")):
                    return "down", "The host does not respond to ping."
            return "error", "Ping failed. Check the ping program, permissions, and network."
    except TimeoutError:
        if process is not None:
            return "down", "The host did not respond to ping before the timeout."
        return "error", "DNS lookup or ping startup timed out."
    except asyncio.CancelledError:
        raise
    except Exception:
        return "error", "Could not start ping. Check the ping program, permissions, and DNS."
    finally:
        if process is not None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            # shield verhindert, dass Timeout/Cancel die Pipe-Leser verwaisen lässt.
            if communication is not None:
                try:
                    await asyncio.shield(communication)
                except Exception:
                    pass
            # Auch ein Fehler des Pipe-Lesers darf keinen Zombie hinterlassen.
            await asyncio.shield(process.wait())


@dataclass(frozen=True)
class Outcome:
    status: str
    reason: str
    host_status: str = "unknown"
    http_status: str = "disabled"


def combine_checks(host_result, http_result=None):
    host_state, host_reason = host_result
    http_state, http_reason = http_result or ("disabled", "HTTP health check is disabled.")
    reason = f"{host_reason} {http_reason}"
    if host_state not in ("up", "down"):
        return Outcome("backend_error", reason, "unknown", http_state)
    if http_state == "backend_error":
        return Outcome("backend_error", reason, host_state, http_state)
    if http_state == "disabled":
        return Outcome("healthy" if host_state == "up" else "unhealthy", reason, host_state, http_state)
    if host_state == "up" and http_state == "healthy":
        return Outcome("healthy", reason, host_state, http_state)
    if host_state == "up" or http_state == "healthy":
        return Outcome("degraded", reason, host_state, http_state)
    return Outcome("unhealthy", reason, host_state, http_state)


async def probe(settings, *, ping=None, http=None):
    ping, http = ping or ping_probe, http or http_probe
    if settings.url:
        host_result, http_result = await asyncio.gather(ping(settings), http(settings))
        return combine_checks(host_result, http_result)
    return combine_checks(await ping(settings))


def send_magic_packet(settings):
    packet = b"\xff" * 6 + bytes.fromhex(settings.mac.replace(":", "")) * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.5)
        sock.sendto(packet, (settings.broadcast, settings.port))


@dataclass
class State:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    settings: Settings | None = None
    revision: str = ""
    status: str = "unknown"
    reason: str = "Not checked yet."
    host_status: str = "unknown"
    http_status: str = "disabled"
    checked: float | None = None
    cooldown_until: float = 0


class Service:
    """Ein Cache/Lock je Backend-Profil, unabhängig von Desktop-Fenstern."""

    def __init__(self, load=load_runtime, check=probe, send=send_magic_packet, clock=time.monotonic):
        self.load, self.check, self.send, self.clock = load, check, send, clock
        self.states = {}

    def config(self):
        try:
            return self.load(), None
        except ConfigError as exc:
            reason = public_config_error(exc)
        except Exception:
            reason = "Could not read plugin settings or credentials for this profile."
        # Auch ungültig → gültig (mit denselben Werten) benötigt einen neuen Check.
        for state in self.states.values():
            state.checked = None
            state.revision = uuid.uuid4().hex
        return None, {"status": "config_error", "reason": reason, "label": "LAN Control",
                      "wake_allowed": False, "revision": "", "valid_for": 0,
                      "next_check_in": 5, "cooldown_remaining": 0}

    def adopt(self, state, settings):
        if state.settings != settings:
            state.settings, state.revision = settings, uuid.uuid4().hex
            state.status, state.reason, state.checked = "unknown", "Not checked yet.", None

    async def refresh(self, state):
        result = await self.check(state.settings)
        state.status, state.reason = result.status, result.reason
        state.host_status, state.http_status = result.host_status, result.http_status
        state.checked = self.clock()

    def snapshot(self, state):
        now, settings = self.clock(), state.settings
        age = now - state.checked if state.checked is not None else math.inf
        valid_for = max(0.0, settings.interval + settings.timeout + 1 - age)
        status = state.status if valid_for > 0 else "stale"
        cooldown = max(0.0, state.cooldown_until - now)
        reason = state.reason if valid_for > 0 else "Status is out of date."
        if state.host_status == "down" and settings.wol_error:
            reason += " " + settings.wol_error
        if cooldown:
            reason += " Please wait before sending another Wake-on-LAN packet."
        return {"label": settings.label, "status": status, "reason": reason,
                "host_status": state.host_status, "http_status": state.http_status,
                "wake_allowed": status in ("unhealthy", "degraded") and state.host_status == "down" and not settings.wol_error and not cooldown,
                "revision": state.revision, "valid_for": valid_for,
                "wake_expires_at": now + REQUEST_BUDGET,
                "wake_valid_for": REQUEST_BUDGET,
                "next_check_in": max(0.1, settings.interval - age),
                "cooldown_remaining": cooldown}

    def unchanged(self, scope, settings):
        current, error = self.config()
        return not error and current == (scope, settings)

    async def status(self, *, force=False):
        try:
            async with asyncio.timeout(REQUEST_BUDGET):
                return await self._status(force=force)
        except TimeoutError:
            return self.expired()

    async def _status(self, *, force=False):
        current, error = self.config()
        if error:
            return error
        scope, settings = current
        state = self.states.setdefault(scope, State())
        async with state.lock:
            # Konfiguration kann sich während des Wartens auf das Lock ändern.
            if not self.unchanged(scope, settings):
                return self.changed()
            self.adopt(state, settings)
            if force or state.checked is None or self.clock() - state.checked >= settings.interval:
                await self.refresh(state)
            if not self.unchanged(scope, settings):
                state.checked = None
                return self.changed()
            return self.snapshot(state)

    @staticmethod
    def expired():
        return {**Service.changed(), "status": "backend_error",
                "reason": "The request timed out. Check again.", "next_check_in": 5}

    @staticmethod
    def changed():
        return {"status": "unknown", "reason": "Settings changed. Check again.",
                "label": "LAN Control", "wake_allowed": False, "revision": "",
                "valid_for": 0, "next_check_in": 0.1, "cooldown_remaining": 0}

    async def wake(self, revision, *, disconnected=None, expires_at=None):
        # Echoed server-clock expiry also accounts for queues in a proxy/plugin
        # host, before this handler starts. It is a lifetime bound, not a grant.
        deadline = min(self.clock() + REQUEST_BUDGET, expires_at) if expires_at is not None else self.clock() + REQUEST_BUDGET
        try:
            async with asyncio.timeout(max(0, deadline - self.clock())):
                return await self._wake(revision, deadline, disconnected)
        except TimeoutError:
            return {"outcome": "blocked", "state": self.expired()}

    async def _wake(self, revision, deadline, disconnected):
        if self.clock() >= deadline:
            return {"outcome": "blocked", "state": self.expired()}
        current, error = self.config()
        if error:
            return {"outcome": "blocked", "state": error}
        scope, settings = current
        state = self.states.setdefault(scope, State())
        async with state.lock:
            if self.clock() >= deadline:
                return {"outcome": "blocked", "state": self.expired()}
            if not self.unchanged(scope, settings):
                return {"outcome": "blocked", "state": self.changed()}
            self.adopt(state, settings)
            snapshot = self.snapshot(state)
            if not revision or revision != state.revision or not snapshot["wake_allowed"]:
                return {"outcome": "blocked", "state": snapshot}
            # Immer frisch prüfen, auch wenn das UI noch Rot zeigt.
            await self.refresh(state)
            if not self.unchanged(scope, settings):
                state.checked = None
                return {"outcome": "blocked", "state": self.changed()}
            if not self.snapshot(state)["wake_allowed"]:
                return {"outcome": "blocked", "state": self.snapshot(state)}
            # Also check the clock explicitly: cleanup or a blocked event loop may
            # overrun the deadline before asyncio can deliver its cancellation.
            if (disconnected is not None and await disconnected()) or self.clock() >= deadline:
                return {"outcome": "blocked", "state": self.expired()}
            # Auch fehlgeschlagene Versandversuche begrenzen; kein Retry-Sturm.
            state.cooldown_until = self.clock() + COOLDOWN
            try:
                self.send(settings)
                outcome = "sent"
            except Exception:
                outcome = "send_error"
            return {"outcome": outcome, "state": self.snapshot(state)}


class WakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(min_length=1, max_length=64)
    expires_at: float = Field(gt=0, allow_inf_nan=False)


service = Service()


@router.get("/status")
async def status_route():
    return await service.status()


@router.post("/recheck")
async def recheck_route():
    return await service.status(force=True)


@router.get("/settings")
async def settings_route():
    try:
        return settings_payload(*read_configuration())
    except Exception:
        raise HTTPException(503, "Could not read settings.") from None


@router.post("/settings")
async def save_settings_route(request: Request):
    # Never let Pydantic echo a token-bearing body in a validation error.
    try:
        body = await request.json()
        if not isinstance(body, dict) or set(body) - {"revision", "values", "token"}:
            raise ValueError()
        values, revision, token = body["values"], body["revision"], body.get("token", "")
        if not isinstance(values, dict) or set(values) - set(DEFAULTS):
            raise ValueError()
        if set(DEFAULTS) - {"expected_status"} - set(values):
            raise ValueError()
        if not isinstance(token, str) or len(token) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError()
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision):
            raise ValueError()
    except Exception:
        raise HTTPException(400, "Invalid settings request. Nothing was saved.") from None
    changed = False
    try:
        scope, _, _ = read_configuration()
        state = service.states.setdefault(scope, State())
        async with asyncio.timeout(REQUEST_BUDGET):
            async with state.lock:
                current_scope, raw, saved_token = read_configuration()
                if current_scope != scope or not hmac.compare_digest(configuration_revision(scope, raw, saved_token), revision):
                    return {"ok": False, "outcome": "conflict"}
                candidate = {**DEFAULTS, **raw, **values}
                # Validate the entire prospective configuration before any write.
                for key in DEFAULTS.keys() - {"wol_port", "interval", "timeout", "expected_status"}:
                    string(candidate, key)
                normalize_host(string(candidate, "host"))
                number(candidate, "wol_port", 9, 1, 65535, integer=True)
                if ("expected_status" in values or candidate["health_url"].strip()) and not isinstance(candidate["expected_status"], str):
                    raise ConfigError("expected_status: invalid text.")
                Settings.parse(candidate, token or saved_token)
                if token and not candidate["health_url"].strip():
                    raise ConfigError("Set a health URL before saving a token.")
                changed = True
                try:
                    # Persist the pause BEFORE changing a secret. Even a crash or
                    # partial filesystem failure must leave HTTP disabled rather
                    # than pair the old endpoint with a new token (or vice versa).
                    write_settings({"health_url": ""})
                    if token:
                        write_token(token)
                        if not hmac.compare_digest(read_configuration()[2], token):
                            raise ConfigError("The saved token is not active for this profile.")
                    write_settings({key: value for key, value in values.items() if key != "health_url"})
                    write_settings({"health_url": values["health_url"]})
                except Exception:
                    try:
                        write_settings({"health_url": ""})
                    except Exception:
                        pass
                    stored = settings_payload(*read_configuration())
                    return {"ok": False, "outcome": "save_error",
                            "settings": stored, "http_paused": not stored["values"]["health_url"]}
                finally:
                    state.checked = None
                    state.revision = uuid.uuid4().hex
                return {"ok": True, "settings": settings_payload(*read_configuration())}
    except ConfigError as exc:
        return {"ok": False, "outcome": "invalid", "message": public_config_error(exc)}
    except TimeoutError:
        return {"ok": False, "outcome": "timeout"}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Could not save settings.") from None
    finally:
        # Emitting only after releasing the lock prevents re-entrant checks from
        # observing an intermediate endpoint/credential pair.
        if changed:
            try:
                notify_settings_changed()
            except Exception:
                pass


@router.post("/wake")
async def wake_route(body: WakeRequest, request: Request):
    return await service.wake(body.revision, expires_at=body.expires_at, disconnected=request.is_disconnected)
