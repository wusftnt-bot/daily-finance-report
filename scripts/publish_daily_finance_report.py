from __future__ import annotations

import datetime as dt
import html
import os
import re
from pathlib import Path

import daily_telegram_push as telegram


TW = dt.timezone(dt.timedelta(hours=8))
DEFAULT_OUTPUT_DIR = Path(os.environ.get("DAILY_FINANCE_REPORT_DIR", "daily-finance-report-site"))


def linkify(text: str) -> str:
    escaped = html.escape(text)
    return re.sub(
        r"(https?://[^\s<]+)",
        lambda match: f'<a href="{match.group(1)}">{match.group(1)}</a>',
        escaped,
    )


def render_html(report_text: str, today: dt.date) -> str:
    generated_at = dt.datetime.now(TW).strftime("%Y-%m-%d %H:%M:%S Asia/Taipei")
    first_line = report_text.splitlines()[0].strip("[] ") if report_text.splitlines() else "Daily Finance Report"
    body = linkify(report_text)
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Daily Finance Report - {today:%Y-%m-%d}</title>
  <style>
    :root {{
      --ink: #17202a;
      --muted: #607080;
      --line: #d8e0e8;
      --bg: #f5f7fa;
      --panel: #ffffff;
      --blue: #1f5fbf;
      --teal: #087f8c;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: "Segoe UI", "Noto Sans TC", Arial, sans-serif;
      line-height: 1.72;
    }}
    a {{ color: var(--blue); text-decoration: none; overflow-wrap: anywhere; }}
    a:hover {{ text-decoration: underline; }}
    header {{
      background:
        linear-gradient(120deg, rgba(10,31,52,.88), rgba(4,84,96,.72)),
        url("assets/finance-newsroom-hero.png") center/cover;
      color: #fff;
    }}
    .hero, main {{ width: min(1120px, 92vw); margin: 0 auto; }}
    .hero {{ min-height: 320px; display: flex; flex-direction: column; justify-content: flex-end; padding: 44px 0; }}
    .kicker {{ font-size: 14px; color: #d9e6f2; }}
    h1 {{ font-size: clamp(30px, 4.5vw, 52px); line-height: 1.1; margin: 12px 0 0; letter-spacing: 0; }}
    .meta {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 22px; }}
    .pill {{ border: 1px solid rgba(255,255,255,.26); background: rgba(255,255,255,.12); border-radius: 999px; padding: 6px 12px; font-size: 14px; }}
    main {{ padding: 28px 0 52px; }}
    .panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 22px; }}
    .report {{ white-space: pre-wrap; overflow-wrap: break-word; font-size: 16px; }}
    .footer {{ color: var(--muted); font-size: 13px; margin-top: 20px; border-top: 1px solid var(--line); padding-top: 14px; }}
    @media (max-width: 720px) {{
      .hero {{ min-height: 360px; }}
      .panel {{ padding: 16px; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="hero">
      <div class="kicker">Daily Finance Report | {generated_at}</div>
      <h1>{html.escape(first_line)}</h1>
      <div class="meta">
        <span class="pill">Date: {today:%Y-%m-%d}</span>
        <span class="pill">Source: GitHub Actions</span>
        <span class="pill">Status: current daily report generated</span>
      </div>
    </div>
  </header>
  <main>
    <section class="panel report">{body}</section>
    <p class="footer">Updated: {generated_at}. This page is generated from the same daily finance workflow used by the Telegram digest.</p>
  </main>
</body>
</html>
"""


def main() -> int:
    telegram.PUSHED_NEWS_KEYS.clear()
    today = dt.datetime.now(TW).date()
    output_dir = DEFAULT_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    report_text = telegram.finance_message(today)
    (output_dir / "index.html").write_text(render_html(report_text, today), encoding="utf-8")
    print(f"Published daily finance report HTML for {today:%Y-%m-%d} to {output_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
