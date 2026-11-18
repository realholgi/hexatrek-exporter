# Credential and data safety

Keep Firebase sessions, ID tokens, refresh tokens, private keys and photo download URLs private.
A Firebase photo URL can grant access without an account login.
Use placeholders for the app's public Firebase API key in examples.

Store session files outside this checkout and outside export directories.
The session code requires a user-owned regular file with private permissions and rejects links.
It does not use ambient proxies or `.netrc` credentials for authentication.
Authentication errors omit credentials and response bodies.
Do not attach session files, source caches, GPXs, archives, photos or raw logs to public issues.

## Local checks

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and run:

```bash
uv sync --frozen --extra dev
uv run --frozen --extra dev pre-commit install
uv run --frozen --extra dev pre-commit run --all-files --hook-stage manual
```

The commit hook scans staged changes with Gitleaks and blocks private artifact filenames.
The manual command and CI scan committed history and check tracked filenames.
Scans run offline and redact detected secrets.

`.gitleaks.toml` extends Gitleaks' standard rules with Firebase session tokens, API keys and photo access URLs.
The scanner version is pinned in `.pre-commit-config.yaml`.
Pre-commit builds Gitleaks in its own environment on the first run.
Keep any finding exceptions specific to verified synthetic data.

Review the staged diff for secrets the scanners miss, including encoded values and renamed private files.
Hooks can be bypassed. Enable GitHub secret scanning and push protection in repository settings where available.

## If a credential is exposed

1. Stop publishing the affected files.
2. Revoke the credential through its provider. Rotate affected session credentials or photo download tokens as applicable.
3. Inspect repository history, CI logs, build artifacts and issue attachments for copies.
4. Remove exposed material from those locations. Deleting the current file does not remove history.
5. Re-run the checks before publishing again.

Report exposed credentials privately.
Use GitHub's private vulnerability reporting when enabled, or contact the maintainer through a private channel.
