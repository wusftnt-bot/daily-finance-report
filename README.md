# Daily Finance Report

Public GitHub Pages URL:
https://wusftnt-bot.github.io/daily-finance-report/

This repository hosts the static web version of the daily investment and finance report.

## Publish Flow

The source report is maintained locally at:
C:\Users\jackietsui\Documents\Codex\2026-05-28\telegram\daily-finance.html

After updating the local report, run:

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\jackietsui\Documents\Codex\2026-05-28\telegram\publish-github-pages.ps1
```

The script copies the local report to `index.html`, refreshes `assets/`, commits only when content changed, and pushes to GitHub Pages.

Telegram messages should use the public URL above instead of the local filesystem path.
