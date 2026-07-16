from __future__ import annotations

import datetime as dt
import csv
import html
import io
import json
import os
import re
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import daily_telegram_push as telegram


TW = dt.timezone(dt.timedelta(hours=8))
DEFAULT_OUTPUT_DIR = Path(os.environ.get("DAILY_FINANCE_REPORT_DIR", "daily-finance-report-site"))
MAX_NEWS_ITEMS = int(os.environ.get("DAILY_FINANCE_REPORT_NEWS_LIMIT", "18"))
MIN_NEWS_ITEMS = int(os.environ.get("DAILY_FINANCE_REPORT_MIN_NEWS", "10"))
LAST_PROCESSED_PAYLOADS: dict[str, dict[str, object]] = {}


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


def render_html(
    news: list[dict[str, str]],
    today: dt.date,
    previous_html: str = "",
    previous_market: dict[str, object] | None = None,
    previous_history: dict[str, object] | None = None,
    previous_macro: dict[str, object] | None = None,
) -> str:
    global LAST_PROCESSED_PAYLOADS
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
    ("S&P 500", "^GSPC", "global"),
    ("Nasdaq", "^IXIC", "global"),
    ("SOX 費半", "^SOX", "global"),
    ("道瓊", "^DJI", "global"),
    ("日經225", "^N225", "global"),
    ("台股加權", "^TWII", "global"),
    ("櫃買指數", "^TWOII", "global"),
    ("上海綜合", "000001.SS", "global"),
    ("恆生國企", "^HSCE", "global"),
    ("印度Nifty", "^NSEI", "global"),
    ("巴西Bovespa", "^BVSP", "global"),
    ("美債10Y", "^TNX", "cross"),
    ("美元指數", "DX-Y.NYB", "cross"),
    ("美元/台幣", "TWD=X", "cross"),
    ("美元/日圓", "JPY=X", "cross"),
    ("美元/人民幣", "CNY=X", "cross"),
    ("VIX", "^VIX", "cross"),
    ("黃金", "GC=F", "cross"),
    ("WTI 原油", "CL=F", "cross"),
    ("布蘭特原油", "BZ=F", "cross"),
    ("銅價", "HG=F", "cross"),
]


MAJOR_EVENT_TEMPLATES = {
    (7, 15): [
        ("20:30", "美國", "PPI / 核心 PPI", "高", "通膨、Fed 預期、10Y、美股科技估值"),
        ("21:15", "美國", "工業生產", "中", "景氣循環、半導體與工業需求"),
    ],
    (7, 16): [
        ("20:30", "美國", "零售銷售", "高", "美國需求、美元、Nasdaq 與台股電子"),
        ("20:30", "美國", "初領失業救濟金", "中", "就業降溫與利率預期"),
    ],
    (7, 17): [
        ("20:30", "美國", "新屋開工 / 建築許可", "中", "利率敏感資產與景氣需求"),
    ],
}


CORE_STOCK_UNIVERSE = [
    {"ticker": "2330", "name": "台積電", "themes": ("ai", "taiwan"), "catalyst": "AI/HPC 需求、先進製程與法說展望", "risk": "估值偏高、外資調節或先進製程指引下修"},
    {"ticker": "2317", "name": "鴻海", "themes": ("ai", "earnings"), "catalyst": "AI 伺服器出貨與集團電動車/雲端布局", "risk": "毛利率改善速度與大型客戶訂單節奏"},
    {"ticker": "2308", "name": "台達電", "themes": ("ai", "energy"), "catalyst": "電源、散熱、資料中心能源管理", "risk": "匯率、毛利率與資本支出循環"},
    {"ticker": "2881", "name": "富邦金", "themes": ("rates", "taiwan"), "catalyst": "利率環境、股債評價回升與金融股防禦性", "risk": "殖利率急升、匯損或信用風險"},
]

STOCK_UNIVERSE = CORE_STOCK_UNIVERSE


PRIORITY_CANDIDATE_MIN_SCORE = 82
PRIORITY_CANDIDATE_MIN_FLOW_SCORE = 14
PRIORITY_CANDIDATE_LIMIT = 6


def fetch_yahoo_quote(symbol: str) -> dict[str, object]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1y&interval=1d"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "daily-finance-report"})
        with urllib.request.urlopen(request, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8"))
        result = data["chart"]["result"][0]
        meta = result.get("meta", {})
        timestamps = result.get("timestamp", [])
        raw_closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        points = [(int(ts), float(close)) for ts, close in zip(timestamps, raw_closes) if close is not None]
        closes = [close for _, close in points]
        current = float(meta.get("regularMarketPrice") or (closes[-1] if closes else 0))
        previous = float(closes[-2] if len(closes) >= 2 else current)
        year_start = first_close_of_year(points, dt.datetime.now(TW).year) or (closes[0] if closes else current)
        high_52w = max(closes) if closes else current
        change_pct = ((current - previous) / previous * 100) if previous else 0.0
        change_5d = pct_change(current, closes[-6] if len(closes) >= 6 else None)
        change_20d = pct_change(current, closes[-21] if len(closes) >= 21 else None)
        ytd_change = pct_change(current, year_start)
        high_gap = ((current - high_52w) / high_52w * 100) if high_52w else None
        market_time = meta.get("regularMarketTime")
        data_time = (
            dt.datetime.fromtimestamp(int(market_time), TW).strftime("%Y-%m-%d %H:%M")
            if isinstance(market_time, (int, float))
            else "收盤/延遲"
        )
        if isinstance(market_time, (int, float)):
            age_days = (dt.datetime.now(TW) - dt.datetime.fromtimestamp(int(market_time), TW)).days
            if age_days > 10:
                return {
                    "value": None,
                    "change_pct": None,
                    "change_5d": None,
                    "change_20d": None,
                    "ytd_change": None,
                    "high_52w_gap": None,
                    "data_time": f"資料更新失敗：最後資料 {data_time}",
                    "ok": False,
                }
        return {
            "value": current,
            "change_pct": change_pct,
            "change_5d": change_5d,
            "change_20d": change_20d,
            "ytd_change": ytd_change,
            "high_52w_gap": high_gap,
            "data_time": data_time,
            "ok": True,
        }
    except Exception as exc:
        print(f"Market quote fallback for {symbol}: {exc}")
        return {"value": None, "change_pct": None, "change_5d": None, "change_20d": None, "ytd_change": None, "high_52w_gap": None, "data_time": "資料更新失敗", "ok": False}


def parse_number(value: object) -> float | None:
    text = clean_text(value).replace(",", "").replace("--", "")
    text = re.sub(r"<[^>]+>", "", text)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value: object) -> int | None:
    number = parse_number(value)
    return int(number) if number is not None else None


def format_ntd_billion(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "待更新"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value / 100_000_000:.1f} 億"


def fetch_twse_json(path: str, params: dict[str, str], timeout: int = 12) -> dict[str, object]:
    url = f"https://www.twse.com.tw/rwd/zh/{path}?" + urllib.parse.urlencode({**params, "response": "json"})
    request = urllib.request.Request(url, headers={"User-Agent": "daily-finance-report"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8-sig"))


def decode_twse_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp950", "big5"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def fetch_twse_csv(path: str, params: dict[str, str], timeout: int = 15) -> str:
    url = f"https://www.twse.com.tw/rwd/zh/{path}?" + urllib.parse.urlencode({**params, "response": "csv"})
    request = urllib.request.Request(url, headers={"User-Agent": "daily-finance-report"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return decode_twse_text(response.read())


def fetch_text_url(url: str, timeout: int = 15, user_agent: str = "daily-finance-report") -> str:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return decode_twse_text(raw)


def fetch_json_url(url: str, timeout: int = 15) -> dict[str, object]:
    request = urllib.request.Request(url, headers={"User-Agent": "daily-finance-report"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8-sig"))


def clean_ticker(value: object) -> str:
    text = clean_text(value).strip().strip('="')
    return re.sub(r"[^0-9A-Z]", "", text)


def latest_twse_t86_rows(today: dt.date) -> dict[str, object]:
    for offset in range(0, 10):
        day = today - dt.timedelta(days=offset)
        try:
            text = fetch_twse_csv(
                "fund/T86",
                {"date": day.strftime("%Y%m%d"), "selectType": "ALLBUT0999"},
            )
        except Exception as exc:
            print(f"TWSE T86 fallback for {day:%Y-%m-%d}: {exc}")
            continue
        records: list[dict[str, object]] = []
        for row in csv.reader(text.splitlines()):
            clean = [cell.strip().strip('="') for cell in row]
            if len(clean) <= 18:
                continue
            ticker = clean_ticker(clean[0])
            if not re.fullmatch(r"\d{4}", ticker) or ticker.startswith("0"):
                continue
            name = clean_text(clean[1])
            foreign = telegram.lots(clean[4])
            investment = telegram.lots(clean[10])
            dealer = telegram.lots(clean[11])
            total = telegram.lots(clean[18])
            records.append(
                {
                    "ticker": ticker,
                    "name": name,
                    "foreign_lots": foreign,
                    "investment_lots": investment,
                    "dealer_lots": dealer,
                    "total_lots": total,
                    "date": day.isoformat(),
                    "source": "TWSE T86",
                }
            )
        if records:
            return {"status": "ok", "date": day.isoformat(), "source": "TWSE T86", "records": records}
    return {"status": "failed", "date": None, "source": "TWSE T86", "records": []}


def latest_twse_price_momentum_rows(today: dt.date) -> dict[str, object]:
    for offset in range(0, 10):
        day = today - dt.timedelta(days=offset)
        try:
            data = fetch_twse_json(
                "afterTrading/MI_INDEX",
                {"date": day.strftime("%Y%m%d"), "type": "ALLBUT0999"},
                timeout=15,
            )
        except Exception as exc:
            print(f"TWSE MI_INDEX stock fallback for {day:%Y-%m-%d}: {exc}")
            continue
        if data.get("stat") != "OK" or not data.get("tables"):
            continue
        records: list[dict[str, object]] = []
        for table in data.get("tables", []):
            fields = table.get("fields") or []
            if "證券代號" not in fields or "成交金額" not in fields or "收盤價" not in fields:
                continue
            index = {str(field): pos for pos, field in enumerate(fields)}
            for raw_row in table.get("data", []):
                row = raw_row.get("value") if isinstance(raw_row, dict) else raw_row
                if not isinstance(row, list):
                    continue
                ticker = clean_ticker(row[index["證券代號"]] if index["證券代號"] < len(row) else "")
                if not re.fullmatch(r"\d{4}", ticker) or ticker.startswith("0"):
                    continue
                name = clean_text(row[index["證券名稱"]] if index.get("證券名稱", 999) < len(row) else ticker)
                close = parse_number(row[index["收盤價"]] if index["收盤價"] < len(row) else None)
                change_abs = parse_number(row[index.get("漲跌價差", -1)] if index.get("漲跌價差", 999) < len(row) else None)
                sign_text = clean_text(row[index.get("漲跌(+/-)", -1)] if index.get("漲跌(+/-)", 999) < len(row) else "")
                if change_abs is None:
                    change_abs = 0.0
                sign = -1 if "green" in sign_text or "-" in sign_text else 1
                signed_change = sign * change_abs
                previous_close = close - signed_change if isinstance(close, (int, float)) else None
                change_pct = (signed_change / previous_close * 100) if previous_close and previous_close > 0 else 0.0
                turnover = parse_number(row[index["成交金額"]] if index["成交金額"] < len(row) else None) or 0.0
                shares = parse_number(row[index.get("成交股數", -1)] if index.get("成交股數", 999) < len(row) else None) or 0.0
                records.append(
                    {
                        "ticker": ticker,
                        "name": name,
                        "close": close,
                        "change_pct": round(change_pct, 2),
                        "turnover": turnover,
                        "volume_lots": int(shares / 1000),
                        "date": day.isoformat(),
                        "source": "TWSE MI_INDEX",
                    }
                )
        if records:
            return {"status": "ok", "date": day.isoformat(), "source": "TWSE MI_INDEX", "records": records}
    return {"status": "failed", "date": None, "source": "TWSE MI_INDEX", "records": []}


def dynamic_candidate_pool(today: dt.date, core_tickers: set[str], limit: int = 10) -> tuple[list[dict[str, object]], dict[str, object]]:
    dataset = latest_twse_t86_rows(today)
    records = dataset.get("records") if isinstance(dataset, dict) else []
    if not isinstance(records, list):
        records = []
    selected: list[dict[str, object]] = []
    for row in records:
        ticker = str(row.get("ticker", ""))
        if ticker in core_tickers:
            continue
        foreign = int(row.get("foreign_lots") or 0)
        investment = int(row.get("investment_lots") or 0)
        total = int(row.get("total_lots") or 0)
        if total < 2500 and foreign < 2000 and investment < 800:
            continue
        if total <= 0:
            continue
        momentum = total + max(foreign, 0) + max(investment, 0) * 2
        selected.append(
            {
                "ticker": ticker,
                "name": row.get("name") or ticker,
                "themes": ("taiwan", "markets"),
                "catalyst": f"動態法人雷達：三大法人{total:+,}張、外資{foreign:+,}張、投信{investment:+,}張",
                "risk": "動態候選須再確認基本面、成交量與是否短線過熱",
                "candidate_source": "dynamic",
                "institutional_snapshot": row,
                "dynamic_momentum": momentum,
            }
        )
    selected.sort(key=lambda item: int(item.get("dynamic_momentum", 0)), reverse=True)
    top_selected = selected[:limit]
    if not top_selected:
        dataset = latest_twse_price_momentum_rows(today)
        price_records = dataset.get("records") if isinstance(dataset, dict) else []
        if not isinstance(price_records, list):
            price_records = []
        selected = []
        for row in price_records:
            ticker = str(row.get("ticker", ""))
            if ticker in core_tickers:
                continue
            change_pct = float(row.get("change_pct") or 0)
            turnover = float(row.get("turnover") or 0)
            volume_lots = int(row.get("volume_lots") or 0)
            if turnover < 300_000_000 or change_pct < 1.5:
                continue
            momentum = int(turnover / 10_000_000 + change_pct * 20 + volume_lots / 1000)
            selected.append(
                {
                    "ticker": ticker,
                    "name": row.get("name") or ticker,
                    "themes": ("taiwan", "markets"),
                    "catalyst": f"動態價量雷達：成交金額{turnover / 100_000_000:.1f}億元、日漲幅{change_pct:+.2f}%",
                    "risk": "價量動能 fallback 未含法人買賣超，須等待 T86 或個股法人資料確認",
                    "candidate_source": "dynamic_price",
                    "price_snapshot": row,
                    "dynamic_momentum": momentum,
                }
            )
        selected.sort(key=lambda item: int(item.get("dynamic_momentum", 0)), reverse=True)
        top_selected = selected[:limit]
        return top_selected, {
            "status": dataset.get("status", "failed"),
            "date": dataset.get("date"),
            "source": "TWSE MI_INDEX fallback",
            "screened_count": len(price_records),
            "qualified_count": len(selected),
            "records": [
                {
                    "ticker": item.get("ticker"),
                    "name": item.get("name"),
                    "change_pct": (item.get("price_snapshot") or {}).get("change_pct"),
                    "turnover": (item.get("price_snapshot") or {}).get("turnover"),
                    "volume_lots": (item.get("price_snapshot") or {}).get("volume_lots"),
                    "dynamic_momentum": item.get("dynamic_momentum"),
                    "source": "TWSE MI_INDEX fallback",
                }
                for item in top_selected
            ],
        }
    return top_selected, {
        "status": dataset.get("status", "failed"),
        "date": dataset.get("date"),
        "source": dataset.get("source", "TWSE T86"),
        "screened_count": len(records),
        "qualified_count": len(selected),
        "records": [
            {
                "ticker": item.get("ticker"),
                "name": item.get("name"),
                "foreign_lots": (item.get("institutional_snapshot") or {}).get("foreign_lots"),
                "investment_lots": (item.get("institutional_snapshot") or {}).get("investment_lots"),
                "dealer_lots": (item.get("institutional_snapshot") or {}).get("dealer_lots"),
                "total_lots": (item.get("institutional_snapshot") or {}).get("total_lots"),
                "dynamic_momentum": item.get("dynamic_momentum"),
                "source": "TWSE T86",
            }
            for item in top_selected
        ],
    }


def fetch_taiwan_capital_flow(today: dt.date) -> dict[str, object]:
    for offset in range(0, 10):
        day = today - dt.timedelta(days=offset)
        try:
            data = fetch_twse_json(
                "fund/BFI82U",
                {"dayDate": day.strftime("%Y%m%d"), "type": "day"},
            )
        except Exception as exc:
            print(f"TWSE BFI82U fallback for {day:%Y-%m-%d}: {exc}")
            continue
        if data.get("stat") != "OK" or not data.get("data"):
            continue
        rows: list[dict[str, object]] = []
        dealer_net = investment_net = foreign_net = total_net = 0.0
        for item in data.get("data", []):
            value = item.get("value", []) if isinstance(item, dict) else item
            if not isinstance(value, list) or len(value) < 4:
                continue
            name = clean_text(value[0])
            buy = parse_number(value[1]) or 0.0
            sell = parse_number(value[2]) or 0.0
            net = parse_number(value[3]) or buy - sell
            rows.append({"name": name, "buy": buy, "sell": sell, "net": net})
            if name.startswith("自營商"):
                dealer_net += net
            elif name == "投信":
                investment_net += net
            elif name.startswith("外資及陸資"):
                foreign_net += net
            elif name == "合計":
                total_net = net
        if not total_net:
            total_net = dealer_net + investment_net + foreign_net
        return {
            "status": "ok",
            "source": "TWSE BFI82U",
            "date": data.get("date") or day.strftime("%Y%m%d"),
            "title": data.get("title") or f"{day:%Y-%m-%d} 三大法人買賣金額",
            "foreign_net": foreign_net,
            "investment_trust_net": investment_net,
            "dealer_net": dealer_net,
            "total_net": total_net,
            "records": rows,
        }
    return {
        "status": "failed",
        "source": "TWSE BFI82U",
        "date": None,
        "title": "三大法人買賣金額",
        "foreign_net": None,
        "investment_trust_net": None,
        "dealer_net": None,
        "total_net": None,
        "records": [],
    }


def html_cells(row_html: str) -> list[str]:
    cells = re.findall(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", row_html, flags=re.I)
    cleaned = []
    for cell in cells:
        text = re.sub(r"<[^>]+>", "", cell)
        cleaned.append(clean_text(html.unescape(text)))
    return cleaned


def fetch_taifex_futures_foreign_position(day: dt.date) -> dict[str, object] | None:
    params = urllib.parse.urlencode(
        {
            "queryType": "1",
            "doQuery": "1",
            "dateaddcnt": "",
            "queryDate": day.strftime("%Y/%m/%d"),
            "commodityId": "TXF",
        }
    )
    url = f"https://www.taifex.com.tw/cht/3/futContractsDate?{params}"
    text = fetch_text_url(url, timeout=20, user_agent="Mozilla/5.0 daily-finance-report")
    current_product = ""
    for row_html in re.findall(r"<tr[\s\S]*?</tr>", text, flags=re.I):
        cells = html_cells(row_html)
        if not cells:
            continue
        if "臺股期貨" in cells or "台股期貨" in cells:
            current_product = "TXF"
        identity_index = 2 if len(cells) >= 15 else 0
        identity = cells[identity_index] if identity_index < len(cells) else ""
        if current_product != "TXF" or identity != "外資":
            continue
        values = cells[identity_index + 1 :]
        if len(values) < 12:
            continue
        return {
            "date": day.isoformat(),
            "contract": "TXF",
            "investor": "foreign",
            "trading_long_lots": parse_int(values[0]),
            "trading_short_lots": parse_int(values[2]),
            "trading_net_lots": parse_int(values[4]),
            "open_interest_long_lots": parse_int(values[6]),
            "open_interest_short_lots": parse_int(values[8]),
            "open_interest_net_lots": parse_int(values[10]),
            "source_url": url,
        }
    return None


def fetch_taifex_put_call_ratio(day: dt.date) -> dict[str, object] | None:
    params = urllib.parse.urlencode({"queryStartDate": day.strftime("%Y/%m/%d"), "queryEndDate": day.strftime("%Y/%m/%d")})
    url = f"https://www.taifex.com.tw/cht/3/pcRatio?{params}"
    text = fetch_text_url(url, timeout=20, user_agent="Mozilla/5.0 daily-finance-report")
    for row_html in re.findall(r"<tr[\s\S]*?</tr>", text, flags=re.I):
        cells = html_cells(row_html)
        if len(cells) < 7 or not re.match(r"\d{4}/\d{1,2}/\d{1,2}", cells[0]):
            continue
        return {
            "date": day.isoformat(),
            "put_volume": parse_int(cells[1]),
            "call_volume": parse_int(cells[2]),
            "put_call_volume_ratio": parse_number(cells[3]),
            "put_open_interest": parse_int(cells[4]),
            "call_open_interest": parse_int(cells[5]),
            "put_call_open_interest_ratio": parse_number(cells[6]),
            "source_url": url,
        }
    return None


def fetch_derivatives_flow(today: dt.date) -> dict[str, object]:
    for offset in range(0, 10):
        day = today - dt.timedelta(days=offset)
        try:
            futures = fetch_taifex_futures_foreign_position(day)
            put_call = fetch_taifex_put_call_ratio(day)
        except Exception as exc:
            print(f"TAIFEX derivatives fallback for {day:%Y-%m-%d}: {exc}")
            continue
        records = []
        if futures:
            records.append({"dataset": "foreign_taiex_futures_net_position", **futures})
        if put_call:
            records.append({"dataset": "txo_put_call_ratio", **put_call})
        if records:
            return {
                "status": "ok" if futures and put_call else "partial",
                "source": "TAIFEX",
                "date": day.isoformat(),
                "records": records,
            }
    return {"status": "failed", "source": "TAIFEX", "date": None, "records": []}


def finmind_dataset(dataset: str, *, data_id: str | None = None, start_date: str | None = None, timeout: int = 18) -> list[dict[str, object]]:
    params = {"dataset": dataset}
    if data_id:
        params["data_id"] = data_id
    if start_date:
        params["start_date"] = start_date
    url = "https://api.finmindtrade.com/api/v4/data?" + urllib.parse.urlencode(params)
    data = fetch_json_url(url, timeout=timeout)
    if data.get("status") != 200:
        raise RuntimeError(f"FinMind {dataset} returned {data.get('status')}: {data.get('msg')}")
    rows = data.get("data") or []
    return rows if isinstance(rows, list) else []


def fetch_market_breadth(today: dt.date, tickers: list[str]) -> dict[str, object]:
    price_dataset = latest_twse_price_momentum_rows(today)
    price_records = price_dataset.get("records") if isinstance(price_dataset, dict) else []
    if not isinstance(price_records, list):
        price_records = []
    advancing = sum(1 for row in price_records if float(row.get("change_pct") or 0) > 0)
    declining = sum(1 for row in price_records if float(row.get("change_pct") or 0) < 0)
    unchanged = sum(1 for row in price_records if float(row.get("change_pct") or 0) == 0)
    margin_records = []
    start_date = (today - dt.timedelta(days=20)).isoformat()
    for ticker in tickers:
        try:
            rows = finmind_dataset("TaiwanStockMarginPurchaseShortSale", data_id=ticker, start_date=start_date)
        except Exception as exc:
            print(f"FinMind margin fallback for {ticker}: {exc}")
            continue
        if not rows:
            continue
        latest = max(rows, key=lambda item: str(item.get("date", "")))
        margin_records.append(
            {
                "ticker": ticker,
                "date": latest.get("date"),
                "margin_purchase_balance": latest.get("MarginPurchaseTodayBalance"),
                "margin_purchase_change": (latest.get("MarginPurchaseTodayBalance") or 0) - (latest.get("MarginPurchaseYesterdayBalance") or 0),
                "short_sale_balance": latest.get("ShortSaleTodayBalance"),
                "short_sale_change": (latest.get("ShortSaleTodayBalance") or 0) - (latest.get("ShortSaleYesterdayBalance") or 0),
                "source": "FinMind TaiwanStockMarginPurchaseShortSale",
            }
        )
    status = "ok" if price_dataset.get("status") == "ok" and margin_records else "partial" if price_records or margin_records else "failed"
    return {
        "status": status,
        "source": "TWSE MI_INDEX / FinMind",
        "date": price_dataset.get("date"),
        "breadth": {
            "listed_stock_count": len(price_records),
            "advancing": advancing,
            "declining": declining,
            "unchanged": unchanged,
            "advance_decline_ratio": round(advancing / declining, 2) if declining else None,
            "new_20d_high": None,
            "new_20d_low": None,
            "new_52w_high": None,
            "new_52w_low": None,
            "new_high_low_status": "not_connected",
        },
        "margin_records": margin_records,
        "securities_lending": {"status": "not_connected", "source": "TWSE / TPEx"},
        "records": price_records[:30],
    }


SECTOR_INDEX_NAMES = {
    "半導體類指數",
    "電腦及週邊設備類指數",
    "通信網路類指數",
    "電子零組件類指數",
    "其他電子類指數",
    "金融保險類指數",
    "航運類指數",
    "鋼鐵類指數",
    "生技醫療類指數",
    "塑膠類指數",
    "電機機械類指數",
    "油電燃氣類指數",
}


def fetch_twse_sector_rotation(today: dt.date) -> dict[str, object]:
    for offset in range(0, 10):
        day = today - dt.timedelta(days=offset)
        try:
            data = fetch_twse_json(
                "afterTrading/MI_INDEX",
                {"date": day.strftime("%Y%m%d"), "type": "ALLBUT0999"},
                timeout=15,
            )
        except Exception as exc:
            print(f"TWSE MI_INDEX fallback for {day:%Y-%m-%d}: {exc}")
            continue
        if data.get("stat") != "OK" or not data.get("tables"):
            continue
        records: list[dict[str, object]] = []
        for table in data.get("tables", []):
            title = str(table.get("title", ""))
            if "價格指數(臺灣證券交易所)" not in title:
                continue
            for row in table.get("data", []):
                if not isinstance(row, list) or len(row) < 5:
                    continue
                name = clean_text(row[0])
                if name not in SECTOR_INDEX_NAMES:
                    continue
                value = parse_number(row[1])
                point_change = parse_number(row[3])
                pct = parse_number(row[4])
                records.append(
                    {
                        "sector": name.replace("類指數", ""),
                        "index_name": name,
                        "close": value,
                        "change_points": point_change,
                        "change_pct": pct,
                        "stage": sector_stage(pct),
                        "source": "TWSE MI_INDEX",
                    }
                )
        if records:
            records.sort(key=lambda item: item.get("change_pct") if isinstance(item.get("change_pct"), (int, float)) else -999, reverse=True)
            return {
                "status": "ok",
                "source": "TWSE MI_INDEX",
                "date": data.get("date") or day.strftime("%Y%m%d"),
                "records": records,
            }
    return {"status": "failed", "source": "TWSE MI_INDEX", "date": None, "records": []}


def sector_stage(change_pct: object) -> str:
    if not isinstance(change_pct, (int, float)):
        return "待更新"
    if change_pct >= 3:
        return "強勢擴張"
    if change_pct >= 1:
        return "復甦偏多"
    if change_pct <= -2:
        return "降溫警戒"
    if change_pct < 0:
        return "整理偏弱"
    return "中性整理"


def pct_change(current: float | None, previous: float | None) -> float | None:
    if not isinstance(current, (int, float)) or not isinstance(previous, (int, float)) or previous == 0:
        return None
    return (current - previous) / previous * 100


def first_close_of_year(points: list[tuple[int, float]], year: int) -> float | None:
    for timestamp, close in points:
        try:
            if dt.datetime.fromtimestamp(int(timestamp), TW).year == year:
                return float(close)
        except Exception:
            continue
    return None


def market_snapshot() -> list[dict[str, object]]:
    rows = []
    for label, symbol, group in MARKET_TICKERS:
        quote = fetch_yahoo_quote(symbol)
        rows.append({"label": label, "symbol": symbol, "group": group, **quote})
    return rows


def market_temperature(snapshot: list[dict[str, object]]) -> dict[str, str]:
    score = 0
    by_label = {row["label"]: row for row in snapshot}
    for label in ("台股加權", "櫃買指數", "Nasdaq", "SOX 費半", "S&P 500"):
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


def metric_score(value: object, positive: float = 0.0, negative: float = -0.8) -> int:
    if not isinstance(value, (int, float)):
        return 50
    if value >= positive:
        return 75
    if value <= negative:
        return 35
    return 55


def average_score(values: list[int]) -> int:
    return int(round(sum(values) / len(values))) if values else 50


def market_environment(snapshot: list[dict[str, object]], news: list[dict[str, str]], capital_flow: dict[str, object] | None = None) -> dict[str, object]:
    by_label = {row["label"]: row for row in snapshot}
    required_labels = ["台股加權", "Nasdaq", "SOX 費半", "美債10Y", "美元指數", "美元/台幣", "VIX", "WTI 原油"]
    ok_count = sum(1 for label in required_labels if by_label.get(label, {}).get("ok"))
    data_quality = round(ok_count / len(required_labels) * 100)
    quality_penalty = 0 if data_quality >= 75 else 5 if data_quality >= 55 else 10
    growth = average_score(
        [
            metric_score(by_label.get("台股加權", {}).get("change_20d"), 1.5, -2.5),
            metric_score(by_label.get("SOX 費半", {}).get("change_20d"), 1.5, -3.0),
            metric_score(theme_counts(news).get("ai", 0), 3, 0),
        ]
    )
    rates = average_score(
        [
            metric_score(-(by_label.get("美債10Y", {}).get("change_20d") or 0), 0.0, -4.0),
            metric_score(-(by_label.get("美元指數", {}).get("change_20d") or 0), 0.0, -1.5),
        ]
    )
    capital = average_score(
        [
            metric_score(-(by_label.get("美元/台幣", {}).get("change_5d") or 0), 0.0, -0.8),
            metric_score(-(by_label.get("美元指數", {}).get("change_5d") or 0), 0.0, -0.8),
            metric_score(by_label.get("台股加權", {}).get("change_5d"), 0.5, -1.5),
            metric_score((capital_flow or {}).get("foreign_net"), 0.0, -8_000_000_000),
            metric_score((capital_flow or {}).get("investment_trust_net"), 0.0, -2_000_000_000),
        ]
    )
    trend = average_score(
        [
            metric_score(by_label.get("Nasdaq", {}).get("change_20d"), 1.5, -3.0),
            metric_score(by_label.get("SOX 費半", {}).get("change_20d"), 1.5, -3.0),
            metric_score(by_label.get("台股加權", {}).get("change_20d"), 1.5, -2.5),
        ]
    )
    risk = average_score(
        [
            metric_score(-(by_label.get("VIX", {}).get("change_5d") or 0), 0.0, -8.0),
            metric_score(-(by_label.get("WTI 原油", {}).get("change_5d") or 0), 0.0, -6.0),
        ]
    )
    total = round(growth * 0.25 + rates * 0.25 + capital * 0.20 + trend * 0.20 + risk * 0.10) - quality_penalty
    total = clamp(total, 0, 100)
    if total >= 70:
        status = "Risk-On"
    elif total >= 58:
        status = "中性偏多"
    elif total >= 45:
        status = "黃燈警戒"
    else:
        status = "Risk-Off"
    factors = environment_factors(snapshot, news)
    return {
        "score": int(total),
        "status": status,
        "previous_score": "待歷史資料",
        "weekly_change": "待歷史資料",
        "data_status": f"公開行情：{ok_count}/{len(required_labels)} 已更新；總經預期/實際值逐步接入",
        "data_quality": data_quality,
        "provisional": data_quality < 75,
        "components": [
            ("成長動能", growth, "ISM、零售、外銷訂單、工業生產、出口；目前以 AI/電子新聞與股市趨勢代理"),
            ("通膨與利率", rates, "CPI、PCE、非農薪資、美債殖利率、Fed 預期；目前以 10Y 與美元壓力代理"),
            ("資金與匯率", capital, "DXY、美元/台幣、外資、投信、台指期；目前以匯率與台股趨勢代理"),
            ("市場趨勢", trend, "Nasdaq、費半、台股、均線、成交量；目前以 20 日價格趨勢代理"),
            ("風險情緒", risk, "VIX、信用風險、油價、地緣政治事件；目前以 VIX/油價與新聞風險代理"),
        ],
        "factors": factors,
        "implication": stock_radar_policy(total),
    }


def environment_factors(snapshot: list[dict[str, object]], news: list[dict[str, str]]) -> list[str]:
    by_label = {row["label"]: row for row in snapshot}
    factors: list[str] = []
    for label in ("台股加權", "Nasdaq", "SOX 費半"):
        change = by_label.get(label, {}).get("change_20d")
        if isinstance(change, (int, float)):
            direction = "轉強" if change >= 1.5 else "轉弱" if change <= -3 else "震盪"
            factors.append(f"{label} 20 日趨勢{direction}（{change:+.2f}%）")
    dxy = by_label.get("美元指數", {}).get("change_5d")
    if isinstance(dxy, (int, float)) and abs(dxy) >= 0.5:
        factors.append(f"美元指數 5 日變化 {dxy:+.2f}%，影響外資與台幣資金條件")
    vix = by_label.get("VIX", {}).get("value")
    if isinstance(vix, (int, float)):
        factors.append(f"VIX {vix:.2f}，作為短線風險情緒檢查")
    if theme_counts(news).get("ai", 0) >= 3:
        factors.append("AI 與半導體新聞密度偏高，需用月營收與法人籌碼驗證")
    return factors[:3] or ["市場資料不足，先降低操作權重並等待更新"]


def stock_radar_policy(score: int) -> str:
    if score >= 70:
        return "正式強勢股與機會股可正常升級，但仍需法人與營收同步支持。"
    if score >= 58:
        return "優先追蹤強勢股，機會股須搭配法人買超與技術突破確認。"
    if score >= 45:
        return "維持黃燈警戒，高估值與漲幅過大個股不宜追價，只保留最強候選。"
    return "市場環境偏弱，暫停升級，只保留風險最低且法人支撐明確的標的。"


def format_market_value(row: dict[str, object]) -> str:
    value = row.get("value")
    if not isinstance(value, (int, float)):
        return "資料暫缺"
    if row.get("label") == "美元/台幣":
        return f"{value:.3f}"
    if row.get("label") == "美債10Y":
        normalized = value / 10 if value > 20 else value
        return f"{normalized:.2f}%"
    return f"{value:,.2f}"


def format_change(row: dict[str, object]) -> str:
    change = row.get("change_pct")
    if not isinstance(change, (int, float)):
        return "待更新"
    sign = "+" if change >= 0 else ""
    return f"{sign}{change:.2f}%"


def format_pct(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "待更新"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}%"


def risk_tag(row: dict[str, object]) -> str:
    label = str(row.get("label", ""))
    change_20d = row.get("change_20d")
    change_5d = row.get("change_5d")
    value = row.get("value")
    if label == "VIX" and isinstance(value, (int, float)):
        return "警戒" if value >= 25 else "偏多" if value < 18 else "中性"
    if label in {"美元指數", "美元/台幣", "美債10Y", "WTI 原油", "布蘭特原油"} and isinstance(change_5d, (int, float)):
        return "警戒" if change_5d > 2 else "偏空" if change_5d > 0.6 else "中性"
    if isinstance(change_20d, (int, float)):
        return "偏多" if change_20d > 1.5 else "偏空" if change_20d < -2.5 else "中性"
    return "待更新"


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


def candidate_institutional_signal(ticker: str, today: dt.date, prefetched: dict[str, object] | None = None) -> dict[str, object]:
    if prefetched:
        latest_date = dt.date.fromisoformat(str(prefetched.get("date") or today.isoformat()))
        foreign = int(prefetched.get("foreign_lots") or 0)
        investment = int(prefetched.get("investment_lots") or 0)
        dealer = int(prefetched.get("dealer_lots") or 0)
        total = int(prefetched.get("total_lots") or 0)
        score = 15
        score += 5 if total > 0 else -6 if total < 0 else 0
        score += 5 if foreign > 0 else -8 if foreign <= -10000 else -4 if foreign < 0 else 0
        score += 5 if investment > 0 else -3 if investment < 0 else 0
        warning = "法人偏買，題材較有延續條件"
        if foreign <= -10000 or total <= -10000:
            warning = "外資/法人明顯賣超，降分並避免追價"
        elif total < 0:
            warning = "法人偏賣，需等籌碼止穩"
        return {
            "score": clamp(score, 0, 30),
            "text": (
                f"{latest_date:%m/%d} 外資{foreign:+,}張、投信{investment:+,}張、"
                f"自營商{dealer:+,}張；三大法人{total:+,}張"
            ),
            "warning": warning,
            "latest_total": total,
        }
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


def score_candidates(news: list[dict[str, str]], analysis: dict, today: dt.date, dynamic_candidates: list[dict[str, object]] | None = None) -> list[dict[str, object]]:
    theme_strength = Counter(item["theme"] for item in news)
    source_count = len({item["source"] for item in news}) or 1
    rows = []
    all_text = " ".join(f"{item['headline']} {item['original_headline']}" for item in news).lower()
    candidate_pool = [*CORE_STOCK_UNIVERSE, *(dynamic_candidates or [])]
    seen: set[str] = set()
    for stock in candidate_pool:
        ticker = str(stock["ticker"])
        if ticker in seen:
            continue
        seen.add(ticker)
        candidate_source = str(stock.get("candidate_source") or "core")
        is_dynamic = candidate_source.startswith("dynamic")
        source_label = "動態價量雷達" if candidate_source == "dynamic_price" else "動態法人雷達" if is_dynamic else "核心追蹤池"
        theme_score = min(30, sum(theme_strength[theme] for theme in stock["themes"]) * 5)
        mention_bonus = 10 if stock["name"].lower() in all_text or stock["ticker"] in all_text else 0
        theme_component = min(35, theme_score + mention_bonus)
        if is_dynamic:
            theme_component = max(theme_component, 18)
        institutional = candidate_institutional_signal(ticker, today, stock.get("institutional_snapshot") if isinstance(stock.get("institutional_snapshot"), dict) else None)
        technical = candidate_technical_signal(str(stock["ticker"]))
        quality = min(10, source_count)
        data_score = 10 if institutional["latest_total"] is not None else 4
        dynamic_bonus = 6 if is_dynamic else 0
        score = clamp(
            int(theme_component) + int(institutional["score"]) + int(technical["score"]) + quality + data_score + dynamic_bonus,
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
                "candidate_source": source_label,
                "theme_score": theme_component,
                "flow_score": institutional["score"],
                "technical_score": technical["score"],
                "data_quality_score": data_score,
                "flow_text": institutional["text"],
                "flow_warning": institutional["warning"],
                "technical_text": technical["text"],
                "inclusion_reason": (
                    f"題材/基本面代理 {theme_component}/35、法人籌碼 {institutional['score']}/30、"
                    f"技術 {technical['score']}/20、資料品質 {data_score}/10；"
                    f"{institutional['warning']}。來源：{source_label}。"
                ),
                "change_history": f"{today:%Y-%m-%d}：依公開新聞主題、TWSE 法人資料與 Yahoo 價格資料重新評分。",
                "data_source": "Google News RSS / TWSE 三大法人公開資料 / Yahoo Finance",
            }
        )
    return sorted(rows, key=lambda item: (-int(item["score"]), item["ticker"]))


def priority_candidates(candidates: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        item
        for item in candidates
        if int(item["score"]) >= PRIORITY_CANDIDATE_MIN_SCORE
        and int(item["flow_score"]) >= PRIORITY_CANDIDATE_MIN_FLOW_SCORE
    ][:PRIORITY_CANDIDATE_LIMIT]


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
    rows: list[dict[str, str]] = []
    for offset in range(3):
        day = today + dt.timedelta(days=offset)
        for time_text, country, event, importance, impact in MAJOR_EVENT_TEMPLATES.get((day.month, day.day), []):
            rows.append(
                {
                    "time": f"{day:%m/%d} {time_text}",
                    "country": country,
                    "event": event,
                    "previous": "待接資料源",
                    "forecast": "待接資料源",
                    "importance": importance,
                    "impact": impact,
                }
            )
    rows.extend(
        [
            {"time": "未來 72 小時", "country": "美國", "event": "CPI / PCE / Fed / 就業資料監控", "previous": "待接資料源", "forecast": "待接資料源", "importance": "中", "impact": "若有臨時公布或修正，影響利率、美元與高估值科技股"},
            {"time": "未來 7 天", "country": "台灣", "event": "上市櫃月營收與重大公告追蹤", "previous": "待接資料源", "forecast": "不適用", "importance": "高", "impact": "AI 伺服器、半導體、電子零組件"},
            {"time": "每日", "country": "台灣", "event": "外資、投信、台指期與新台幣", "previous": "待接資料源", "forecast": "不適用", "importance": "高", "impact": "台股資金、權值股、ETF、期貨避險部位"},
        ]
    )
    return rows[:6]


def macro_record_value(macro_indicators: dict[str, object], series_id: str) -> str | None:
    row = next((item for item in macro_indicators.get("records", []) if item.get("series_id") == series_id and item.get("status") == "ok"), None)
    if not row:
        return None
    value = row.get("actual")
    date = row.get("date")
    unit = row.get("unit") or ""
    if value is None:
        return None
    if isinstance(value, float):
        value_text = f"{value:,.2f}"
    else:
        value_text = f"{value:,}" if isinstance(value, int) else str(value)
    return f"{value_text} {unit} ({date})".strip()


def enrich_event_calendar(events: list[dict[str, str]], macro_indicators: dict[str, object]) -> list[dict[str, str]]:
    enriched = []
    for event in events:
        item = dict(event)
        name = item.get("event", "")
        previous = None
        if "零售銷售" in name:
            previous = macro_record_value(macro_indicators, "RSAFS")
        elif "初領失業救濟金" in name:
            previous = macro_record_value(macro_indicators, "ICSA")
        elif "新屋開工" in name or "建築許可" in name:
            housing = macro_record_value(macro_indicators, "HOUST")
            permit = macro_record_value(macro_indicators, "PERMIT")
            if housing or permit:
                previous = f"HOUST {housing or '待接'} / PERMIT {permit or '待接'}"
        elif "Fed" in name or "利率" in name:
            previous = macro_record_value(macro_indicators, "FEDFUNDS")
        if previous:
            item["previous"] = previous
            if item.get("forecast") == "待接資料源":
                item["forecast"] = "市場預期待接"
        enriched.append(item)
    return enriched


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


def render_market_environment(env: dict[str, object], generated_at: str) -> str:
    components = "\n".join(
        f"""
        <div class="score-row">
          <div><b>{html.escape(name)}</b><span>{html.escape(note)}</span></div>
          <strong>{score}</strong>
        </div>
        """
        for name, score, note in env["components"]
    )
    factors = "".join(f"<li>{html.escape(str(item))}</li>" for item in env["factors"])
    return f"""
    <section class="regime-panel" id="dashboard">
      <div class="regime-score">
        <span>市場環境分數</span>
        <strong>{env["score"]}</strong>
        <em>{html.escape(str(env["status"]))}</em>
      </div>
      <div class="regime-detail">
        <div class="status-grid">
          <div><span>前一日分數</span><b>{html.escape(str(env["previous_score"]))}</b></div>
          <div><span>一週變化</span><b>{html.escape(str(env["weekly_change"]))}</b></div>
          <div><span>更新時間</span><b>{html.escape(generated_at)}</b></div>
          <div><span>資料狀態</span><b>{html.escape(str(env["data_status"]))}</b></div>
        </div>
        <div class="score-grid">{components}</div>
        <div class="factor-grid">
          <div><h3>造成分數變動的前三項因素</h3><ul>{factors}</ul></div>
          <div><h3>對台股與 Stock Radar 的操作意義</h3><p>{html.escape(str(env["implication"]))}</p></div>
        </div>
      </div>
    </section>
    """


def render_global_market_cards(snapshot: list[dict[str, object]]) -> str:
    rows = [row for row in snapshot if row.get("group") == "global"]
    return "\n".join(
        f"""
        <article class="market-card">
          <div class="card-head"><h3>{html.escape(str(row["label"]))}</h3><span>{html.escape(str(row["data_time"]))}</span></div>
          <strong>{html.escape(format_market_value(row))}</strong>
          <dl>
            <dt>日漲跌</dt><dd>{html.escape(format_pct(row.get("change_pct")))}</dd>
            <dt>5日</dt><dd>{html.escape(format_pct(row.get("change_5d")))}</dd>
            <dt>20日</dt><dd>{html.escape(format_pct(row.get("change_20d")))}</dd>
            <dt>YTD</dt><dd>{html.escape(format_pct(row.get("ytd_change")))}</dd>
            <dt>距52週高點</dt><dd>{html.escape(format_pct(row.get("high_52w_gap")))}</dd>
          </dl>
        </article>
        """
        for row in rows
    )


def render_cross_asset_cards(snapshot: list[dict[str, object]]) -> str:
    rows = [row for row in snapshot if row.get("group") == "cross"]
    return "\n".join(
        f"""
        <article class="asset-card">
          <div class="card-head"><h3>{html.escape(str(row["label"]))}</h3><span class="tag {html.escape(risk_tag(row))}">{html.escape(risk_tag(row))}</span></div>
          <strong>{html.escape(format_market_value(row))}</strong>
          <div class="mini-trend">
            <span>日 {html.escape(format_pct(row.get("change_pct")))}</span>
            <span>週 {html.escape(format_pct(row.get("change_5d")))}</span>
            <span>月 {html.escape(format_pct(row.get("change_20d")))}</span>
          </div>
          <p>20 日趨勢：{html.escape(format_pct(row.get("change_20d")))}｜資料：{html.escape(str(row.get("data_time", "待更新")))}</p>
        </article>
        """
        for row in rows
    )


def render_capital_flow_summary(candidates: list[dict[str, object]], snapshot: list[dict[str, object]], capital_flow: dict[str, object]) -> str:
    buy_count = sum(1 for item in candidates if int(item.get("flow_score", 0)) >= 20)
    neutral_count = sum(1 for item in candidates if 14 <= int(item.get("flow_score", 0)) < 20)
    twd = next((row for row in snapshot if row["label"] == "美元/台幣"), {})
    flow_date = str(capital_flow.get("date") or "待更新")
    rows = [
        ("候選股法人支持", f"{buy_count} 檔強、{neutral_count} 檔中性", "來自 TWSE 個股三大法人資料"),
        ("三大法人合計", format_ntd_billion(capital_flow.get("total_net")), f"TWSE BFI82U｜{flow_date}"),
        ("外資買賣超", format_ntd_billion(capital_flow.get("foreign_net")), "外資及陸資買賣差額"),
        ("投信買賣超", format_ntd_billion(capital_flow.get("investment_trust_net")), "本國投信買賣差額"),
        ("新台幣匯率", f"{format_market_value(twd)} / 5日 {format_pct(twd.get('change_5d'))}", "美元/台幣作為外資資金壓力代理"),
        ("台指期外資淨部位", "待接資料源", "P1 接入期交所公開資料"),
        ("融資融券與借券", "待接資料源", "P1 接入 TWSE/TPEx 公開資料"),
    ]
    return "\n".join(
        f"""
        <article class="capital-card">
          <span>{html.escape(title)}</span>
          <strong>{html.escape(value)}</strong>
          <p>{html.escape(note)}</p>
        </article>
        """
        for title, value, note in rows
    )


def format_plain_number(value: object, suffix: str = "") -> str:
    if isinstance(value, int):
        return f"{value:,}{suffix}"
    if isinstance(value, float):
        return f"{value:,.2f}{suffix}"
    return "待接資料源"


def render_derivatives_and_breadth(derivatives_flow: dict[str, object], market_breadth: dict[str, object]) -> str:
    futures = next((row for row in derivatives_flow.get("records", []) if row.get("dataset") == "foreign_taiex_futures_net_position"), {})
    put_call = next((row for row in derivatives_flow.get("records", []) if row.get("dataset") == "txo_put_call_ratio"), {})
    breadth = market_breadth.get("breadth") if isinstance(market_breadth.get("breadth"), dict) else {}
    rows = [
        ("外資台指期淨部位", format_plain_number(futures.get("open_interest_net_lots"), " 口"), f"TAIFEX {derivatives_flow.get('date') or '資料更新失敗'}"),
        ("TXO Put/Call Ratio", format_plain_number(put_call.get("put_call_volume_ratio"), "%"), "成交量 Put/Call；>100 代表賣權成交量高於買權"),
        ("上市上漲/下跌家數", f"{format_plain_number(breadth.get('advancing'))} / {format_plain_number(breadth.get('declining'))}", f"A/D Ratio {format_plain_number(breadth.get('advance_decline_ratio'))}"),
        ("核心股融資融券", f"{len(market_breadth.get('margin_records', []))} 檔已更新", "FinMind 公開融資融券資料；借券/新高新低仍分階段接入"),
    ]
    return "\n".join(
        f"""
        <article class="capital-card">
          <span>{html.escape(title)}</span>
          <strong>{html.escape(value)}</strong>
          <p>{html.escape(note)}</p>
        </article>
        """
        for title, value, note in rows
    )


def render_macro_table(macro_indicators: dict[str, object], prefix: str) -> str:
    records = [row for row in macro_indicators.get("records", []) if str(row.get("category", "")).startswith(prefix)]
    if not records:
        return "<p>待接資料源</p>"
    body = []
    for row in records:
        body.append(
            f"""
            <tr>
              <td>{html.escape(str(row.get('name')))}</td>
              <td>{html.escape(str(row.get('status')))}</td>
              <td>{html.escape(format_plain_number(row.get('actual')))}</td>
              <td>{html.escape(str(row.get('date') or '待接資料源'))}</td>
              <td>{html.escape(format_plain_number(row.get('mom_pct'), '%'))}</td>
              <td>{html.escape(format_plain_number(row.get('yoy_pct'), '%'))}</td>
              <td>{html.escape(str(row.get('source')))}</td>
            </tr>
            """
        )
    return f"""
    <table>
      <thead><tr><th>指標</th><th>狀態</th><th>最新值</th><th>日期</th><th>MoM</th><th>YoY</th><th>來源</th></tr></thead>
      <tbody>{''.join(body)}</tbody>
    </table>
    """


def render_fundamentals_table(fundamentals: dict[str, object]) -> str:
    rows = []
    for row in fundamentals.get("records", []):
        rows.append(
            f"""
            <tr>
              <td>{html.escape(str(row.get('ticker')))} {html.escape(str(row.get('name')))}</td>
              <td>{html.escape(str(row.get('status')))}</td>
              <td>{html.escape(format_plain_number(row.get('latest_month_revenue')))}</td>
              <td>{html.escape(str(row.get('latest_quarter') or '待接資料源'))}</td>
              <td>{html.escape(format_plain_number(row.get('eps')))}</td>
              <td>{html.escape(format_plain_number(row.get('gross_margin_pct'), '%'))}</td>
              <td>{html.escape(format_plain_number(row.get('roe_pct_annualized'), '%'))}</td>
              <td>{html.escape(format_plain_number(row.get('inventory')))}</td>
              <td>{html.escape(format_plain_number(row.get('accounts_receivable')))}</td>
            </tr>
            """
        )
    return f"""
    <table>
      <thead><tr><th>股票</th><th>狀態</th><th>月營收</th><th>季度</th><th>EPS</th><th>毛利率</th><th>ROE</th><th>存貨</th><th>應收帳款</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """


def render_candidate_table(candidates: list[dict[str, object]]) -> str:
    if not candidates:
        return """
        <tr>
          <td colspan="10"><b>No priority candidates today.</b><br>今日沒有同時通過分數與籌碼門檻的優先追蹤標的，避免因題材熱度不足或法人轉弱而誤列推薦。</td>
        </tr>
        """
    return "\n".join(
        f"""
        <tr>
          <td><b>{html.escape(str(item['bucket']))}</b></td>
          <td>{html.escape(str(item['ticker']))} {html.escape(str(item['name']))}<br><small>{html.escape(str(item.get('candidate_source', '核心追蹤池')))}</small></td>
          <td><strong>{item['score']}</strong></td>
          <td>{html.escape(str(item['theme_score']))}</td>
          <td>{html.escape(str(item['flow_score']))}</td>
          <td>{html.escape(str(item['technical_score']))}</td>
          <td>{html.escape(str(item['flow_text']))}<br><b>{html.escape(str(item['flow_warning']))}</b></td>
          <td>{html.escape(str(item['inclusion_reason']))}<br><small>{html.escape(str(item['change_history']))}</small></td>
          <td>{html.escape(str(item['risk']))}<br><small>來源：{html.escape(str(item['data_source']))}</small></td>
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
        <tr>
          <td>{html.escape(event['time'])}</td>
          <td>{html.escape(event['country'])}</td>
          <td><b>{html.escape(event['event'])}</b></td>
          <td>{html.escape(event['previous'])}</td>
          <td>{html.escape(event['forecast'])}</td>
          <td><span class="importance {html.escape(event['importance'])}">{html.escape(event['importance'])}</span></td>
          <td>{html.escape(event['impact'])}</td>
        </tr>
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


def render_sector_rotation(sector_rotation: dict[str, object], news: list[dict[str, str]], analysis: dict) -> str:
    records = sector_rotation.get("records") if isinstance(sector_rotation, dict) else []
    cards: list[str] = []
    if isinstance(records, list) and records:
        for item in records[:6]:
            change = item.get("change_pct")
            change_text = format_pct(change)
            cards.append(
                f"""
                <article class="theme-card">
                  <div class="axis-rank">TWSE</div>
                  <div class="theme-topline">
                    <h3>{html.escape(str(item.get("sector", "產業")))}</h3>
                    <strong>{html.escape(change_text)}</strong>
                  </div>
                  <dl>
                    <dt>景氣定位</dt><dd>{html.escape(str(item.get("stage", "待更新")))}</dd>
                    <dt>收盤指數</dt><dd>{html.escape(format_market_value({"value": item.get("close")}))}</dd>
                    <dt>資料來源</dt><dd>{html.escape(str(item.get("source", "TWSE MI_INDEX")))}</dd>
                  </dl>
                </article>
                """
            )
        return "\n".join(cards)
    return render_theme_cards(news, analysis)


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


def serializable_snapshot(snapshot: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "label": row.get("label"),
            "symbol": row.get("symbol"),
            "group": row.get("group"),
            "value": row.get("value"),
            "display_value": format_market_value(row),
            "change_pct": row.get("change_pct"),
            "change_5d": row.get("change_5d"),
            "change_20d": row.get("change_20d"),
            "ytd_change": row.get("ytd_change"),
            "high_52w_gap": row.get("high_52w_gap"),
            "data_time": row.get("data_time"),
            "status": "ok" if row.get("ok") else "failed",
            "source": "Yahoo Finance",
        }
        for row in snapshot
    ]


FRED_SERIES = {
    "CPIAUCSL": {"name": "US CPI", "category": "US inflation", "unit": "index"},
    "PCEPI": {"name": "US PCE Price Index", "category": "US inflation", "unit": "index"},
    "PAYEMS": {"name": "US Nonfarm Payrolls", "category": "US employment", "unit": "thousand persons"},
    "ICSA": {"name": "US Initial Jobless Claims", "category": "US employment", "unit": "persons"},
    "RSAFS": {"name": "US Retail Sales", "category": "US demand", "unit": "million USD"},
    "HOUST": {"name": "US Housing Starts", "category": "US demand", "unit": "thousand units"},
    "PERMIT": {"name": "US Building Permits", "category": "US demand", "unit": "thousand units"},
    "FEDFUNDS": {"name": "Fed Funds Rate", "category": "US rates", "unit": "%"},
    "DGS2": {"name": "US 2Y Treasury Yield", "category": "US rates", "unit": "%"},
    "DGS10": {"name": "US 10Y Treasury Yield", "category": "US rates", "unit": "%"},
}


def fetch_fred_series(series_id: str) -> dict[str, object]:
    start_date = (dt.datetime.now(TW).date() - dt.timedelta(days=560)).isoformat()
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?{urllib.parse.urlencode({'id': series_id, 'cosd': start_date})}"
    last_error: Exception | None = None
    text = ""
    timeout = 6 if os.environ.get("GITHUB_ACTIONS") else 20
    user_agents = ("Mozilla/5.0 daily-finance-report",) if os.environ.get("GITHUB_ACTIONS") else ("Mozilla/5.0 daily-finance-report", "daily-finance-report")
    for candidate_url in (
        url,
        f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={urllib.parse.quote(series_id)}",
    ):
        for user_agent in user_agents:
            try:
                text = fetch_text_url(candidate_url, timeout=timeout, user_agent=user_agent)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                time.sleep(0.2)
        if text:
            break
    if not text and last_error:
        raise last_error
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for row in reader:
        date = row.get("observation_date")
        raw_value = row.get(series_id)
        if not date or raw_value in (None, ".", ""):
            continue
        value = parse_number(raw_value)
        if value is None:
            continue
        rows.append({"date": date, "value": value})
    if not rows:
        raise RuntimeError(f"FRED {series_id} has no numeric observations")
    latest = rows[-1]
    previous = rows[-2] if len(rows) >= 2 else None
    yoy = None
    if len(rows) >= 13:
        prior_year = rows[-13]
        if prior_year["value"]:
            yoy = (latest["value"] / prior_year["value"] - 1) * 100
    mom = None
    if previous and previous["value"]:
        mom = (latest["value"] / previous["value"] - 1) * 100
    meta = FRED_SERIES.get(series_id, {})
    return {
        "series_id": series_id,
        "name": meta.get("name", series_id),
        "category": meta.get("category", "US macro"),
        "date": latest["date"],
        "actual": latest["value"],
        "previous": previous["value"] if previous else None,
        "mom_pct": round(mom, 2) if mom is not None else None,
        "yoy_pct": round(yoy, 2) if yoy is not None else None,
        "forecast": None,
        "surprise": None,
        "unit": meta.get("unit"),
        "source": "FRED",
        "status": "ok",
    }


def fetch_macro_indicators(today: dt.date) -> dict[str, object]:
    records = []
    fred_failures = 0
    for series_id in FRED_SERIES:
        if os.environ.get("GITHUB_ACTIONS") and fred_failures >= 3:
            records.append(
                {
                    "series_id": series_id,
                    "name": FRED_SERIES[series_id]["name"],
                    "category": FRED_SERIES[series_id]["category"],
                    "status": "failed",
                    "source": "FRED",
                    "error": "skipped after repeated FRED failures",
                }
            )
            continue
        try:
            records.append(fetch_fred_series(series_id))
        except Exception as exc:
            print(f"FRED fallback for {series_id}: {exc}")
            fred_failures += 1
            records.append(
                {
                    "series_id": series_id,
                    "name": FRED_SERIES[series_id]["name"],
                    "category": FRED_SERIES[series_id]["category"],
                    "status": "failed",
                    "source": "FRED",
                }
            )
    records.extend(
        [
            {"series_id": "ISM_MANUFACTURING", "name": "ISM Manufacturing PMI", "category": "US demand", "status": "not_connected", "source": "ISM / FRED"},
            {"series_id": "TW_EXPORTS", "name": "Taiwan exports", "category": "Taiwan macro", "status": "not_connected", "source": "MOEA"},
            {"series_id": "TW_EXPORT_ORDERS", "name": "Taiwan export orders", "category": "Taiwan macro", "status": "not_connected", "source": "MOEA"},
            {"series_id": "TW_INDUSTRIAL_PRODUCTION", "name": "Taiwan industrial production", "category": "Taiwan macro", "status": "not_connected", "source": "MOEA"},
            {"series_id": "TW_NDC_SIGNAL", "name": "Taiwan NDC business cycle signal", "category": "Taiwan macro", "status": "not_connected", "source": "NDC"},
            {"series_id": "TW_M1B_M2", "name": "Taiwan M1B / M2", "category": "Taiwan liquidity", "status": "not_connected", "source": "CBC"},
        ]
    )
    ok_count = sum(1 for row in records if row.get("status") == "ok")
    return {
        "status": "ok" if ok_count >= len(FRED_SERIES) else "partial" if ok_count else "failed",
        "source": "FRED / ISM / MOEA / NDC / CBC",
        "date": today.isoformat(),
        "records": records,
    }


def merge_macro_fallback(macro_indicators: dict[str, object], previous_macro: dict[str, object]) -> dict[str, object]:
    previous_records = previous_macro.get("records") if isinstance(previous_macro, dict) else []
    if not isinstance(previous_records, list):
        previous_records = []
    previous_by_id = {row.get("series_id"): row for row in previous_records if isinstance(row, dict) and row.get("status") == "ok"}
    records = []
    reused = 0
    for row in macro_indicators.get("records", []):
        if isinstance(row, dict) and row.get("status") == "failed" and row.get("series_id") in previous_by_id:
            cached = dict(previous_by_id[row.get("series_id")])
            cached["status"] = "ok"
            cached["source"] = f"{cached.get('source', 'FRED')} cached fallback"
            cached["fallback_note"] = "Current FRED fetch failed; reused last successful public value."
            records.append(cached)
            reused += 1
        else:
            records.append(row)
    if reused:
        macro_indicators = dict(macro_indicators)
        macro_indicators["records"] = records
        ok_count = sum(1 for row in records if isinstance(row, dict) and row.get("status") == "ok")
        macro_indicators["status"] = "ok" if ok_count >= len(FRED_SERIES) else "partial" if ok_count else "failed"
        macro_indicators["source"] = f"{macro_indicators.get('source', 'FRED')} / cached fallback"
    return macro_indicators


def latest_record(rows: list[dict[str, object]]) -> dict[str, object] | None:
    if not rows:
        return None
    return max(rows, key=lambda item: str(item.get("date", "")))


def value_by_type(rows: list[dict[str, object]], type_name: str) -> float | None:
    for row in rows:
        if row.get("type") == type_name:
            value = row.get("value")
            return float(value) if isinstance(value, (int, float)) else parse_number(value)
    return None


def fetch_company_fundamentals(today: dt.date, stocks: list[dict[str, object]]) -> dict[str, object]:
    start_date = (today - dt.timedelta(days=550)).isoformat()
    records = []
    for stock in stocks:
        ticker = str(stock.get("ticker"))
        try:
            revenue_rows = finmind_dataset("TaiwanStockMonthRevenue", data_id=ticker, start_date=start_date)
            statement_rows = finmind_dataset("TaiwanStockFinancialStatements", data_id=ticker, start_date=start_date)
            balance_rows = finmind_dataset("TaiwanStockBalanceSheet", data_id=ticker, start_date=start_date)
        except Exception as exc:
            print(f"FinMind fundamentals fallback for {ticker}: {exc}")
            records.append({"ticker": ticker, "name": stock.get("name"), "status": "failed", "source": "FinMind", "error": str(exc)[:120]})
            continue
        latest_revenue = latest_record(revenue_rows)
        latest_statement_date = max((str(row.get("date", "")) for row in statement_rows), default="")
        latest_balance_date = max((str(row.get("date", "")) for row in balance_rows), default="")
        statement_latest = [row for row in statement_rows if str(row.get("date", "")) == latest_statement_date]
        balance_latest = [row for row in balance_rows if str(row.get("date", "")) == latest_balance_date]
        revenue = value_by_type(statement_latest, "Revenue")
        gross_profit = value_by_type(statement_latest, "GrossProfit")
        operating_income = value_by_type(statement_latest, "OperatingIncome")
        income_after_tax = value_by_type(statement_latest, "IncomeAfterTaxes")
        equity = value_by_type(balance_latest, "Equity") or value_by_type(balance_latest, "EquityAttributableToOwnersOfParent")
        records.append(
            {
                "ticker": ticker,
                "name": stock.get("name"),
                "status": "ok",
                "source": "FinMind",
                "latest_month_revenue_date": latest_revenue.get("date") if latest_revenue else None,
                "latest_month_revenue": latest_revenue.get("revenue") if latest_revenue else None,
                "latest_quarter": latest_statement_date or None,
                "eps": value_by_type(statement_latest, "EPS"),
                "gross_margin_pct": round(gross_profit / revenue * 100, 2) if revenue and gross_profit is not None else None,
                "operating_margin_pct": round(operating_income / revenue * 100, 2) if revenue and operating_income is not None else None,
                "roe_pct_annualized": round(income_after_tax / equity * 4 * 100, 2) if income_after_tax is not None and equity else None,
                "inventory": value_by_type(balance_latest, "Inventories"),
                "accounts_receivable": value_by_type(balance_latest, "AccountsReceivableNet"),
            }
        )
    ok_count = sum(1 for row in records if row.get("status") == "ok")
    return {
        "status": "ok" if ok_count == len(stocks) else "partial" if ok_count else "failed",
        "source": "FinMind TaiwanStockMonthRevenue / FinancialStatements / BalanceSheet",
        "date": today.isoformat(),
        "records": records,
    }


def read_json_file(path: Path) -> dict[str, object]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"Previous JSON fallback for {path.name}: {exc}")
    return {}


def build_market_history(today: dt.date, environment: dict[str, object], previous_market: dict[str, object], previous_history: dict[str, object]) -> dict[str, object]:
    records = previous_history.get("records") if isinstance(previous_history, dict) else []
    if not isinstance(records, list):
        records = []
    previous_environment = previous_market.get("environment") if isinstance(previous_market.get("environment"), dict) else {}
    previous_date = previous_market.get("data_date")
    previous_score = previous_environment.get("score")
    if previous_score is not None and previous_date and not any(row.get("date") == previous_date for row in records if isinstance(row, dict)):
        records.append({"date": previous_date, "score": previous_score, "status": previous_environment.get("status")})
    records = [row for row in records if isinstance(row, dict) and row.get("date")]
    records = [row for row in records if str(row.get("date")) != today.isoformat()]
    records.append({"date": today.isoformat(), "score": environment.get("score"), "status": environment.get("status")})
    dedup: dict[str, dict[str, object]] = {}
    for row in records:
        dedup[str(row.get("date"))] = row
    records = [dedup[key] for key in sorted(dedup)][-45:]
    prev_rows = [row for row in records if str(row.get("date")) < today.isoformat() and isinstance(row.get("score"), int)]
    previous_row = prev_rows[-1] if prev_rows else None
    week_cutoff = today - dt.timedelta(days=7)
    week_rows = [row for row in prev_rows if str(row.get("date")) <= week_cutoff.isoformat()]
    week_row = week_rows[-1] if week_rows else (prev_rows[0] if prev_rows else None)
    current_score = environment.get("score")
    previous_display = previous_row.get("score") if previous_row else "待歷史資料"
    weekly_display: object = "待歷史資料"
    if isinstance(current_score, int) and week_row and isinstance(week_row.get("score"), int):
        weekly_display = f"{current_score - int(week_row['score']):+d}"
    environment["previous_score"] = previous_display
    environment["weekly_change"] = weekly_display
    return {
        "status": "ok",
        "source": "Generated market_summary history",
        "records": records,
    }


def enrich_snapshot_with_macro(snapshot: list[dict[str, object]], macro_indicators: dict[str, object]) -> list[dict[str, object]]:
    dgs2 = next((row for row in macro_indicators.get("records", []) if row.get("series_id") == "DGS2" and row.get("status") == "ok"), None)
    if not dgs2 or not isinstance(dgs2.get("actual"), (int, float)):
        return snapshot
    filtered = [row for row in snapshot if row.get("label") != "美債2Y"]
    filtered.append(
        {
            "label": "美債2Y",
            "symbol": "FRED:DGS2",
            "group": "cross",
            "value": dgs2.get("actual"),
            "change_pct": None,
            "change_5d": None,
            "change_20d": dgs2.get("mom_pct"),
            "ytd_change": None,
            "high_52w_gap": None,
            "data_time": dgs2.get("date"),
            "ok": True,
        }
    )
    return filtered


def build_processed_payloads(
    *,
    today: dt.date,
    generated_at: str,
    snapshot: list[dict[str, object]],
    environment: dict[str, object],
    capital_flow: dict[str, object],
    sector_rotation: dict[str, object],
    dynamic_stock_pool: dict[str, object],
    derivatives_flow: dict[str, object],
    market_breadth: dict[str, object],
    macro_indicators: dict[str, object],
    fundamentals: dict[str, object],
    market_history: dict[str, object],
    candidates: list[dict[str, object]],
    events: list[dict[str, str]],
    analysis: dict,
    news: list[dict[str, str]],
) -> dict[str, dict[str, object]]:
    base = {
        "generated_at": generated_at,
        "data_date": today.isoformat(),
        "timezone": "Asia/Taipei",
        "status": "ok",
    }
    market_rows = serializable_snapshot(snapshot)
    stock_rows = [
        {
            "ticker": item.get("ticker"),
            "name": item.get("name"),
            "bucket": item.get("bucket"),
            "candidate_source": item.get("candidate_source"),
            "score": item.get("score"),
            "theme_score": item.get("theme_score"),
            "flow_score": item.get("flow_score"),
            "technical_score": item.get("technical_score"),
            "data_quality_score": item.get("data_quality_score"),
            "flow_text": item.get("flow_text"),
            "flow_warning": item.get("flow_warning"),
            "technical_text": item.get("technical_text"),
            "inclusion_reason": item.get("inclusion_reason"),
            "change_history": item.get("change_history"),
            "data_source": item.get("data_source"),
            "risk": item.get("risk"),
            "action": item.get("action"),
        }
        for item in candidates
    ]
    data_health_rows = [
        {
            "dataset": "market_summary",
            "status": "ok" if all(row["status"] == "ok" for row in market_rows if row["group"] == "global") else "partial",
            "source": "Yahoo Finance",
            "last_successful_update": generated_at,
        },
        {
            "dataset": "market_history",
            "status": market_history.get("status", "failed"),
            "source": market_history.get("source", "Generated market_summary history"),
            "last_successful_update": generated_at if market_history.get("status") == "ok" else None,
        },
        {
            "dataset": "stock_radar",
            "status": "ok" if stock_rows else "empty",
            "source": "Google News RSS / TWSE / Yahoo Finance",
            "last_successful_update": generated_at,
        },
        {
            "dataset": "dynamic_stock_pool",
            "status": dynamic_stock_pool.get("status", "failed"),
            "source": dynamic_stock_pool.get("source", "TWSE T86"),
            "last_successful_update": generated_at if dynamic_stock_pool.get("status") == "ok" else None,
        },
        {
            "dataset": "capital_flow",
            "status": capital_flow.get("status", "failed"),
            "source": capital_flow.get("source", "TWSE BFI82U"),
            "last_successful_update": generated_at if capital_flow.get("status") == "ok" else None,
        },
        {
            "dataset": "sector_rotation",
            "status": sector_rotation.get("status", "failed"),
            "source": sector_rotation.get("source", "TWSE MI_INDEX"),
            "last_successful_update": generated_at if sector_rotation.get("status") == "ok" else None,
        },
        {
            "dataset": "derivatives_flow",
            "status": derivatives_flow.get("status", "failed"),
            "source": derivatives_flow.get("source", "TAIFEX"),
            "last_successful_update": generated_at if derivatives_flow.get("status") in ("ok", "partial") else None,
        },
        {
            "dataset": "market_breadth_margin_lending",
            "status": market_breadth.get("status", "failed"),
            "source": market_breadth.get("source", "TWSE / TPEx"),
            "last_successful_update": generated_at if market_breadth.get("status") in ("ok", "partial") else None,
        },
        {
            "dataset": "us_macro_actual_forecast",
            "status": macro_indicators.get("status", "failed"),
            "source": macro_indicators.get("source", "FRED / official macro sources"),
            "last_successful_update": generated_at if macro_indicators.get("status") in ("ok", "partial") else None,
        },
        {
            "dataset": "taiwan_macro_actual_forecast",
            "status": "not_connected",
            "source": "DGBAS / MOEA / NDC / CBC / TWSE",
            "last_successful_update": None,
        },
        {
            "dataset": "company_fundamentals",
            "status": fundamentals.get("status", "failed"),
            "source": fundamentals.get("source", "MOPS / TWSE / TPEx / FinMind"),
            "last_successful_update": generated_at if fundamentals.get("status") in ("ok", "partial") else None,
        },
    ]
    return {
        "market_summary.json": {**base, "source": "Yahoo Finance / Google News RSS", "environment": environment, "markets": market_rows},
        "market_history.json": {**base, "source": "Generated market_summary history", **market_history},
        "capital_flow.json": {**base, "source": "TWSE BFI82U", **capital_flow},
        "dynamic_stock_pool.json": {**base, "source": "TWSE T86", **dynamic_stock_pool},
        "stock_radar.json": {**base, "source": "Google News RSS / TWSE / Yahoo Finance", "records": stock_rows},
        "economic_calendar.json": {**base, "source": "Manual P0 template; official calendar API pending", "records": events},
        "sector_rotation.json": {**base, "source": "TWSE MI_INDEX", **sector_rotation, "news_axes": analysis.get("axes", [])},
        "derivatives_flow.json": {**base, "source": "TAIFEX", **derivatives_flow},
        "market_breadth.json": {**base, "source": "TWSE / TPEx / FinMind", **market_breadth},
        "macro_indicators.json": {**base, "source": "FRED / ISM / MOEA / NDC / CBC", **macro_indicators},
        "fundamentals.json": {**base, "source": "FinMind / MOPS-derived public data", **fundamentals},
        "data_health.json": {**base, "source": "GitHub Actions pipeline", "records": data_health_rows, "news_count": len(news)},
    }


def write_processed_payloads(output_dir: Path, payloads: dict[str, dict[str, object]]) -> None:
    processed_dir = output_dir / "data" / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    for filename, payload in payloads.items():
        (processed_dir / filename).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def render_html(
    news: list[dict[str, str]],
    today: dt.date,
    previous_html: str = "",
    previous_market: dict[str, object] | None = None,
    previous_history: dict[str, object] | None = None,
    previous_macro: dict[str, object] | None = None,
) -> str:
    global LAST_PROCESSED_PAYLOADS
    generated_at = dt.datetime.now(TW).strftime("%Y-%m-%d %H:%M:%S Asia/Taipei")
    previous = extract_previous_analysis(previous_html)
    analysis = build_market_analysis(news, today, previous)
    snapshot = market_snapshot()
    capital_flow = fetch_taiwan_capital_flow(today)
    sector_rotation = fetch_twse_sector_rotation(today)
    dynamic_candidates, dynamic_stock_pool = dynamic_candidate_pool(today, {str(item["ticker"]) for item in CORE_STOCK_UNIVERSE})
    core_tickers = [str(item["ticker"]) for item in CORE_STOCK_UNIVERSE]
    derivatives_flow = fetch_derivatives_flow(today)
    market_breadth = fetch_market_breadth(today, core_tickers)
    macro_indicators = fetch_macro_indicators(today)
    macro_indicators = merge_macro_fallback(macro_indicators, previous_macro or {})
    snapshot = enrich_snapshot_with_macro(snapshot, macro_indicators)
    fundamentals = fetch_company_fundamentals(today, CORE_STOCK_UNIVERSE)
    temperature = market_temperature(snapshot)
    environment = market_environment(snapshot, news, capital_flow)
    market_history = build_market_history(today, environment, previous_market or {}, previous_history or {})
    all_candidates = score_candidates(news, analysis, today, dynamic_candidates)
    candidates = priority_candidates(all_candidates)
    events = enrich_event_calendar(event_calendar(today), macro_indicators)
    LAST_PROCESSED_PAYLOADS = build_processed_payloads(
        today=today,
        generated_at=generated_at,
        snapshot=snapshot,
        environment=environment,
        capital_flow=capital_flow,
        sector_rotation=sector_rotation,
        dynamic_stock_pool=dynamic_stock_pool,
        derivatives_flow=derivatives_flow,
        market_breadth=market_breadth,
        macro_indicators=macro_indicators,
        fundamentals=fundamentals,
        market_history=market_history,
        candidates=candidates,
        events=events,
        analysis=analysis,
        news=news,
    )
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
  <title>台股投資決策儀表板 - {today:%Y-%m-%d}</title>
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
    .top-nav {{ position: sticky; top: 0; z-index: 5; background: rgba(255,255,255,.96); border-bottom: 1px solid var(--line); }}
    .top-nav div {{ width: min(1240px, 92vw); margin: 0 auto; display: flex; gap: 8px; overflow-x: auto; padding: 10px 0; }}
    .top-nav a {{ flex: 0 0 auto; color: var(--ink); border: 1px solid var(--line); border-radius: 999px; padding: 7px 11px; font-size: 13px; background: #fff; }}
    .regime-panel {{ display: grid; grid-template-columns: 280px minmax(0, 1fr); gap: 16px; margin-bottom: 22px; }}
    .regime-score, .regime-detail, .market-card, .asset-card, .capital-card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 18px; }}
    .regime-score {{ display: grid; place-items: center; text-align: center; background: #10263d; color: #fff; min-height: 260px; }}
    .regime-score span {{ color: #c8d8e8; }}
    .regime-score strong {{ font-size: 72px; line-height: 1; }}
    .regime-score em {{ font-style: normal; font-size: 22px; font-weight: 800; }}
    .status-grid, .score-grid, .factor-grid, .market-grid, .asset-grid, .capital-grid {{ display: grid; gap: 12px; }}
    .status-grid {{ grid-template-columns: repeat(4, minmax(0, 1fr)); margin-bottom: 14px; }}
    .status-grid div, .score-row {{ border: 1px solid var(--line); border-radius: 8px; padding: 12px; background: #f8fafb; }}
    .status-grid span, .score-row span, .capital-card span {{ display: block; color: var(--muted); font-size: 12px; }}
    .score-grid {{ grid-template-columns: repeat(5, minmax(0, 1fr)); }}
    .score-row {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; align-items: start; }}
    .score-row strong {{ font-size: 28px; color: var(--teal); }}
    .factor-grid {{ grid-template-columns: 1.2fr .8fr; margin-top: 14px; }}
    .factor-grid h3 {{ margin-top: 0; }}
    .market-grid {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
    .asset-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    .capital-grid {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
    .card-head {{ display: flex; align-items: start; justify-content: space-between; gap: 12px; }}
    .card-head h3 {{ margin: 0; }}
    .card-head span {{ color: var(--muted); font-size: 12px; }}
    .market-card strong, .asset-card strong, .capital-card strong {{ display: block; margin: 8px 0 10px; font-size: 24px; }}
    .market-card dl {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px 10px; margin: 0; }}
    .market-card dt {{ font-size: 12px; color: var(--muted); }}
    .market-card dd {{ margin: 0; }}
    .mini-trend {{ display: flex; flex-wrap: wrap; gap: 8px; color: var(--muted); font-size: 13px; }}
    .tag, .importance {{ border-radius: 999px; padding: 4px 9px; border: 1px solid var(--line); background: #eef3f5; color: #34485a; font-size: 12px; font-weight: 800; }}
    .tag.偏多 {{ background: #e8f4ee; color: var(--green); }} .tag.偏空, .tag.警戒 {{ background: #f8eaea; color: var(--red); }}
    .importance.高 {{ background: #f8eaea; color: var(--red); }} .importance.中 {{ background: #fff4df; color: var(--amber); }}
    .research-note {{ background: #fff; border-left: 4px solid var(--teal); padding: 14px 16px; color: #334454; }}
    .footer {{ color: var(--muted); font-size: 13px; margin-top: 20px; border-top: 1px solid var(--line); padding-top: 14px; }}
    @media (max-width: 920px) {{ .metrics, .quotes, .themes, .impact-grid, .events, .regime-panel, .status-grid, .score-grid, .factor-grid, .market-grid, .asset-grid, .capital-grid {{ grid-template-columns: 1fr; }} .news-row {{ grid-template-columns: 1fr; }} table {{ display: block; overflow-x: auto; }} .regime-score strong {{ font-size: 56px; }} }}
  </style>
</head>
<body>
  <header>
    <div class="hero">
      <div class="kicker">JT Investment Dashboard | {generated_at}</div>
      <h1>{today:%Y-%m-%d} 台股投資決策儀表板</h1>
      <p>以台股為核心，整合美國與台灣總經、全球跨資產、法人籌碼、產業趨勢與個股研究。今日主軸：{html.escape(dominant_label)}。</p>
      <div class="meta">
        <span class="pill">市場環境：{environment["score"]} / 100｜{html.escape(str(environment["status"]))}</span>
        <span class="pill">新聞：{len(news)} 則</span>
        <span class="pill">來源：{source_count} 個</span>
        <span class="pill">資料狀態：收盤/延遲資料</span>
      </div>
    </div>
  </header>
  <nav class="top-nav" aria-label="Dashboard sections">
    <div>
      <a href="#dashboard">首頁 Market Dashboard</a>
      <a href="#market-pulse">全球市場</a>
      <a href="#us-macro">US Macro</a>
      <a href="#taiwan-macro">Taiwan Macro</a>
      <a href="#capital-flow">台股資金法人</a>
      <a href="#sector">產業追蹤</a>
      <a href="#stock-radar">Stock Radar</a>
      <a href="#calendar">事件行事曆</a>
      <a href="#methodology">方法論</a>
    </div>
  </nav>
  <main>
    {render_market_environment(environment, generated_at)}

    <div class="section-title" id="market-pulse"><h2>全球市場 Market Pulse</h2><p>不只看單日漲跌，同時呈現 5 日、20 日、YTD 與距 52 週高點。</p></div>
    <section class="market-grid">{render_global_market_cards(snapshot)}</section>

    <div class="section-title"><h2>跨資產與風險雷達</h2><p>利率、美元、匯率、VIX、商品是台股資金與估值的重要背景。</p></div>
    <section class="asset-grid">{render_cross_asset_cards(snapshot)}</section>

    <div class="section-title" id="calendar"><h2>未來 72 小時重大事件</h2><p>高重要性事件會優先影響利率、匯率、台股資金與高估值科技股。</p></div>
    <table>
      <thead><tr><th>時間</th><th>國家</th><th>事件</th><th>前值</th><th>市場預期</th><th>重要性</th><th>對台股影響</th></tr></thead>
      <tbody>{render_event_calendar(events)}</tbody>
    </table>

    <div class="section-title" id="capital-flow"><h2>台股資金與法人 Taiwan Capital Flow</h2><p>先顯示已接資料與待接資料源，避免用舊資料或假資料冒充即時資料。</p></div>
    <section class="capital-grid">{render_capital_flow_summary(candidates, snapshot, capital_flow)}{render_derivatives_and_breadth(derivatives_flow, market_breadth)}</section>

    <div class="section-title" id="stock-radar"><h2>四層選股 Stock Radar</h2><p>核心追蹤池僅保留鴻海、富邦金、台積電、台達電；其他標的須由 TWSE T86 動態法人雷達進入候選。</p></div>
    <table>
      <thead><tr><th>分層</th><th>股票</th><th>總分</th><th>題材</th><th>法人籌碼</th><th>技術</th><th>今日籌碼</th><th>進榜原因 / 異動紀錄</th><th>主要風險 / 資料來源</th><th>升級/維持原因</th></tr></thead>
      <tbody>{render_candidate_table(candidates)}</tbody>
    </table>
    <p class="research-note">Macro Regime Score：{environment["score"]} / 100。{html.escape(str(environment["implication"]))} 個股分數仍由基本面、法人籌碼、技術面、估值、產業趨勢與風險扣分獨立計算；市場環境只作為升級/降級門檻。</p>

    <div class="section-title" id="sector"><h2>產業追蹤 Sector Rotation</h2><p>優先顯示 TWSE 產業指數當日輪動；後續再補 5/20/60 日報酬、法人買賣超與營收年增率。</p></div>
    <section class="themes">{render_sector_rotation(sector_rotation, news, analysis)}</section>

    <div class="section-title"><h2>投資影響卡</h2><p>每則重點新聞對應受惠族群、台股標的與驗證指標。</p></div>
    <section class="impact-grid">{render_impact_cards(news)}</section>

    <div class="section-title" id="us-macro"><h2>美國總經 US Macro</h2><p>FRED 已接 CPI、PCE、非農、零售銷售、Fed Funds、2Y/10Y；ISM 與市場預期/Surprise 仍保留待接狀態。</p></div>
    <section class="panel">{render_macro_table(macro_indicators, "US")}</section>

    <div class="section-title" id="taiwan-macro"><h2>台灣總經 Taiwan Macro</h2><p>台灣出口、外銷訂單、工業生產、景氣燈號、M1B/M2 已列入資料層契約；待官方端點驗證後逐一接入。</p></div>
    <section class="panel">{render_macro_table(macro_indicators, "Taiwan")}</section>

    <div class="section-title"><h2>核心股基本面 Company Fundamentals</h2><p>核心追蹤池月營收、EPS、毛利率、ROE、存貨、應收帳款先以 FinMind 公開資料接入；金融股部分財報欄位可能不適用，會顯示待接資料源。</p></div>
    <section class="panel">{render_fundamentals_table(fundamentals)}</section>

    <div class="section-title"><h2>重要新聞清單</h2><p>保留來源連結，避免只留下摘要文字。</p></div>
    <section class="news-list">{render_news_list(news)}</section>

    <div class="section-title" id="methodology"><h2>方法論與資料來源 Methodology</h2><p>所有分數、篩選結果與資料狀態必須可追溯。</p></div>
    <section class="panel">
      <p><b>資料來源：</b>Yahoo Finance 公開行情、Google News RSS、TWSE BFI82U 三大法人買賣金額、TWSE MI_INDEX 產業指數、TWSE 個股三大法人公開資料；MOPS、TPEx、FinMind、台灣主計總處、經濟部、國發會、央行、美國官方總經資料為 P1/P2 接入項目。</p>
      <p><b>資料層：</b>GitHub Actions 會同步產生 <code>data/processed/market_summary.json</code>、<code>market_history.json</code>、<code>capital_flow.json</code>、<code>dynamic_stock_pool.json</code>、<code>stock_radar.json</code>、<code>economic_calendar.json</code>、<code>sector_rotation.json</code>、<code>derivatives_flow.json</code>、<code>market_breadth.json</code>、<code>macro_indicators.json</code>、<code>fundamentals.json</code> 與 <code>data_health.json</code>。後續新增資料源一律先寫入 processed JSON，再由網站與 Telegram 摘要讀取。</p>
      <p><b>資料品質規則：</b>抓取失敗時顯示資料更新失敗或待接資料源，不顯示假資料；每個主要區塊保留更新時間與資料狀態；市場環境分數會因必要資料不足而扣分。</p>
      <p><b>風險揭露：</b>本網站僅提供公開資料整理、量化篩選與研究輔助，不構成買賣建議或保證獲利。投資人應自行評估風險。</p>
    </section>

    <div class="section-title"><h2>資料來源分布</h2><p>來源分散度會影響信號品質分數，目前信號品質 {signal_score}，主題分散度 {theme_count}。</p></div>
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
    previous_market = read_json_file(output_dir / "data" / "processed" / "market_summary.json")
    previous_history = read_json_file(output_dir / "data" / "processed" / "market_history.json")
    previous_macro = read_json_file(output_dir / "data" / "processed" / "macro_indicators.json")
    news = collect_news(today)
    if len(news) < MIN_NEWS_ITEMS:
        raise RuntimeError(
            f"Refusing to publish an incomplete report: got {len(news)} news items, "
            f"need at least {MIN_NEWS_ITEMS}"
        )
    (output_dir / "index.html").write_text(
        render_html(
            news,
            today,
            previous_html,
            previous_market=previous_market,
            previous_history=previous_history,
            previous_macro=previous_macro,
        ),
        encoding="utf-8",
    )
    write_processed_payloads(output_dir, LAST_PROCESSED_PAYLOADS)
    print(f"Published dashboard finance report for {today:%Y-%m-%d} with {len(news)} items to {output_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
