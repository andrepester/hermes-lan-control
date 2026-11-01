# Hermes LAN Control project instructions

- Keep public documentation, plugin UI, and GitHub-facing content in natural English.
- Follow the official Hermes native plugin layout and catalog admission policy:
  https://hermes-agent.nousresearch.com/docs/developer-guide/desktop-plugin-sdk#one-package-both-sdks
  https://github.com/NousResearch/hermes-agent/blob/main/plugin-catalog/README.md
  Keep `plugin.yaml` and `__init__.py` at the package root, the desktop entrypoint
  at `desktop/plugin.js`, and the backend at `dashboard/manifest.json` plus
  `dashboard/plugin_api.py`. Do not introduce portable `plugin.json` packaging
  for this native plugin.
- Hermes does not prescribe a fixed README section order or a runbook location.
  Keep user setup and the required behavior disclosures in `README.md` and
  developer checks in `RUNBOOK.md`.
  Treat this documentation split as a project convention, not an upstream rule.
- Disclose network calls, native processes, reads outside plugin data, stored
  credentials, and any background activity in the README, as required by catalog
  rule 13. Keep license/homepage in the native manifest. Verify compatibility
  before setting `requires_hermes`; do not copy an example floor from the docs.
- A future catalog submission adds `plugin-catalog/<name>.yaml` to
  `NousResearch/hermes-agent`, with an exact reviewed commit and accurate
  capabilities. It does not move this plugin's source into Hermes core.
- Keep the existing `lan-control` plugin ID stable. The project title is
  Hermes LAN Control and the repository name is `hermes-lan-control`.
- Use public Hermes plugin APIs and the desktop SDK. Do not patch Hermes core,
  its app bundle, or its DOM. Native Desktop/Agent switches belong to Hermes.
- Preserve the independent ping/optional HTTP checks and the established WoL
  rules. A successful HTTP check may refer to another machine.
- Use mocked UDP and loopback HTTP in automated tests. Do not trigger real
  Wake-on-LAN or change the installed plugin as an incidental test step.
- Use the validation commands in `RUNBOOK.md`. Keep test dependencies isolated
  from the production Hermes environment.
- Never publish credentials, private host configuration, local working notes,
  or build caches. Do not import private history or configuration into this repository.
- The public author is André Pester; the GitHub repository is
  `andrepester/hermes-lan-control`. The license is MIT, with copyright held by André Pester.
  The supported Hermes minimum is v0.22.3, verified as documented in
  `docs/compatibility/older-hermes.md`. Compatibility-floor changes require new
  evidence; release publication requires maintainer approval.
