#!/usr/bin/env python3
"""全球热点新闻推送 — 每4小时抓取、翻译为中文、排名、分析、推送 Telegram"""

import os
import re
import json
import hashlib
import time
import xml.etree.ElementTree as ET
from urllib.request import Request, urlopen, quote
from datetime import datetime, timezone, timedelta

# ── 配置 ──────────────────────────────────────────────
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
BEIJING_TZ = timezone(timedelta(hours=8))

# 全球 RSS 源
RSS_FEEDS = [
    ("路透社", "https://feeds.reuters.com/reuters/worldNews"),
    ("路透头条", "https://feeds.reuters.com/reuters/topNews"),
    ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("纽约时报", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"),
    ("半岛电视台", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("卫报", "https://www.theguardian.com/world/rss"),
    ("NPR", "https://feeds.npr.org/1004/rss.xml"),
]

# 关键词权重（英文关键词，用于原始英文文本打分）
WEIGHT_KEYWORDS = {
    "war": 5, "invasion": 6, "nuclear": 7, "sanction": 4, "coup": 6,
    "missile": 5, "troop": 4, "military": 4, "ceasefire": 5, "conflict": 4,
    "attack": 5, "strike": 4, "drone": 4,
    "tariff": 5, "recession": 6, "inflation": 5, "fed": 4, "interest rate": 5,
    "stock market": 4, "crash": 6, "rally": 3, "gdp": 3, "debt": 4,
    "oil price": 4, "energy": 3, "trade war": 6,
    "ai": 3, "artificial intelligence": 4, "chip": 3, "semiconductor": 4,
    "tesla": 3, "apple": 3, "microsoft": 3, "google": 3, "openai": 4,
    "crypto": 4, "bitcoin": 4,
    "china": 3, "beijing": 3, "taiwan": 5, "xi": 3, "south china sea": 5,
    "earthquake": 6, "tsunami": 6, "hurricane": 5, "pandemic": 6,
    "outbreak": 5, "explosion": 6, "shooting": 5, "hostage": 6,
}

# ── Google 翻译（免费，无需 API Key）───────────────────

def translate_text(text, target="zh-CN", source="en", max_len=500):
    """调用 Google Translate 免费接口翻译文本"""
    if not text or not text.strip():
        return text
    text = text[:max_len]  # 限制长度
    try:
        url = (
            "https://translate.googleapis.com/translate_a/single"
            f"?client=gtx&sl={source}&tl={target}&dt=t&q={quote(text)}"
        )
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)",
        })
        resp = urlopen(req, timeout=10)
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
        # 结果格式: [[["翻译文本","原文",...]],...]
        parts = []
        for block in data[0]:
            if block[0]:
                parts.append(block[0])
        return "".join(parts)
    except Exception:
        return text  # 翻译失败返回原文


def translate_entries(entries):
    """批量翻译标题和描述为中文"""
    for e in entries:
        e["title_cn"] = translate_text(e["title"])
        e["desc_cn"] = translate_text(e["desc"]) if e["desc"] else ""
        # 控制翻译速率
        time.sleep(0.3)
    return entries


# ── RSS 抓取 ───────────────────────────────────────────

def fetch_feed(name, url, timeout=12):
    """抓取单个 RSS feed，返回条目列表"""
    entries = []
    try:
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)",
            "Accept": "application/rss+xml, application/xml, text/xml",
        })
        resp = urlopen(req, timeout=timeout)
        raw = resp.read().decode("utf-8", errors="replace")
        root = ET.fromstring(raw)
        for item in root.iter("item"):
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            desc = item.findtext("description", "").strip()
            pub_date = item.findtext("pubDate", "")
            desc = re.sub(r"<[^>]+>", "", desc)[:200]
            if title and link:
                entries.append({
                    "title": title,
                    "link": link,
                    "desc": desc,
                    "source": name,
                    "pub_date": pub_date,
                })
    except Exception:
        pass
    return entries


# ── 评分与排名 ─────────────────────────────────────────

def compute_score(entry):
    text = (entry["title"] + " " + entry["desc"]).lower()
    score = 0
    for kw, w in WEIGHT_KEYWORDS.items():
        if kw in text:
            score += w
    return score


def title_hash(title):
    return hashlib.md5(title.strip().lower().encode()).hexdigest()[:8]


def deduplicate(entries):
    seen = {}
    result = []
    for e in entries:
        h = title_hash(e["title"])
        if h not in seen:
            seen[h] = e
            result.append(e)
    return result


def rank_and_pick(entries, top_n=10):
    entries.sort(key=lambda e: compute_score(e), reverse=True)
    unique = deduplicate(entries)
    return unique[:top_n]


# ── 分析生成（全中文）──────────────────────────────────

def generate_analysis(entry, rank):
    """基于关键词匹配生成中文简短分析 + 建议"""
    text = (entry["title"] + " " + entry["desc"]).lower()
    analysis = ""

    # 地缘政治
    if any(w in text for w in ["war", "invasion", "attack", "missile", "troop", "military"]):
        analysis = "⚠️ 地缘风险升温，避险资产（黄金/美债）可能受益，风险资产承压"
    elif any(w in text for w in ["ceasefire", "peace talk", "negotiation"]):
        analysis = "🕊 局势缓和信号，关注风险偏好回升带动股市反弹"
    elif any(w in text for w in ["nuclear", "sanction"]):
        analysis = "🔴 高烈度博弈，能源/供应链波动风险大，建议减仓观望"

    # 经济
    if any(w in text for w in ["recession", "crash", "plunge"]):
        analysis = "📉 衰退/暴跌信号，定投可加速加仓，短线建议止损"
    elif any(w in text for w in ["inflation", "cpi"]):
        analysis = "📊 通胀数据影响美联储路径，数据超预期利空股市，低于预期利好"
    elif any(w in text for w in ["fed", "interest rate", "rate cut", "rate hike"]):
        analysis = "🏦 货币政策风向标，降息利好科技/成长股，加息利好现金/短债"
    elif any(w in text for w in ["tariff", "trade war"]):
        analysis = "🌐 贸易摩擦升级，出口导向型企业/供应链行业首当其冲"
    elif any(w in text for w in ["stock market", "rally", "record"]):
        analysis = "📈 市场乐观，但高位追涨风险大，建议分批止盈"
    elif any(w in text for w in ["oil", "energy"]):
        analysis = "⛽ 能源价格波动，关注通胀预期传导，能源股短多机会"

    # 科技
    if any(w in text for w in ["ai", "artificial intelligence", "openai", "gpt"]):
        analysis = "🤖 AI 赛道持续演进，关注算力/芯片/应用三条线，纳指定投正当时"
    elif any(w in text for w in ["semiconductor", "chip"]):
        analysis = "💾 芯片行业是地缘博弈核心，台积电/英伟达动向影响全球供应链"
    elif any(w in text for w in ["crypto", "bitcoin"]):
        if "crash" in text or "drop" in text or "fall" in text:
            analysis = "🪙 加密暴跌，恐慌是定投良机，但不要接飞刀——等企稳再动"
        else:
            analysis = "🪙 加密市场波动，做好仓位管理，不要追高"

    # 中国
    if "taiwan" in text:
        analysis = "🇹🇼 台海敏感话题，半导体供应链受直接影响，关注台积电/军工板块"
    elif "china" in text and any(w in text for w in ["economy", "growth", "gdp"]):
        analysis = "🇨🇳 中国经济数据影响全球需求预期，大宗商品/奢侈品板块敏感"

    # 突发事件
    if any(w in text for w in ["earthquake", "tsunami", "hurricane"]):
        analysis = "🌪 自然灾害短期冲击当地经济，灾后重建拉动基建/保险板块"

    if not analysis:
        analysis = "📌 持续关注后续发展，短期无明确交易信号"

    return analysis


# ── 格式化推送消息（全中文）────────────────────────────

def format_message(stories):
    now = datetime.now(BEIJING_TZ).strftime("%m月%d日 %H:%M")
    lines = [
        f"🌍 全球热点速报 · {now}（北京时间）",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    emojis = ["❶", "❷", "❸", "❹", "❺", "❻", "❼", "❽", "❾", "❿"]
    for i, s in enumerate(stories):
        emoji = emojis[i] if i < len(emojis) else f"{i+1}."
        # 优先使用中文翻译
        title = s.get("title_cn") or s["title"]
        desc = s.get("desc_cn") or s["desc"]

        lines.append(f"\n{emoji} {title}")
        if desc:
            desc = desc[:120]
            lines.append(f"   {desc}")
        lines.append(f"   📰 来源：{s['source']}")
        analysis = generate_analysis(s, i + 1)
        lines.append(f"   💡 {analysis}")

    lines.append(f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("—— Hermes · 全球热点")
    return "\n".join(lines)


# ── Telegram 发送 ──────────────────────────────────────

def send_telegram(text):
    import urllib.parse
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }).encode()
    req = Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    resp = urlopen(req, timeout=15)
    return json.loads(resp.read())


# ── 主流程 ─────────────────────────────────────────────

def main():
    print(f"[{datetime.now(BEIJING_TZ).isoformat()}] 开始抓取全球新闻...")

    all_entries = []
    for name, url in RSS_FEEDS:
        entries = fetch_feed(name, url)
        print(f"  {name}: {len(entries)} 条")
        all_entries.extend(entries)

    print(f"总共抓取 {len(all_entries)} 条")

    if len(all_entries) < 5:
        print("新闻太少，跳过推送")
        return

    top = rank_and_pick(all_entries, top_n=10)
    print(f"选出 Top {len(top)} 条，正在翻译为中文...")

    top = translate_entries(top)
    print("翻译完成")

    msg = format_message(top)
    result = send_telegram(msg)
    print(f"推送结果: {result.get('ok', False)}")


if __name__ == "__main__":
    main()
