"""
PostEx Loadsheet Scraper
========================
1. Logs into merchant.postex.pk using Playwright (headless Chrome)
2. Navigates to /main/load-sheet-logs
3. Finds loadsheets matching yesterday's date
4. Extracts the numeric loadsheet ID from the dropdown class  more-menu-XXXX
5. Calls the PostEx API directly (with JWT captured from browser) to get orders
6. Saves everything to data/loadsheet_YYYY-MM-DD.json
7. Optionally POSTs to an N8N webhook

GitHub Secrets needed:
  POSTEX_USERNAME   – merchant login email
  POSTEX_PASSWORD   – merchant password
  N8N_WEBHOOK_URL   – (optional) N8N webhook URL
"""

import os
import re
import json
import time
import logging
import requests
from datetime import datetime, timedelta
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────
BASE_URL      = "https://merchant.postex.pk"
LOGIN_URL     = f"{BASE_URL}/login"
LOADSHEET_URL = f"{BASE_URL}/main/load-sheet-logs"
API_BASE      = "https://api.postex.pk/services/merchant/api/load-sheet"

USERNAME      = os.environ["POSTEX_USERNAME"]
PASSWORD      = os.environ["POSTEX_PASSWORD"]
N8N_WEBHOOK   = os.environ.get("N8N_WEBHOOK_URL", "")

OUTPUT_DIR    = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)

# Support manual date override from GitHub Actions workflow_dispatch
_override = os.environ.get("DATE_OVERRIDE", "").strip()
if _override:
    TARGET_DATE = datetime.strptime(_override, "%Y-%m-%d")
    log.info(f"Using overridden date: {_override}")
else:
    TARGET_DATE = datetime.now() - timedelta(days=1)

# PostEx date format in table: "May 9, 2026, 9:16:58 PM"
# We match just "May 9, 2026" inside that string
try:
    TARGET_LABEL = TARGET_DATE.strftime("%b %-d, %Y")   # Linux
except ValueError:
    TARGET_LABEL = TARGET_DATE.strftime("%b %#d, %Y")   # Windows


# ══════════════════════════════════════════════════════════════════════════
# STEP 1 – Login
# ══════════════════════════════════════════════════════════════════════════

def login(page):
    log.info("Navigating to login page ...")
    page.goto(LOGIN_URL, wait_until="networkidle")
    time.sleep(1)

    for sel in [
        'input[type="email"]',
        'input[name="email"]',
        'input[name="username"]',
        'input[placeholder*="mail" i]',
        'input[placeholder*="user" i]',
    ]:
        try:
            if page.locator(sel).count() > 0:
                page.fill(sel, USERNAME)
                log.info(f"  Username filled ({sel})")
                break
        except Exception:
            continue

    page.fill('input[type="password"]', PASSWORD)
    page.click('button[type="submit"]')
    page.wait_for_url(f"{BASE_URL}/main/**", timeout=30_000)
    log.info("Login successful")


# ══════════════════════════════════════════════════════════════════════════
# STEP 2 – Capture JWT from browser localStorage
# ══════════════════════════════════════════════════════════════════════════

def get_auth_headers(page) -> dict:
    token = page.evaluate("""() => {
        const keys = ['token','authToken','access_token','accessToken','jwt','Authorization'];
        for (const k of keys) {
            const v = localStorage.getItem(k) || sessionStorage.getItem(k);
            if (v) return v;
        }
        for (let i = 0; i < localStorage.length; i++) {
            const k = localStorage.key(i);
            const v = localStorage.getItem(k);
            if (v && v.startsWith('eyJ')) return v;
        }
        return '';
    }""")

    headers = {
        "Content-Type": "application/json",
        "Accept":       "application/json",
        "Origin":       BASE_URL,
        "Referer":      LOADSHEET_URL,
    }

    if token:
        token = token.replace("Bearer ", "").strip()
        headers["Authorization"] = f"Bearer {token}"
        log.info("JWT captured from localStorage")
    else:
        log.warning("JWT not found — API calls may return 401")

    return headers


def get_cookies(page) -> dict:
    return {c["name"]: c["value"] for c in page.context.cookies()}


# ══════════════════════════════════════════════════════════════════════════
# STEP 3 – Scrape loadsheet table and extract IDs
# ══════════════════════════════════════════════════════════════════════════

def scrape_loadsheet_rows(page) -> list:
    """
    KEY INSIGHT: The loadsheet numeric ID is embedded in the dropdown class:
      <ul class="dropdown-list more-menu-4786">
    The number after 'more-menu-' IS the loadsheet ID for the API.
    """
    log.info(f"Opening loadsheet logs page ... (looking for '{TARGET_LABEL}')")
    page.goto(LOADSHEET_URL, wait_until="networkidle")

    try:
        page.wait_for_selector("table#excel-table tbody tr", timeout=25_000)
        log.info("Loadsheet table rendered")
    except PWTimeout:
        log.error("Table never appeared — saving screenshot")
        page.screenshot(path="data/debug_page.png")
        return []

    time.sleep(2)

    rows = page.query_selector_all("table#excel-table tbody tr")
    log.info(f"Total rows in table: {len(rows)}")

    matched = []
    for i, row in enumerate(rows):
        cells = row.query_selector_all("td")
        if len(cells) < 7:
            continue

        lds_number = cells[0].inner_text().strip()
        total_text = cells[1].inner_text().strip()
        picked_txt = cells[2].inner_text().strip()
        unpick_txt = cells[3].inner_text().strip()
        rider      = cells[4].inner_text().strip()
        date_text  = cells[5].inner_text().strip()
        status     = cells[6].inner_text().strip()

        log.info(f"  Row {i+1}: {lds_number} | date='{date_text}'")

        if TARGET_LABEL not in date_text:
            continue

        # Extract ID from class="dropdown-list more-menu-XXXX"
        sheet_id = None
        ul_el = row.query_selector("ul[class*='more-menu-']")
        if ul_el:
            ul_class = ul_el.get_attribute("class") or ""
            m = re.search(r"more-menu-(\d+)", ul_class)
            if m:
                sheet_id = m.group(1)

        if not sheet_id:
            log.warning(f"  Could not extract ID for {lds_number} — skipping")
            continue

        def safe_int(s):
            s = re.sub(r"[^\d]", "", s)
            return int(s) if s else 0

        matched.append({
            "loadsheet_number": lds_number,
            "loadsheet_id":     sheet_id,
            "total_orders":     safe_int(total_text),
            "picked":           safe_int(picked_txt),
            "unpicked":         safe_int(unpick_txt),
            "rider":            rider,
            "date":             date_text,
            "status":           status,
        })
        log.info(f"  MATCHED: {lds_number} => ID={sheet_id}")

    log.info(f"Loadsheets matched for {TARGET_LABEL}: {len(matched)}")
    return matched


# ══════════════════════════════════════════════════════════════════════════
# STEP 4 – Fetch orders from PostEx API
# ══════════════════════════════════════════════════════════════════════════

STATUS_OPTIONS = ["booked", "reattempt", "returned", "cancelled", "delivered"]


def fetch_orders(sheet_id: str, status: str, headers: dict, cookies: dict) -> list:
    all_orders = []
    page_num   = 1
    page_size  = 200

    while True:
        url = (
            f"{API_BASE}/{sheet_id}/order"
            f"?loadSheetId={sheet_id}"
            f"&orderStatusOption={status}"
            f"&direction=desc"
            f"&page={page_num}"
            f"&limit={page_size}"
        )
        log.info(f"    API [{status}] page {page_num} ...")

        try:
            resp = requests.get(url, headers=headers, cookies=cookies, timeout=30)
        except requests.RequestException as e:
            log.error(f"    Request error: {e}")
            break

        if resp.status_code == 401:
            log.error("    401 Unauthorized — JWT invalid or expired")
            break

        if not resp.ok:
            log.warning(f"    HTTP {resp.status_code} for status={status}")
            break

        try:
            data = resp.json()
        except ValueError:
            log.warning("    Non-JSON response")
            break

        orders = (
            data.get("data")
            or data.get("orders")
            or data.get("result")
            or data.get("items")
            or (data if isinstance(data, list) else [])
        )

        if not orders:
            break

        all_orders.extend(orders)
        log.info(f"    Got {len(orders)} orders (total: {len(all_orders)})")

        if len(orders) < page_size:
            break
        page_num += 1

    return all_orders


def fetch_all_orders_for_sheet(sheet_id: str, headers: dict, cookies: dict) -> dict:
    result = {}
    for status in STATUS_OPTIONS:
        orders = fetch_orders(sheet_id, status, headers, cookies)
        result[status] = orders
        log.info(f"    [{status}] => {len(orders)} orders")
        time.sleep(0.3)
    return result


# ══════════════════════════════════════════════════════════════════════════
# STEP 5 – Build summary
# ══════════════════════════════════════════════════════════════════════════

def build_grand_summary(loadsheets: list) -> dict:
    total_handed = sum(s.get("total_orders", 0) for s in loadsheets)
    total_picked = sum(s.get("picked",       0) for s in loadsheets)
    total_unpick = sum(s.get("unpicked",     0) for s in loadsheets)

    status_counts = {}
    total_cod     = 0.0

    for sheet in loadsheets:
        for status, orders in sheet.get("orders_by_status", {}).items():
            status_counts[status] = status_counts.get(status, 0) + len(orders)
            if status == "delivered":
                for o in orders:
                    amt = (
                        o.get("codAmount")
                        or o.get("cod_amount")
                        or o.get("amount")
                        or o.get("orderAmount")
                        or o.get("collectedAmount")
                        or 0
                    )
                    try:
                        total_cod += float(str(amt).replace(",", ""))
                    except (ValueError, TypeError):
                        pass

    return {
        "total_loadsheets":         len(loadsheets),
        "total_orders_handed_over": total_handed,
        "total_picked":             total_picked,
        "total_unpicked":           total_unpick,
        "orders_by_status":         status_counts,
        "reattempt_count":          status_counts.get("reattempt", 0),
        "returned_count":           status_counts.get("returned",  0),
        "delivered_count":          status_counts.get("delivered", 0),
        "estimated_cod_pkr":        round(total_cod, 2),
        "cheque_note": (
            "COD estimate from delivered orders. "
            "Actual cheque depends on PostEx settlement cycle."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════

def main():
    date_tag    = TARGET_DATE.strftime("%Y-%m-%d")
    output_file = OUTPUT_DIR / f"loadsheet_{date_tag}.json"

    log.info(f"=== PostEx Scraper | Target: {TARGET_LABEL} ===")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        # 1. Login
        login(page)

        # 2. Capture JWT + cookies for direct API calls
        auth_headers = get_auth_headers(page)
        auth_cookies = get_cookies(page)

        # 3. Scrape the loadsheet table
        loadsheets = scrape_loadsheet_rows(page)
        browser.close()

    if not loadsheets:
        log.warning(f"No loadsheets found for {TARGET_LABEL}.")
        result = {
            "scrape_date":   date_tag,
            "target_date":   TARGET_LABEL,
            "loadsheets":    [],
            "grand_summary": {},
        }
        output_file.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        return

    # 4. Fetch orders via API for each loadsheet
    log.info(f"Fetching orders for {len(loadsheets)} loadsheet(s) via API ...")
    for sheet in loadsheets:
        sid = sheet["loadsheet_id"]
        log.info(f"  {sheet['loadsheet_number']} (id={sid})")
        sheet["orders_by_status"] = fetch_all_orders_for_sheet(
            sid, auth_headers, auth_cookies
        )

    # 5. Grand summary
    grand_summary = build_grand_summary(loadsheets)

    result = {
        "scrape_date":   date_tag,
        "target_date":   TARGET_LABEL,
        "grand_summary": grand_summary,
        "loadsheets":    loadsheets,
    }

    # 6. Save JSON
    output_file.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    log.info(f"Data saved -> {output_file}")

    # 7. Push to N8N (optional)
    if N8N_WEBHOOK:
        try:
            r = requests.post(N8N_WEBHOOK, json=result, timeout=30)
            r.raise_for_status()
            log.info(f"Pushed to N8N webhook (HTTP {r.status_code})")
        except Exception as e:
            log.error(f"N8N webhook failed: {e}")

    # 8. Print summary
    log.info("=" * 50)
    log.info("GRAND SUMMARY")
    log.info("=" * 50)
    for k, v in grand_summary.items():
        if k != "cheque_note":
            log.info(f"  {k}: {v}")
    log.info("=" * 50)


if __name__ == "__main__":
    main()
