# Daily Finance Report

Public GitHub Pages URL:
https://wusftnt-bot.github.io/daily-finance-report/

This repository hosts the static web version of the daily investment and finance report.

## Expected Repository Contents

Keep this repository intentionally small. The expected tracked files are:

```text
README.md
index.html
assets/finance-newsroom-hero.png
```

Do not keep generated test files, duplicate asset folders, local cache folders, screenshots, logs, or old report copies in this repository. If future updates add new assets, keep only assets that are referenced by `index.html`.

## Publish Flow

The source report is maintained locally at:
C:\Users\jackietsui\Documents\Codex\2026-05-28\telegram\daily-finance.html

After updating the local report, run:

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\jackietsui\Documents\Codex\2026-05-28\telegram\publish-github-pages.ps1
```

The script copies the local report to `index.html`, refreshes `assets/`, commits only when content changed, and pushes to GitHub Pages.

Telegram messages should use the public URL above instead of the local filesystem path.

## Cleanup Rule

Before each publish, check `git status --short` and `git ls-tree -r --name-only HEAD`. Remove any stale generated files or unreferenced asset folders before committing. The Pages repository should stay static, readable, and easy to audit.
