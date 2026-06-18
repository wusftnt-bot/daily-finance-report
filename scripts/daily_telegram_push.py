from __future__ import annotations

import csv
import datetime as dt
import email.utils
import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET


def compact_secret(name: str) -> str:
    return "".join(os.environ[name].split())


def optional_secret(name: str) -> str:
    return "".join(os.environ.get(name, "").split())


CHAT_ID = optional_secret("TELEGRAM_CHAT_ID")
FINANCE_TOKEN = optional_secret("TELEGRAM_FINANCE_BOT_TOKEN")
FOXCONN_TOKEN = optional_secret("TELEGRAM_FOXCONN_BOT_TOKEN")
GEMINI_API_KEY = optional_secret("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
DAILY_FINANCE_REPORT_URL = os.environ.get("DAILY_FINANCE_REPORT_URL", "https://wusftnt-bot.github.io/daily-finance-report/")
TW = dt.timezone(dt.timedelta(hours=8))
PUSHED_NEWS_KEYS: set[str] = set()
MARKET_STATE_RULES = """
Market-state and time-order rules:
- Apply these rules to every named sector, company, ETF, index, commodity, and risk theme. Risk reminders and Taiwan-stock observation points must also obey them.
- Build every Taiwan-market interpretation from three layers in this order: (1) latest relevant news timestamp, (2) prior Taiwan close for Taiwan stocks/sectors, and (3) overnight US market, ADR, futures, sector ETF, commodity, or peer-stock moves. The newest market-state evidence overrides older theme narratives.
- If overnight US peers, ADRs, futures, or sector ETFs have sold off, do not publish a bullish Taiwan-sector conclusion just because an older Taiwan article mentioned recent gains. Reframe it as pre-market downside risk, theme/price divergence, or short-term momentum deterioration.
- For every named Taiwan stock or sector, state whether the evidence is pre-market, prior-close, intraday, or after-hours. Before the Taiwan market opens, do not write as if today's Taiwan session has already risen or fallen.
- Never use bullish Traditional Chinese wording equivalent to large gains, strong capital inflow, strong momentum, taking over the rally, or bull trend continuation when latest evidence shows a sharp drop, downgrade, weak guidance, ADR/US peer selloff, futures pressure, or broad peer weakness.
- If a narrative conflicts with the latest price/action evidence, label it as theme-price divergence, turned from strong to weak, or pre-market risk rising, and add risk language.
- Examples: if US memory, AI server, semiconductor equipment, energy, financial, or tech peers fall sharply overnight, the Taiwan observation must describe pre-market pressure rather than using old theme news alone.
"""


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "JT-PM daily telegram bot"})
    with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context()) as res:
        return json.loads(res.read().decode("utf-8-sig"))


def fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "JT-PM daily telegram bot"})
    with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context()) as res:
        return res.read().decode("utf-8", errors="replace")


def telegram_chunks(text: str, limit: int = 3900) -> list[str]:
    text = text.strip()
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit)
        if cut < 1200:
            cut = remaining.rfind("\n", 0, limit)
        if cut < 1200:
            cut = limit
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def send_telegram(token: str, text: str, label: str) -> list[dict]:
    receipts: list[dict] = []
    chunks = telegram_chunks(text)
    for chunk_index, chunk in enumerate(chunks, start=1):
        data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": chunk}).encode("utf-8")
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context()) as res:
            body = json.loads(res.read().decode("utf-8"))
            if not body.get("ok"):
                raise RuntimeError(body)
            result = body.get("result", {})
            receipts.append({
                "label": label,
                "chunk_index": chunk_index,
                "chunk_count": len(chunks),
                "ok": True,
                "message_id": result.get("message_id"),
                "date": result.get("date"),
                "chat_id": str(result.get("chat", {}).get("id", CHAT_ID)),
            })
    return receipts


def write_delivery_receipt(today: dt.date, receipts: list[dict]) -> None:
    if len(receipts) < 3:
        raise RuntimeError(f"Expected at least 3 Telegram receipts, got {len(receipts)}")
    sent_dir = os.environ.get("TELEGRAM_SENT_DIR", ".sent/telegram")
    os.makedirs(sent_dir, exist_ok=True)
    payload = {
        "date": today.isoformat(),
        "sent_at_taipei": dt.datetime.now(TW).isoformat(),
        "expected_messages": ["daily_finance", "technology_news", "jt_dashboard"],
        "receipt_count": len(receipts),
        "receipts": receipts,
    }
    path = os.path.join(sent_dir, f"daily-telegram-{today.isoformat()}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"Wrote Telegram delivery receipt: {path}")


def call_gemini(prompt: str, fallback: str, max_tokens: int = 1200) -> str:
    if not GEMINI_API_KEY:
        return fallback + "\n- AI 判讀：尚未設定 GEMINI_API_KEY，目前先使用規則式觀察。"
    payload = {
        "system_instruction": {
            "parts": [{"text": "你是台灣投資研究助理。請用繁體中文整理國際財經新聞。所有新聞重點與重要性說明都必須翻譯成繁體中文；只允許新聞來源名稱、公司名、產品名、股票代號保留英文。避免空泛分類，不要保證獲利，不要稱為個人化投資建議。"}]
        },
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.25, "maxOutputTokens": max_tokens},
    }
    url = "https://generativelanguage.googleapis.com/v1beta/models/" + urllib.parse.quote(GEMINI_MODEL) + ":generateContent"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=60, context=ssl.create_default_context()) as res:
            data = json.loads(res.read().decode("utf-8"))
        parts: list[str] = []
        for candidate in data.get("candidates", []):
            content = candidate.get("content", {})
            for part in content.get("parts", []):
                text = part.get("text")
                if text:
                    parts.append(text)
        return "\n".join(parts).strip() or fallback
    except urllib.error.HTTPError as exc:
        print(f"Gemini HTTPError {exc.code}: " + exc.read().decode("utf-8", errors="replace")[:300])
        return fallback
    except Exception as exc:
        print(f"Gemini error: {type(exc).__name__}: {exc}")
        return fallback


def roc_to_iso(value: str) -> str:
    year, month, day = [int(part) for part in value.split("/")]
    return f"{year + 1911:04d}-{month:02d}-{day:02d}"


def twse_stock_month(stock_no: str, today: dt.date) -> list[list[str]]:
    url = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?" + urllib.parse.urlencode({"response": "json", "date": today.strftime("%Y%m%d"), "stockNo": stock_no})
    data = fetch_json(url)
    return data.get("data") or []


def twse_institutional(day: dt.date, stock_no: str) -> list[str] | None:
    url = "https://www.twse.com.tw/rwd/zh/fund/T86?" + urllib.parse.urlencode({"response": "csv", "date": day.strftime("%Y%m%d"), "selectType": "ALLBUT0999"})
    text = fetch_text(url)
    for row in csv.reader(text.splitlines()):
        clean = [cell.strip().strip('="') for cell in row]
        if len(clean) > 18 and clean[0] == stock_no:
            return clean
    return None


def number(value: str) -> float:
    return float(value.replace(",", "").replace("--", "0").strip() or 0)


def lots(value: str) -> int:
    return round(number(value) / 1000)


def latest_institutional_rows(today: dt.date, stock_no: str, limit: int = 5) -> list[tuple[dt.date, list[str]]]:
    rows: list[tuple[dt.date, list[str]]] = []
    for offset in range(0, 14):
        day = today - dt.timedelta(days=offset)
        try:
            row = twse_institutional(day, stock_no)
        except Exception:
            row = None
        if row:
            rows.append((day, row))
        if len(rows) >= limit:
            break
    if not rows:
        raise RuntimeError(f"TWSE institutional data unavailable for {stock_no}")
    return list(reversed(rows))


ALLOWED_FINANCE_SOURCES = [
    "WSJ",
    "The Wall Street Journal",
    "Wall Street Journal",
    "Financial Times",
    "The Economist",
    "Bloomberg",
    "Reuters",
    "ECB",
    "European Central Bank",
    "Investing.com",
    "TradingView",
    "CNBC",
    "Seeking Alpha",
    "Yahoo Finance",
    "Reddit",
    "WallStreetBets",
    "MacroMicro",
    "財經M平方",
    "鉅亨網",
    "Anue",
]

EXCLUDED_MARKET_TERMS = [
    "china", "chinese stocks", "hong kong", "hk stocks", "h-shares", "a-shares", "hang seng",
    "中國", "陸股", "港股", "香港", "恒生", "恆生", "滬深", "上證", "深證", "A股", "H股",
    "人民幣", "中概股", "阿里巴巴", "騰訊", "百度", "京東", "美團", "小米",
    "byd", "nio", "li auto", "xpeng", "geely", "catl", "比亞迪", "蔚來", "理想汽車", "小鵬", "吉利", "寧德時代",
    "baidu", "alibaba", "tencent", "jd.com", "meituan", "netease", "bilibili", "pdd",
]

SOURCE_QUERIES = [
    ("Bloomberg", "site:bloomberg.com (Nvidia OR TSMC OR Apple OR Microsoft OR Nasdaq OR Fed OR Treasury OR Taiwan semiconductor)"),
    ("Reuters", "site:reuters.com/markets (oil OR Fed OR Treasury OR Nasdaq OR Nvidia OR TSMC OR stocks)"),
    ("ECB", "site:ecb.europa.eu financial stability OR markets OR bonds OR risks"),
    ("CNBC", "site:cnbc.com (Nvidia OR Apple OR Microsoft OR Nasdaq OR S&P 500 OR Fed OR Treasury)"),
    ("Yahoo Finance", "site:finance.yahoo.com (NVDA OR TSM OR AAPL OR MSFT OR AMD OR Nasdaq OR S&P 500)"),
    ("Seeking Alpha", "site:seekingalpha.com (NVDA OR TSM OR AAPL OR MSFT OR AMD OR semiconductor OR earnings)"),
    ("Investing.com", "site:investing.com (Nvidia OR TSMC OR Nasdaq OR S&P 500 OR Fed OR Treasury OR US stocks)"),
    ("WSJ", "site:wsj.com (Nvidia OR TSMC OR Wall Street OR Nasdaq OR Fed OR Treasury)"),
    ("Financial Times", "site:ft.com (Nvidia OR TSMC OR Wall Street OR Fed OR Treasury OR semiconductor)"),
    ("The Economist", "site:economist.com (US economy OR technology OR semiconductors OR markets)"),
    ("MacroMicro", "site:macromicro.me (Fed OR CPI OR 美債 OR 台股 OR 台積電 OR 半導體 OR 景氣)"),
    ("鉅亨網", "site:news.cnyes.com (美股 OR 台股 OR 台積電 OR 輝達 OR 聯準會 OR 美債 OR 半導體)"),
    ("Reddit WallStreetBets", "site:reddit.com/r/wallstreetbets (NVDA OR TSLA OR AMD OR PLTR OR AAPL OR MSFT)"),
    ("TradingView", "site:tradingview.com/news (NVDA OR TSM OR AAPL OR MSFT OR AMD OR Nasdaq OR S&P 500)")
]


def google_news_items(query: str, today: dt.date, limit: int = 12, max_age_days: int = 2) -> list[dict[str, str]]:
    cutoff = today - dt.timedelta(days=max_age_days - 1)
    query_with_time = f"({query}) when:{max_age_days}d"
    params = urllib.parse.urlencode({"q": query_with_time, "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"})
    xml_text = fetch_text(f"https://news.google.com/rss/search?{params}")
    root = ET.fromstring(xml_text)
    items: list[dict[str, str]] = []
    for item in root.findall(".//item")[:limit]:
        title = item.findtext("title") or ""
        pub_raw = item.findtext("pubDate") or ""
        link = item.findtext("link") or ""
        if not title:
            continue
        try:
            published = email.utils.parsedate_to_datetime(pub_raw)
            if published.tzinfo is None:
                published = published.replace(tzinfo=dt.timezone.utc)
            published_tw = published.astimezone(TW)
            published_date = published_tw.date()
        except Exception:
            published_date = today
        if published_date < cutoff or published_date > today:
            continue
        source, headline = parse_source(title)
        if not is_allowed_finance_source(source, headline):
            continue
        if is_excluded_market_news(source, headline):
            continue
        if not is_relevant_tw_us_market(source, headline):
            continue
        if is_stale_or_low_signal(headline, today):
            continue
        items.append({"source": normalize_source_name(source, headline), "headline": headline, "published": published_date.isoformat(), "link": link})
    return items


def source_note() -> str:
    return "固定來源定義：WSJ、Financial Times、The Economist、Bloomberg、Reuters；Investing.com、TradingView、CNBC；Seeking Alpha、Yahoo Finance 討論區、Reddit WallStreetBets；MacroMicro、鉅亨網。投資主軸為台股與美股；非上述來源、中國/港股相關股票新聞與香港財經網站一律排除。"


def public_finance_page_status(today: dt.date) -> tuple[bool, str]:
    expected = today.isoformat()
    stale_dates = {(today - dt.timedelta(days=offset)).isoformat() for offset in range(1, 4)}
    base = DAILY_FINANCE_REPORT_URL
    sep = "&" if "?" in base else "?"
    urls = [base, f"{base}{sep}cb={int(dt.datetime.now(TW).timestamp())}"]
    results: list[str] = []
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "daily-telegram-date-check", "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as response:
                html = response.read().decode("utf-8", errors="replace")
            if expected not in html:
                stale_found = sorted(date for date in stale_dates if date in html)
                results.append(f"{url} missing {expected}; stale={stale_found or 'none'}")
                continue
            results.append(f"{url} ok")
        except Exception as exc:
            results.append(f"{url} error={exc}")
    ok = all(item.endswith(" ok") for item in results)
    return ok, "; ".join(results)


def parse_source(title: str) -> tuple[str, str]:
    if " - " in title:
        headline, source = title.rsplit(" - ", 1)
        return source.strip(), headline.strip()
    return "Google News", title.strip()


def normalize_source_name(source: str, headline: str) -> str:
    combined = f"{source} {headline}".lower()
    if "wall street journal" in combined or source.lower() == "wsj":
        return "WSJ"
    if "financial times" in combined or source.lower() == "ft":
        return "Financial Times"
    if "economist" in combined:
        return "The Economist"
    if "bloomberg" in combined:
        return "Bloomberg"
    if "reuters" in combined:
        return "Reuters"
    if "ecb" in combined or "european central bank" in combined:
        return "ECB"
    if "investing.com" in combined:
        return "Investing.com"
    if "tradingview" in combined:
        return "TradingView"
    if "cnbc" in combined:
        return "CNBC"
    if "seeking alpha" in combined:
        return "Seeking Alpha"
    if "yahoo finance" in combined:
        return "Yahoo Finance"
    if "wallstreetbets" in combined or "reddit" in combined:
        return "Reddit WallStreetBets"
    if "macromicro" in combined or "財經m平方" in combined:
        return "MacroMicro"
    if "鉅亨" in combined or "anue" in combined or "cnyes" in combined:
        return "鉅亨網"
    return source


def is_allowed_finance_source(source: str, headline: str) -> bool:
    combined = f"{source} {headline}".lower()
    allowed = [keyword.lower() for keyword in ALLOWED_FINANCE_SOURCES]
    return any(keyword in combined for keyword in allowed)


def has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def normalize_headline(text: str) -> str:
    lowered = text.lower()
    lowered = re.sub(r"https?://\S+", " ", lowered)
    lowered = re.sub(r"[0-9０-９]+", "#", lowered)
    lowered = re.sub(r"[^a-z\u4e00-\u9fff#]+", "", lowered)
    stopwords = ["新聞", "財經", "即時", "分析", "評論", "日報", "重點", "快訊", "市場", "投資"]
    for word in stopwords:
        lowered = lowered.replace(word, "")
    return lowered[:80]


def is_relevant_tw_us_market(source: str, headline: str) -> bool:
    combined = f"{source} {headline}".lower()
    relevant_terms = [
        "taiwan", "taiwan stocks", "twse", "taiex", "tsmc", "tsm", "台股", "台積電", "鴻海", "聯發科",
        "us stocks", "wall street", "nasdaq", "s&p 500", "dow", "美股", "那斯達克", "標普",
        "nvidia", "nvda", "amd", "broadcom", "avgo", "apple", "aapl", "microsoft", "msft", "tesla", "tsla",
        "ai", "semiconductor", "chip", "server", "半導體", "晶片", "輝達", "ai伺服器",
        "fed", "fomc", "powell", "central bank independence", "fed independence", "treasury", "yield", "cpi", "pce", "rate cut", "rate hike", "oil", "crude", "iran", "middle east", "energy", "ecb", "financial stability", "computex", "ai pc", "windows pc", "arm pc", "qualcomm", "robotics", "edge ai", "ai agent", "ai server", "networking", "switch", "optical", "taiwan dollar", "foreign inflow", "market cap", "twse", "taiex", "聯準會", "鮑爾", "央行獨立", "美債", "殖利率", "通膨", "降息", "升息", "油價", "原油", "伊朗", "中東", "能源", "金融穩定", "台北國際電腦展", "台北電腦展", "computex", "ai pc", "ai伺服器", "網通", "交換器", "光通訊", "機器人", "邊緣運算", "台幣", "新台幣", "外資", "台股市值", "證交所"
    ]
    return any(term in combined for term in relevant_terms)


def is_excluded_market_news(source: str, headline: str) -> bool:
    combined = f"{source} {headline}".lower()
    return any(term.lower() in combined for term in EXCLUDED_MARKET_TERMS)


def is_stale_or_low_signal(headline: str, today: dt.date) -> bool:
    text = headline.lower()
    stale_patterns = [r"20[0-2][0-5]", r"2026[./-]0?[1-4][./-]", r"5[./-](1[0-9]|2[0-5])", r"5月(1[0-9]|2[0-5])", r"q1|第一季|第1季"]
    if any(re.search(pattern, text) for pattern in stale_patterns):
        return True
    low_signal_terms = ["懶人包", "一文看懂", "回顧", "總整理", "教學", "入門", "可以買嗎", "泡沫行情會崩盤嗎"]
    return any(term.lower() in text for term in low_signal_terms)


def relevance_score(item: dict[str, str], today: dt.date) -> int:
    headline = item["headline"].lower()
    score = 0
    if item.get("published") == today.isoformat():
        score += 8
    important_terms = ["fed", "聯準會", "鮑爾", "央行獨立", "fomc", "pce", "cpi", "通膨", "降息", "升息", "利率", "殖利率", "美債", "nvidia", "輝達", "台積電", "tsmc", "ai", "半導體", "晶片", "nasdaq", "那斯達克", "oil", "原油", "brent", "wti", "iran", "middle east", "ecb", "financial stability", "美元", "新台幣", "台幣", "外資", "黃金", "bitcoin", "比特幣", "關稅", "貿易", "金融穩定", "computex", "台北電腦展", "台北國際電腦展", "ai pc", "arm pc", "windows pc", "qualcomm", "amd", "ai server", "ai伺服器", "網通", "交換器", "光通訊", "機器人", "邊緣運算", "台股市值", "證交所", "twse", "taiex", "market cap", "foreign inflow"]
    score += sum(2 for term in important_terms if term in headline)
    if any(term in headline for term in ["分析師", "專家", "看好", "怎麼買", "可以買"]):
        score -= 3
    return score


def chinese_market_note(headline: str) -> str:
    lower = headline.lower()
    if any(key in lower for key in ["fed", "rate", "yield", "treasury", "powell", "利率", "殖利率", "美債", "降息", "升息", "通膨"]):
        return "利率與通膨預期會直接影響股債評價，尤其牽動科技股折現率與長債價格。"
    if any(key in lower for key in ["nvidia", "tsmc", "ai", "chip", "semiconductor", "輝達", "台積電", "半導體", "晶片"]):
        return "AI 與半導體仍是台股主線，需觀察台積電、鴻海與伺服器供應鏈資金流向。"
    if any(key in lower for key in ["oil", "iran", "middle east", "energy", "原油", "中東", "伊朗", "能源"]):
        return "能源與地緣政治會影響通膨預期、避險需求與全球風險偏好。"
    if any(key in lower for key in ["dollar", "forex", "usd", "美元", "匯率"]):
        return "美元與匯率變化會影響外資風險偏好、新興市場資金與台股估值。"
    if any(key in lower for key in ["gold", "黃金"]):
        return "黃金反映避險需求、美元與實質利率變化，可作為市場風險溫度計。"
    return "這則新聞反映今日市場風險因子，適合放入股債匯商品的脈絡觀察。"




def translate_headline_to_zh_tw(headline: str) -> str:
    if has_cjk(headline):
        return headline
    try:
        params = urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": "zh-TW", "dt": "t", "q": headline})
        data = fetch_json(f"https://translate.googleapis.com/translate_a/single?{params}")
        translated = "".join(part[0] for part in data[0] if part and part[0]).strip()
        if translated:
            return translated
    except Exception as exc:
        print(f"Headline translation failed: {type(exc).__name__}: {exc}")
    return "國際財經新聞：" + headline[:90]




def remember_pushed_news(items: list[dict[str, str]]) -> None:
    for item in items:
        key = normalize_headline(item.get("headline", ""))
        if key:
            PUSHED_NEWS_KEYS.add(key)


def filter_unpushed_news(items: list[dict[str, str]]) -> list[dict[str, str]]:
    filtered: list[dict[str, str]] = []
    for item in items:
        key = normalize_headline(item.get("headline", ""))
        if key and key in PUSHED_NEWS_KEYS:
            continue
        filtered.append(item)
    return filtered


def fallback_chinese_digest(items: list[dict[str, str]], today: dt.date) -> str:
    selected = []
    for idx, item in enumerate(items[:20], 1):
        headline = translate_headline_to_zh_tw(item["headline"])
        selected.append(
            f"{idx}. {headline}\n"
            f"來源：{item['source']}｜日期：{item['published']}\n"
            f"連結：{item.get('link', '')}"
        )
    if len(selected) < 20:
        selected.append(f"\n今日固定來源中，台北日期為今天且符合台股/美股主軸的最新合格新聞只有 {len(items)} 則；系統不使用過去新聞硬湊滿 20 則。")
    return "\n\n".join(selected)



def finance_candidates(today: dt.date) -> list[dict[str, str]]:
    seen: set[str] = set()
    per_source: dict[str, list[dict[str, str]]] = {}
    yesterday = today - dt.timedelta(days=1)
    allowed_dates = {today.isoformat(), yesterday.isoformat()}

    source_query_groups = [
        ("Reuters", [
            "site:reuters.com/markets stocks OR markets OR Wall Street OR Nasdaq OR S&P 500 OR Fed OR Treasury OR oil",
            "source:Reuters markets OR stocks OR Wall Street OR Nasdaq OR S&P 500 OR Fed OR Treasury OR oil",
            "site:reuters.com/technology Nvidia OR AMD OR Qualcomm OR AI OR semiconductor OR Computex OR Taiwan",
            "source:Reuters Nvidia OR AMD OR Qualcomm OR AI OR semiconductor OR Computex OR Taiwan",
        ]),
        ("Bloomberg", [
            "site:bloomberg.com markets OR stocks OR Wall Street OR Nasdaq OR Fed OR Treasury OR Nvidia OR AI OR Taiwan",
            "source:Bloomberg markets OR stocks OR Wall Street OR Nasdaq OR Fed OR Treasury OR Nvidia OR AI OR Taiwan",
            "site:bloomberg.com technology OR chips OR AI PC OR semiconductor OR Computex",
        ]),
        ("CNBC", [
            "site:cnbc.com markets OR stocks OR Nasdaq OR S&P 500 OR Fed OR Treasury OR Nvidia OR Apple OR Microsoft",
            "source:CNBC markets OR stocks OR Nasdaq OR S&P 500 OR Fed OR Treasury OR Nvidia OR Apple OR Microsoft",
            "site:cnbc.com technology OR AI OR chips OR semiconductor OR AI PC",
        ]),
        ("Yahoo Finance", [
            "site:finance.yahoo.com stocks OR market OR Nasdaq OR S&P 500 OR NVDA OR TSM OR AMD OR AAPL OR MSFT OR TSLA",
        ]),
        ("Seeking Alpha", [
            "site:seekingalpha.com market news OR stocks OR NVDA OR TSM OR AMD OR AAPL OR MSFT OR semiconductor OR earnings",
            "source:Seeking Alpha market news OR stocks OR NVDA OR TSM OR AMD OR AAPL OR MSFT OR semiconductor OR earnings",
        ]),
        ("Investing.com", [
            "site:investing.com news stock market OR Nasdaq OR S&P 500 OR Fed OR Treasury OR oil OR US stocks",
            "source:Investing.com stock market OR Nasdaq OR S&P 500 OR Fed OR Treasury OR oil OR US stocks",
            "site:investing.com Taiwan OR TSMC OR Nvidia OR semiconductor OR AI",
        ]),
        ("WSJ", [
            "site:wsj.com markets OR stocks OR Wall Street OR Nasdaq OR Fed OR Treasury OR Nvidia OR tech",
            "source:Wall Street Journal markets OR stocks OR Wall Street OR Nasdaq OR Fed OR Treasury OR Nvidia OR tech",
        ]),
        ("Financial Times", [
            "site:ft.com markets OR stocks OR Wall Street OR Fed OR Treasury OR Nvidia OR semiconductors OR Taiwan",
            "source:Financial Times markets OR stocks OR Wall Street OR Fed OR Treasury OR Nvidia OR semiconductors OR Taiwan",
        ]),
        ("The Economist", [
            "site:economist.com finance OR markets OR US economy OR technology OR semiconductors",
        ]),
        ("鉅亨網", [
            "site:news.cnyes.com 台股 OR 美股 OR 台積電 OR 輝達 OR AI OR 半導體 OR 聯準會 OR 美債 OR 那斯達克 OR Computex OR 台北電腦展 OR 外資 OR 新台幣 OR AI伺服器 OR 網通",
            "site:news.cnyes.com memory OR DRAM OR Micron OR Nanya Technology OR Winbond OR selloff OR limit-down",
        ]),
        ("MacroMicro", [
            "site:macromicro.me Fed OR CPI OR PCE OR 美債 OR 台股 OR 台積電 OR 景氣 OR 股市 OR 美股",
        ]),
        ("TradingView", [
            "site:tradingview.com/news stocks OR markets OR NVDA OR TSM OR AAPL OR MSFT OR AMD OR Nasdaq OR S&P 500 OR Computex OR AI PC",
            "source:TradingView stocks OR markets OR NVDA OR TSM OR AAPL OR MSFT OR AMD OR Nasdaq OR S&P 500",
        ]),
        ("Reddit WallStreetBets", [
            "site:reddit.com/r/wallstreetbets NVDA OR TSLA OR AMD OR PLTR OR AAPL OR MSFT OR market",
        ]),
    ]

    def source_rss_items(source_name: str, query: str, limit: int = 30) -> list[dict[str, str]]:
        query_with_time = f"({query}) when:2d"
        items: list[dict[str, str]] = []
        canonical = normalize_source_name(source_name, source_name)
        seen_local: set[str] = set()
        locale_params = [("zh-TW", "TW", "TW:zh-Hant"), ("en-US", "US", "US:en")]
        rss_nodes = []
        for hl, gl, ceid in locale_params:
            params = urllib.parse.urlencode({"q": query_with_time, "hl": hl, "gl": gl, "ceid": ceid})
            xml_text = fetch_text(f"https://news.google.com/rss/search?{params}")
            root = ET.fromstring(xml_text)
            rss_nodes.extend(root.findall(".//item")[:limit])
        for item in rss_nodes:
            title = item.findtext("title") or ""
            pub_raw = item.findtext("pubDate") or ""
            link = item.findtext("link") or ""
            if not title:
                continue
            try:
                published = email.utils.parsedate_to_datetime(pub_raw)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=dt.timezone.utc)
                published_date = published.astimezone(TW).date()
            except Exception:
                published_date = today
            if published_date.isoformat() not in allowed_dates:
                continue
            _source, headline = parse_source(title)
            key = normalize_headline(headline)
            if not key:
                continue
            if is_excluded_market_news(canonical, headline):
                continue
            if is_stale_or_low_signal(headline, today):
                continue
            if key in seen_local:
                continue
            seen_local.add(key)
            # Source-specific searches are constrained by site:/source:, so do not require every title
            # to contain the same narrow Taiwan/US keywords. Gemini ranks relevance later.
            items.append({"source": canonical, "headline": headline, "published": published_date.isoformat(), "link": link})
        return items

    for source_name, queries in source_query_groups:
        canonical = normalize_source_name(source_name, source_name)
        for query in queries:
            try:
                raw_items = source_rss_items(source_name, query, limit=30)
            except Exception as exc:
                print(f"News query failed [{source_name}]: {type(exc).__name__}: {exc}")
                continue
            for item in raw_items:
                key = normalize_headline(item["headline"])
                if not key or any(key in old or old in key for old in seen):
                    continue
                seen.add(key)
                per_source.setdefault(canonical, []).append(item)

    selected: list[dict[str, str]] = []
    source_order = [normalize_source_name(source_name, source_name) for source_name, _queries in source_query_groups]
    per_source_limit = {"TradingView": 3, "鉅亨網": 3, "MacroMicro": 3, "Reddit WallStreetBets": 2}
    for source in source_order:
        candidates = per_source.get(source, [])
        candidates.sort(key=lambda item: (relevance_score(item, today), item.get("published", "")), reverse=True)
        selected.extend(candidates[:per_source_limit.get(source, 3)])

    # If fewer than 20 survived after de-duplication, fill with next-best fresh items from every source.
    if len(selected) < 20:
        selected_keys = {normalize_headline(item["headline"]) for item in selected}
        leftovers: list[dict[str, str]] = []
        for source in source_order:
            for item in per_source.get(source, []):
                key = normalize_headline(item["headline"])
                if key not in selected_keys:
                    leftovers.append(item)
        leftovers.sort(key=lambda item: (relevance_score(item, today), item.get("published", "")), reverse=True)
        for item in leftovers:
            selected.append(item)
            if len(selected) >= 25:
                break

    selected.sort(key=lambda item: (relevance_score(item, today), item.get("published", "")), reverse=True)
    print("Finance source candidates: " + json.dumps({k: len(v) for k, v in sorted(per_source.items())}, ensure_ascii=False))
    print(f"Finance source-selected candidates: {len(selected[:40])}")
    return selected[:40]


def looks_english(text: str) -> bool:
    letters = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return letters > max(80, cjk * 0.7)


def numbered_item_count(text: str) -> int:
    return len(re.findall(r"(?m)^\s*\d+[\.、]", text or ""))




def diversify_items_by_source(items: list[dict[str, str]], today: dt.date, limit: int = 20, per_source_cap: int = 3) -> list[dict[str, str]]:
    preferred_order = [
        "Reuters", "Bloomberg", "CNBC", "Yahoo Finance", "Seeking Alpha", "Investing.com",
        "WSJ", "Financial Times", "The Economist", "鉅亨網", "MacroMicro", "TradingView", "Reddit WallStreetBets",
        "DIGITIMES", "Nikkei Asia", "Japan Times", "The Verge", "TechCrunch",
    ]
    grouped: dict[str, list[dict[str, str]]] = {}
    for item in items:
        grouped.setdefault(item.get("source", "Unknown"), []).append(item)
    for candidates in grouped.values():
        candidates.sort(key=lambda item: (relevance_score(item, today), item.get("published", "")), reverse=True)

    selected: list[dict[str, str]] = []
    selected_keys: set[str] = set()
    counts: dict[str, int] = {}

    def add_item(item: dict[str, str], respect_cap: bool = True) -> bool:
        source = item.get("source", "Unknown")
        key = normalize_headline(item.get("headline", ""))
        if not key or key in selected_keys:
            return False
        if respect_cap and counts.get(source, 0) >= per_source_cap:
            return False
        selected.append(item)
        selected_keys.add(key)
        counts[source] = counts.get(source, 0) + 1
        return True

    for source in preferred_order:
        candidates = grouped.get(source, [])
        if candidates and len(selected) < limit:
            add_item(candidates[0])

    made_progress = True
    while made_progress and len(selected) < limit:
        made_progress = False
        for source in preferred_order:
            for item in grouped.get(source, []):
                if add_item(item):
                    made_progress = True
                    break
            if len(selected) >= limit:
                break

    if len(selected) < limit:
        leftovers = [item for group in grouped.values() for item in group]
        leftovers.sort(key=lambda item: (relevance_score(item, today), item.get("published", "")), reverse=True)
        for item in leftovers:
            add_item(item, respect_cap=False)
            if len(selected) >= limit:
                break

    return selected[:limit]


def source_distribution(items: list[dict[str, str]]) -> str:
    counts: dict[str, int] = {}
    for item in items:
        source = item.get("source", "Unknown")
        counts[source] = counts.get(source, 0) + 1
    return "、".join(f"{source} {count}則" for source, count in counts.items())

def finance_message(today: dt.date) -> str:
    items = finance_candidates(today)
    is_weekend = today.weekday() >= 5
    mode = "週末財經重大新聞回顧20則" if is_weekend else "每日財經重大新聞20則"
    selected_items = diversify_items_by_source(items, today, limit=20, per_source_cap=3)
    remember_pushed_news(selected_items)
    fallback_news = fallback_chinese_digest(selected_items, today)
    candidate_lines = [
        f"{idx}. [{item['source']}] {item['published']}｜{item['headline']}｜{item.get('link', '')}"
        for idx, item in enumerate(selected_items, 1)
    ]
    distribution = source_distribution(selected_items)

    selection_prompt = f"""今天日期：{today:%Y-%m-%d}
模式：{mode}
{source_note()}

任務：以下 20 則已由程式依固定來源與來源分散規則選定。請逐則整理成繁體中文投資新聞摘要，並補上 Gemini 觀察。使用者主要投資台股與美股，優先關注 AI、半導體、台積電供應鏈、NVIDIA/AMD/Qualcomm/美國大型科技股、Computex/AI PC/AI伺服器/網通供應鏈、Fed/鮑爾/央行獨立性/美債殖利率、美元與新台幣、外資流向、油價通膨、地緣政治對風險資產的影響。

已選定新聞：
{chr(10).join(candidate_lines)}

硬性規則：
1. 必須按照已選定新聞逐則輸出，不可刪除、不可改用候選以外新聞或媒體。
2. 標題必須翻譯成繁體中文；不可只保留英文原文標題，也不要改用中文轉載站。
3. 每則格式固定為四行：
   編號. 標題
   來源：來源名稱｜日期：YYYY-MM-DD
   連結：原文 URL
   Gemini觀察：一句話說明它對台股/美股、AI半導體、Computex/AI PC/台灣供應鏈、Fed利率或風險資產的影響。
4. 每則之間空一行。
5. 禁止 CMoney、LINE TODAY、FX168、番新聞、TVB、香港財經網站、簡中財經網站或其他非指定來源。
6. 排除中國/港股上市公司、A股/H股、中概股、香港市場新聞，除非它直接影響台積電、NVIDIA、Apple、Microsoft、Fed 或美國利率。
7. 最後加「三、Gemini整體建議」3 點，聚焦台股/美股投資觀察，不要給保證式買賣建議。
8. 總長度適合 Telegram；不要輸出逐則『為什麼重要』套話，只用 Gemini觀察。
9. 台股個股或族群觀察必須通過最新市場狀態檢查；若最新價格/新聞已轉弱，必須改寫為風險升高或題材與股價背離，不可沿用過期強勢敘事。
10. 早上 8 點前輸出時，台股內容只能使用前一交易日、盤前與美股隔夜訊號語氣；不可宣稱今日台股盤中已上漲或下跌，除非來源明確是當日盤中。

9. Apply market-state binding to every risk reminder and Taiwan-stock observation: combine latest news timestamp, prior Taiwan close, overnight US market/ADR/futures, and peer-stock moves before making any conclusion. Do not infer from old theme news alone.
10. If overnight US peers, ADRs, futures, sector ETFs, or relevant commodities have already fallen sharply, describe the Taiwan sector/company as facing pre-market pressure, theme/price divergence, or higher short-term risk; do not call it strong, chased, or rising unless latest Taiwan-market evidence confirms it.
11. Before 08:00 Taipei, Taiwan-stock wording must be pre-market/prior-close/overnight-signal wording; do not claim today's Taiwan intraday rise or fall unless the source is explicitly today's intraday data.
"""
    gemini_digest = call_gemini(selection_prompt, fallback_news, max_tokens=3800).strip()
    if numbered_item_count(gemini_digest) < min(18, len(selected_items)):
        advice_prompt = f"""請根據以下固定來源新聞清單，用繁體中文給 3 點整體市場觀察建議。聚焦台股/美股、AI半導體、Fed/美債殖利率、油價通膨。不要新增新聞，不要保證獲利。

新聞清單：
{chr(10).join(candidate_lines)}
"""
        advice = call_gemini(advice_prompt, "1. 觀察 AI 與半導體資金是否延續。\n2. 留意美債殖利率與 Fed 預期對科技股估值的壓力。\n3. 追蹤油價與地緣政治是否重新推升通膨風險。", max_tokens=700).strip()
        gemini_digest = fallback_news + "\n\n三、Gemini整體建議\n" + advice

    count_note = ""
    if len(selected_items) < 20:
        count_note = f"\n\n資料提醒：固定來源最新視窗內候選共 {len(selected_items)} 則；系統採來源配額制，避免單一網站霸榜，且最多只使用今天或最近 24-36 小時內美股/歐美時區新聞。"

    return f"""【{mode}｜{today:%Y-%m-%d}】

完整圖文網頁已更新：{DAILY_FINANCE_REPORT_URL}

{source_note()}

來源分布：{distribution}

篩選規則：固定來源、台股/美股主軸；程式先依指定來源做分散配額，每個來源原則最多 3 則，再由 Gemini 逐則整理與補充觀察。只使用今天或最近 24-36 小時內美股/歐美時區新聞。Telegram 會自動分段，避免新聞被截斷。

{gemini_digest}{count_note}

提醒：這是投資資訊整理與觀察方向，不是保證式買賣建議。"""



TECH_SOURCE_GROUPS = [
    ("DIGITIMES", [
        "site:digitimes.com.tw/tech AI OR 半導體 OR 晶片 OR Computex OR AI伺服器 OR 伺服器 OR 網通 OR PC OR 日本",
        "site:digitimes.com.tw NVIDIA OR AMD OR Qualcomm OR TSMC OR 台積電 OR AI PC OR supply chain",
    ]),
    ("Reuters", [
        "site:reuters.com/technology AI OR chips OR Nvidia OR AMD OR Apple OR Microsoft OR Japan OR semiconductor",
        "source:Reuters technology AI OR chips OR Nvidia OR AMD OR Apple OR Microsoft OR Japan OR semiconductor",
    ]),
    ("CNBC", [
        "site:cnbc.com/technology AI OR chips OR Nvidia OR AMD OR Apple OR Microsoft OR Japan OR semiconductor",
        "source:CNBC technology AI OR chips OR Nvidia OR AMD OR Apple OR Microsoft OR Japan OR semiconductor",
    ]),
    ("Yahoo Finance", [
        "site:finance.yahoo.com technology AI OR semiconductor OR NVDA OR AMD OR TSM OR AAPL OR MSFT OR Japan",
        "source:Yahoo Finance technology AI OR semiconductor OR NVDA OR AMD OR TSM OR AAPL OR MSFT OR Japan",
    ]),
    ("Seeking Alpha", [
        "site:seekingalpha.com technology AI OR semiconductor OR NVDA OR AMD OR TSM OR AAPL OR MSFT",
        "source:Seeking Alpha technology AI OR semiconductor OR NVDA OR AMD OR TSM OR AAPL OR MSFT",
    ]),
    ("Investing.com", [
        "site:investing.com news technology AI OR semiconductor OR Nvidia OR AMD OR Apple OR Microsoft OR Japan",
        "source:Investing.com technology AI OR semiconductor OR Nvidia OR AMD OR Apple OR Microsoft OR Japan",
    ]),
    ("Nikkei Asia", [
        "site:asia.nikkei.com/Business/Technology AI OR chips OR semiconductor OR Japan OR SoftBank OR TSMC",
        "source:Nikkei Asia technology AI OR chips OR semiconductor OR Japan OR SoftBank OR TSMC",
    ]),
    ("Japan Times", [
        "site:japantimes.co.jp technology AI OR chips OR semiconductor OR startup OR Japan",
    ]),
    ("The Verge", [
        "site:theverge.com AI OR Microsoft OR Apple OR Nvidia OR chips OR Windows OR PC",
        "source:The Verge AI OR Microsoft OR Apple OR Nvidia OR chips OR Windows OR PC",
    ]),
    ("TechCrunch", [
        "site:techcrunch.com AI OR OpenAI OR Nvidia OR Microsoft OR Apple OR Japan OR chips",
        "source:TechCrunch AI OR OpenAI OR Nvidia OR Microsoft OR Apple OR Japan OR chips",
    ]),
]


def rss_search_items(source_name: str, queries: list[str], today: dt.date, max_age_days: int = 2, per_query_limit: int = 20) -> list[dict[str, str]]:
    earliest = today - dt.timedelta(days=max_age_days - 1)
    allowed_dates = {(earliest + dt.timedelta(days=offset)).isoformat() for offset in range(max_age_days)}
    canonical = normalize_source_name(source_name, source_name)
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for query in queries:
        query_with_time = f"({query}) when:{max_age_days}d"
        rss_nodes = []
        locale_params = [("zh-TW", "TW", "TW:zh-Hant"), ("en-US", "US", "US:en")]
        for hl, gl, ceid in locale_params:
            params = urllib.parse.urlencode({"q": query_with_time, "hl": hl, "gl": gl, "ceid": ceid})
            try:
                xml_text = fetch_text(f"https://news.google.com/rss/search?{params}")
                root = ET.fromstring(xml_text)
                rss_nodes.extend(root.findall(".//item")[:per_query_limit])
            except Exception as exc:
                print(f"Tech query failed [{source_name}]: {type(exc).__name__}: {exc}")
                continue
        for item in rss_nodes:
            title = item.findtext("title") or ""
            pub_raw = item.findtext("pubDate") or ""
            link = item.findtext("link") or ""
            if not title:
                continue
            try:
                published = email.utils.parsedate_to_datetime(pub_raw)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=dt.timezone.utc)
                published_date = published.astimezone(TW).date()
            except Exception:
                published_date = today
            if published_date.isoformat() not in allowed_dates:
                continue
            _source, headline = parse_source(title)
            key = normalize_headline(headline)
            if not key or key in seen:
                continue
            if is_excluded_market_news(canonical, headline) or is_stale_or_low_signal(headline, today):
                continue
            seen.add(key)
            items.append({"source": canonical if canonical != "Google News" else source_name, "headline": headline, "published": published_date.isoformat(), "link": link})
    return items


def tech_news_candidates(today: dt.date) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for source_name, queries in TECH_SOURCE_GROUPS:
        items.extend(rss_search_items(source_name, queries, today, max_age_days=2, per_query_limit=20))
    items.sort(key=lambda item: (relevance_score(item, today), item.get("published", "")), reverse=True)
    print("Tech source candidates before de-dupe: " + json.dumps(source_distribution(items), ensure_ascii=False))
    items = filter_unpushed_news(items)
    print("Tech source candidates after finance de-dupe: " + json.dumps(source_distribution(items), ensure_ascii=False))
    return diversify_items_by_source(items, today, limit=10, per_source_cap=2)


def tech_news_message(today: dt.date) -> str:
    items = tech_news_candidates(today)
    fallback_news = fallback_chinese_digest(items, today)
    candidate_lines = [
        f"{idx}. [{item['source']}] {item['published']}｜{item['headline']}｜{item.get('link', '')}"
        for idx, item in enumerate(items, 1)
    ]
    distribution = source_distribution(items)
    prompt = f"""今天日期：{today:%Y-%m-%d}
任務：請根據以下已選定的每日科技新聞，整理 10 則適合台股與美股投資人追蹤的科技重點。主軸包含 AI、半導體、AI PC、資料中心、雲端、NVIDIA/AMD/Qualcomm/Apple/Microsoft、TSMC/台灣供應鏈與亞洲科技供應鏈。

已選定新聞：
{chr(10).join(candidate_lines)}

硬性規則：
1. 必須按照已選定新聞逐則輸出，不可刪除、不可改用候選以外新聞或媒體。
2. 每則格式固定為四行：
   編號. 標題
   來源：來源名稱｜日期：YYYY-MM-DD
   連結：原文 URL
   Gemini觀察：一句話說明它對美股科技股、台股半導體/AI供應鏈或日本科技供應鏈的影響。
3. 每則之間空一行。
4. 標題必須翻譯成繁體中文；不要為了中文化改用中文轉載站。
5. 最後加「科技投資觀察」3 點，不要給保證式買賣建議。
"""
    gemini_digest = call_gemini(prompt, fallback_news, max_tokens=2400).strip()
    if numbered_item_count(gemini_digest) < min(8, len(items)):
        gemini_digest = fallback_news + "\n\n科技投資觀察\n1. AI 與半導體仍是美股與台股科技股主軸。\n2. 留意美國大型科技股資本支出是否繼續支持台灣供應鏈。\n3. 日本半導體與設備鏈若有新投資或政策，會影響亞洲科技股評價。"
    count_note = ""
    if len(items) < 10:
        count_note = f"\n\n資料提醒：最新視窗內符合每日科技主軸的合格新聞只有 {len(items)} 則；系統不使用舊新聞硬湊。"
    return f"""【每日科技新聞10則｜{today:%Y-%m-%d}】

來源範圍：DIGITIMES、Reuters Technology、CNBC Technology、Yahoo Finance、Seeking Alpha、Investing.com、Nikkei Asia、Japan Times、The Verge、TechCrunch。

來源分布：{distribution}

{gemini_digest}{count_note}

提醒：這是每日科技產業與投資資訊整理，不是保證式買賣建議。"""


DASHBOARD_SYMBOLS = [
    {"name": "鴻海", "symbol": "2317", "market": "TWSE", "yahoo": "2317.TW"},
    {"name": "元大台灣50", "symbol": "0050", "market": "TWSE", "yahoo": "0050.TW"},
    {"name": "凱基台灣TOP50", "symbol": "009816", "market": "TWSE", "yahoo": "009816.TW"},
    {"name": "富邦金", "symbol": "2881", "market": "TWSE", "yahoo": "2881.TW"},
    {"name": "微星", "symbol": "2377", "market": "TWSE", "yahoo": "2377.TW"},
    {"name": "群聯", "symbol": "8299", "market": "TPEx", "yahoo": "8299.TWO"},
]


def fmt_signed(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "暫無"
    return f"{value:+,.{digits}f}"


def fmt_plain(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "暫無"
    return f"{value:,.{digits}f}"


def yahoo_stock_month(yahoo_symbol: str) -> list[dict]:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(yahoo_symbol) + "?range=1mo&interval=1d"
    data = fetch_json(url)
    result = (data.get("chart", {}).get("result") or [None])[0]
    if not result:
        return []
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    rows = []
    for idx, stamp in enumerate(timestamps):
        close = (quote.get("close") or [None] * len(timestamps))[idx]
        if close is None:
            continue
        rows.append(
            {
                "date": dt.datetime.fromtimestamp(stamp, TW).date().strftime("%Y/%m/%d"),
                "open": (quote.get("open") or [None] * len(timestamps))[idx],
                "high": (quote.get("high") or [None] * len(timestamps))[idx],
                "low": (quote.get("low") or [None] * len(timestamps))[idx],
                "close": close,
                "volume": (quote.get("volume") or [None] * len(timestamps))[idx] or 0,
            }
        )
    return rows


def twse_snapshot(stock_no: str, today: dt.date) -> dict | None:
    rows = twse_stock_month(stock_no, today)
    if not rows:
        return None
    latest = rows[-1]
    volumes = [number(row[1]) for row in rows if row[1] != "--"]
    last_volume = number(latest[1])
    avg_volume = sum(volumes) / len(volumes) if volumes else 0
    return {
        "source": "TWSE",
        "date": roc_to_iso(latest[0]),
        "open": number(latest[3]),
        "high": number(latest[4]),
        "low": number(latest[5]),
        "close": number(latest[6]),
        "change": number(latest[7]) if latest[7] not in {"--", "X0.00"} else None,
        "volume_lots": round(last_volume / 1000),
        "avg_volume_lots": round(avg_volume / 1000) if avg_volume else 0,
        "ratio": last_volume / avg_volume if avg_volume else None,
    }


def yahoo_snapshot(yahoo_symbol: str) -> dict | None:
    rows = yahoo_stock_month(yahoo_symbol)
    if not rows:
        return None
    latest = rows[-1]
    previous = rows[-2] if len(rows) >= 2 else None
    avg_volume = sum(row["volume"] for row in rows) / len(rows)
    return {
        "source": "Yahoo",
        "date": latest["date"],
        "open": latest["open"],
        "high": latest["high"],
        "low": latest["low"],
        "close": latest["close"],
        "change": latest["close"] - previous["close"] if previous else None,
        "volume_lots": round(latest["volume"] / 1000),
        "avg_volume_lots": round(avg_volume / 1000) if avg_volume else 0,
        "ratio": latest["volume"] / avg_volume if avg_volume else None,
    }


def snapshot_for(item: dict, today: dt.date) -> dict | None:
    if item["market"] == "TWSE":
        try:
            snap = twse_snapshot(item["symbol"], today)
            if snap:
                return snap
        except Exception as exc:
            print(f"TWSE snapshot failed for {item['symbol']}: {exc}", file=sys.stderr)
    try:
        return yahoo_snapshot(item["yahoo"])
    except Exception as exc:
        print(f"Yahoo snapshot failed for {item['symbol']}: {exc}", file=sys.stderr)
        return None


def institutional_summary(stock_no: str, today: dt.date) -> tuple[str, int | None]:
    try:
        rows = latest_institutional_rows(today, stock_no, 5)
    except Exception as exc:
        print(f"Institutional data failed for {stock_no}: {exc}", file=sys.stderr)
        return "法人：暫未取得。", None
    if not rows:
        return "法人：暫未取得或該標的未納入 TWSE 三大法人日報。", None
    latest_date, latest = rows[-1]
    foreign = lots(latest[4])
    investment = lots(latest[10])
    dealer = lots(latest[11])
    total = lots(latest[18])
    positive_days = sum(1 for _, row in rows if lots(row[18]) > 0)
    return (
        f"法人：{latest_date:%m/%d} 外資{foreign:+,}張、投信{investment:+,}張、自營商{dealer:+,}張；三大法人合計{total:+,}張，近5日{positive_days}買/{len(rows) - positive_days}賣。",
        total,
    )


def dashboard_observation(name: str, snap: dict, inst_total: int | None) -> str:
    ratio = snap.get("ratio")
    change = snap.get("change")
    parts = []
    if change is not None and change > 0:
        parts.append("價格偏強")
    elif change is not None and change < 0:
        parts.append("價格整理")
    else:
        parts.append("價格持平觀察")
    if ratio is not None and ratio >= 1.5:
        parts.append("量能明顯放大")
    elif ratio is not None and ratio <= 0.7:
        parts.append("量能偏低")
    else:
        parts.append("量能接近月均")
    if inst_total is not None and inst_total > 0:
        parts.append("法人偏買")
    elif inst_total is not None and inst_total < 0:
        parts.append("法人偏賣")
    else:
        parts.append("法人訊號中性或不足")
    action = "追價前先看量價是否連續" if change is not None and change > 0 and ratio is not None and ratio >= 1 else "以支撐、均量與法人方向確認節奏"
    return f"觀察：{name}目前{'、'.join(parts)}；{action}。"


def dashboard_block(idx: int, item: dict, today: dt.date) -> str:
    snap = snapshot_for(item, today)
    inst_text, inst_total = institutional_summary(item["symbol"], today)
    if not snap:
        return f"{idx}. {item['name']}（{item['symbol']}）\n- 行情：暫未取得，請盤後再確認。\n- {inst_text}\n- 觀察：先不做追價判斷，等價格與量能資料補齊。"
    ratio = snap.get("ratio")
    ratio_text = f"月均{ratio:.2f}倍" if ratio is not None else "月均暫無"
    return "\n".join(
        [
            f"{idx}. {item['name']}（{item['symbol']}）",
            f"- 行情：{snap['date']} 收盤{fmt_plain(snap['close'])}，漲跌{fmt_signed(snap.get('change'))}；高低{fmt_plain(snap['high'])}/{fmt_plain(snap['low'])}，量{snap['volume_lots']:,}張，{ratio_text}。",
            f"- {inst_text}",
            f"- {dashboard_observation(item['name'], snap, inst_total)}",
        ]
    )


def jt_dashboard_message(today: dt.date) -> str:
    blocks = [dashboard_block(idx, item, today) for idx, item in enumerate(DASHBOARD_SYMBOLS, 1)]
    return "\n\n".join(
        [
            f"【JT每日投資儀表板更新｜{today:%Y-%m-%d}】",
            "追蹤標的：鴻海、元大台灣50、凱基台灣TOP50、富邦金、微星、群聯。",
            "資料來源：TWSE 公開資訊為主；上櫃或缺漏標的以 Yahoo Finance 補行情。",
            *blocks,
            "提醒：這是每日資訊整理與觀察方向，不是保證式買賣建議。",
        ]
    )

def main() -> int:
    missing = [
        name
        for name, value in (
            ("TELEGRAM_CHAT_ID", CHAT_ID),
            ("TELEGRAM_FINANCE_BOT_TOKEN", FINANCE_TOKEN),
            ("TELEGRAM_FOXCONN_BOT_TOKEN", FOXCONN_TOKEN),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing required Telegram secrets: {', '.join(missing)}")
    PUSHED_NEWS_KEYS.clear()
    today = dt.datetime.now(TW).date()
    receipts: list[dict] = []
    finance_page_ok, finance_page_note = public_finance_page_status(today)
    finance_text = finance_message(today)
    if not finance_page_ok:
        finance_text = finance_text.replace(
            f"完整圖文網頁已更新：{DAILY_FINANCE_REPORT_URL}",
            f"完整圖文網頁尚未確認更新到 {today:%Y-%m-%d}，暫不引用 URL 以避免誤用舊頁。檢查結果：{finance_page_note}",
        )
    finance_receipts = send_telegram(FINANCE_TOKEN, finance_text, "daily_finance")
    for item in finance_receipts:
        item["finance_page_current"] = finance_page_ok
        item["finance_page_note"] = finance_page_note
    receipts.extend(finance_receipts)
    receipts.extend(send_telegram(FINANCE_TOKEN, tech_news_message(today), "technology_news"))
    receipts.extend(send_telegram(FOXCONN_TOKEN, jt_dashboard_message(today), "jt_dashboard"))
    write_delivery_receipt(today, receipts)
    labels = ", ".join(f"{item['label']}#{item.get('message_id')}" for item in receipts)
    print(f"Sent daily Telegram messages: {labels}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
