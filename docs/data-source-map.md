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
- `data/processed/capital_flow.json`
- `data/processed/dynamic_stock_pool.json`
- `data/processed/stock_radar.json`
- `data/processed/economic_calendar.json`
- `data/processed/sector_rotation.json`
- `data/processed/derivatives_flow.json`
- `data/processed/market_breadth.json`
- `data/processed/macro_indicators.json`
- `data/processed/fundamentals.json`
- `data/processed/data_health.json`

## P0 Sources

| Area | Dataset | Current Source | Status | Secret Required | Failure Behavior |
| --- | --- | --- | --- | --- | --- |
| Global markets | S&P 500, Nasdaq, SOX, Dow, Nikkei, TAIEX, OTC, Shanghai, HSCE, Nifty, Bovespa | Yahoo Finance | connected | no | mark row failed |
| Cross asset | US 10Y, DXY, USD/TWD, USD/JPY, USD/CNY, VIX, gold, WTI, Brent, copper | Yahoo Finance | connected | no | mark row failed |
| Taiwan capital flow | foreign investors, investment trusts, dealers, total institutional net buy/sell | TWSE BFI82U | connected | no | show failed/partial and keep FX proxy |
| Dynamic stock pool | full-market institutional buy/sell candidates | TWSE T86 | connected | no | only include names passing institutional thresholds |
| Sector rotation | TWSE sector index close and daily percentage change | TWSE MI_INDEX | connected | no | fall back to news theme cards |
| Stock radar | 4 fixed core names plus dynamic TWSE T86 candidates, public news themes, institutional rows, Yahoo price fallback | Google News RSS / TWSE / Yahoo Finance | connected / partial | no | exclude weak or unverified names |
| Economic calendar | Near-term high-impact event template | Manual P0 template | partial | no | show `待接資料源` for actual/forecast |
| Sector news context | News theme classifier | Google News RSS | partial | no | label as qualitative context |
| TAIFEX derivatives flow | foreign TAIEX futures net position, Put/Call Ratio | TAIFEX | not_connected | no | show `not_connected` |
| TWSE/TPEx breadth and margin | margin, short sale, securities lending, advance/decline, new highs/lows | TWSE / TPEx | not_connected | no | show `not_connected` |
| Macro indicators | US/Taiwan actual, forecast, prior, surprise | Official macro sources / FRED | not_connected | maybe FRED optional | show `not_connected` |
| Company fundamentals | monthly revenue, EPS, margins, ROE, inventory, receivables | MOPS / TWSE / TPEx / FinMind | not_connected | optional FinMind | show `not_connected` |

## P1 Sources

| Area | Dataset | Target Source | Notes |
| --- | --- | --- | --- |
| US macro | CPI, PPI, PCE, payrolls, wages, ISM, retail sales, durable goods | BLS / BEA / Census / ISM / FRED | store actual, forecast, prior, revision, surprise |
| Taiwan macro | exports, export orders, industrial production, NDC signal, M1B/M2, CPI, unemployment | MOEA / NDC / CBC / DGBAS | monthly cadence; show release date |
| Taiwan capital flow | futures net position, margin, securities lending, market breadth, new high/new low | TWSE / TPEx / TAIFEX | daily cadence |
| Company fundamentals | monthly revenue, EPS, ROE, gross margin, operating margin | MOPS / TWSE / TPEx / FinMind | do not show incomplete metrics as final |
| Sector rotation | 1/5/20/60 day return, turnover, institutional flow, revenue growth | TWSE / TPEx / FinMind / Yahoo Finance | build sector heat matrix |

## Remaining Missing Sources

These sources are not yet connected and must be added in small, testable batches:

- US macro actual/forecast/surprise: BLS, BEA, Census, FRED, Federal Reserve.
- Taiwan macro actual/forecast/surprise: DGBAS, MOEA, NDC, CBC.
- TAIFEX futures foreign net position and options put/call ratio.
- TWSE/TPEx margin, short sale, securities lending, market breadth, 20-day/52-week new highs and lows.
- Company fundamentals: MOPS monthly revenue, quarterly EPS, margins, ROE, inventory, receivables.
- Sector rotation depth: 5/20/60-day sector returns, sector turnover, institutional flow by sector, revenue growth by sector.

## Secret Safety Checklist

Before changing workflows or scripts:

- Search for token patterns and secret names.
- Do not print `os.environ`, `GEMINI_API_KEY`, `JT_PM_ACTIONS_TOKEN`, Telegram tokens, LINE tokens, or any GitHub token.
- Use GitHub Actions Secrets for any required key.
- Do not add local `.env`, generated credential files, browser cookies, or private portfolio files.
- Confirm public generated files contain only public market/news data.
