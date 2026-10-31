# Hermes LAN Control development runbook

Run commands from the repository root unless a different directory is shown.
The unit tests and browser harness use simulated ping results, mocked UDP,
and loopback HTTP. The separate lifecycle integration installs a disposable
Git fixture into temporary Hermes directories with network I/O blocked.
No test changes the production installation or sends real Wake-on-LAN packets.
User setup and runtime behavior are described in [README.md](README.md).

This runbook's location and section order are project conventions. The runtime
package layout follows Hermes' [unified package documentation](https://hermes-agent.nousresearch.com/docs/developer-guide/desktop-plugin-sdk#one-package-both-sdks).
Catalog acceptance follows the official [submission guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission)
and [admission policy](https://github.com/NousResearch/hermes-agent/blob/main/plugin-catalog/README.md).

## Package structure

The native plugin entrypoints remain at:

```text
plugin.yaml                  # Native manifest, identity, settings, secrets
__init__.py                  # Registration entrypoint (no chat tools)
desktop/plugin.js            # Desktop SDK entrypoint
dashboard/manifest.json      # Backend discovery manifest
dashboard/plugin_api.py      # Backend router
```

`README.md` contains user setup and behavior disclosures. `LICENSE` holds the
MIT text. Tests and the React harness live under `tests/`. Do not package test
virtual environments, node_modules, generated bundles, caches, or private
configuration. Keep all executable payloads reviewable as source.

## Python tests

Create a dedicated test environment, separate from production Hermes:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r tests/requirements.txt
.venv/bin/python -B -m unittest discover -s tests -p 'test_*.py' -v
```

These commands use a POSIX shell. On Windows, use the corresponding interpreter
under `.venv/Scripts/`. The pinned test libraries are in
[tests/requirements.txt](tests/requirements.txt); do not install them into
Hermes' production environment.

Use an interpreter supported by the pinned dependencies. The suite is verified
with Python 3.14; if a newer system Python requires unavailable native builds,
create the environment with `python3.14 -m venv .venv` instead.

The tests cover configuration validation, secret isolation, packet construction,
the ping/HTTP matrix, authentication errors, redirect/TLS/proxy policy, network
timeouts, API routes, profile revisions, caching, concurrent requests, cooldowns,
and host rechecks before sending. Ping processes and UDP sends are mocked. The
HTTP integration server binds only to `127.0.0.1` on a free port.

Regression coverage includes concurrent revision-bound saves, endpoint/token
pairing during partial writes, persisted HTTP pause and retry, secret-scope
refresh after lock waits, pre-save validation, and request deadlines including
lock contention and upstream buffering. Long check intervals renew wake expiries
without additional network probes. A loopback HTTP test verifies that a disconnected wake client
does not reach the mocked UDP sender later.

To exercise the real Hermes config and secret APIs, use an existing dedicated
Hermes development interpreter with its dependencies and source checkout:

```sh
/path/to/hermes-test-env/bin/python -B tests/check_hermes_config.py --hermes-source /path/to/hermes-agent
```

This check creates temporary `HERMES_HOME` and `HERMES_DESKTOP_USER_DATA_DIR`
directories before importing Hermes, uses synthetic tokens, and never probes a
host or sends UDP. It verifies persistence, conflicts, validation, and preservation
of unrelated settings through Hermes' public config/secret APIs.

## JavaScript tests and manifest checks

The controller tests need Node.js without an npm installation:

```sh
node --check desktop/plugin.js
node --experimental-vm-modules --test tests/test_desktop.mjs
```

The experimental VM modules warning is expected. The tests load the actual
plugin module with SDK and React mocks. Coverage includes pending requests,
stale results, manual rechecks, repeated clicks, profile/connection changes,
late responses, cleanup, accessibility attributes, native ListRow props,
ordered settings fields, optional HTTP settings, and partial save failures.

If Ruby is available, check YAML/JSON consistency without adding a parser to
Hermes' environment:

```sh
ruby -ryaml -rjson -e 'm=YAML.load_file(ARGV[0]); j=JSON.parse(File.read(ARGV[1])); abort unless m["name"] == j["name"] && m["version"] == j["version"] && m["license"] == "MIT" && m["homepage"] == "https://github.com/andrepester/hermes-lan-control" && m["config_schema"]["bearer_token"]["type"] == "secret"; puts "Metadata OK"' plugin.yaml dashboard/manifest.json
```

## Browser harness

Install only the harness's local dependencies and build from its lockfile:

```sh
cd tests/harness
npm ci --ignore-scripts --no-audit --no-fund --cache .npm-cache
npm run build
cd ../..
.venv/bin/python -B tests/harness/server.py --port 18765
```

Open [the local harness](http://127.0.0.1:18765). The server binds only to
loopback. It renders the actual desktop component with React and an SDK mock.
The surrounding Hermes shell and native UI primitives are simulations.

The API uses the real plugin router, but ping is simulated, UDP is replaced by
a counter before the router is mounted, and every health URL is redirected to
the loopback fake endpoint. Settings and tokens stay in memory. Use synthetic
tokens only. No test mode is added to the production plugin.

Check the following behavior:

1. With ping down and HTTP 503, the dot is red and power is enabled. Clicking
   power increments the simulated packet counter once; cooldown prevents
   repeated sends.
2. Clicking the name or dot immediately increments the ping and, when enabled,
   HTTP counters without sending a packet. Repeated clicks do not create
   parallel manual requests. Verify keyboard activation with Tab and
   Enter/Space.
3. Verify all combinations in the README's status table. In particular, ping
   down with HTTP 200 is yellow with power enabled; ping up with HTTP failure
   is yellow with power disabled. HTTP 401 does not itself block wake when
   ping is down.
4. Disable HTTP: ping alone determines the color, and the HTTP counter stops.
   A local ping error remains red with power disabled, even if HTTP succeeds.
5. Open the plugin settings. Empty the health URL: token and expected-status
   fields become disabled. Save and confirm stored values are retained. Set a
   health URL, enter a synthetic token, and save once: the token counter grows,
   the draft clears, and the placeholder reports an existing token. A blank
   token does not overwrite it. Settings and token use one revision-bound
   `POST /settings`; no normal settings RPC or separate token POST is sent.
6. Simulate a backend outage or configuration error: red with power disabled.
   Start from green and suspend responses: with the default timing, the result
   becomes stale within eight seconds. Resume responses or reload to recover.
7. Check long names, a narrow status bar, themes, tooltips, keyboard focus,
   and accessible status labels. The compact display stays dot, name, power.
8. Switch profile or connection during checks and saves. Old results, drafts,
   and success messages must not appear in the new context. Disable/re-enable
   the plugin and check cleanup and reloading.
9. Capture harness screenshots at 100% browser zoom. Identify them as harness
   screenshots; use native screenshots for a future catalog submission.

Stop only this test server with Ctrl-C. Disposable outputs are
`tests/harness/dist/`, `tests/harness/node_modules/`,
`tests/harness/.npm-cache/`, and the test `.venv/`. Keep source, tests, and
lockfiles. Do not stop unrelated Hermes processes.

### README screenshots

The five images in `docs/screenshots/` are cropped native Hermes Desktop
v0.22.5 captures on macOS, taken after updating the installed package and
rescanning its managed desktop copy. They use the actual theme's red, yellow,
and green variables and a neutral `LAN host` display name.
`statusbar-context.png` adds a wider view of the bottom-right window edge,
including the neighboring native indicators, Smart, and the app version.
`settings.png` shows the native configuration form with an unsaved example draft.
Use `LAN host`, `192.0.2.10`, `02:00:00:00:00:10`, `192.0.2.255`, port `9`,
interval `5`, timeout `2`, `https://example.com`, and expected status `200`.
Leave the bearer token blank. Do not save or probe these example values. Reduce
the native view zoom only as needed to fit all fields and buttons, crop to the
form, then restore the previous zoom and discard the draft with **Reload**.

To refresh them, back up the installed package, verify ownership of the managed
desktop copy, update from the reviewed checkout, rescan, and restart the gateway
when backend code changes. Record the original plugin settings privately. Use a
neutral display name, then capture red with both checks failing, yellow with
loopback ping responding and the existing HTTP check failing, and green with
loopback ping responding and HTTP disabled. Keep any saved token paired with its
existing endpoint. Never click the power icon for a screenshot.

Close Settings before each status-bar capture so its backdrop does not blur it.
Crop the three state details to the plugin alone. For the context image, include
some workspace above the bar and the adjacent native indicators. Exclude private
host values, unrelated app content, and floating companions in every crop.
Restore the original settings and verify that credentials
are unchanged. Inspect every final crop before updating the README. The package
allowlist includes these images so README links also work in release archives.
These captures establish the appearance of these three states, not full native
acceptance or hardware Wake-on-LAN behavior.

## Official Hermes validation

Before a catalog submission, run the official check against the exact commit
that will be pinned, in a dedicated Hermes validation environment:

```sh
hermes plugins validate "$PWD" --install-deps
```

`--install-deps` can prepare dependencies; keep this environment separate from
production Hermes. Fix every failure and read all warnings. This is additional
to the unit tests and harness checks above. Passing it does not establish native
UI or hardware behavior.

The manifest's `requires_hermes` must be based on verified compatibility, not
merely the last source version inspected. A future catalog entry must declare
the same floor, accurate capabilities, and a full 40-character commit SHA. That
entry belongs in `NousResearch/hermes-agent/plugin-catalog/`, via a reviewed PR;
the plugin's source remains in this repository. Add native UI screenshots to
the submission and disclose the runtime behavior documented in the README.

## Native desktop verification

Before launching an isolated Hermes instance, verify that both `HERMES_HOME`
and `HERMES_DESKTOP_USER_DATA_DIR` point to separate test locations. Check the
actual launcher's resolved plugin paths, browser data, ports, and process
ownership. Do not copy production configuration, tokens, or conversation data.
A backend profile alone does not isolate app-wide desktop plugin storage.

There is no universal launch command here: isolation must be verified for the
launcher in use. Until that is established, use the harness and record native
verification as pending. For a simulated native test, replace the backend UDP
sender with a mock before loading the router. A documentation-only broadcast
address is not a substitute for intercepting sends.

The maintainer confirmed native functional acceptance in their local installation
on 2026-10-31. This is maintainer-reported manual acceptance; the automated tests
do not send real Wake-on-LAN packets.
The macOS compatibility smoke test below has passed against v0.22.3–v0.22.5; it covers
the plugin's native SDK integration and real backend routes, not every UI state.
The offline Git lifecycle below covers installation/update/removal and the real
desktop filesystem reconciler, without launching the desktop application.
Linux and Windows ping behavior has only been checked through process mocks.
Do not turn those tests into claims of native platform support.

### Native compatibility smoke test (macOS)

[tests/check_native_compatibility.mjs](tests/check_native_compatibility.mjs) uses
Playwright's Electron driver with an existing built release and a dedicated Python
environment containing that release's dependencies. Keep the release tree, its
generated `dist/`, node dependencies and Python environment separate from the
production installation. The source's real `install-stamp.json` must match the
reviewed SHA in `tests/hermes-revision.txt`; do not fabricate a stamp for a
different or dirty build. A matching existing build and stamp can be copied.

In that disposable Python environment only, install
[tests/native_no_udp.py](tests/native_no_udp.py) as `sitecustomize.py` in its
site-packages directory. This intercepts every Python `socket.sendto` before
transmission; the driver requires the guard and checks it with a synthetic audit
event, without sending a test packet. Never install it into production/shared
Python, and do not overwrite an existing sitecustomize module.

```sh
node tests/check_native_compatibility.mjs /absolute/test/hermes-agent /absolute/test/python-env/bin/python /absolute/test/node-runtime-root /absolute/new/test-state
```

The node runtime root supplies Playwright, YAML and the macOS Electron executable
under `node_modules/`. The state directory must not exist. The driver starts an
inert loopback provider, copies the native plugin into a new test home, and uses
fresh backend and desktop data. It passes only basic environment variables;
provider credentials and inherited Hermes selectors are excluded. Only the child
process's home is redirected; the caller's environment is unchanged. This extra
home isolation is required by Hermes' upstream E2E fixtures because some backend
profile discovery is home-anchored. The driver also disables lazy installation
and OS protocol registration, selects the isolated backend mode, and prewarms
its gateway before starting the renderer.

The test activates the desktop half through the native Plugins UI, verifies a
healthy loopback ping, clicks the manual recheck, saves a name, loopback health
URL and synthetic bearer token through the real settings page, and verifies an
authenticated HTTP request and persisted configuration/secret. MAC and broadcast
stay empty and the wake button must remain disabled; it is never clicked. The
Python UDP send guard is an additional enforced boundary. This
is not a hardware WoL test. The current UI selectors cover English/German shells.

The result directory retains a native screenshot, UI transcript, source/bundle
hashes and logs for review. These artifacts can contain local test paths; do not
publish them wholesale. The app is closed and only that directory's gateway is
stopped in `finally`. The driver needs permission to launch native applications.
Hermes itself may fetch public catalog/update information during startup; this
native test is not offline. The plugin/provider traffic is restricted by the
fixture configuration to loopback. See the
[compatibility report](docs/compatibility/older-hermes.md) for the verified scope.

For an older candidate, supply `--expected-revision <reviewed-full-SHA>` and
`--candidate` before the four positional paths. Candidate mode removes only
`requires_hermes` in the disposable plugin manifest so that a policy rejection
does not masquerade as a runtime failure. It never changes the repository's
manifest or plugin code. Candidate Desktop builds must carry a matching clean
build stamp; build the actual candidate source with its own locked dependencies
and Electron, rather than copying a newer renderer into the older source tree.
Both the tested snapshot and repository source hashes are recorded. The driver
supports inline plugin rows and the later modal catalog UI. The explicit
`--no-prewarm` candidate option supports releases such as v0.22.2 whose CLI has
no `gateway ensure`; those use their own app-supervised `serve` process, closed
with the app. It does not install or change a system service.

## Automated Git lifecycle

[tests/check_hermes_lifecycle.py](tests/check_hermes_lifecycle.py) drives the actual
Hermes plugin CLI parser/commands, admission scanner, PM client/worker and
publication transactions. [tests/check_desktop_lifecycle.mjs](tests/check_desktop_lifecycle.mjs)
imports the real `desktop-plugins-root.ts` module with Node's built-in TypeScript
support. No upstream source file is modified. The reviewed upstream revision is
pinned in [tests/hermes-revision.txt](tests/hermes-revision.txt). This pin selects
test inputs; the supported floor is a separate manifest declaration backed by
the compatibility report and native smoke test above.

The test verifies:

1. Fresh Git install remains disabled by default; the plugin and backend manifest
   are discovered, and the desktop half is copied with the correct ownership marker.
2. Explicit activation goes through real Hermes dependency admission and persists.
   The installed backend's settings route works without exposing a synthetic token.
3. An update to a second temporary Git revision replaces both manifests and desktop
   bytes, preserving activation, settings, token and untracked user data. The updated
   backend can still read that state and serve its settings route.
4. Exact-SHA reinstall records the pin, and a mutable update of that pin is refused.
5. Deactivation and removal clear the plugin tree, install record and config entries;
   desktop reconciliation removes the managed copy. Unrelated settings and standalone
   plugins remain intact, including a conflicting marker-less `lan-control` desktop copy.
6. Hermes retains env-backed secrets on removal, as documented. A separate explicit
   Hermes secret deletion removes the synthetic token before the temporary home is discarded.

The application dependency graph is a minimal offline core fixture because this
plugin declares no additional Python packages. The prepared Python/uv tools are
injected at the same acquisition seam as Hermes' own `plugin_worker_support`
tests; the resolver, PM worker protocol, locks and publication logic run unchanged.
The backend uses real FastAPI/HTTPX and Hermes config/secret APIs in the separate
test interpreter. This does not test provisioning Hermes' full provider/LLM stack,
a running gateway, native renderer activation or visible UI behavior.

Every run creates separate temporary `HERMES_HOME`, desktop user data, runtime,
cache and Git fixture directories. Only a small allowlist of basic environment
variables is inherited; credentials and production Hermes selectors are excluded.
Python audit hooks reject network I/O and writes outside the temporary root in
both the driver and PM worker. Git is limited to the `file` protocol, uv runs
offline, and the desktop helper only performs local filesystem reconciliation.
These guards are checked with synthetic audit events. No network probe or UDP
sender is invoked. Fixture-only Git commits simulate revisions; the project
repository's index and history are not changed. Temporary files are cleaned up.

For a local run, prepare a dedicated driver environment and a clean pinned source
checkout (or supply an existing clean checkout at that same revision):

```sh
python3.14 -m venv .hermes-lifecycle/driver
.hermes-lifecycle/driver/bin/python -m pip install -r tests/lifecycle-requirements.txt
git init .hermes-lifecycle/hermes-agent
git -C .hermes-lifecycle/hermes-agent fetch --depth=1 https://github.com/NousResearch/hermes-agent.git "$(cat tests/hermes-revision.txt)"
git -C .hermes-lifecycle/hermes-agent checkout --detach FETCH_HEAD
.hermes-lifecycle/driver/bin/python -B tests/check_hermes_lifecycle.py --hermes-source .hermes-lifecycle/hermes-agent
```

Use Node 24 or newer. Dependencies/source acquisition uses the network; the
lifecycle itself runs offline. `.hermes-lifecycle/` is ignored and never packaged.
CI runs this job on Linux and macOS and requires it through `CI gate`, including
when CI is called by release preparation. Update the Hermes pin deliberately,
review upstream changes, and rerun these checks when upgrading the baseline.
Changing the test pin does not automatically change the supported minimum.

## Installation, update, and removal verification

Use an isolated environment and verify the complete lifecycle before release:
installation, discovery of both components, explicit activation, settings and
secret persistence across an update, and deactivation/removal. Inspect the
package marker before modifying a desktop copy; Hermes owns copies originating
from a unified package. Do not overwrite a separate standalone installation.

For a manually copied package, deactivate both components and remove only the
chosen package directory. On rescan, Hermes removes its managed desktop copy.
Remove the optional bearer token through Hermes' secret management if it is no
longer needed. Do not delete other profiles or plugin directories.

Real LAN testing is separate from automated validation. First agree on the
sender, target, MAC address, broadcast address, and port. A user-approved power
click may send a real magic packet when ping is down, including a yellow state
with a healthy independent HTTP endpoint. Enabling a configured plugin can
start real ping and HTTP traffic. Do not perform a hardware wake test as an
incidental validation step.

## CI and security automation

The CI and Security workflows under [.github/workflows](.github/workflows) run
on pull requests and pushes to `main`. Security checks also run weekly on Monday
(UTC). Manual runs are available. CI and security are reusable workflows;
release preparation runs both again against the exact requested commit.

| Workflow | Checks |
| --- | --- |
| CI | Python 3.11/3.14 on Linux; Python 3.14 on macOS/Windows; JavaScript/controller tests; harness build; native package and release boundary checks; private/cache file guard; offline Hermes Git lifecycle on Linux/macOS |
| Security | Full fetched Git history and committed-tree Gitleaks scans; Bandit; pip-audit for test, lifecycle and CI dependencies; full harness npm audit; CycloneDX test/harness SBOMs; actionlint |
| CodeQL | Extended Python, JavaScript/TypeScript, and Actions analysis when the repository is public or licensed private scanning is explicitly enabled |
| Prepare release draft | Manual, existing tag and exact main commit verification; fresh CI/security; release compatibility gate; allowlisted ZIP, inventory and SHA-256 checksums; categorized generated release notes; GitHub draft only |
| Prepare catalog update | Published stable release or manual preview; manifest/pin checks; catalog patch and English PR text as artifacts; fork/push/PR job hard-disabled |

The unit-test OS matrix exercises mocks and loopback HTTP. The additional lifecycle
matrix uses pinned Hermes command/PM/desktop filesystem code with the offline
fixture described above. Neither is a declaration of native UI or hardware compatibility. The
package has no bundled npm dependencies. The SBOMs describe development/test
environments, not the user's Hermes runtime; the release inventory describes
the source files actually shipped.

All dependency audit findings fail the security gate. Bandit blocks medium/high
findings and retains the full report for low-severity review. The initial scan
reported seven low findings: the secret-key identifier, empty optional-token
values, and best-effort exception cleanup/notification paths. They are not
suppressed. Reassess them if those paths change. See [SECURITY.md](SECURITY.md).

Actions are pinned to exact SHAs with read-only permissions and no persisted
checkout credentials. Pull request code is never executed using
`pull_request_target`. The only write token is in the release-draft job, which
downloads the built package and does not check out or execute plugin source.
Only the isolated lifecycle workflow installs a temporary test copy. No workflow
accesses the production Hermes environment, sends real Wake-on-LAN, publishes a
release, or submits a catalog update.
The catalog preview workflow has a prepared submission job, hard-disabled by
an explicit `false` condition; only read-only preparation is wired to releases.
Artifacts are retained for 14 days. Dependabot proposes weekly pip, harness npm,
and GitHub Actions updates; updates are not automatically merged. Scanner binary
versions and archive SHA-256 pins in `security.yml` need deliberate maintenance too.

Run the additional local checks in the isolated test environment:

```sh
.venv/bin/python -m pip install -r tests/ci-requirements.txt
.venv/bin/python -B scripts/check_package.py
.venv/bin/python -B -m unittest discover -s tests -p 'check_release.py' -v
.venv/bin/python -B -m unittest discover -s tests -p 'check_catalog.py' -v
.venv/bin/python -m bandit -r dashboard __init__.py --severity-level medium
.venv/bin/python -m pip_audit -r tests/requirements.txt --progress-spinner off
.venv/bin/python -m pip_audit -r tests/ci-requirements.txt --progress-spinner off
npm --prefix tests/harness audit --package-lock-only
```

The Security workflow downloads the official Linux x64 release binaries for
Gitleaks and actionlint, verifies their pinned archive SHA-256 checksums before
extracting the executables, and adds them to the job's PATH. No Go toolchain is
required. Versions, download URLs, and checksums are declared together in
`security.yml`; review all three when upgrading. For local checks, use release
binaries for your operating system at the same versions. Scan Git history with
`gitleaks git . --config .gitleaks.toml --log-opts="--all" --redact`
and run `actionlint` against the workflows. A directory scan of the entire
working directory can include ignored private notes or caches; scan an exported
committed tree instead. Before the first commit, create a temporary snapshot
from `git ls-files --cached --others --exclude-standard` and scan that snapshot.
Never include ignored files in a CI artifact.

[.gitleaks.toml](.gitleaks.toml) extends Gitleaks' default rules. Its only
exception applies to `generic-api-key` when both the exact reviewed backend
SHA-256 fingerprint and one of the three named compatibility report paths match.
The `api` in the backend filename otherwise makes that fingerprint look like a
credential. When the fingerprint changes, review the new value before updating
the exception. For exported-tree scans, pass `--config .gitleaks.toml` explicitly.
Verify changes with synthetic findings in a disposable snapshot: a different
API-key value in an allowed report and the allowed fingerprint outside those
report paths must both remain detectable.

## GitHub repository controls

The repository is public with explicit maintainer authorization as of
2026-10-31. Configured baseline settings are:

- Squash merging only, automatic deletion of merged branches, and PR branch updates.
- GitHub-owned Actions only, with mandatory full-SHA pinning.
- Default Actions token permissions: read; Actions cannot approve pull requests.
- Dependabot vulnerability alerts and automatic security-fix PRs are enabled.
- GitHub secret scanning, push protection, and private vulnerability reporting are enabled.

The rules are stored in [.github/rulesets/main.json](.github/rulesets/main.json)
and [.github/rulesets/releases.json](.github/rulesets/releases.json). They are
active on GitHub: `Protect main` (ID 26286471) and `Protect release tags`
(ID 26286472), with enforcement verified through the API. The repository was
still empty when the rules were enabled; CI check names need confirmation after
the first authorized push and green workflow runs. No source has been published
as part of configuring repository visibility or protection.

Update the existing rulesets by ID rather than creating duplicates:

```sh
gh api --method PUT repos/andrepester/hermes-lan-control/rulesets/26286471 --input .github/rulesets/main.json
gh api --method PUT repos/andrepester/hermes-lan-control/rulesets/26286472 --input .github/rulesets/releases.json
```

Verify enforcement with a test PR after the initial source push. The main ruleset requires
PRs, resolved conversations, a linear history, current-branch status checks
`CI gate` and `Security gate` from the GitHub Actions app (integration 15368), and
forbids deletion/force-push. It has no bypass actors. Required approval count is
zero to allow a solo maintainer to merge their own PR after checks; add a second
maintainer before requiring independent approval. CODEOWNERS routes ownership
to `@andrepester`, without requiring an impossible self-approval. Release tag
rules forbid modifying or deleting `v*` tags while allowing initial creation.

The prepared CodeQL workflow will run on the public repository once it has been
pushed. It is not yet a required check. Private repositories would need a Code
Security entitlement and an explicit `ENABLE_CODEQL=true` opt-in. Private
vulnerability reporting is enabled; use GitHub's **Report a vulnerability**
feature as described in SECURITY.md. No default-setup CodeQL workflow is created,
so it will not conflict with the prepared advanced workflow.

## Release preparation and Hermes distribution

GitHub releases can provide versioned source packages, checksums, and release
notes for this native plugin. They are not a Hermes auto-update channel. Hermes
installs a Git repository; `--ref` accepts an exact 40-character commit SHA,
not a branch or tag. Catalog entries also require a full reviewed SHA. Updating
a catalog pin requires a separate reviewed upstream PR; the user's update is
performed by Hermes. Do not add a plugin self-updater.

The manual release workflow runs only from `main` and requires an existing
`v<manifest-version>` tag at the exact `main` SHA captured when dispatching it,
explicit confirmation of the native and
official Hermes checks, and a verified `requires_hermes` declaration. The supported
floor is now `>=0.22.3`; the evidence and limitations are recorded in
[the compatibility report](docs/compatibility/older-hermes.md). This does not
authorize release publication or replace validation of the exact release commit.
Workflow checkboxes record maintainer confirmation; they cannot prove that
manual tests were performed. Keep supporting evidence with the release review.

The commit input is a confirmation, not a checkout selector: it must equal the
dispatch event's immutable `github.sha`. Preflight also checks the actual checkout,
main ancestry and exact tag match. CI, security and packaging all use that event
SHA. An older tagged ancestor is rejected, even if it was previously reviewed;
this workflow prepares drafts for the current main snapshot only. If main moves
after dispatch, every job still uses the captured SHA.

This closes the input-to-checkout path reported by CodeQL alerts
[#1](https://github.com/andrepester/hermes-lan-control/security/code-scanning/1)
and [#2](https://github.com/andrepester/hermes-lan-control/security/code-scanning/2).
The original SARIF path was preflight's commit output → package checkout →
Python setup → pip installation. The former SHA/tag/ancestry checks constrained
the input to main history, but allowed executing an older tree using the current
main workflow's runtime privileges. No arbitrary fork execution was demonstrated.
The narrowed policy removes that selection ability instead of adding a scanner
exception.

Neither the package job nor its pinned setup/upload actions enables Actions
dependency caching; pip's local download cache is not shared between fresh hosted
runners. This alone does not eliminate cache poisoning: manually dispatched
main workflows retain a write-capable runtime cache token independently of
`contents: read`, as described in GitHub's
[cache security update](https://github.blog/changelog/2026-06-26-read-only-actions-cache-for-untrusted-triggers/).
Only protected-main code now executes in that context. The package job receives
no repository secrets and persists no checkout credentials. Its release artifact
passes to a separate draft job with a write token, which never checks out or
executes package code. Changes to these boundaries require a fresh security review.

The draft uses `gh release create --generate-notes` to append GitHub-generated
release notes after the installation and verification instructions. GitHub uses
[.github/release.yml](.github/release.yml) to group merged pull requests by label:
`security` into Security, `bug` into Fixes, `dependencies` into Dependencies,
and other pull requests into Changes. `skip-changelog` excludes a pull request.
Keep PR titles in English and label them before preparing the draft.

GitHub automatically selects the previous release tag as the comparison base;
the first release needs no previous tag. Generated notes summarize merged pull
requests and include contributors and a full changelog link; they do not turn
every direct commit into a release-note entry. Review the comparison range and
add an English overview for the initial release or changes without a PR before
publishing. There is no separately maintained `CHANGELOG.md`.
See the [GitHub CLI reference](https://cli.github.com/manual/gh_release_create)
and [GitHub release notes guide](https://docs.github.com/en/repositories/releasing-projects-on-github/automatically-generated-release-notes).

After compatibility and publication decisions have been approved:

1. Verify the exact commit with `hermes plugins validate <checkout> --install-deps`
   in an isolated Hermes environment. Complete the native lifecycle checks above.
2. Confirm both manifests agree on the version and that the compatibility floor
   has evidence. Review all release files and the source diff.
3. Create and push the matching version tag only when authorized. Run
   **Prepare release draft** from `main`, entering its current full commit SHA, existing
   tag, and confirmation that the required validation is complete.
4. Review the draft, generated ZIP, SHA256SUMS, inventory, SBOM artifacts, and
   notes. Publishing is a separate manual decision. No npm/PyPI publication or
   deployment is needed for this native plugin.
5. If submitting to the official catalog, make the separately authorized public
   source accessible and propose its exact SHA in a reviewed catalog PR.

`scripts/check_package.py` validates native metadata without importing plugin
code. `scripts/build_release.py` creates a deterministic ZIP from an explicit
explicit package allowlist (including the README screenshots) plus a source
inventory and SHA-256 checksums; tests,
automation, local notes, configuration, and caches are excluded. These scripts
are repository build infrastructure and must stay with this project's workflows.
The inventory is traceability data, not a signed provenance attestation.
GitHub's automatic source archives include the whole committed tree; the
custom ZIP is the minimal manual-install package.

## Catalog update automation (preview only; submission disabled)

[Prepare catalog update](.github/workflows/catalog-update.yml) prepares a
SHA-bump PR after a stable GitHub release is published. It can also be invoked
manually from `main` with an existing published stable tag. Drafts, prereleases,
tags outside `main` history, and mismatched manifest versions are rejected.
The existing release-draft workflow still requires an existing tag and manual
validation confirmation; this addition does not change that release gate.

The enabled preparation job has only `contents: read`. It resolves the full
commit from the release tag, extracts the two manifests without executing
release code, reads the current upstream catalog entry, and generates:

- `catalog-update.patch`: the change to `plugin-catalog/lan-control.yaml`.
- `catalog-entry.yaml`: the resulting entry for inspection.
- `pull-request.md` and `pull-request-title.txt`: English draft PR text.
- `update.json`: exact old/new SHAs, version, branch name, and no-op status.

The files are uploaded as `catalog-update-preview` for 14 days. Inspect them in
the Actions run. No fork, Git push, commit, or upstream PR is made by this job.
No extra credential or repository-variable opt-in is configured.

This is an updater for an **existing reviewed catalog entry**. Initial admission
is separate: without `plugin-catalog/lan-control.yaml` upstream, preparation
fails with an explanatory message. It never invents capabilities, screenshots,
platform support, or a compatibility floor. Only SHA and version are changed;
the existing `requires_hermes` must match the release manifest. A floor change
requires a separately reviewed metadata update. Capabilities and disclosures
must also be checked against the new source before submitting; retaining the
previous catalog values does not establish that they are still accurate.

The `submit` job contains the future fork/branch/push/draft-PR commands but is
**hard-disabled** with `if: ${{ false && needs.prepare.outputs.changed == 'true' }}`.
Adding a secret, setting a variable, publishing a release, or manually running
the workflow cannot enable it. Activation requires a separately authorized,
reviewed workflow edit. Until then, leave `CATALOG_PR_TOKEN` unconfigured.

After explicit authorization to activate submission:

1. Ensure the first catalog admission has been reviewed and merged, and that
   native/official validation evidence exists for the released commit.
2. Review a real preview and its upstream diff. Test credentials with the least
   permissions needed to write the maintainer's Hermes fork and open upstream
   PRs. The plugin repository's normal `GITHUB_TOKEN` cannot provide that
   cross-repository access. A suitable maintainer PAT stored as
   `CATALOG_PR_TOKEN` is the prepared path; never put its value in source or logs.
   An App alternative needs suitable installations and permissions, not just
   installation on this plugin repository.
3. Review and replace the hard-disabled job condition. The job verifies the
   authenticated account is `andrepester`, creates or verifies its Hermes fork,
   applies only the catalog-entry patch on a new branch, and opens a **draft**
   PR to `NousResearch/hermes-agent:main`. Existing PRs, including closed ones,
   are detected to avoid duplicates. It never force-pushes or auto-merges;
   a branch left behind without a PR needs manual recovery.
4. Review the draft, complete the unchecked validation checklist, and mark it
   ready only when the metadata and source match. Hermes maintainers still
   review and merge each SHA bump; this cannot be automated by this repository.

Publishing the release manually produces the normal `release: published`
event. If release publication is later automated using `GITHUB_TOKEN`, do not
assume it will trigger another workflow: GitHub suppresses most such events.
Use an explicit `workflow_dispatch` to the preview workflow or a deliberately
configured alternative credential. See GitHub's
[workflow triggering rules](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)
and Hermes' [catalog policy](https://github.com/NousResearch/hermes-agent/blob/main/plugin-catalog/README.md).

`scripts/prepare_catalog_update.py` is an offline repository build helper. It
uses the already declared CI dependency PyYAML, preserves unrelated entry fields
and comments, rejects duplicate keys and ambiguous metadata, and writes only
the preview directory. Test it with the `check_catalog.py` command above. It
stays with the project workflows as reusable release infrastructure; it does
not belong in the runtime ZIP. A local preview, given manifest files from the
release checkout and an existing upstream entry, is:

```sh
.venv/bin/python -B scripts/prepare_catalog_update.py \
  --manifest <release-checkout>/plugin.yaml \
  --backend-manifest <release-checkout>/dashboard/manifest.json \
  --entry <hermes-checkout>/plugin-catalog/lan-control.yaml \
  --commit <full-40-character-release-sha> --tag <vX.Y.Z> \
  --output reports/catalog-update
```

The workflow and fork/PR path have not been exercised against GitHub yet. Local
tests use only synthetic manifests/catalog entries and a temporary patch tree.
