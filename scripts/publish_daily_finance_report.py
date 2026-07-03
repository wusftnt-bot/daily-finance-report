from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import time
import urllib.request
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
    if not telegram.GEMINI_API_KEY:
        return [translate_headline(headline) for headline in headlines]

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
        return [translate_headline(headline) for headline in headlines]

    by_id = {item.get("id"): clean_text(item.get("headline")) for item in translations if isinstance(item, dict)}
    result = headlines.copy()
    for item in pending:
        translated = by_id.get(item["id"])
        if translated and translated != item["headline"]:
            result[item["id"]] = translated[:300]
        else:
            result[item["id"]] = translate_headline(item["headline"])
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


# Clean Traditional Chinese dashboard renderer.
# The earlier renderer is kept above for compatibility, but these definitions
# intentionally override it so fallback text remains readable even without Gemini.
THEMES = {
    "ai": {
        "label": "AI 與半導體供應鏈",
        "keywords": ("ai", "nvidia", "amd", "semiconductor", "chip", "tsmc", "apple", "microsoft", "data center", "gpu", "server", "memory", "hbm", "人工智慧", "半導體", "晶片", "台積電", "輝達", "伺服器"),
    },
    "taiwan": {
        "label": "台股與亞洲市場",
        "keywords": ("taiwan", "taipei", "taiex", "asia", "japan", "nikkei", "korea", "台股", "台灣", "亞洲", "日股", "韓股", "新台幣", "加權指數"),
    },
    "rates": {
        "label": "利率、通膨與估值",
        "keywords": ("fed", "fomc", "yield", "rate", "treasury", "bond", "inflation", "cpi", "ppi", "利率", "通膨", "殖利率", "公債", "聯準會", "降息", "升息"),
    },
    "fx": {
        "label": "美元、匯率與資金流",
        "keywords": ("dollar", "currency", "forex", "yen", "yuan", "exchange rate", "dxy", "美元", "匯率", "日圓", "人民幣", "資金流", "外匯"),
    },
    "earnings": {
        "label": "財報、營收與企業展望",
        "keywords": ("earnings", "revenue", "profit", "guidance", "forecast", "sales", "margin", "財報", "營收", "獲利", "毛利率", "展望", "法說"),
    },
    "energy": {
        "label": "能源、原物料與航運",
        "keywords": ("oil", "opec", "energy", "crude", "gas", "gold", "copper", "shipping", "原油", "能源", "黃金", "銅", "航運", "原物料"),
    },
    "geopolitics": {
        "label": "地緣政治與政策風險",
        "keywords": ("iran", "israel", "war", "tariff", "sanction", "trade", "policy", "關稅", "制裁", "戰爭", "政策", "地緣", "貿易"),
    },
    "markets": {
        "label": "全球股市與風險情緒",
        "keywords": (),
    },
}


MARKET_TICKERS = [
    ("台股加權", "^TWII"),
    ("櫃買指數", "^TWOII"),
    ("Nasdaq", "^IXIC"),
    ("SOX", "^SOX"),
    ("S&P 500", "^GSPC"),
    ("VIX", "^VIX"),
    ("美債10Y", "^TNX"),
    ("美元指數", "DX-Y.NYB"),
    ("美元/台幣", "TWD=X"),
]


STOCK_UNIVERSE = [
    {"ticker": "2330", "name": "台積電", "themes": ("ai", "taiwan"), "catalyst": "AI/HPC 需求、先進製程與法說展望", "risk": "估值偏高、外資調節或先進製程指引下修"},
    {"ticker": "2317", "name": "鴻海", "themes": ("ai", "earnings"), "catalyst": "AI 伺服器出貨與集團電動車/雲端布局", "risk": "毛利率改善速度與大型客戶訂單節奏"},
    {"ticker": "2382", "name": "廣達", "themes": ("ai", "earnings"), "catalyst": "AI 伺服器與資料中心資本支出", "risk": "題材擁擠、出貨延後或毛利率不如預期"},
    {"ticker": "3231", "name": "緯創", "themes": ("ai", "earnings"), "catalyst": "AI 伺服器訂單與營收成長", "risk": "短線漲幅過大、營收未能連續驗證"},
    {"ticker": "6669", "name": "緯穎", "themes": ("ai", "earnings"), "catalyst": "雲端客戶拉貨與高階伺服器需求", "risk": "客戶集中與估值敏感度"},
    {"ticker": "2308", "name": "台達電", "themes": ("ai", "energy"), "catalyst": "電源、散熱、資料中心能源管理", "risk": "匯率、毛利率與資本支出循環"},
    {"ticker": "3661", "name": "世芯-KY", "themes": ("ai", "earnings"), "catalyst": "ASIC 與客製化 AI 晶片需求", "risk": "單一客戶與高本益比修正"},
    {"ticker": "2454", "name": "聯發科", "themes": ("ai", "earnings"), "catalyst": "手機復甦、邊緣 AI 與高階晶片", "risk": "中國手機需求與競爭壓力"},
    {"ticker": "6488", "name": "環球晶", "themes": ("taiwan", "earnings"), "catalyst": "半導體景氣復甦與矽晶圓報價", "risk": "庫存去化速度與資本支出保守"},
    {"ticker": "2881", "name": "富邦金", "themes": ("rates", "taiwan"), "catalyst": "利率環境、股債評價回升與金融股防禦性", "risk": "殖利率急升、匯損或信用風險"},
]


def fetch_yahoo_quote(symbol: str) -> dict[str, object]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "daily-finance-report"})
        with urllib.request.urlopen(request, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8"))
        result = data["chart"]["result"][0]
        meta = result.get("meta", {})
        closes = [value for value in result.get("indicators", {}).get("quote", [{}])[0].get("close", []) if value is not None]
        current = float(meta.get("regularMarketPrice") or (closes[-1] if closes else 0))
        previous = float(meta.get("chartPreviousClose") or (closes[-2] if len(closes) >= 2 else current))
        change_pct = ((current - previous) / previous * 100) if previous else 0.0
        return {"value": current, "change_pct": change_pct, "ok": True}
    except Exception as exc:
        print(f"Market quote fallback for {symbol}: {exc}")
        return {"value": None, "change_pct": None, "ok": False}


def market_snapshot() -> list[dict[str, object]]:
    rows = []
    for label, symbol in MARKET_TICKERS:
        quote = fetch_yahoo_quote(symbol)
        rows.append({"label": label, "symbol": symbol, **quote})
    return rows


def market_temperature(snapshot: list[dict[str, object]]) -> dict[str, str]:
    score = 0
    by_label = {row["label"]: row for row in snapshot}
    for label in ("台股加權", "櫃買指數", "Nasdaq", "SOX", "S&P 500"):
        change = by_label.get(label, {}).get("change_pct")
        if isinstance(change, (int, float)):
            score += 1 if change > 0 else -1 if change < -0.8 else 0
    vix = by_label.get("VIX", {}).get("value")
    if isinstance(vix, (int, float)):
        score += 1 if vix < 18 else -2 if vix > 25 else 0
    dxy = by_label.get("美元指數", {}).get("change_pct")
    if isinstance(dxy, (int, float)) and dxy > 0.4:
        score -= 1
    if score >= 3:
        return {"label": "偏多", "tone": "risk-on", "note": "股市與波動指標偏向風險承擔，適合追蹤主流成長族群的延續性。"}
    if score <= -2:
        return {"label": "偏空", "tone": "risk-off", "note": "風險指標轉弱，應降低追價並提高對匯率、利率與量能的要求。"}
    return {"label": "中性", "tone": "neutral", "note": "市場訊號分歧，適合用法人籌碼與營收驗證篩選可操作標的。"}


def format_market_value(row: dict[str, object]) -> str:
    value = row.get("value")
    if not isinstance(value, (int, float)):
        return "資料暫缺"
    if row.get("label") == "美元/台幣":
        return f"{value:.3f}"
    if row.get("label") == "美債10Y":
        return f"{value / 10:.2f}%"
    return f"{value:,.2f}"


def format_change(row: dict[str, object]) -> str:
    change = row.get("change_pct")
    if not isinstance(change, (int, float)):
        return "待更新"
    sign = "+" if change >= 0 else ""
    return f"{sign}{change:.2f}%"


def build_market_analysis(news: list[dict[str, str]], today: dt.date, previous: dict) -> dict:
    counts = theme_counts(news)
    selected_keys = ranked_theme_keys(news, today, previous)
    axes = []
    for key in selected_keys:
        count = counts[key]
        examples = top_items_for_theme(news, key, limit=2)
        sample = "；".join(item["headline"] for item in examples) or "今日新聞權重較低，先以市場數據確認方向"
        axes.append(
            {
                "theme_key": key,
                "title": THEMES[key]["label"],
                "summary": f"{THEMES[key]['label']}出現 {count} 則相關訊號，重點新聞包含：{sample}",
                "impact": theme_impact_text(key),
                "watch": theme_watch_text(key),
                "count": count,
            }
        )
    market_state = "今日以新聞主題、主要市場數據與候選股因子交叉判讀，先確認風險燈號，再追蹤可驗證的營收、法人與技術條件。"
    change = "與前一版相比，本版加入市場數據、候選股分層、投資影響卡、事件行事曆與信號品質，降低只讀新聞標題的盲點。"
    return {"market_state": market_state, "change_from_previous": change, "axes": axes}


def theme_impact_text(theme_key: str) -> str:
    return {
        "ai": "直接影響 AI 伺服器、晶圓代工、散熱、電源與高速傳輸供應鏈，需用月營收與法人籌碼驗證。",
        "taiwan": "影響台股風險偏好、外資匯入匯出與電子權值股估值，需觀察台幣與成交值。",
        "rates": "利率變化會改變高估值成長股折現率，也影響金融、債券與美元方向。",
        "fx": "美元與台幣走勢會影響外資動能、出口電子匯兌與原物料成本。",
        "earnings": "企業財報與指引是題材能否轉成獲利的關鍵，優先看營收趨勢與毛利率。",
        "energy": "能源與原物料會影響通膨、運輸與部分製造成本，留意成本轉嫁能力。",
        "geopolitics": "政策與地緣風險容易造成短線避險與供應鏈重估，追價需更嚴格。",
        "markets": "全球市場風險情緒會決定資金是否願意承接成長股與高本益比族群。",
    }.get(theme_key, "需觀察新聞是否轉化為可驗證的價格、籌碼與基本面訊號。")


def theme_watch_text(theme_key: str) -> str:
    return {
        "ai": "觀察台積電、AI 伺服器、散熱、電源與 PCB 族群是否量價同步，並確認月營收是否加速。",
        "taiwan": "觀察加權指數、櫃買、成交值、外資現貨與台指期淨部位是否同向。",
        "rates": "觀察美債 10Y、FedWatch、美元指數與高估值科技股的同向或背離。",
        "fx": "觀察美元/台幣、DXY、外資買賣超與電子權值股是否同步轉強或轉弱。",
        "earnings": "觀察營收 YoY/MoM、法說指引、毛利率與 EPS 預估是否上修。",
        "energy": "觀察油價、航運報價、原物料價格與成本敏感產業的報價能力。",
        "geopolitics": "觀察避險資產、油價、美元與供應鏈受影響族群是否出現異常波動。",
        "markets": "觀察 VIX、Nasdaq、SOX、S&P 500 與台股電子成交比重。",
    }.get(theme_key, "觀察價格、成交量、法人籌碼與基本面是否同時驗證。")


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def candidate_institutional_signal(ticker: str, today: dt.date) -> dict[str, object]:
    try:
        rows = telegram.latest_institutional_rows(today, ticker, 5)
    except Exception as exc:
        print(f"Candidate institutional fallback for {ticker}: {exc}")
        return {
            "score": 12,
            "text": "法人資料暫缺",
            "warning": "籌碼未驗證，避免只因題材追價",
            "latest_total": None,
        }
    latest_date, latest = rows[-1]
    foreign = telegram.lots(latest[4])
    investment = telegram.lots(latest[10])
    dealer = telegram.lots(latest[11])
    total = telegram.lots(latest[18])
    five_day_total = sum(telegram.lots(row[18]) for _, row in rows)
    positive_days = sum(1 for _, row in rows if telegram.lots(row[18]) > 0)
    score = 15
    score += 5 if total > 0 else -6 if total < 0 else 0
    score += 5 if foreign > 0 else -8 if foreign <= -10000 else -4 if foreign < 0 else 0
    score += 5 if five_day_total > 0 else -8 if five_day_total <= -10000 else -4 if five_day_total < 0 else 0
    score += (positive_days - 2) * 2
    warning = "法人偏買，題材較有延續條件"
    if foreign <= -10000 or total <= -10000 or five_day_total <= -20000:
        warning = "外資/法人明顯賣超，降分並避免追價"
    elif total < 0 or five_day_total < 0:
        warning = "法人偏賣，需等籌碼止穩"
    return {
        "score": clamp(score, 0, 30),
        "text": (
            f"{latest_date:%m/%d} 外資{foreign:+,}張、投信{investment:+,}張、"
            f"自營商{dealer:+,}張；三大法人{total:+,}張，近5日合計{five_day_total:+,}張"
        ),
        "warning": warning,
        "latest_total": total,
    }


def candidate_technical_signal(ticker: str) -> dict[str, object]:
    quote = fetch_yahoo_quote(f"{ticker}.TW")
    if not quote.get("ok"):
        quote = fetch_yahoo_quote(f"{ticker}.TWO")
    change = quote.get("change_pct")
    if not isinstance(change, (int, float)):
        return {"score": 8, "text": "技術資料暫缺"}
    score = 10 + (4 if change > 1 else 2 if change > 0 else -4 if change < -1 else -1)
    return {"score": clamp(score, 0, 20), "text": f"近一日股價變化 {change:+.2f}%"}


def score_candidates(news: list[dict[str, str]], analysis: dict, today: dt.date) -> list[dict[str, object]]:
    theme_strength = Counter(item["theme"] for item in news)
    source_count = len({item["source"] for item in news}) or 1
    rows = []
    all_text = " ".join(f"{item['headline']} {item['original_headline']}" for item in news).lower()
    for stock in STOCK_UNIVERSE:
        theme_score = min(30, sum(theme_strength[theme] for theme in stock["themes"]) * 5)
        mention_bonus = 10 if stock["name"].lower() in all_text or stock["ticker"] in all_text else 0
        theme_component = min(35, theme_score + mention_bonus)
        institutional = candidate_institutional_signal(str(stock["ticker"]), today)
        technical = candidate_technical_signal(str(stock["ticker"]))
        quality = min(10, source_count)
        data_score = 10 if institutional["latest_total"] is not None else 4
        score = clamp(
            int(theme_component) + int(institutional["score"]) + int(technical["score"]) + quality + data_score,
            0,
            95,
        )
        if score >= 82:
            bucket = "Dashboard 強勢觀察"
            action = "追蹤延續"
        elif score >= 74:
            bucket = "題材轉強觀察"
            action = "等突破或營收確認"
        elif score >= 66:
            bucket = "中性追蹤"
            action = "持續追蹤"
        else:
            bucket = "低分觀察"
            action = "先不追價"
        if "明顯賣超" in str(institutional["warning"]):
            bucket = "籌碼降級觀察"
            action = "等外資止賣"
        elif "法人偏賣" in str(institutional["warning"]) and score < 74:
            action = "等籌碼止穩"
        rows.append(
            {
                **stock,
                "score": score,
                "bucket": bucket,
                "action": action,
                "theme_score": theme_component,
                "flow_score": institutional["score"],
                "technical_score": technical["score"],
                "flow_text": institutional["text"],
                "flow_warning": institutional["warning"],
                "technical_text": technical["text"],
            }
        )
    return sorted(rows, key=lambda item: (-int(item["score"]), item["ticker"]))


def impact_card(item: dict[str, str]) -> dict[str, str]:
    theme = item["theme"]
    mapping = {
        "ai": ("GPU、伺服器、晶圓代工、散熱、電源", "台積電、廣達、緯創、緯穎、台達電", "月營收、法人買超、突破前高"),
        "taiwan": ("台股權值股、櫃買成長股、金融", "台積電、鴻海、富邦金、主要 ETF", "成交值、外資、台幣、均線"),
        "rates": ("高估值成長股、金融、債券", "AI 高本益比股、金融股", "10Y 殖利率、DXY、Fed 利率預期"),
        "fx": ("出口電子、金融、原物料", "大型電子權值、壽險金控", "美元/台幣、外資買賣超"),
        "earnings": ("公布財報與指引公司", "月營收加速或法說上修個股", "營收 YoY/MoM、毛利率、EPS 預估"),
        "energy": ("能源、航運、原物料成本敏感族群", "航運、塑化、原物料與用電大戶", "油價、運價、成本轉嫁"),
        "geopolitics": ("供應鏈、能源、避險資產", "半導體供應鏈、能源與防禦型資產", "油價、美元、VIX"),
        "markets": ("整體風險資產", "權值股、ETF、指數期貨", "VIX、SOX、Nasdaq、成交值"),
    }
    sector, taiwan_names, verify = mapping.get(theme, mapping["markets"])
    return {
        "event": item["headline"],
        "source": item["source"],
        "link": item["link"],
        "impact": "高" if theme in {"ai", "rates", "taiwan"} else "中",
        "horizon": "1-3 個月" if theme in {"ai", "earnings"} else "盤中至 1-2 週",
        "sector": sector,
        "taiwan": taiwan_names,
        "verify": verify,
        "action": "觀察" if theme in {"geopolitics", "energy"} else "等待驗證後操作",
    }


def event_calendar(today: dt.date) -> list[dict[str, str]]:
    month_start = today.replace(day=1)
    return [
        {"window": "未來 7 天", "event": "台股月營收公告高峰", "affected": "AI 伺服器、半導體、電子零組件", "watch": "YoY/MoM 是否連續加速，弱於同業者降權"},
        {"window": "未來 7 天", "event": "台指期/選擇權籌碼變化", "affected": "權值股、ETF、期貨避險部位", "watch": "外資期現貨是否同向，逆價差是否擴大"},
        {"window": "未來 30 天", "event": "美國 CPI/PPI/非農與 FOMC 訊號", "affected": "高估值科技股、金融、美元資產", "watch": "10Y 殖利率與 DXY 是否同時上行"},
        {"window": f"{month_start:%m/%d} 起", "event": "企業法說與財報指引", "affected": "成長股與高本益比族群", "watch": "指引上修、毛利率改善與訂單能見度"},
    ]


def render_market_snapshot(snapshot: list[dict[str, object]]) -> str:
    return "\n".join(
        f"""
        <div class="quote-tile {'up' if isinstance(row.get('change_pct'), (int, float)) and row['change_pct'] >= 0 else 'down'}">
          <span>{html.escape(str(row['label']))}</span>
          <strong>{html.escape(format_market_value(row))}</strong>
          <em>{html.escape(format_change(row))}</em>
        </div>
        """
        for row in snapshot
    )


def render_candidate_table(candidates: list[dict[str, object]]) -> str:
    return "\n".join(
        f"""
        <tr>
          <td><b>{html.escape(str(item['bucket']))}</b></td>
          <td>{html.escape(str(item['ticker']))} {html.escape(str(item['name']))}</td>
          <td><strong>{item['score']}</strong></td>
          <td>{html.escape(str(item['theme_score']))}</td>
          <td>{html.escape(str(item['flow_score']))}</td>
          <td>{html.escape(str(item['technical_score']))}</td>
          <td>{html.escape(str(item['flow_text']))}<br><b>{html.escape(str(item['flow_warning']))}</b></td>
          <td>{html.escape(str(item['catalyst']))}</td>
          <td>{html.escape(str(item['risk']))}</td>
          <td>{html.escape(str(item['action']))}</td>
        </tr>
        """
        for item in candidates
    )


def render_impact_cards(news: list[dict[str, str]]) -> str:
    cards = [impact_card(item) for item in news[:8]]
    return "\n".join(
        f"""
        <article class="impact-card">
          <div class="impact-meta">
            <span>{html.escape(card['impact'])}影響</span>
            <span>{html.escape(card['horizon'])}</span>
            <span>{html.escape(card['action'])}</span>
          </div>
          <h3><a href="{html.escape(card['link'])}">{html.escape(card['event'])}</a></h3>
          <dl>
            <dt>直接受惠/受影響</dt><dd>{html.escape(card['sector'])}</dd>
            <dt>台股對應</dt><dd>{html.escape(card['taiwan'])}</dd>
            <dt>驗證指標</dt><dd>{html.escape(card['verify'])}</dd>
          </dl>
        </article>
        """
        for card in cards
    )


def render_event_calendar(events: list[dict[str, str]]) -> str:
    return "\n".join(
        f"""
        <article class="event-row">
          <span>{html.escape(event['window'])}</span>
          <h3>{html.escape(event['event'])}</h3>
          <p><b>敏感族群：</b>{html.escape(event['affected'])}</p>
          <p><b>觀察條件：</b>{html.escape(event['watch'])}</p>
        </article>
        """
        for event in events
    )


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
              <div class="axis-rank">主軸 {index}</div>
              <div class="theme-topline">
                <h3>{html.escape(axis["title"])}</h3>
                <strong>{axis["count"]}</strong>
              </div>
              <p>{html.escape(axis["summary"])}</p>
              <dl>
                <dt>投資影響</dt><dd>{html.escape(axis["impact"])}</dd>
                <dt>24 小時觀察</dt><dd>{html.escape(axis["watch"])}</dd>
              </dl>
              <ul>{headlines}</ul>
            </article>
            """
        )
    return "\n".join(cards)


def render_news_list(news: list[dict[str, str]]) -> str:
    if not news:
        return '<div class="empty">今日新聞數量不足，系統保留不完整報告以避免誤導。</div>'
    rows = []
    for index, item in enumerate(news, 1):
        label = THEMES.get(item["theme"], {"label": "市場新聞"})["label"]
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
                <p>投資解讀：先判斷是否影響營收、毛利率、估值或資金流，再用法人籌碼、成交量與關鍵價位確認是否已反映。</p>
              </div>
            </article>
            """
        )
    return "\n".join(rows)


def render_source_table(news: list[dict[str, str]]) -> str:
    counts = Counter(item["source"] for item in news)
    return "\n".join(
        f'<span class="source-pill">{html.escape(source)} <b>{count}</b></span>'
        for source, count in counts.most_common()
    )


def render_html(news: list[dict[str, str]], today: dt.date, previous_html: str = "") -> str:
    generated_at = dt.datetime.now(TW).strftime("%Y-%m-%d %H:%M:%S Asia/Taipei")
    previous = extract_previous_analysis(previous_html)
    analysis = build_market_analysis(news, today, previous)
    snapshot = market_snapshot()
    temperature = market_temperature(snapshot)
    candidates = score_candidates(news, analysis, today)
    events = event_calendar(today)
    source_count = len({item["source"] for item in news})
    theme_count = len({item["theme"] for item in news})
    signal_score = min(100, 45 + len(news) * 2 + source_count * 3 + theme_count * 4)
    dominant_label = analysis["axes"][0]["title"]
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="report-date" content="{today:%Y-%m-%d}">
  <meta name="report-news-count" content="{len(news)}">
  <meta name="report-generator" content="github-actions">
  <title>JT 投資儀表板 - {today:%Y-%m-%d}</title>
  <style>
    :root {{
      --ink: #17212b; --muted: #637383; --line: #d8e0e6; --bg: #f5f7f8; --panel: #fff;
      --navy: #10263d; --blue: #235fb7; --teal: #087f8c; --amber: #a76b12; --red: #b94040; --green: #117a4b;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--ink); font-family: "Segoe UI", "Noto Sans TC", Arial, sans-serif; line-height: 1.62; }}
    a {{ color: var(--blue); text-decoration: none; overflow-wrap: anywhere; }}
    a:hover {{ text-decoration: underline; }}
    header {{ background: linear-gradient(120deg, rgba(10,31,52,.92), rgba(4,84,96,.72)), url("assets/finance-newsroom-hero.png") center/cover; color: #fff; }}
    .hero, main {{ width: min(1240px, 92vw); margin: 0 auto; }}
    .hero {{ min-height: 330px; display: grid; align-content: end; padding: 44px 0; }}
    .kicker {{ font-size: 14px; color: #dbe7f0; }}
    h1 {{ max-width: 900px; margin: 12px 0 0; font-size: clamp(30px, 4vw, 52px); line-height: 1.1; letter-spacing: 0; }}
    .hero p {{ max-width: 860px; margin: 16px 0 0; color: #edf5f8; font-size: 18px; }}
    .meta {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 22px; }}
    .pill {{ border: 1px solid rgba(255,255,255,.28); background: rgba(255,255,255,.13); border-radius: 999px; padding: 6px 12px; font-size: 14px; }}
    main {{ padding: 28px 0 54px; }}
    .metrics, .quotes {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 18px; }}
    .quotes {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    .metric, .panel, .theme-card, .news-row, .quote-tile, .impact-card, .event-row {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }}
    .metric, .quote-tile {{ padding: 16px; }}
    .metric span, .quote-tile span {{ display: block; color: var(--muted); font-size: 13px; }}
    .metric strong, .quote-tile strong {{ display: block; margin-top: 6px; font-size: 24px; }}
    .quote-tile em {{ font-style: normal; color: var(--muted); }}
    .quote-tile.up em {{ color: var(--green); }} .quote-tile.down em {{ color: var(--red); }}
    .section-title {{ display: flex; align-items: end; justify-content: space-between; gap: 16px; margin: 28px 0 12px; }}
    .section-title h2 {{ margin: 0; font-size: 22px; }}
    .section-title p {{ margin: 0; color: var(--muted); font-size: 14px; }}
    .themes, .impact-grid, .events {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }}
    .theme-card, .impact-card, .event-row, .panel {{ padding: 18px; }}
    .axis-rank {{ color: var(--teal); font-size: 13px; font-weight: 800; }}
    .theme-topline {{ display: flex; align-items: start; justify-content: space-between; gap: 12px; color: var(--teal); }}
    .theme-topline h3 {{ margin: 6px 0 0; color: var(--ink); font-size: 19px; }}
    .theme-topline strong {{ color: var(--ink); font-size: 24px; }}
    dl {{ margin: 12px 0; }} dt {{ color: var(--muted); font-size: 12px; font-weight: 800; }} dd {{ margin: 2px 0 10px; color: #334454; }}
    table {{ width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }}
    th, td {{ padding: 11px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ background: #eef3f5; color: #34485a; font-size: 13px; }}
    tr:last-child td {{ border-bottom: 0; }}
    .news-list {{ display: grid; gap: 12px; }}
    .news-row {{ display: grid; grid-template-columns: 48px minmax(0, 1fr); gap: 14px; padding: 16px; }}
    .rank {{ width: 38px; height: 38px; border-radius: 999px; display: grid; place-items: center; background: #e8f0f2; color: var(--navy); font-weight: 800; }}
    .news-meta, .impact-meta {{ display: flex; flex-wrap: wrap; gap: 8px; color: var(--muted); font-size: 13px; }}
    .impact-meta span {{ background: #eef3f5; border-radius: 999px; padding: 4px 9px; }}
    h3 {{ margin: 6px 0 6px; font-size: 18px; line-height: 1.38; }}
    .sources {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    .source-pill {{ background: #eef3f5; border: 1px solid var(--line); border-radius: 999px; padding: 7px 11px; color: #34485a; }}
    .footer {{ color: var(--muted); font-size: 13px; margin-top: 20px; border-top: 1px solid var(--line); padding-top: 14px; }}
    @media (max-width: 920px) {{ .metrics, .quotes, .themes, .impact-grid, .events {{ grid-template-columns: 1fr; }} .news-row {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <header>
    <div class="hero">
      <div class="kicker">JT Investment Dashboard | {generated_at}</div>
      <h1>{today:%Y-%m-%d} JT 投資儀表板</h1>
      <p>{html.escape(temperature["note"])} 今日主軸：{html.escape(dominant_label)}。</p>
      <div class="meta">
        <span class="pill">市場燈號：{html.escape(temperature["label"])}</span>
        <span class="pill">新聞：{len(news)} 則</span>
        <span class="pill">來源：{source_count} 個</span>
        <span class="pill">個人持股區：未啟用</span>
      </div>
    </div>
  </header>
  <main>
    <section class="metrics">
      <div class="metric"><span>市場風險燈號</span><strong>{html.escape(temperature["label"])}</strong></div>
      <div class="metric"><span>信號品質分數</span><strong>{signal_score}</strong></div>
      <div class="metric"><span>今日主軸</span><strong>{html.escape(dominant_label)}</strong></div>
      <div class="metric"><span>主題分散度</span><strong>{theme_count}</strong></div>
    </section>

    <div class="section-title"><h2>市場即時儀表板</h2><p>以公開行情源抓取，失敗時保留待更新，不中斷報告。</p></div>
    <section class="quotes">{render_market_snapshot(snapshot)}</section>

    <div class="section-title"><h2>候選股分層清單</h2><p>依新聞主題、來源分散度與題材對應做初步排序，尚未納入個人持股。</p></div>
    <table>
      <thead><tr><th>分層</th><th>股票</th><th>總分</th><th>題材</th><th>籌碼</th><th>技術</th><th>今日籌碼</th><th>催化劑</th><th>風險</th><th>操作狀態</th></tr></thead>
      <tbody>{render_candidate_table(candidates)}</tbody>
    </table>

    <div class="section-title"><h2>今日產業主軸</h2><p>把新聞轉成可驗證的市場假設。</p></div>
    <section class="themes">{render_theme_cards(news, analysis)}</section>

    <div class="section-title"><h2>投資影響卡</h2><p>每則重點新聞對應受惠族群、台股標的與驗證指標。</p></div>
    <section class="impact-grid">{render_impact_cards(news)}</section>

    <div class="section-title"><h2>未來事件行事曆</h2><p>先追蹤 7/30 天內會影響成長股與法人資金的事件。</p></div>
    <section class="events">{render_event_calendar(events)}</section>

    <div class="section-title"><h2>重要新聞清單</h2><p>保留來源連結，避免只留下摘要文字。</p></div>
    <section class="news-list">{render_news_list(news)}</section>

    <div class="section-title"><h2>資料來源分布</h2><p>來源分散度會影響信號品質分數。</p></div>
    <section class="panel sources">{render_source_table(news)}</section>

    {analysis_data_html(analysis)}
    <p class="footer">Updated: {generated_at}. This page is generated independently by GitHub Actions. Personal holdings and private risk data are intentionally not included.</p>
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
