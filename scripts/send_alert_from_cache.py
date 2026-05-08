#!/usr/bin/env python3
"""Send Feishu alert from cached Keepa data.
If cache is valid (< 3 hours), use cached data (zero Token).
If cache is expired (> 3 hours), call Keepa API to refresh, then send alert.
Usage: python scripts/send_alert_from_cache.py [--targets alert_targets.json] [--label "每日预警"]
"""

# ========== Auto-injected: Skill Usage Reporting ==========
# 此代码由 Skills 市场自动注入，用于统计调用次数
# 完全异步执行，不影响业务逻辑
try:
    import threading as _t
    def _report():
        try:
            from http.client import HTTPSConnection as _HC
            _c = _HC("ai-tools-market.anker-in.com", timeout=3)
            _c.request("POST", "/api/v1/statistics/skill/keepa-data-interface/count/1")
            _c.getresponse()
            _c.close()
        except: pass
    _t.Thread(target=_report, daemon=True).start()
except: pass
# ===========================================================

import json, hashlib, hmac, base64, time, sys, os, requests
from datetime import datetime, timezone, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(SCRIPT_DIR, "cache")
CACHE_TTL_SECONDS = 3 * 3600  # 3 hours

# Amazon domain URLs for product links
AMAZON_DOMAINS = {
    1: "https://www.amazon.com/dp/{asin}",
    2: "https://www.amazon.co.uk/dp/{asin}",
    3: "https://www.amazon.de/dp/{asin}",
    4: "https://www.amazon.fr/dp/{asin}",
    5: "https://www.amazon.co.jp/dp/{asin}",
    6: "https://www.amazon.ca/dp/{asin}",
    7: "https://www.amazon.cn/dp/{asin}",
    8: "https://www.amazon.it/dp/{asin}",
    9: "https://www.amazon.es/dp/{asin}",
    10: "https://www.amazon.in/dp/{asin}",
    11: "https://www.amazon.com.mx/dp/{asin}",
    12: "https://www.amazon.com.br/dp/{asin}",
    13: "https://www.amazon.ae/dp/{asin}",
    14: "https://www.amazon.nl/dp/{asin}",
    15: "https://www.amazon.ie/dp/{asin}",
    16: "https://www.amazon.sg/dp/{asin}",
    17: "https://www.amazon.pl/dp/{asin}",
    18: "https://www.amazon.se/dp/{asin}",
    19: "https://www.amazon.co.za/dp/{asin}",
    20: "https://www.amazon.com.tr/dp/{asin}",
    22: "https://www.amazon.com.au/dp/{asin}",
}

def load_cached_data(asin, domain_id):
    """Find cached data for a given ASIN and domain. Returns None if expired or missing."""
    key_raw = f"{asin}_{domain_id}"
    key = hashlib.md5(key_raw.encode()).hexdigest()[:12]
    cache_file = os.path.join(CACHE_DIR, f"{key}.json")
    if not os.path.exists(cache_file):
        return None
    with open(cache_file, "r") as f:
        data = json.load(f)
    # Check TTL
    ts = data.get("timestamp", 0)
    if ts < (datetime.now(tz=timezone.utc).timestamp() - CACHE_TTL_SECONDS):
        return "expired"
    return data

def get_last_of_day(data_points, target_date):
    """Get the last valid data point for a given date (UTC)."""
    if not data_points:
        return None
    day_end = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59, tzinfo=timezone.utc)
    result = None
    for dt, val in data_points:
        if dt <= day_end:
            result = val
        if dt > day_end:
            break
    return result

def parse_price_history(p):
    """Parse price history from keepa product data."""
    csv_data = p.get("csv", [])
    csv_idx = 9 if len(csv_data) > 9 and csv_data[9] else 1
    price_history = []
    if csv_idx < len(csv_data) and csv_data[csv_idx]:
        for i in range(0, len(csv_data[csv_idx]), 2):
            if i + 1 >= len(csv_data[csv_idx]):
                break
            kt, val = csv_data[csv_idx][i], csv_data[csv_idx][i + 1]
            if val == -1:
                continue
            ts = (kt + 21564000) * 60
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            price_history.append((dt, val / 100))
    return price_history

def get_category_names(p):
    """Extract category name mapping from product data.
    Returns {cat_id: "Category Name", ...}
    """
    categories = {}
    cat_tree = p.get("categories") or []
    for cat in cat_tree:
        if isinstance(cat, dict):
            cat_id = cat.get("catId")
            name = cat.get("name")
            if cat_id and name:
                categories[str(cat_id)] = name
    # Also check categoryTree
    cat_tree2 = p.get("categoryTree") or []
    for cat in cat_tree2:
        if isinstance(cat, dict):
            cat_id = cat.get("catId")
            name = cat.get("name")
            if cat_id and name:
                categories[str(cat_id)] = name
    return categories


def parse_all_ranks(p):
    """Parse ALL rank categories from keepa product data.
    返回: {cat_id: {"current_rank": int, "history": [(dt, rank), ...]}}
    """
    sales_ranks = p.get("salesRanks") or {}
    if not sales_ranks:
        return {}

    all_ranks = {}
    for cat_id, rd in sales_ranks.items():
        if not rd or len(rd) < 2:
            continue
        # Parse history
        history = []
        for i in range(0, len(rd), 2):
            if i + 1 >= len(rd):
                break
            if rd[i + 1] == -1:
                continue
            ts = (rd[i] + 21564000) * 60
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            history.append((dt, rd[i + 1]))

        # Current rank = last valid value
        current_rank = None
        for i in range(len(rd) - 1, 0, -2):
            if rd[i] > 0:
                current_rank = rd[i]
                break

        if history:
            all_ranks[cat_id] = {
                "current_rank": current_rank,
                "history": history,
            }

    return all_ranks

def fmt_price(c):
    return f"${c:.2f}" if c is not None else "N/A"

def fmt_dod_price(c, currency="$"):
    if c is None:
        return "N/A"
    return f"+{c:.2f}" if c >= 0 else f"{c:.2f}"

def fmt_rank_dod(c):
    if c is None:
        return "N/A"
    if c < 0:
        return f"{c} (上升)"
    if c > 0:
        return f"+{c} (下降)"
    return "0 (无变化)"

def fetch_from_api(asin, domain_id, api_key):
    """Call Keepa API to fetch latest data for an ASIN. Returns product data or None."""
    url = f'https://api.keepa.com/product?key={api_key}&domain={domain_id}&asin={asin}&history=1&stats=90'
    try:
        resp = requests.get(url, timeout=120)
        data = resp.json()
        if 'products' in data and data['products']:
            product = data['products'][0]
            # Save to cache
            product["timestamp"] = datetime.now(tz=timezone.utc).timestamp()
            key_raw = f"{asin}_{domain_id}"
            key = hashlib.md5(key_raw.encode()).hexdigest()[:12]
            cache_file = os.path.join(CACHE_DIR, f"{key}.json")
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(cache_file, "w") as f:
                json.dump(product, f, default=str)
            print(f"[API刷新] {asin}", file=sys.stderr)
            return product
        else:
            print(f"[API无数据] {asin}", file=sys.stderr)
            return None
    except Exception as e:
        print(f"[API失败] {asin}: {e}", file=sys.stderr)
        return None


def process_asin(asin, domain_id):
    """Process a single ASIN from cache or API, return alert data."""
    p = load_cached_data(asin, domain_id)

    # Cache expired or missing -> fetch from API
    if p is None or p == "expired":
        api_key = os.environ.get("KEEPA_API_KEY")
        if not api_key:
            print(f"[跳过] {asin}: 无 API Key", file=sys.stderr)
            return None
        p = fetch_from_api(asin, domain_id, api_key)

    if not p:
        return None

    title = (p.get("title") or "Unknown")[:60]
    brand = p.get("brand") or "Unknown"
    domain_names = {1: "US", 2: "UK", 3: "DE", 4: "FR", 5: "JP", 6: "CA",
                    7: "CN", 8: "IT", 9: "ES", 10: "IN", 11: "MX", 12: "BR",
                    13: "AE", 14: "NL", 15: "IE", 16: "SG", 17: "PL", 18: "SE",
                    19: "ZA", 20: "TR", 22: "AU"}
    domain_str = domain_names.get(domain_id, f"Domain{domain_id}")
    currency = {"BR": "R$", "US": "$", "UK": "£", "DE": "€", "FR": "€",
                "JP": "¥", "CA": "C$", "IN": "₹"}.get(domain_str, "$")

    price_history = parse_price_history(p)
    all_ranks = parse_all_ranks(p)
    category_names = get_category_names(p)

    today = datetime.now(tz=timezone.utc).date()
    yesterday = today - timedelta(days=1)

    price_today = get_last_of_day(price_history, today)
    price_yesterday = get_last_of_day(price_history, yesterday)
    price_dod = (price_today - price_yesterday) if (price_today is not None and price_yesterday is not None) else None

    # Build per-category rank info
    rank_details = []
    for cat_id, rank_data in all_ranks.items():
        history = rank_data["history"]
        today_rank = get_last_of_day(history, today)
        yesterday_rank = get_last_of_day(history, yesterday)
        rank_dod = (today_rank - yesterday_rank) if (today_rank is not None and yesterday_rank is not None) else None
        rank_details.append({
            "category_id": cat_id,
            "category_name": category_names.get(cat_id, ""),
            "today_rank": today_rank,
            "yesterday_rank": yesterday_rank,
            "rank_dod": rank_dod,
        })

    # Sort by current rank (ascending) for readability
    rank_details.sort(key=lambda x: x["today_rank"] if x["today_rank"] else float('inf'))

    return {
        "asin": asin,
        "domain_id": domain_id,
        "domain": domain_str,
        "title": f"[{brand}] {title}",
        "current_price": int(price_today * 100) if price_today is not None else 0,
        "price_dod": int(price_dod * 100) if price_dod is not None else None,
        "rank_details": rank_details,
        "currency": currency,
    }

def send_alert(webhook, secret, items, label="每日预警"):
    """Send alert card to Feishu group."""
    timestamp = str(int(time.time()))
    sign = base64.b64encode(hmac.new(f"{timestamp}\n{secret}".encode("utf-8"),
                                      digestmod=hashlib.sha256).digest()).decode("utf-8")

    elements = []
    for item in items:
        asin = item["asin"]
        domain_id = item["domain_id"]
        domain = item["domain"]
        title = item["title"][:80]
        currency = item.get("currency", "$")

        # Build Amazon product link
        amazon_url = AMAZON_DOMAINS.get(domain_id, "https://www.amazon.com/dp/{asin}").format(asin=asin)

        # Build rank details section
        rank_details = item.get("rank_details", [])
        rank_lines = []
        for rd in rank_details:
            cat_id = rd["category_id"]
            cat_name = rd.get("category_name", "")
            today_r = rd["today_rank"]
            yesterday_r = rd["yesterday_rank"]
            dod = rd["rank_dod"]
            if today_r is None:
                continue
            tod_str = f"#{today_r}"
            yes_str = f"#{yesterday_r}" if yesterday_r is not None else "#"
            dod_str = fmt_rank_dod(dod)
            label = f"{cat_name}" if cat_name else f"大类 {cat_id}"
            rank_lines.append(f"**{label}**: {tod_str} (昨日: {yes_str}) DOD: {dod_str}")

        rank_section = "\n".join(rank_lines) if rank_lines else "**排名**: 暂无数据"

        card_md = f"[**{asin}**]({amazon_url}) ({domain})\n**产品**: {title}\n**价格**: {currency}{item['current_price']/100:.2f}  DOD: {fmt_dod_price(item['price_dod']/100 if item['price_dod'] is not None else None, currency)}\n{rank_section}"
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": card_md}})
        elements.append({"tag": "hr"})

    # Remove trailing hr
    if elements and elements[-1].get("tag") == "hr":
        elements = elements[:-1]

    payload = {
        "timestamp": timestamp,
        "sign": sign,
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"template": "blue", "title": {"tag": "plain_text", "content": f"ASIN 每日预警 | {label}"}},
            "elements": elements,
        },
    }

    headers = {"Content-Type": "application/json"}
    resp = requests.post(webhook, data=json.dumps(payload), headers=headers, timeout=10)
    if resp.status_code == 200:
        result = resp.json()
        if result.get("code") == 0:
            print("Alert sent successfully.")
        else:
            print(f"API error: {result}", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"HTTP error: {resp.status_code} - {resp.text}", file=sys.stderr)
        sys.exit(1)

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--targets", default=None, help="Path to alert_targets.json")
    parser.add_argument("--label", default="每日预警", help="Alert label")
    args = parser.parse_args()

    # Load config: only from environment variables (GitHub Actions Secrets)
    keepa_api_key = os.environ.get("KEEPA_API_KEY")
    feishu_webhook = os.environ.get("FEISHU_WEBHOOK")
    feishu_secret = os.environ.get("FEISHU_SECRET")

    if not all([keepa_api_key, feishu_webhook, feishu_secret]):
        print("ERROR: Missing required environment variables: KEEPA_API_KEY, FEISHU_WEBHOOK, FEISHU_SECRET", file=sys.stderr)
        sys.exit(1)

    # Load targets from file (required)
    if args.targets and os.path.exists(args.targets):
        with open(args.targets) as f:
            targets = json.load(f)
        asins = targets.get("asins", [])
    else:
        print("ERROR: No targets file provided. Specify --targets <path> with a valid alert_targets.json containing ASINs.", file=sys.stderr)
        sys.exit(1)

    if not asins:
        print("No ASINs to alert.", file=sys.stderr)
        sys.exit(1)

    items = []
    for target in asins:
        asin = target.get("asin")
        domain_id = target.get("domain", 1)
        item = process_asin(asin, domain_id)
        if item:
            items.append(item)

    if not items:
        print("No valid cached data to alert.", file=sys.stderr)
        sys.exit(1)

    send_alert(feishu_webhook, feishu_secret, items, args.label)

if __name__ == "__main__":
    main()
