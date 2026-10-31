# Hermes compatibility baseline

Initial baseline verified on 2026-10-31: **Hermes v0.22.5**, for both the backend
and Desktop. The manifest at that check declared `requires_hermes: ">=0.22.5"`.
This report and its fingerprints retain that original snapshot. Subsequent
[older-release tests](older-hermes.md) established the current supported minimum
of **v0.22.3**. The original conservative baseline was not a claim that v0.22.5
was the first possible implementation.

## Inputs and identity

- Official [v0.22.5 release](https://github.com/NousResearch/hermes-agent/releases/tag/v0.22.5).
- Exact upstream commit: `bf0203a794a07aac3ff764972ab9087baa4e4f4e`, also pinned
  in `tests/hermes-revision.txt` for the separate offline Git lifecycle test.
- Backend source: a fresh archive at that exact commit, with the matching,
  clean build identity. The runtime reported base version `0.22.5`.
- Desktop: an existing local build from the clean v0.22.5 source, copied into
  the disposable release tree. This was not a newly downloaded packaged app
  or a fresh build of all frontend dependencies.
- Python: a separate copy of the prepared release environment; FastAPI
  `0.133.1`, HTTPX `0.28.1`, Pydantic `2.13.4`, Uvicorn `0.41.0`.
- Native execution: macOS, using Playwright's Electron driver. Exact plugin
  source and desktop bundle hashes are in the accompanying
  [native result](hermes-0.22.5-native.json).

The `requires_hermes` gate compares the backend's base release version. It does
not independently enforce a remote client's Desktop version; the Desktop
requirement is therefore also stated in README.md.

## API review and older releases

The plugin uses the public Desktop SDK's `host`, `STATUSBAR_AREAS`,
`gatewayActivationEpoch`, `Input`, `Button`, `ListRow`, settings registration,
events, timers, disposal and profile-scoped REST. The backend uses Hermes'
profile-aware config/secret readers, `save_config` with preservation and merge
options, secure env writing, secret-scope refresh and plugin event broadcasting.
The native smoke below exercises these with the real SDK/backend.

A source comparison found this non-monotonic SDK export history:

| Hermes tag | `gatewayActivationEpoch` exported by the public SDK |
| --- | --- |
| v0.22.0 | No |
| v0.22.1 | Yes |
| v0.22.2 | No |
| v0.22.3 | Yes |
| v0.22.4 | Yes |
| v0.22.5 | Yes |

This checks the actual SDK export module; the runtime shim imports that module.
The missing export prevents this plugin's ESM import from loading unchanged on
v0.22.2. Symbol presence alone is not proof of compatibility on the other older
releases. At the time of this initial report they had not been subjected to the
full native smoke. The subsequent older-release report supplies that additional
evidence; do not lower a manifest floor solely from this export table.

## Verification

| Check | Result |
| --- | --- |
| Actual Hermes config/secret API integration in temporary homes | Passed: persistence, revision conflicts, pre-save validation, unrelated settings retained |
| Official admission validator at the pinned release | 16 checks passed, no warnings; no dependency installation requested |
| Backend version gate | v0.22.5 accepted; v0.22.4 rejected by the declared range |
| Python regression suite | 50 tests passed |
| JavaScript syntax/controller suite | Syntax passed; 26 tests passed |
| Native package discovery and explicit Desktop activation | Passed |
| Native status rendering and manual recheck | Passed against loopback ping |
| Own native settings page | Passed with actual SDK `ListRow`/`Input`/`Button` components |
| Save name, health URL and synthetic bearer token | Passed through the real backend routes; config and secure env persistence confirmed |
| Authenticated HTTP health check | Passed; the loopback fixture received the expected synthetic bearer token |
| Wake control | Disabled throughout; never clicked; MAC and broadcast remained empty |
| Python UDP boundary | Test-only audit guard intercepts `socket.sendto`; synthetic negative control passed without sending a packet |
| Cleanup | Native application closed and its own profile gateway stopped |

The native driver is `tests/check_native_compatibility.mjs`; its UDP interception
fixture is `tests/native_no_udp.py`. Setup and invocation are documented in
RUNBOOK.md. It creates fresh backend, desktop and child-home directories,
inherits no credentials, disables protocol registration and lazy installation,
and uses inert provider credentials. Plugin checks and the provider fixture use
loopback. Hermes itself can fetch public catalogs/update metadata; the native
test is not an offline sandbox.

Early probe iterations were used to adapt the UI selectors and startup order.
One stopped before plugin loading because the gateway cold start timed out.
The successful final run prewarmed its own gateway, closed the native package
dialog before interacting with the status bar, and checked the actual settings
page. No upstream source patch, app-bundle patch, or DOM patch was used.

## Remaining release work and limits

This verifies the chosen baseline's integration, not all native UI states,
profile-switch races on actual hardware, Linux/Windows Desktop behavior, or
physical Wake-on-LAN. Those claims require additional evidence. The standalone
unit suite still supplies mocked concurrency and failure-path coverage.

The prepared dependency environment was reused, not installed from scratch by
Hermes PM. The official validator was run without `--install-deps`. Before a
release/catalog submission, validate the exact committed release tree with
`hermes plugins validate <checkout> --install-deps` in a dedicated environment.
No release, catalog submission, source commit or push is implied by this report.
The production Hermes installation and its profiles were not modified by these
compatibility probes.
