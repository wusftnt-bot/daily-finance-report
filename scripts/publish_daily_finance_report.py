from __future__ import annotations

import datetime as dt
import html
import os
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import daily_telegram_push as telegram


TW = dt.timezone(dt.timedelta(hours=8))
DEFAULT_OUTPUT_DIR = Path(os.environ.get("DAILY_FINANCE_REPORT_DIR", "daily-finance-report-site"))
MAX_NEWS_ITEMS = int(os.environ.get("DAILY_FINANCE_REPORT_NEWS_LIMIT", "18"))


THEMES = {
    "rates": {
        "label": "利率與估值",
        "keywords": ("fed", "fomc", "yield", "rate", "treasury", "bond", "inflation", "cpi", "美元", "殖利率", "利率", "通膨", "聯準會", "美債"),
    },
    "energy": {
        "label": "能源與地緣風險",
        "keywords": ("oil", "iran", "israel", "opec", "energy", "war", "tariff", "crude", "原油", "伊朗", "以色列", "關稅", "地緣", "能源"),
    },
    "ai": {
        "label": "AI 與半導體",
        "keywords": ("ai", "nvidia", "semiconductor", "chip", "tsmc", "apple", "microsoft", "data center", "半導體", "晶片", "台積電", "輝達", "資料中心"),
    },
    "taiwan": {
        "label": "台股與亞洲市場",
        "keywords": ("taiwan", "taipei", "taiex", "asia", "china", "japan", "台股", "台灣", "亞洲", "日股", "陸股", "港股"),
    },
}


def clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_key(headline: str) -> str:
    normalized = headline.lower()
    normalized = re.sub(r"\bby\s+reuters\b", "", normalized)
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def source_host(link: str) -> str:
    host = urlparse(link).netloc or "source"
    return host.removeprefix("www.")


def classify_item(headline: str) -> str:
    haystack = headline.lower()
    for key, theme in THEMES.items():
        if any(keyword.lower() in haystack for keyword in theme["keywords"]):
            return key
    return "markets"


def translate_headline(headline: str) -> str:
    if re.search(r"[\u4e00-\u9fff]", headline):
        return headline
    try:
        translated = clean_text(telegram.translate_headline_to_zh_tw(headline))
    except Exception as exc:
        print(f"Headline translation fallback: {exc}")
        translated = headline
    return translated or headline


def collect_news(today: dt.date) -> list[dict[str, str]]:
    telegram.PUSHED_NEWS_KEYS.clear()
    candidates = telegram.finance_candidates(today)
    selected = telegram.diversify_items_by_source(candidates, today, limit=MAX_NEWS_ITEMS + 8, per_source_cap=3)

    seen: set[str] = set()
    news: list[dict[str, str]] = []
    for item in selected:
        source = clean_text(item.get("source"))
        headline = clean_text(item.get("headline"))
        link = clean_text(item.get("link") or item.get("url"))
        published = clean_text(item.get("published") or item.get("date"))
        if not source or not headline or not link:
            continue
        if "reddit" in source.lower():
            continue
        key = normalize_key(headline)
        if not key or key in seen:
            continue
        seen.add(key)
        title = translate_headline(headline)
        news.append(
            {
                "source": source,
                "headline": title,
                "original_headline": headline,
                "published": published or today.strftime("%Y-%m-%d"),
                "link": link,
                "host": source_host(link),
                "theme": classify_item(f"{headline} {title} {source}"),
            }
        )
        if len(news) >= MAX_NEWS_ITEMS:
            break
    return news


def theme_counts(news: list[dict[str, str]]) -> Counter[str]:
    counts: Counter[str] = Counter(item["theme"] for item in news)
    counts.setdefault("rates", 0)
    counts.setdefault("energy", 0)
    counts.setdefault("ai", 0)
    counts.setdefault("taiwan", 0)
    return counts


def market_read(theme_key: str, count: int) -> str:
    reads = {
        "rates": "長天期利率、美元與通膨預期仍是估值壓力的主軸；成長股反彈若缺乏殖利率配合，容易出現震盪。",
        "energy": "能源與地緣新聞會直接影響通膨預期、航運成本與避險情緒，短線需留意油價與美元同步變化。",
        "ai": "AI 與半導體仍是資金風險偏好的核心題材，但估值與財報展望的容錯空間正在變小。",
        "taiwan": "台股需同時觀察外資期現貨、台幣與電子權值股；若國際風險升溫，盤面可能先反映在成交量與類股輪動。",
    }
    if count <= 0:
        return "今日此主題新聞權重較低，暫列為背景變數，等待後續數據或市場價格確認。"
    return reads.get(theme_key, "全球股匯債商品訊號分歧，適合用分批與事件風險控管取代單點判斷。")


def top_items_for_theme(news: list[dict[str, str]], theme_key: str, limit: int = 2) -> list[dict[str, str]]:
    return [item for item in news if item["theme"] == theme_key][:limit]


def render_theme_cards(news: list[dict[str, str]]) -> str:
    counts = theme_counts(news)
    cards: list[str] = []
    for theme_key in ("rates", "energy", "ai", "taiwan"):
        theme = THEMES[theme_key]
        related = top_items_for_theme(news, theme_key)
        headlines = "".join(
            f'<li><a href="{html.escape(item["link"])}">{html.escape(item["headline"])}</a></li>'
            for item in related
        )
        if not headlines:
            headlines = "<li>目前沒有高權重新聞，保留追蹤。</li>"
        cards.append(
            f"""
            <article class="theme-card">
              <div class="theme-topline">
                <span>{html.escape(theme["label"])}</span>
                <strong>{counts[theme_key]}</strong>
              </div>
              <p>{html.escape(market_read(theme_key, counts[theme_key]))}</p>
              <ul>{headlines}</ul>
            </article>
            """
        )
    return "\n".join(cards)


def render_news_list(news: list[dict[str, str]]) -> str:
    if not news:
        return '<div class="empty">今日新聞來源暫時未回傳可用資料，請稍後重跑 GitHub Actions。</div>'
    rows = []
    for index, item in enumerate(news, 1):
        label = THEMES.get(item["theme"], {"label": "全球市場"})["label"]
        rows.append(
            f"""
            <article class="news-row">
              <div class="rank">{index:02d}</div>
              <div>
                <div class="news-meta">
                  <span>{html.escape(label)}</span>
                  <span>{html.escape(item["source"])}</span>
                  <span>{html.escape(item["published"])}</span>
                </div>
                <h3><a href="{html.escape(item["link"])}">{html.escape(item["headline"])}</a></h3>
                <p>觀察重點：這則新聞可能影響相關資產的風險偏好、估值假設或事件溢價；進場前應再對照價格、成交量與美元/利率變化。</p>
              </div>
            </article>
            """
        )
    return "\n".join(rows)


def render_source_table(news: list[dict[str, str]]) -> str:
    counts = Counter(item["source"] for item in news)
    if not counts:
        return ""
    return "\n".join(
        f'<span class="source-pill">{html.escape(source)} <b>{count}</b></span>'
        for source, count in counts.most_common()
    )


def render_html(news: list[dict[str, str]], today: dt.date) -> str:
    generated_at = dt.datetime.now(TW).strftime("%Y-%m-%d %H:%M:%S Asia/Taipei")
    source_count = len({item["source"] for item in news})
    counts = theme_counts(news)
    dominant = max(("rates", "energy", "ai", "taiwan"), key=lambda key: counts[key])
    dominant_label = THEMES[dominant]["label"]
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Daily Finance Report - {today:%Y-%m-%d}</title>
  <style>
    :root {{
      --ink: #18222d;
      --muted: #667789;
      --line: #d9e1e8;
      --bg: #f4f7f8;
      --panel: #ffffff;
      --navy: #10263d;
      --blue: #235fb7;
      --teal: #087f8c;
      --amber: #a76b12;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: "Segoe UI", "Noto Sans TC", Arial, sans-serif;
      line-height: 1.65;
    }}
    a {{ color: var(--blue); text-decoration: none; overflow-wrap: anywhere; }}
    a:hover {{ text-decoration: underline; }}
    header {{
      background:
        linear-gradient(120deg, rgba(10,31,52,.9), rgba(4,84,96,.7)),
        url("assets/finance-newsroom-hero.png") center/cover;
      color: #fff;
    }}
    .hero, main {{ width: min(1160px, 92vw); margin: 0 auto; }}
    .hero {{ min-height: 320px; display: grid; align-content: end; padding: 42px 0; }}
    .kicker {{ font-size: 14px; color: #dbe7f0; }}
    h1 {{ max-width: 820px; margin: 12px 0 0; font-size: clamp(30px, 4.2vw, 54px); line-height: 1.08; letter-spacing: 0; }}
    .hero p {{ max-width: 760px; margin: 16px 0 0; color: #edf5f8; font-size: 18px; }}
    .meta {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 22px; }}
    .pill {{ border: 1px solid rgba(255,255,255,.28); background: rgba(255,255,255,.13); border-radius: 999px; padding: 6px 12px; font-size: 14px; }}
    main {{ padding: 28px 0 54px; }}
    .metrics {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 20px; }}
    .metric, .panel, .theme-card, .news-row {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }}
    .metric {{ padding: 16px; }}
    .metric span {{ display: block; color: var(--muted); font-size: 13px; }}
    .metric strong {{ display: block; margin-top: 6px; font-size: 24px; }}
    .section-title {{ display: flex; align-items: end; justify-content: space-between; gap: 16px; margin: 26px 0 12px; }}
    .section-title h2 {{ margin: 0; font-size: 22px; }}
    .section-title p {{ margin: 0; color: var(--muted); font-size: 14px; }}
    .themes {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
    .theme-card {{ padding: 18px; }}
    .theme-topline {{ display: flex; align-items: center; justify-content: space-between; color: var(--teal); font-weight: 700; }}
    .theme-topline strong {{ color: var(--ink); font-size: 24px; }}
    .theme-card p {{ margin: 12px 0; color: #334454; }}
    .theme-card ul {{ margin: 0; padding-left: 18px; }}
    .news-list {{ display: grid; gap: 12px; }}
    .news-row {{ display: grid; grid-template-columns: 48px minmax(0, 1fr); gap: 14px; padding: 16px; }}
    .rank {{ width: 38px; height: 38px; border-radius: 999px; display: grid; place-items: center; background: #e8f0f2; color: var(--navy); font-weight: 800; }}
    .news-meta {{ display: flex; flex-wrap: wrap; gap: 8px; color: var(--muted); font-size: 13px; }}
    .news-meta span {{ border-right: 1px solid var(--line); padding-right: 8px; }}
    .news-meta span:last-child {{ border-right: 0; padding-right: 0; }}
    h3 {{ margin: 6px 0 6px; font-size: 18px; line-height: 1.38; }}
    .news-row p {{ margin: 0; color: #415161; }}
    .panel {{ padding: 18px; }}
    .watch-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }}
    .watch-grid div {{ border-left: 3px solid var(--amber); padding-left: 12px; }}
    .watch-grid h3 {{ margin-top: 0; }}
    .sources {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    .source-pill {{ background: #eef3f5; border: 1px solid var(--line); border-radius: 999px; padding: 7px 11px; color: #34485a; }}
    .footer {{ color: var(--muted); font-size: 13px; margin-top: 20px; border-top: 1px solid var(--line); padding-top: 14px; }}
    .empty {{ color: var(--muted); padding: 16px; }}
    @media (max-width: 820px) {{
      .hero {{ min-height: 360px; }}
      .metrics, .themes, .watch-grid {{ grid-template-columns: 1fr; }}
      .news-row {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="hero">
      <div class="kicker">Daily Finance Report | {generated_at}</div>
      <h1>{today:%Y-%m-%d} 投資儀表版</h1>
      <p>整理今日全球財經新聞、利率與能源脈動、AI/半導體題材，以及台股開盤前後應觀察的資金與風險訊號。</p>
      <div class="meta">
        <span class="pill">Date: {today:%Y-%m-%d}</span>
        <span class="pill">Source: GitHub Actions</span>
        <span class="pill">Mode: dashboard report</span>
      </div>
    </div>
  </header>
  <main>
    <section class="metrics" aria-label="Report metrics">
      <div class="metric"><span>整理新聞</span><strong>{len(news)}</strong></div>
      <div class="metric"><span>新聞來源</span><strong>{source_count}</strong></div>
      <div class="metric"><span>今日主軸</span><strong>{html.escape(dominant_label)}</strong></div>
      <div class="metric"><span>更新狀態</span><strong>Current</strong></div>
    </section>

    <div class="section-title">
      <h2>市場主題</h2>
      <p>依新聞權重歸納今日環境，作為投資儀表版摘要。</p>
    </div>
    <section class="themes">{render_theme_cards(news)}</section>

    <div class="section-title">
      <h2>重點新聞</h2>
      <p>保留來源連結，避免只留下 Telegram 摘要文字。</p>
    </div>
    <section class="news-list">{render_news_list(news)}</section>

    <div class="section-title">
      <h2>台股觀察與風險</h2>
      <p>把新聞轉成明天/今日盤中可追蹤的檢查項。</p>
    </div>
    <section class="panel watch-grid">
      <div>
        <h3>權值與族群</h3>
        <p>先看台積電、AI 伺服器、半導體設備與金融權值是否同步；若只有單一族群撐盤，追價風險較高。</p>
      </div>
      <div>
        <h3>外部變數</h3>
        <p>美元、美債殖利率與油價若同向走高，容易壓縮風險資產估值；若回落，科技股反彈較有延續條件。</p>
      </div>
      <div>
        <h3>操作節奏</h3>
        <p>重大數據與央行會議前，優先控管部位與停損；把新聞當成情境清單，再用價格與量能確認。</p>
      </div>
    </section>

    <div class="section-title">
      <h2>來源分布</h2>
      <p>用來檢查是否過度集中在單一媒體。</p>
    </div>
    <section class="panel sources">{render_source_table(news)}</section>

    <p class="footer">Updated: {generated_at}. This page is generated independently by GitHub Actions and no longer depends on Codex automation.</p>
  </main>
</body>
</html>
"""


def main() -> int:
    today = dt.datetime.now(TW).date()
    output_dir = DEFAULT_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    news = collect_news(today)
    (output_dir / "index.html").write_text(render_html(news, today), encoding="utf-8")
    print(f"Published dashboard finance report for {today:%Y-%m-%d} with {len(news)} items to {output_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
