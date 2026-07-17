# Daily Finance Report

Public GitHub Pages URL:
https://wusftnt-bot.github.io/daily-finance-report/

This repository owns the static daily finance page and its GitHub Actions generation flow.

## Product Positioning

This site is the primary investment decision platform, not only a Telegram/LINE message archive.

Target product definition:

- Taiwan-equity-centered investment dashboard.
- Integrates US macro, Taiwan macro, global cross-asset conditions, Taiwan capital flow, sector rotation, and stock research.
- Telegram/LINE should stay concise: market light, top 3 changes, major event alerts, priority stock radar, and a website link.
- The website owns full data, charts, source traceability, methodology, historical records, and deeper drill-down.

Decision flow:

`Market regime -> capital flow -> sector trend -> stock conditions -> investment risk`.

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
- The candidate stock table is generated inside this repository and is not copied from the LINE smart-stock bot.

## Candidate Stock Selection

The dashboard candidate table is an independent public-market watchlist, not the LINE bot's recommendation list and not a personal portfolio.

Current selection and scoring inputs:

- Core candidate universe: only `2317 鴻海`, `2881 富邦金`, `2330 台積電`, and `2308 台達電` remain as fixed core-tracking names.
- Dynamic candidate universe: additional names must come from public-market data, currently TWSE T86 institutional flow, and must pass the public priority thresholds before display.
- Theme score: daily finance/news themes such as AI, semiconductors, earnings, rates, Taiwan market, FX, and geopolitics.
- Institutional flow score: TWSE public three-major-institution data when available, including foreign investors, investment trusts, dealers, latest daily total, and recent 5-trading-day trend.
- Technical score: public Yahoo Finance price movement fallback.
- Data quality score: reduced when institutional or market data is unavailable.

Rules:

- Strong news themes alone must not create a high total score when institutional flow is clearly negative.
- The dashboard classification names must stay distinct from LINE bot recommendation categories. Use dashboard-only labels such as `Dashboard 強勢觀察`, `題材轉強觀察`, `中性追蹤`, `低分觀察`, and `籌碼降級觀察`.
- Large foreign/institutional selling must downgrade the stock to `籌碼降級觀察` or wait-for-stabilization status.
- The table is an information dashboard for screening and follow-up, not a guaranteed buy/sell recommendation.
- LINE bot outputs, LINE cache files, and LINE credentials must not be used here unless a future change explicitly documents a safe cross-repo design.

## Public Priority Candidate Rule

The public dashboard must show only priority candidates. Low-score, neutral, waitlist, downgrade, or warning-only stocks must not appear in the public candidate table.

- Current public threshold: total score >= 82 and institutional-flow score >= 14.
- Current public cap: 6 names.
- Strong news themes alone must not qualify a stock when institutional flow is weak.
- Large foreign/institutional selling must exclude the stock from the public priority table until flow stabilizes.
- Dashboard labels must stay distinct from LINE bot recommendation categories.

## Macro Data Roadmap

The dashboard should gradually add macro indicators that can be tied to Taiwan equity decisions. Keep the first version compact and decision-oriented:

- United States monthly core set: core CPI, core PCE, nonfarm payrolls / wage growth, ISM manufacturing new orders, retail sales, 10Y Treasury yield, and DXY.
- Taiwan monthly core set: exports, export orders, industrial production, NDC business cycle signal, M1B / M2, USD/TWD, foreign investor net buy/sell, and listed-company monthly revenue.
- Quarterly checks: GDP details, listed-company margin / EPS / inventory trends, key supply-chain earnings calls, FOMC projections, and Taiwan central bank meetings.
- Each macro item should record actual value, consensus, prior value / revision, MoM / YoY / 3-month trend, surprise direction, and market reaction in yields, USD, TAIEX futures, and foreign flow.
- Macro signals should be rendered as decision lights: rate-pressure, recovery, and risk-weakening.

## Data Source Integration Rule

New data sources must be integrated through the data layer first, not directly hard-coded into `index.html`.

- Source map: `docs/data-source-map.md`.
- Processed data path: `data/processed/*.json`.
- Current P0 processed files: `market_summary.json`, `market_history.json`, `capital_flow.json`, `dynamic_stock_pool.json`, `stock_radar.json`, `economic_calendar.json`, `sector_rotation.json`, `derivatives_flow.json`, `market_breadth.json`, `macro_indicators.json`, `fundamentals.json`, and `data_health.json`.
- Required fields: `generated_at`, `data_date`, `timezone`, `source`, `status`, and `records` or an equivalent named object.
- If a source fails, show `資料更新失敗`, `待接資料源`, or `not_connected`; do not show stale data as current.
- Telegram should read only summary-ready public data and must not be blocked by cosmetic website section-title changes.
- LINE smart-stock bot data and credentials must stay separate from this public dashboard repository.

## 72-Hour Material Event Rule

The economic-calendar section must not hard-code a single example event as a permanent output. Company events such as TSMC earnings, Foxconn guidance, Delta Electronics margin updates, or Fubon Financial results must be selected dynamically from public news, official calendars, filings, and processed data.

- Rank events by recency, source reliability, event type, market impact, and whether the information can be verified by public data.
- Earnings releases, investor conferences, guidance changes, margin/EPS/profit updates, capex changes, major announcements, and regulatory filings outrank old monthly revenue.
- Monthly revenue is only a fallback when no higher-impact company event is available.
- Do not hard-code a quarter such as Q2 or Q3 into the output logic. Quarter-specific text may appear only when it comes from the current selected public source.
- Every selected material event should show the source, update time or publish time, why it matters, and what data should be checked next.

## Outstanding Dashboard Quality Items

Track these as product-quality requirements rather than cosmetic backlog:

- Publish workflow stability: keep job-level timeout, step-level timeout, concurrency cancellation, and external-source fallback so one slow source cannot block the full site or Telegram workflow.
- Macro score calculation: replace proxy wording with direct calculations from CPI, PCE, ISM, export orders, industrial production, exports, capital flow, FX, yields, and market breadth as sources become stable.
- Forecast / Surprise: use a reliable public or low-cost source when available; otherwise explicitly show that no reliable free consensus is connected. Never fabricate consensus or surprise values.
- Financial-sector fundamentals: financial stocks such as Fubon Financial need an industry-specific model instead of manufacturing-style gross margin, inventory, and receivables fields.
- Source and freshness display: every major card should show source, update time, market timezone, data status, and whether the value is delayed, official close, intraday, fallback, or unavailable.
- Stock Radar traceability: each selected stock must keep entry reason, score components, score change, upgrade/downgrade trigger, invalidation condition, source, and update time.

Current connected expansion batch:

- `derivatives_flow.json`: TAIFEX foreign TAIEX futures net position and TXO Put/Call Ratio.
- `market_history.json`: generated daily market-regime score history used for previous-day score and weekly change.
- `market_breadth.json`: TWSE listed-stock advance/decline breadth, 20-trading-day new high/new low counts, fixed-core-stock margin/short-sale balances, and fixed-core-stock securities-lending transactions through public TWSE / FinMind data. Full-market official TWSE/TPEx lending-sale balance remains a follow-up because the public page endpoint is not yet stable for unattended GitHub Actions access.
- `macro_indicators.json`: FRED public actuals for US CPI, PCE, nonfarm payrolls, initial claims, retail sales, housing starts, building permits, Fed Funds, US 2Y, and US 10Y; ISM Manufacturing PMI and New Orders through official-ISM-first / MacroMicro-public fallback; Taiwan exports, export orders, industrial production, NDC business-cycle signal, M1B, and M2 through Customs / MOEA / NDC / CBC public data.
- Forecast / Surprise fields remain blank when a free public source does not provide market consensus. Do not fabricate consensus or surprise values.
- `fundamentals.json`: fixed core pool monthly revenue, EPS, gross margin, ROE, inventory, and receivables through FinMind public datasets. Direct MOPS/TWSE/TPEx validation remains a follow-up item.

## Development Priorities

P0:

- Rebuild the home page as `Market Dashboard`.
- Add `Macro Regime Score` with growth, rates/inflation, capital/FX, market trend, and risk-sentiment components.
- Add global market cards, cross-asset radar, 72-hour event table, Taiwan capital-flow summary, and priority-only Stock Radar.
- Show source, update time, data status, and failure/placeholder state clearly.
- Keep mobile layout readable.

P1:

- Add full US Macro and Taiwan Macro pages with actual/forecast/prior/surprise/trend fields.
- Add sector rotation with 1/5/20/60-day return, turnover, institutional flow, revenue growth, and heat matrix.
- Add Stock Detail pages with fundamentals, institutional flow, technicals, valuation, events, and change history.

P2:

- Add personalized watchlists only through a private design that never exposes portfolio data on GitHub Pages.
- Add backtests, sector/stock comparison, and deeper Telegram/LINE linking.

## Secret Safety

- `GEMINI_API_KEY` must be stored only in GitHub Actions Secrets and referenced as `${{ secrets.GEMINI_API_KEY }}`.
- `JT_PM_ACTIONS_TOKEN`, if configured, must be stored only in GitHub Actions Secrets. Never print it, write it to generated HTML, upload it as an artifact, or reuse it for LINE or Telegram bot API calls.
- Telegram and LINE tokens must never be added to this repository, logs, generated HTML, artifacts, caches, or workflow summaries.
- No API key, token, chat id, credential, cookie, private path, or personal/private portfolio data may appear in local generated files, committed GitHub files, GitHub Pages HTML, workflow artifacts, workflow cache, workflow summaries, or any external public URL.
- Public pages may contain only public market/news data and non-sensitive generated analysis.
- Never print environment variables or use dangerous output such as `print(os.environ["GEMINI_API_KEY"])`, `echo "$GEMINI_API_KEY"`, or a full environment dump.
- Before changing workflows or scripts, search current files and Git history for `AIza`, token patterns, secret names, and environment dumps.

## Cleanup Rule

Do not commit generated test files, duplicate assets, caches, screenshots, logs, old report copies, API keys, or bot tokens. Keep source files readable and avoid packed or obfuscated code so security-sensitive behavior remains auditable.
