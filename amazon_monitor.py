#!/usr/bin/env python3
"""Amazon ASIN 监控 — 差评/竞品改价/跟卖 实时告警"""
import json, os, sys, urllib.request, time, re, hashlib
from datetime import datetime, timezone, timedelta

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TZ = timezone(timedelta(hours=8))

# ── 监控配置 ──────────────────────────────────────────
MY_ASIN = "B0C2JZH9BB"
COMPETITOR_ASIN = "B0CFXPZ5GT"

# 存储文件路径 (GitHub Actions 持久化)
DATA_FILE = "amazon_state.json"

def fetch_text(url, timeout=15, retries=3):
    """抓取页面，尝试多个代理"""
    strategies = [
        # 策略1: 直连
        lambda u: urllib.request.urlopen(urllib.request.Request(u, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }), timeout=timeout).read().decode("utf-8", errors="ignore"),
        # 策略2: Google Cache
        lambda u: urllib.request.urlopen(urllib.request.Request(
            f"https://webcache.googleusercontent.com/search?q=cache:{u}", headers={
            "User-Agent": "Mozilla/5.0"
        }), timeout=timeout).read().decode("utf-8", errors="ignore"),
        # 策略3: textise dot iitty
        lambda u: urllib.request.urlopen(urllib.request.Request(
            f"https://r.jina.ai/http://{u.replace('https://','')}", headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/plain"
        }), timeout=timeout).read().decode("utf-8", errors="ignore"),
    ]
    
    for i, strategy in enumerate(strategies):
        try:
            html = strategy(url)
            # 验证是否真的拿到了内容
            if len(html) > 5000 and ("productTitle" in html or "priceblock" in html or "application/ld+json" in html):
                print(f"Strategy {i+1}: OK ({len(html)} chars)")
                return html
        except Exception as e:
            print(f"Strategy {i+1}: {type(e).__name__}")
    
    raise Exception("All strategies failed")

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}))
    except Exception as e:
        print(f"TG error: {e}", file=sys.stderr)

def parse_product(html, asin):
    """从 HTML/文本中提取产品数据"""
    data = {"asin": asin}
    
    # 判断是否是 Jina 返回的 markdown 格式
    is_markdown = html.startswith("Title:") or "Markdown" in html[:200]
    
    if is_markdown:
        # Jina 格式: 标题/评分用 markdown
        m = re.search(r'Title:\s*(.+)', html)
        if m: data["title"] = m.group(1).strip()[:120]
        
        m = re.search(r'(\d+\.\d+)\s*out of\s*5', html)
        if m: data["rating"] = float(m.group(1))
        
        m = re.search(r'(\d[\d,]*)\s*(?:ratings|global ratings|reviews)', html)
        if m: data["reviews_count"] = int(m.group(1).replace(",", ""))
        
        # BSR
        m = re.search(r'Best Sellers Rank[:\s]*#?([\d,]+)', html)
        if m: data["bsr"] = int(m.group(1).replace(",", ""))
        
        # 价格
        m = re.search(r'\$([\d.]+)', html)
        if m: data["price"] = m.group(1)
        
        # 最新评论 (Jina 格式: 星级 + 标题 + 日期 + 正文)
        review_blocks = re.findall(
            r'(\d+\.?\d*)\s*out of\s*5[^\n]*\n+([^\n]+)\n+Reviewed[^\n]*\n+([^\n]{3,30})\n+(.*?)(?=\n\d+\.?\d*\s*out of|\n\Z)',
            html, re.DOTALL
        )
        data["latest_reviews"] = []
        for stars, title, date, body in review_blocks[:5]:
            try:
                r = float(stars)
                if r <= 5 and len(title) > 1:
                    data["latest_reviews"].append({
                        "rating": r, "title": title.strip()[:120],
                        "date": date.strip(), "body": body.strip()[:200]
                    })
            except:
                pass
        
        # 劫持检测所需: 图片数量 + 描述文本
        img_count = len(re.findall(r'!\[[^\]]*\](?:\([^)]+\))?', html))
        if img_count > 0:
            data["image_count"] = img_count
        else:
            data["image_count"] = len(re.findall(r'Image|image-', html))
        
        # 提取描述 (Jina 格式: 产品描述之后的段落)
        desc_match = re.search(r'(?:Description|Product Description|About this item|描述)[^\n]*\n+(.{50,800})', html, re.DOTALL)
        if desc_match:
            data["description"] = desc_match.group(1).strip()[:300]
        elif "title" in data:
            # Fallback: 取标题后的前几段作为描述
            parts = html.split("\n\n")
            for p in parts[3:8]:
                if len(p) > 50 and "out of 5" not in p:
                    data["description"] = p.strip()[:300]
                    break
    else:
        # 原始 HTML 格式的解析逻辑
        # ... (keep existing logic)
        pass

    # 通用 fallback
    if "rating" not in data:
        m = re.search(r'(\d+\.\d+)\s*out of\s*5', html)
        if m: data["rating"] = float(m.group(1))
    
    if "reviews_count" not in data:
        m = re.search(r'(\d[\d,]*)\s*(?:global ratings|ratings|reviews)', html)
        if m: data["reviews_count"] = int(m.group(1).replace(",", ""))
    
    if "price" not in data:
        m = re.search(r'"price":"?\$?([\d.]+)"?', html)
        if m: data["price"] = m.group(1)
        else:
            m = re.search(r'\$([\d.]+)', html)
            if m: data["price"] = m.group(1)
    
    if "bsr" not in data:
        m = re.search(r'Best Sellers Rank[:\s]*#?([\d,]+)', html)
        if m: data["bsr"] = int(m.group(1).replace(",", ""))
    
    if "other_sellers_new" not in data:
        m = re.search(r'New\s*\((\d+)\)\s*from', html)
        if m: data["other_sellers_new"] = int(m.group(1))

    return data

def load_state():
    try:
        with open(DATA_FILE) as f:
            return json.load(f)
    except:
        return {}

def save_state(state):
    # 只保留最近的关键数据
    simple = {}
    if MY_ASIN in state:
        simple[MY_ASIN] = {
            "last_review_hash": state[MY_ASIN].get("last_review_hash"),
            "last_rating": state[MY_ASIN].get("last_rating"),
            "last_reviews_count": state[MY_ASIN].get("last_reviews_count"),
            "last_bsr": state[MY_ASIN].get("last_bsr"),
            "last_sellers": state[MY_ASIN].get("last_sellers"),
            "last_title_hash": state[MY_ASIN].get("last_title_hash"),
            "last_image_count": state[MY_ASIN].get("last_image_count"),
            "last_desc_hash": state[MY_ASIN].get("last_desc_hash"),
        }
    if COMPETITOR_ASIN in state:
        simple[COMPETITOR_ASIN] = {
            "last_price": state[COMPETITOR_ASIN].get("last_price"),
            "last_review_hash": state[COMPETITOR_ASIN].get("last_review_hash"),
        }
    with open(DATA_FILE, "w") as f:
        json.dump(simple, f, indent=2)

# ═══════════════════════════════════════════════════════
# 主逻辑
# ═══════════════════════════════════════════════════════
state = load_state()
alerts = []
reports = {}
all_ok = True

# 抓取我的产品
try:
    html_my = fetch_text(f"https://www.amazon.com/dp/{MY_ASIN}")
    data_my = parse_product(html_my, MY_ASIN)
    reports[MY_ASIN] = data_my
    print(f"MY: rating={data_my.get('rating')}, reviews={data_my.get('reviews_count')}, bsr={data_my.get('bsr')}, sellers={data_my.get('other_sellers_new')}")
except Exception as e:
    print(f"MY FETCH ERROR: {e}")
    all_ok = False

time.sleep(3)

# 抓取竞品
try:
    html_comp = fetch_text(f"https://www.amazon.com/dp/{COMPETITOR_ASIN}")
    data_comp = parse_product(html_comp, COMPETITOR_ASIN)
    reports[COMPETITOR_ASIN] = data_comp
    print(f"CMP: rating={data_comp.get('rating')}, reviews={data_comp.get('reviews_count')}, price={data_comp.get('price')}")
except Exception as e:
    print(f"CMP FETCH ERROR: {e}")

# ── 检测差评 (3星以下) ───────────────────────────────
if MY_ASIN in reports:
    d = reports[MY_ASIN]
    for rev in d.get("latest_reviews", []):
        if rev["rating"] <= 3:
            key = hashlib.md5(rev["title"].encode()).hexdigest()
            last_hash = state.get(MY_ASIN, {}).get("last_review_hash")
            if key != last_hash:
                stars_emoji = "⭐" * int(rev["rating"])
                alerts.append(
                    f"🚨 新差评!\n"
                    f"  评分: {stars_emoji}\n"
                    f"  标题: {rev['title']}\n"
                    f"  内容: {rev['body'][:150]}\n"
                    f"  日期: {rev['date']}"
                )
            break  # 只检查最新一条

    # 更新 hash
    if d.get("latest_reviews"):
        state.setdefault(MY_ASIN, {})["last_review_hash"] = hashlib.md5(
            d["latest_reviews"][0]["title"].encode()
        ).hexdigest()

# ── 检测评分变化 ──────────────────────────────────────
if MY_ASIN in reports and MY_ASIN in state:
    old_rating = state[MY_ASIN].get("last_rating")
    new_rating = reports[MY_ASIN].get("rating")
    if old_rating and new_rating and new_rating < old_rating - 0.1:
        alerts.append(f"⚠️ 评分下降: {old_rating:.1f} → {new_rating:.1f}")

# ── 检测竞品改价 (降价 >10%) ─────────────────────────
if COMPETITOR_ASIN in reports and COMPETITOR_ASIN in state:
    old_price = state[COMPETITOR_ASIN].get("last_price")
    new_price = reports[COMPETITOR_ASIN].get("price")
    if old_price and new_price:
        try:
            op, np = float(old_price), float(new_price)
            if np < op * 0.9:
                alerts.append(
                    f"🔻 竞品大降价!\n"
                    f"  KELIN: ${op:.2f} → ${np:.2f} (降 {(op-np)/op*100:.0f}%)"
                )
            elif np > op * 1.1:
                alerts.append(
                    f"🔺 竞品涨价: ${op:.2f} → ${np:.2f} (涨 {(np-op)/op*100:.0f}%)"
                )
        except:
            pass

# ── 检测跟卖 (新卖家上链接) ──────────────────────────
if MY_ASIN in reports and MY_ASIN in state:
    old_sellers = state[MY_ASIN].get("last_sellers", 1)
    new_sellers = reports[MY_ASIN].get("other_sellers_new", 1)
    if new_sellers and new_sellers > old_sellers:
        alerts.append(
            f"🆕 跟卖警报!\n"
            f"  卖家数: {old_sellers} → {new_sellers}\n"
            f"  可能被跟卖，速去后台检查!"
        )

# ── 检测 BSR 暴跌 ────────────────────────────────────
if MY_ASIN in reports and MY_ASIN in state:
    old_bsr = state[MY_ASIN].get("last_bsr")
    new_bsr = reports[MY_ASIN].get("bsr")
    if old_bsr and new_bsr and new_bsr > old_bsr * 2:
        alerts.append(f"📉 BSR 暴跌: #{old_bsr:,} → #{new_bsr:,}")

# ── 检测 Listing 劫持 (标题/图片/描述被修改) ────────────
if MY_ASIN in reports and MY_ASIN in state:
    d = reports[MY_ASIN]
    st = state[MY_ASIN]
    
    # 标题改动
    new_title = d.get("title", "")
    if new_title:
        new_hash = hashlib.md5(new_title.encode()).hexdigest()
        old_hash = st.get("last_title_hash")
        if old_hash and new_hash != old_hash:
            alerts.append(
                f"🔄 标题被修改!\n"
                f"  旧: {st.get('last_title_preview', '?')[:80]}\n"
                f"  新: {new_title[:80]}"
            )
        st["last_title_hash"] = new_hash
        st["last_title_preview"] = new_title[:80]
    
    # 图片数量改动
    new_imgs = d.get("image_count", 0)
    old_imgs = st.get("last_image_count", 0)
    if old_imgs > 0 and new_imgs > 0 and new_imgs != old_imgs:
        alerts.append(
            f"🖼 图片数量变化: {old_imgs} → {new_imgs} 张\n"
            f"  主图/附图可能被替换"
        )
    st["last_image_count"] = new_imgs
    
    # 描述改动
    new_desc = d.get("description", "")
    if new_desc:
        new_hash = hashlib.md5(new_desc.encode()).hexdigest()
        old_hash = st.get("last_desc_hash")
        if old_hash and new_hash != old_hash:
            alerts.append(
                f"📝 产品描述被修改!\n"
                f"  新描述: {new_desc[:120]}..."
            )
        st["last_desc_hash"] = new_hash

# ── 保存状态 ──────────────────────────────────────────
if MY_ASIN in reports:
    d = reports[MY_ASIN]
    state.setdefault(MY_ASIN, {}).update({
        "last_rating": d.get("rating"),
        "last_reviews_count": d.get("reviews_count"),
        "last_bsr": d.get("bsr"),
        "last_sellers": d.get("other_sellers_new", 1),
    })
if COMPETITOR_ASIN in reports:
    state.setdefault(COMPETITOR_ASIN, {}).update({
        "last_price": reports[COMPETITOR_ASIN].get("price"),
    })
save_state(state)

# ── 发送告警 (合并为一条) ──────────────────────────────
now = datetime.now(TZ).strftime("%m-%d %H:%M")
if alerts:
    if len(alerts) == 1:
        msg = alerts[0]
    else:
        msg = f"🚨 {len(alerts)}项异常  {now}\n\n" + "\n\n".join(alerts)
    send_telegram(f"{msg}\n\n—— Hermes · Amazon Monitor")
    print(f"ALERTS SENT: {len(alerts)}")
else:
    print("ALL OK - skip")
