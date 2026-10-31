# Security policy

Until a first supported release is published, this project is pre-release.
There is no established security maintenance window yet. Use only commits you
have reviewed, and keep Hermes and its environment updated independently.

## Reporting a vulnerability

Please use GitHub's **Report a vulnerability** feature when it is available on
this repository. Do not disclose credentials, private host configuration, or
exploit details in public issues. If private reporting is unavailable, open an
issue asking for a private reporting channel without including sensitive details.
No response-time guarantee is currently offered.

## Automated checks and their limits

CI runs Gitleaks, Bandit on production Python, pip-audit on the isolated test
dependency graphs (including lifecycle integration tools), and npm audit on the
complete harness lockfile. Medium and high
Bandit findings block CI; its full report also retains low-severity findings for
review. Dependency audits fail on every reported vulnerability. Weekly scans
check existing dependencies again as advisory databases change.

The Gitleaks configuration retains the default rules and narrowly exempts one
reviewed backend SHA-256 fingerprint from `generic-api-key` detection in three
named compatibility reports. Both the fingerprint and report path must match;
other values and paths remain scanned. See [.gitleaks.toml](.gitleaks.toml).

The plugin imports FastAPI, HTTPX, and Pydantic from Hermes at runtime. Auditing
the test environment does not audit an installed Hermes environment. The harness
dependencies and CI tools are not shipped in the native package. These checks
do not replace review, full native desktop acceptance, or the official Hermes
admission scanner on the exact release commit.
