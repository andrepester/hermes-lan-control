# Earlier Hermes release compatibility

Checked on 2026-10-31. The supported minimum is now **v0.22.3** for both
backend and Desktop, replacing the initial conservative v0.22.5 minimum.
The manifest declares `requires_hermes: ">=0.22.3"`.

## Results

| Hermes | Config/secret API integration | Native Desktop result | Support decision |
| --- | --- | --- | --- |
| v0.21.6 | Passed | Not run: required SDK export absent in source | Unsupported |
| v0.21.7 | Passed | Not run: required SDK export absent in source | Unsupported |
| v0.22.0 | Passed | Not run: required SDK export absent in source | Unsupported |
| v0.22.1 | Passed | Full smoke passed on its own Desktop build | Passing older island; outside supported range |
| v0.22.2 | Passed | Loader rejected the plugin: missing `gatewayActivationEpoch` export | Unsupported; native negative control |
| v0.22.3 | Passed | Full smoke passed on its own Desktop build | Supported minimum |
| v0.22.4 | Passed | Full smoke passed on its own Desktop build | Supported |
| v0.22.5 | Passed in prior baseline check | Full smoke passed in prior baseline check | Supported |

The backend column refers specifically to `tests/check_hermes_config.py`:
persistence, revision conflicts, pre-save validation, and preservation of
unrelated settings. It is not a full backend or native acceptance result for
the three releases whose Desktop API contract already fails source inspection.

The `v0.22.2` native UI reported that its SDK module does not provide an export
named `gatewayActivationEpoch`. This confirms the source-level result using the
actual old loader. The plugin was never activated successfully on that release.
Earlier `v0.22.0`, `v0.21.7`, and `v0.21.6` also lack that named SDK export; their
SDK runtime shims re-export the public module's actual names.

The passing `v0.22.1` probe does not justify a simple `>=0.22.1` declaration:
that would include the broken `v0.22.2` release. The chosen support policy uses
the lowest tested **continuous** working range, `>=0.22.3`, without a special
exception range. Current packages therefore deliberately reject v0.22.1 through
metadata even though its runtime probe passed.

## What was exercised

The native smoke ran on macOS with actual release-specific Desktop source,
not a newer renderer placed over an older Python checkout. It covered:

- Discovery of the unified package and explicit Desktop activation.
- Real SDK registration, status rendering and a healthy loopback ping.
- Clicking the manual recheck control.
- The plugin's own settings page, built from the actual native SDK UI components.
- Saving a name, loopback health URL and synthetic bearer token through the real
  backend route; verifying config and secure env persistence.
- An authenticated HTTP health request received by the loopback fixture.
- A disabled wake button, empty MAC/broadcast settings and a Python UDP-send
  guard with a synthetic negative control. No real Wake-on-LAN packets.
- Closing the native app and cleaning up only the run's own backend processes.

Candidate probes removed `requires_hermes` **only from the disposable plugin
manifest**. Otherwise a policy rejection by the previous v0.22.5 declaration
would say nothing about runtime compatibility. Plugin Python/JavaScript and the
backend manifest were byte-identical to the repository. The final v0.22.3
confirmation used the real `>=0.22.3` manifest without candidate modification.

Results, reviewed upstream commit pins and source/bundle fingerprints are in
[older-hermes.json](older-hermes.json). The final supported-floor confirmation
is [hermes-0.22.3-native.json](hermes-0.22.3-native.json). The previous
[v0.22.5 report](hermes-0.22.5.md) retains its original snapshot and limitations.

## Release and build inputs

Every release source archive was fetched by a full commit resolved from its
official tag. Native builds used the unmodified upstream build scripts and
the release's own npm lockfile. v0.22.3 and v0.22.4 have byte-identical lockfiles,
so v0.22.3 reused an isolated dependency copy of the completed v0.22.4 install;
its own renderer/main/preloads were still rebuilt from v0.22.3 source.

The four older native builds all declare Electron `40.10.2`; that runtime was
downloaded once and copied only between identical Electron versions. Their
native inputs were staged by the upstream builder. This differs from the
previous v0.22.5 probe, which reused an existing local build. Root and Desktop
build identities were checked against the reviewed release commit. Build
identity was generated using the official stamp writers with the verified
archive commit; no Hermes source or app bundle was patched.

Python used the separate prepared test environment from the initial baseline.
The relevant libraries match these releases' Python 3.14 declarations: HTTPX
`0.28.1`, Pydantic `2.13.4`, FastAPI `0.133.1`, Uvicorn `0.41.0`. This is not a
fresh provisioning test of each release's entire provider/LLM dependency graph.
The v0.22.3 official admission validator also passed all 16 checks without
warnings; dependency installation was not requested.

## Reproduction and limits

See RUNBOOK.md for `tests/check_native_compatibility.mjs`, the disposable Python
UDP guard, and isolation requirements. For a previously unsupported candidate,
pass `--expected-revision <full-SHA> --candidate` before the four paths. For
confirmation of the real supported floor, pass the reviewed v0.22.3 revision
without `--candidate`. The driver's default CI-source pin remains v0.22.5;
changing the support floor does not silently change that separate test input.

v0.22.1 uses inline plugin rows rather than the later searchable catalog and
modal details. The driver supports both. v0.22.2 has no `gateway ensure` command and
needs `--candidate --no-prewarm`, leaving its own Desktop to start and supervise
the isolated `serve` backend. Early driver startup/layout failures were corrected
before evaluating compatibility; the final negative result is the explicit SDK
loader error, not a harness timeout.

The native tests remain macOS smoke tests. They do not establish Windows/Linux
Desktop behavior, every profile-switch/failure state, physical WoL, or full
dependency bootstrap support. Hermes can fetch public catalogs/update metadata
during native startup; only plugin/provider fixture traffic is loopback-only.
All homes and credentials were synthetic and separate from production. No
source commit, push, release, catalog submission or production plugin change was
performed. Validate the exact release commit with `--install-deps` separately
before release/catalog submission.
