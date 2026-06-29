# Daily Finance Report

Public GitHub Pages URL:
https://wusftnt-bot.github.io/daily-finance-report/

This repository owns the static daily finance page and its GitHub Actions generation flow.

## Publish Flow

GitHub Actions polls on a short interval but only generates during the configured Taipei morning window. It refuses to publish when fewer than the minimum required news items are available. The generated page exposes machine-readable report date and news count metadata for the Telegram workflow to verify.

Telegram messages use the public URL above instead of a local filesystem path.

When the page is current during the Taipei morning window, this repository may dispatch
`wusftnt-bot/JT-PM`'s `daily-telegram.yml` workflow as a cross-repo wake-up signal. That
dispatch is optional and requires `JT_PM_ACTIONS_TOKEN` in this repository's GitHub
Actions Secrets. The token must be fine-grained, limited to `wusftnt-bot/JT-PM`, and
granted only the minimum Actions permission needed to dispatch workflows.

## Project Boundary

- This repository owns only the public daily finance page and its news collection helpers.
- Telegram delivery is owned by the `wusftnt-bot/JT-PM` repository.
- LINE smart-stock workflows are separate and must not be imported, called, or supplied with credentials here.

## Secret Safety

- `GEMINI_API_KEY` must be stored only in GitHub Actions Secrets and referenced as `${{ secrets.GEMINI_API_KEY }}`.
- `JT_PM_ACTIONS_TOKEN`, if configured, must be stored only in GitHub Actions Secrets. Never print it, write it to generated HTML, upload it as an artifact, or reuse it for LINE or Telegram bot API calls.
- Telegram and LINE tokens must never be added to this repository, logs, generated HTML, artifacts, caches, or workflow summaries.
- Never print environment variables or use dangerous output such as `print(os.environ["GEMINI_API_KEY"])`, `echo "$GEMINI_API_KEY"`, or a full environment dump.
- Before changing workflows or scripts, search current files and Git history for `AIza`, token patterns, secret names, and environment dumps.

## Cleanup Rule

Do not commit generated test files, duplicate assets, caches, screenshots, logs, old report copies, API keys, or bot tokens. Keep source files readable and avoid packed or obfuscated code so security-sensitive behavior remains auditable.
