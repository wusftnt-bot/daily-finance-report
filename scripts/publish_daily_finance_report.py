from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import daily_telegram_push as telegram


TW = dt.timezone(dt.timedelta(hours=8))
DEFAULT_OUTPUT_DIR = Path(os.environ.get("DAILY_FINANCE_REPORT_DIR", "daily-finance-report-site"))
MAX_NEWS_ITEMS = int(os.environ.get("DAILY_FINANCE_REPORT_NEWS_LIMIT", "18"))
MIN_NEWS_ITEMS = int(os.environ.get("DAILY_FINANCE_REPORT_MIN_NEWS", "10"))


THEMES = {
    "ai": {
        "label": "AI、半導體與科技資本支出",
        "keywords": ("ai", "nvidia", "amd", "semiconductor", "chip", "tsmc", "apple", "microsoft", "data center", "半導體", "晶片", "台積電", "輝達", "資料中心", "人工智慧", "科技"),
    },
    "taiwan": {
        "label": "台股與亞洲市場",
        "keywords": ("taiwan", "taipei", "taiex", "asia", "japan", "nikkei", "korea", "台股", "台灣", "亞洲", "日股", "韓股", "陸股", "港股"),
    },
    "rates": {
        "label": "利率、通膨與流動性",
        "keywords": ("fed", "fomc", "yield", "rate", "treasury", "bond", "inflation", "cpi", "美元", "殖利率", "利率", "通膨", "聯準會", "美債"),
    },
    "fx": {
        "label": "匯率、美元與跨境資金",
        "keywords": ("dollar", "currency", "forex", "yen", "yuan", "exchange rate", "美元", "日圓", "人民幣", "匯率", "台幣", "外資", "資金流"),
    },
    "earnings": {
        "label": "企業財報與產業展望",
        "keywords": ("earnings", "revenue", "profit", "guidance", "forecast", "sales", "margin", "財報", "營收", "獲利", "利潤", "展望", "法說會"),
    },
    "energy": {
        "label": "能源、原物料與運輸成本",
        "keywords": ("oil", "opec", "energy", "crude", "gas", "gold", "copper", "shipping", "原油", "能源", "黃金", "銅", "天然氣", "航運", "運費"),
    },
    "geopolitics": {
        "label": "地緣政治與政策風險",
        "keywords": ("iran", "israel", "war", "tariff", "sanction", "trade", "policy", "伊朗", "以色列", "戰爭", "關稅", "制裁", "地緣", "政策"),
    },
    "markets": {
        "label": "全球風險偏好與資產配置",
        "keywords": (),
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


def keyword_matches(haystack: str, keyword: str) -> bool:
    needle = keyword.lower()
    if needle.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None
    return needle in haystack


def classify_item(headline: str) -> str:
    haystack = headline.lower()
    scores: dict[str, int] = {}
    for key, theme in THEMES.items():
        if key == "markets":
            continue
        matches = [keyword for keyword in theme["keywords"] if keyword_matches(haystack, keyword)]
        if matches:
            scores[key] = sum(2 if len(keyword) >= 5 else 1 for keyword in matches)
    if not scores:
        return "markets"
    return sorted(scores, key=lambda key: (-scores[key], key))[0]


def translate_headline(headline: str) -> str:
    if re.search(r"[\u4e00-\u9fff]", headline):
        return headline
    try:
        translated = clean_text(telegram.translate_headline_to_zh_tw(headline))
    except Exception as exc:
        print(f"Headline translation fallback: {exc}")
        translated = headline
    return translated or headline


def translate_headlines(headlines: list[str]) -> list[str]:
    pending = [
        {"id": index, "headline": headline}
        for index, headline in enumerate(headlines)
        if not re.search(r"[\u4e00-\u9fff]", headline)
    ]
    if not pending:
        return headlines

    fallback = {"translations": pending}
    prompt = (
        "Translate every headline into Traditional Chinese (Taiwan). "
        "Keep company names, product names, and stock symbols in English. "
        "Return JSON only in this shape: "
        '{"translations":[{"id":0,"headline":"translated headline"}]}.\n'
        f"Headlines: {json.dumps(pending, ensure_ascii=False)}"
    )
    raw = telegram.call_gemini(prompt, json.dumps(fallback, ensure_ascii=False), max_tokens=2400)
    candidate = parse_json_object(raw)
    translations = candidate.get("translations") if isinstance(candidate, dict) else None
    if not isinstance(translations, list):
        return headlines

    by_id = {item.get("id"): clean_text(item.get("headline")) for item in translations if isinstance(item, dict)}
    result = headlines.copy()
    for item in pending:
        translated = by_id.get(item["id"])
        if translated:
            result[item["id"]] = translated[:300]
    return result


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
        title = headline
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

    translated = translate_headlines([item["original_headline"] for item in news])
    for item, title in zip(news, translated):
        item["headline"] = title
        item["theme"] = classify_item(f"{item['original_headline']} {title} {item['source']}")
    return news


def theme_counts(news: list[dict[str, str]]) -> Counter[str]:
    counts: Counter[str] = Counter(item["theme"] for item in news)
    for key in THEMES:
        counts.setdefault(key, 0)
    return counts


def extract_previous_analysis(previous_html: str) -> dict:
    if not previous_html:
        return {}
    match = re.search(
        r'<script type="application/json" id="market-analysis-data">(.*?)</script>',
        previous_html,
        flags=re.DOTALL,
    )
    if not match:
        return {}
    try:
        return json.loads(html.unescape(match.group(1)))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def theme_evidence(news: list[dict[str, str]], today: dt.date, key: str) -> tuple[float, int, int]:
    related = news if key == "markets" else [item for item in news if item["theme"] == key]
    if key == "markets":
        related = [item for item in related if item["theme"] == "markets"]
    sources = len({item["source"] for item in related})
    recent = sum(1 for item in related if item.get("published", "").startswith(today.isoformat()))
    score = len(related) * 3.0 + sources * 0.8 + recent * 0.5
    return score, sources, recent


def ranked_theme_keys(news: list[dict[str, str]], today: dt.date, previous: dict) -> list[str]:
    previous_keys = {axis.get("theme_key") for axis in previous.get("axes", []) if axis.get("theme_key")}
    scored = []
    for key in THEMES:
        score, sources, recent = theme_evidence(news, today, key)
        if score <= 0:
            continue
        repeat_penalty = 0.25 if key in previous_keys else 0.0
        scored.append((score - repeat_penalty, sources, recent, key))
    scored.sort(key=lambda row: (-row[0], -row[1], -row[2], row[3]))
    selected = [row[3] for row in scored[:3]]
    if len(selected) < 3 and "markets" not in selected:
        selected.append("markets")
    for key in THEMES:
        if len(selected) >= 3:
            break
        if key not in selected:
            selected.append(key)
    return selected[:3]


def top_items_for_theme(news: list[dict[str, str]], theme_key: str, limit: int = 2) -> list[dict[str, str]]:
    related = [item for item in news if item["theme"] == theme_key]
    if not related and theme_key == "markets":
        related = news
    return related[:limit]


def fallback_axis(theme_key: str, count: int) -> dict[str, str]:
    reads = {
        "ai": (
            "今日新聞焦點集中在 AI 需求、半導體供應鏈與科技資本支出。",
            "有利高品質成長股，但估值偏高標的對展望修正更敏感。",
            "觀察龍頭公司指引、伺服器需求與半導體接單動能。",
        ),
        "taiwan": (
            "台股與亞股焦點落在外資流向、權值股表現與區域風險偏好。",
            "若匯率與外資同步轉強，電子權值股的支撐度較高。",
            "觀察台幣、外資現貨與期貨部位、成交量與類股輪動。",
        ),
        "rates": (
            "利率、通膨與流動性仍是影響全球估值與資金成本的核心變數。",
            "殖利率上行壓縮成長股估值，降溫則有利風險資產修復。",
            "觀察美債殖利率、美元指數、Fed 官員談話與通膨數據。",
        ),
        "fx": (
            "匯率變動正在重新定價跨境資金流向與亞洲市場的風險承受度。",
            "美元走強容易壓抑新興市場，台幣走勢則影響外資配置。",
            "觀察美元、日圓、台幣與外資現貨買賣超的方向是否一致。",
        ),
        "earnings": (
            "企業財報與展望是驗證估值與產業需求的直接證據。",
            "營收與利潤率優於預期有利股價，指引保守則可能引發重新定價。",
            "觀察管理層指引、訂單能見度、利潤率與庫存變化。",
        ),
        "energy": (
            "能源與原物料價格正在影響通膨預期與企業成本。",
            "油價與運費上升對航空、運輸與消費產業不利，卻支撐能源類股。",
            "觀察原油、黃金、銅價、運費指數與庫存數據。",
        ),
        "geopolitics": (
            "地緣政治與政策消息提高了市場的事件風險溢價。",
            "局勢升溫時避險資產與能源較受支撐，高波動資產易承壓。",
            "觀察官方聲明、制裁與關稅變化，以及油價與金價反應。",
        ),
        "markets": (
            "全球股匯債商品訊號分歧，市場焦點正在資產配置與風險控管。",
            "風險偏好回升有利股市，若債匯波動擴大則適合降低單一方向暴露。",
            "觀察美股期貨、波動率、美元、殖利率與主要股指廣度。",
        ),
    }
    summary, impact, watch = reads[theme_key]
    return {
        "theme_key": theme_key,
        "title": THEMES[theme_key]["label"],
        "summary": summary,
        "impact": impact,
        "watch": watch,
        "count": count,
    }


def parse_json_object(value: str) -> dict:
    value = clean_text(value).replace("```json", "").replace("```", "")
    start = value.find("{")
    end = value.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        return json.loads(value[start : end + 1])
    except json.JSONDecodeError:
        return {}


def build_market_analysis(news: list[dict[str, str]], today: dt.date, previous: dict) -> dict:
    counts = theme_counts(news)
    selected_keys = ranked_theme_keys(news, today, previous)
    fallback_axes = [fallback_axis(key, counts[key]) for key in selected_keys]
    previous_titles = [clean_text(axis.get("title")) for axis in previous.get("axes", []) if axis.get("title")]
    evidence = [
        {
            "source": item["source"],
            "published": item["published"],
            "headline": item["headline"],
            "theme_key": item["theme"],
        }
        for item in news
    ]
    fallback = {
        "market_state": f"今日以{fallback_axes[0]['title']}為首要主軸，並結合{fallback_axes[1]['title']}與{fallback_axes[2]['title']}觀察資金輪動。",
        "change_from_previous": (
            "主軸已依今日新聞權重重新排序，並與昨日焦點交叉比對。"
            if previous_titles
            else "今日建立第一份動態市場比較基準。"
        ),
        "axes": fallback_axes,
    }
    prompt = f"""
你是財經編輯，請只根據提供的新聞證據，用繁體中文產生每日投資儀表板解讀。不可捏造數字、來源或事件。
報告日：{today.isoformat()}
已選定的三個主軸 key：{json.dumps(selected_keys, ensure_ascii=False)}
昨日主軸：{json.dumps(previous_titles, ensure_ascii=False)}
新聞證據：{json.dumps(evidence, ensure_ascii=False)}

只回傳一個 JSON object，不要 Markdown：
{{
  "market_state": "2至3句今日市場狀態總結",
  "change_from_previous": "1至2句說明今日和昨日的主軸差異；若無昨日資料要直說",
  "axes": [
    {{"theme_key":"必須是指定 key", "title":"今日具體主軸", "summary":"2句證據解讀", "impact":"1至2句對資產的可能影響", "watch":"未來24小時可驗證指標"}}
  ]
}}
規則：axes 必須恰好三組，並按指定 key 順序。文字要具體、可驗證，避免只寫「保留追蹤」或永遠重複利率與評價。
"""
    candidate: dict = {}
    candidate_axes = None
    for attempt in range(3):
        raw = telegram.call_gemini(prompt, json.dumps(fallback, ensure_ascii=False), max_tokens=1800)
        candidate = parse_json_object(raw)
        candidate_axes = candidate.get("axes") if isinstance(candidate, dict) else None
        if isinstance(candidate_axes, list):
            break
        if attempt < 2:
            time.sleep(2 ** attempt)
    if not isinstance(candidate_axes, list):
        return fallback

    by_key = {axis.get("theme_key"): axis for axis in candidate_axes if isinstance(axis, dict)}
    axes = []
    for fallback_item in fallback_axes:
        key = fallback_item["theme_key"]
        item = by_key.get(key, {})
        axis = fallback_item.copy()
        for field in ("title", "summary", "impact", "watch"):
            value = clean_text(item.get(field))
            if value:
                axis[field] = value[:360]
        axes.append(axis)

    market_state = clean_text(candidate.get("market_state")) or fallback["market_state"]
    change = clean_text(candidate.get("change_from_previous")) or fallback["change_from_previous"]
    return {"market_state": market_state[:500], "change_from_previous": change[:400], "axes": axes}


def render_theme_cards(news: list[dict[str, str]], analysis: dict) -> str:
    cards: list[str] = []
    for index, axis in enumerate(analysis["axes"], 1):
        related = top_items_for_theme(news, axis["theme_key"])
        headlines = "".join(
            f'<li><a href="{html.escape(item["link"])}">{html.escape(item["headline"])}</a></li>'
            for item in related
        )
        cards.append(
            f"""
            <article class="theme-card">
              <div class="axis-rank">今日主軸 {index}</div>
              <div class="theme-topline">
                <h3>{html.escape(axis["title"])}</h3>
                <strong>{axis["count"]}</strong>
              </div>
              <p>{html.escape(axis["summary"])}</p>
              <dl>
                <dt>市場影響</dt><dd>{html.escape(axis["impact"])}</dd>
                <dt>24 小時觀察</dt><dd>{html.escape(axis["watch"])}</dd>
              </dl>
              <ul>{headlines}</ul>
            </article>
            """
        )
    return "\n".join(cards)


def render_watch_grid(analysis: dict) -> str:
    watches = "；".join(axis["watch"] for axis in analysis["axes"])
    impacts = "；".join(axis["impact"] for axis in analysis["axes"])
    return f"""
      <div><h3>今日市場狀態</h3><p>{html.escape(analysis["market_state"])}</p></div>
      <div><h3>相較昨日</h3><p>{html.escape(analysis["change_from_previous"])}</p></div>
      <div><h3>台股與亞洲風險清單</h3><p>{html.escape(impacts)} 驗證點：{html.escape(watches)}</p></div>
    """


def analysis_data_html(analysis: dict) -> str:
    payload = json.dumps(analysis, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return f'<script type="application/json" id="market-analysis-data">{payload}</script>'



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


def render_html(news: list[dict[str, str]], today: dt.date, previous_html: str = "") -> str:
    generated_at = dt.datetime.now(TW).strftime("%Y-%m-%d %H:%M:%S Asia/Taipei")
    source_count = len({item["source"] for item in news})
    previous = extract_previous_analysis(previous_html)
    analysis = build_market_analysis(news, today, previous)
    dominant_label = analysis["axes"][0]["title"]
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="report-date" content="{today:%Y-%m-%d}">
  <meta name="report-news-count" content="{len(news)}">
  <meta name="report-generator" content="github-actions">
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
    .themes {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }}
    .theme-card {{ padding: 18px; }}
    .axis-rank {{ color: var(--teal); font-size: 13px; font-weight: 800; }}
    .theme-topline {{ display: flex; align-items: start; justify-content: space-between; gap: 12px; color: var(--teal); }}
    .theme-topline h3 {{ margin: 6px 0 0; color: var(--ink); font-size: 19px; }}
    .theme-topline strong {{ color: var(--ink); font-size: 24px; }}
    .theme-card dl {{ margin: 14px 0; }}
    .theme-card dt {{ color: var(--muted); font-size: 12px; font-weight: 800; }}
    .theme-card dd {{ margin: 2px 0 10px; color: #334454; }}
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
      <p>{html.escape(analysis["market_state"])}</p>
      <div class="meta">
        <span class="pill">Date: {today:%Y-%m-%d}</span>
        <span class="pill">Source: GitHub Actions</span>
        <span class="pill">Mode: dashboard report</span>
      </div>
    </div>
  </header>
  <main>
    <section class="metrics" aria-label="Report metrics">
      <div class="metric"><span>今日新聞</span><strong>{len(news)}</strong></div>
      <div class="metric"><span>新聞來源</span><strong>{source_count}</strong></div>
      <div class="metric"><span>第一主軸</span><strong>{html.escape(dominant_label)}</strong></div>
      <div class="metric"><span>解讀模式</span><strong>Dynamic 3</strong></div>
    </section>

    <section class="panel market-brief">
      <strong>相較昨日</strong>
      <p>{html.escape(analysis["change_from_previous"])}</p>
    </section>

    <div class="section-title">
      <h2>市場主題</h2>
      <p>依新聞權重歸納今日環境，作為投資儀表版摘要。</p>
    </div>
    <section class="themes">{render_theme_cards(news, analysis)}</section>

    <div class="section-title">
      <h2>重點新聞</h2>
      <p>保留來源連結，避免只留下 Telegram 摘要文字。</p>
    </div>
    <section class="news-list">{render_news_list(news)}</section>

    <div class="section-title">
      <h2>台股觀察與風險</h2>
      <p>把新聞轉成明天/今日盤中可追蹤的檢查項。</p>
    </div>
    <section class="panel watch-grid">{render_watch_grid(analysis)}</section>

    <div class="section-title">
      <h2>來源分布</h2>
      <p>用來檢查是否過度集中在單一媒體。</p>
    </div>
    <section class="panel sources">{render_source_table(news)}</section>

    {analysis_data_html(analysis)}

    <p class="footer">Updated: {generated_at}. This page is generated independently by GitHub Actions and no longer depends on Codex automation.</p>
  </main>
</body>
</html>
"""


def main() -> int:
    today = dt.datetime.now(TW).date()
    output_dir = DEFAULT_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    previous_html = (output_dir / "index.html").read_text(encoding="utf-8") if (output_dir / "index.html").exists() else ""
    news = collect_news(today)
    if len(news) < MIN_NEWS_ITEMS:
        raise RuntimeError(
            f"Refusing to publish an incomplete report: got {len(news)} news items, "
            f"need at least {MIN_NEWS_ITEMS}"
        )
    (output_dir / "index.html").write_text(render_html(news, today, previous_html), encoding="utf-8")
    print(f"Published dashboard finance report for {today:%Y-%m-%d} with {len(news)} items to {output_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
