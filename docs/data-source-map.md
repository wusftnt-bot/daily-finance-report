# Data Source Map

This document is the contract for connecting new data sources to the public daily finance dashboard.

## Principles

- Public dashboard data must come from public market, news, macro, or company-disclosure sources.
- Secrets, API keys, bot tokens, chat IDs, cookies, private file paths, and personal portfolio data must never be written to HTML, JSON, CSV, logs, artifacts, caches, workflow summaries, or GitHub Pages.
- New data sources must write to `data/processed/*.json` first. The website and Telegram summaries should read processed JSON instead of calling raw APIs directly.
- If a source fails, show `資料更新失敗`, `待接資料源`, or `not_connected`; never reuse stale data as if it were current.
- LINE smart-stock bot data, cache, token, and recommendation logic are out of scope for this repository unless a future design explicitly documents a safe cross-repo interface.

## Processed JSON Contracts

All processed JSON files must include:

- `generated_at`
- `data_date`
- `timezone`
- `source`
- `status`
- `records` or a clearly named equivalent object

Current P0 files:

- `data/processed/market_summary.json`
- `data/processed/stock_radar.json`
- `data/processed/economic_calendar.json`
- `data/processed/sector_rotation.json`
- `data/processed/data_health.json`

## P0 Sources

| Area | Dataset | Current Source | Status | Secret Required | Failure Behavior |
| --- | --- | --- | --- | --- | --- |
| Global markets | S&P 500, Nasdaq, SOX, Dow, Nikkei, TAIEX, OTC, Shanghai, HSCE, Nifty, Bovespa | Yahoo Finance | connected | no | mark row failed |
| Cross asset | US 10Y, DXY, USD/TWD, USD/JPY, USD/CNY, VIX, gold, WTI, Brent, copper | Yahoo Finance | connected | no | mark row failed |
| Stock radar | Fixed public research universe, public news themes, TWSE institutional rows, Yahoo price fallback | Google News RSS / TWSE / Yahoo Finance | connected / partial | no | exclude weak or unverified names |
| Economic calendar | Near-term high-impact event template | Manual P0 template | partial | no | show `待接資料源` for actual/forecast |
| Sector rotation | News theme classifier | Google News RSS | partial | no | label as proxy until market/fundamental data is connected |

## P1 Sources

| Area | Dataset | Target Source | Notes |
| --- | --- | --- | --- |
| US macro | CPI, PPI, PCE, payrolls, wages, ISM, retail sales, durable goods | BLS / BEA / Census / ISM / FRED | store actual, forecast, prior, revision, surprise |
| Taiwan macro | exports, export orders, industrial production, NDC signal, M1B/M2, CPI, unemployment | MOEA / NDC / CBC / DGBAS | monthly cadence; show release date |
| Taiwan capital flow | foreign/investment trust/dealer, futures net position, margin, securities lending | TWSE / TPEx / TAIFEX | daily cadence |
| Company fundamentals | monthly revenue, EPS, ROE, gross margin, operating margin | MOPS / TWSE / TPEx / FinMind | do not show incomplete metrics as final |
| Sector rotation | 1/5/20/60 day return, turnover, institutional flow, revenue growth | TWSE / TPEx / FinMind / Yahoo Finance | build sector heat matrix |

## Secret Safety Checklist

Before changing workflows or scripts:

- Search for token patterns and secret names.
- Do not print `os.environ`, `GEMINI_API_KEY`, `JT_PM_ACTIONS_TOKEN`, Telegram tokens, LINE tokens, or any GitHub token.
- Use GitHub Actions Secrets for any required key.
- Do not add local `.env`, generated credential files, browser cookies, or private portfolio files.
- Confirm public generated files contain only public market/news data.
