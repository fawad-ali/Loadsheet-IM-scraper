
"""
PostEx Loadsheet Scraper
========================
Logs into merchant.postex.pk, extracts loadsheet IDs from the rendered page HTML,
calls PostEx API endpoints directly (using JWT + cookies captured from browser),
and saves the combined data into JSON.

This version:
- Uses robust date matching (works across platforms)
- Parses the table HTML content for rows and the numeric ID embedded in the
  dropdown class "more-menu-<id>"
- Calls the API directly for each loadsheet and status (booked/delivered/etc.)
- Saves results to data/loadsheet_YYYY-MM-DD.json
- Verbose logging to help debug remaining issues
"""

import os
import re
import json
import time
import logging
import requests

from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

# ---------------------------
# Logging (verbose)
# ---------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(funcName)s:%(lineno)d | %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------
# Config
# ---------------------------
BASE_URL = "https://merchant.postex.pk"
LOGIN_URL = f"{BASE_URL}/login"
LOADSHEET_URL = f"{BASE_URL}/main/load-sheet-logs"
API_BASE = "https://api.postex.pk/services/merchant/api/load-sheet"

USERNAME = os.environ.get("POSTEX_USERNAME", "")
PASSWORD = os.environ.get("POSTEX_PASSWORD", "")

if not USERNAME or not PASSWORD:
    log.error("POSTEX_USERNAME or POSTEX_PASSWORD environment variable missing")

N8N_WEBHOOK = os.environ.get("N8N_WEBHOOK_URL", "")

OUTPUT_DIR = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)

log.debug(f"BASE_URL: {BASE_URL}")
log.debug(f"LOGIN_URL: {LOGIN_URL}")
log.debug(f"LOADSHEET_URL: {LOADSHEET_URL}")
log.debug(f"API_BASE: {API_BASE}")
log.debug(f"OUTPUT_DIR: {OUTPUT_DIR}")

# ---------------------------
# Date handling (target = yesterday by default)
# ---------------------------
DATE_OVERRIDE = os.environ.get("DATE_OVERRIDE")

if DATE_OVERRIDE:
    TARGET_DATE = datetime.strptime(DATE_OVERRIDE, "%Y-%m-%d")
    log.info(f"Using DATE_OVERRIDE: {DATE_OVERRIDE}")
else:
    TARGET_DATE = datetime.now() - timedelta(days=1)
    log.info("Using yesterday's date as target")

DATE_TAG = TARGET_DATE.strftime("%Y-%m-%d")
TARGET_MONTH = TARGET_DATE.strftime("%b")  # May
TARGET_DAY = TARGET_DATE.day               # 9
TARGET_YEAR = TARGET_DATE.year             # 2026

log.info(f"Target date components: {TARGET_MONTH} {TARGET_DAY}, {TARGET_YEAR}")

# ---------------------------
# Utility: robust date matcher
# ---------------------------
def matches_target_date(date_text: str) -> bool:
    """
    Parse date_text for 'Month Day, Year' and compare to TARGET_DATE components.
    Handles strings like: 'May 9, 2026, 9:16:58 PM' or 'May 09, 2026'
    """
    if not date_text:
        return False
    txt = date_text.strip()
    m = re.search(r"(\w+)\s+(\d{1,2}),\s+(\d{4})", txt)
    if not m:
        log.debug(f"Could not parse date from '{date_text}'")
        return False
    month, day_s, year_s = m.group(1), m.group(2), m.group(3)
    try:
        day = int(day_s)
        year = int(year_s)
    except ValueError:
        return False
    match = (month == TARGET_MONTH and day == TARGET_DAY and year == TARGET_YEAR)
    log.debug(f"Parsed date: {month} {day}, {year} -> matches={match}")
    return match

# ---------------------------
# Login
# ---------------------------
def login(page):
    log.info(">>> STEP 1: LOGIN")
    page.goto(LOGIN_URL, wait_until="networkidle")
    log.debug(f"Login page title: {page.title()}")
    # Try typical email selectors (site uses input[type=email])
    try:
        page.fill('input[type="email"]', USERNAME)
    except Exception:
        # fallback: attempt other selectors silently
        for sel in ('input[name="email"]', 'input[placeholder*="mail" i]', 'input[placeholder*="Email" i]'):
            try:
                if page.locator(sel).count() > 0:
                    page.fill(sel, USERNAME)
                    break
            except Exception:
                continue
    page.fill('input[type="password"]', PASSWORD)
    page.click('button[type="submit"]')
    page.wait_for_url(f"{BASE_URL}/main/**", timeout=30_000)
    log.info(f"✓ Logged in - current url: {page.url}")

# ---------------------------
# Capture session (JWT + cookies) -> requests.Session
# ---------------------------
def get_auth_session(page) -> requests.Session:
    log.info(">>> STEP 2: CAPTURE AUTH (JWT + COOKIES)")
    token = page.evaluate("""() => {
        return localStorage.getItem('token')
            || localStorage.getItem('authToken')
            || localStorage.getItem('access_token')
            || localStorage.getItem('accessToken')
            || sessionStorage.getItem('token')
            || '';
    }""")
    if not token:
        # try scanning localStorage keys for something that looks like JWT
        token = page.evaluate("""() => {
            for (let i = 0; i < localStorage.length; i++) {
                const k = localStorage.key(i);
                const v = localStorage.getItem(k);
                if (v && v.startsWith('eyJ')) return v;
            }
            return '';
        }""")
    if not token:
        log.error("JWT token not found in localStorage/sessionStorage after login")
        raise Exception("JWT token not found")

    # strip Bearer if present
    token = token.replace("Bearer ", "").strip()
    log.debug(f"Token (first 40 chars): {token[:40]}...")

    session = requests.Session()
    session.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Authorization": f"Bearer {token}",
        "Origin": "https://merchant.postex.pk",
        "Referer": "https://merchant.postex.pk/",
        "User-Agent": (
            "Mozilla/5.0 (Playwright) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
        )
    })

    # copy cookies
    cookies = page.context.cookies()
    log.debug(f"Found {len(cookies)} browser cookies")
    for c in cookies:
        session.cookies.set(c["name"], c["value"])
        log.debug(f"  cookie set: {c['name']}")

    log.info("✓ Auth session created (requests.Session)")
    return session

# ---------------------------
# Extract loadsheet rows & numeric IDs from page HTML
# ---------------------------
def extract_sheet_ids_from_html(page) -> list:
    """
    Parse page.content() to find loadsheet rows and numeric sheet IDs embedded in
    the dropdown class 'more-menu-<id>'.

    Returns list of dicts:
      {
        "loadsheet_number": "LDS-ES7BY484060",
        "loadsheet_id": "5381184",
        "total_orders": "28",
        "date": "May 9, 2026, 9:16:58 PM",
        "status": "COMPLETED"
      }
    """
    html = page.content()
    results = []

    # find tr blocks (non-greedy)
    tr_blocks = re.findall(r"(<tr[^>]*>.*?</tr>)", html, flags=re.DOTALL | re.IGNORECASE)
    log.debug(f"HTML contains {len(tr_blocks)} <tr> blocks")

    for block in tr_blocks:
        # look for loadsheet number: inside a span with 'lead' or text 'LDS-...'
        m_lds = re.search(r">(LDS[-A-Z0-9_]+)\s*<", block, flags=re.IGNORECASE)
        if not m_lds:
            m_lds = re.search(r"(LDS[-A-Z0-9_]+)", block, flags=re.IGNORECASE)
        if not m_lds:
            continue
        loadsheet_number = m_lds.group(1).strip()

        # numeric id from 'more-menu-<id>' class inside the block (dropdown)
        m_id = re.search(r"more-menu-(\d+)", block)
        if not m_id:
            # sometimes the class is on a nested element - already searching the block covers that
            continue
        sheet_id = m_id.group(1)

        # date (Month Day, Year) inside the row; pick the first occurrence
        m_date = re.search(r"(\w+\s+\d{1,2},\s+\d{4})", block)
        date_text = m_date.group(1).strip() if m_date else ""

        # total orders - try to find the first numeric span in second cell
        m_total = re.search(
            r"<td[^>]*>\s*<span[^>]*class=[\"'][^\"']*smaller-text[^\"']*[\"'][^>]*>\s*(\d+)\s*</span>",
            block,
            flags=re.IGNORECASE,
        )
        total_orders = m_total.group(1).strip() if m_total else ""

        # status (badge or text) - look for dt-status cell text
        m_status = re.search(r"class=[\"'][^\"']*dt-detail[^\"']*[\"'][^>]*>.*?(?:<span[^>]*>(.*?)</span>)", block, flags=re.IGNORECASE | re.DOTALL)
        status = m_status.group(1).strip() if m_status and m_status.group(1) else ""
        # fallback: any 'COMPLETED' or similar in block
        if not status:
            m_status2 = re.search(r"(COMPLETED|PENDING|OPEN|CLOSED|COMPLETED|COMPLETED\s*)", block, flags=re.IGNORECASE)
            if m_status2:
                status = m_status2.group(1).strip()

        entry = {
            "loadsheet_number": loadsheet_number,
            "loadsheet_id": sheet_id,
            "total_orders": total_orders,
            "date": date_text,
            "status": status,
        }

        # filter by date match if date_text exists, else include (so we can debug later)
        if date_text:
            if matches_target_date(date_text):
                log.debug(f"Matched target date row: {loadsheet_number} => id={sheet_id}")
                results.append(entry)
            else:
                log.debug(f"Row date {date_text} does not match target -> skipping: {loadsheet_number}")
        else:
            # if no date found in block, still include for manual inspection
            log.debug(f"No date found in block for {loadsheet_number}; included for inspection")
            results.append(entry)

    log.info(f"extract_sheet_ids_from_html -> found {len(results)} matching loadsheet(s)")
    return results

# ---------------------------
# Fetch orders from API
# ---------------------------
STATUS_OPTIONS = ["booked", "delivered", "returned", "reattempt", "cancelled"]

def fetch_orders(session: requests.Session, sheet_id: str, status: str = "booked") -> list:
    """
    Calls API GET /load-sheet/{sheet_id}/order with params.
    Supports pagination if API paginates via page/limit query params.
    """
    log.info(f"Fetching orders [{status}] for sheet {sheet_id}")
    page_num = 1
    page_size = 200
    all_orders = []

    while True:
        url = f"{API_BASE}/{sheet_id}/order"
        params = {
            "loadSheetId": sheet_id,
            "orderStatusOption": status,
            "direction": "desc",
            # server might support page/limit - include defaults
            "page": page_num,
            "limit": page_size,
        }
        log.debug(f"GET {url} params={params}")
        try:
            resp = session.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            log.error(f"Request failed: {e}")
            break

        log.debug(f"HTTP {resp.status_code}")
        if resp.status_code == 401:
            log.error("401 Unauthorized - token may have expired")
            break
        if not resp.ok:
            log.warning(f"HTTP {resp.status_code} - skipping further pages for this status")
            break

        try:
            data = resp.json()
        except ValueError:
            log.warning("Response not JSON")
            break

        # possible shapes: list OR dict with keys 'data'/'result'/'orders'/'rows'/'dist'
        if isinstance(data, list):
            items = data
        else:
            items = (
                data.get("data")
                or data.get("result")
                or data.get("orders")
                or data.get("rows")
                or data.get("dist")
                or []
            )

        if not items:
            log.debug("No items returned for this page/status")
            break

        if isinstance(items, list):
            all_orders.extend(items)
            log.info(f"Got {len(items)} orders (accumulated {len(all_orders)})")
        else:
            log.debug("Items is not a list - stop pagination")
            break

        # stop if fewer than page_size returned (last page)
        if len(items) < page_size:
            break
        page_num += 1
        time.sleep(0.2)

    return all_orders

# ---------------------------
# Main orchestration
# ---------------------------
def main():
    log.info("=" * 80)
    log.info("POSTEX LOADSHEET SCRAPER - HTML PARSE + DIRECT API CALLS")
    log.info("=" * 80)
    output_file = OUTPUT_DIR / f"loadsheet_{DATE_TAG}.json"
    log.info(f"Output -> {output_file}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context()
        page = context.new_page()
        try:
            # 1. Login
            login(page)

            # 2. Capture requests.Session with JWT + cookies
            session = get_auth_session(page)

            # 3. Navigate to LOADSHEET_URL and allow render
            log.info(f"Navigating to loadsheet page: {LOADSHEET_URL}")
            page.goto(LOADSHEET_URL, wait_until="domcontentloaded", timeout=60_000)
            # give Angular time to paint DOM fragments
            time.sleep(5)

            # 4. Extract sheet ids from page HTML
            sheets = extract_sheet_ids_from_html(page)
            log.info(f"Found {len(sheets)} sheets matching date from page HTML")

        except Exception as e:
            log.error(f"Error while scraping page: {e}", exc_info=True)
            sheets = []
        finally:
            browser.close()

    if not sheets:
        log.warning("No loadsheets found - writing empty result and exiting")
        result = {
            "scrape_date": DATE_TAG,
            "target_date": f"{TARGET_MONTH} {TARGET_DAY}, {TARGET_YEAR}",
            "loadsheets": [],
            "error": "No loadsheets matched target date"
        }
        output_file.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        log.error(f"Saved empty result -> {output_file}")
        return

    # 5. For each sheet -> call APIs for statuses
    final_data = []
    for s in sheets:
        sid = s["loadsheet_id"]
        log.info(f"Processing sheet {s['loadsheet_number']} (id={sid})")
        orders_by_status = {}
        for status in STATUS_OPTIONS:
            orders = fetch_orders(session, sid, status)
            orders_by_status[status] = orders
            # small delay between statuses
            time.sleep(0.2)
        s["orders_by_status"] = orders_by_status
        final_data.append(s)

    # 6. Build summary
    total_orders_count = sum(
        len(s["orders_by_status"].get("booked", []))
        + len(s["orders_by_status"].get("delivered", []))
        + len(s["orders_by_status"].get("returned", []))
        + len(s["orders_by_status"].get("reattempt", []))
        + len(s["orders_by_status"].get("cancelled", []))
        for s in final_data
    )

    result = {
        "scrape_date": DATE_TAG,
        "target_date": f"{TARGET_MONTH} {TARGET_DAY}, {TARGET_YEAR}",
        "loadsheets": final_data,
        "total_loadsheets": len(final_data),
        "summary": {
            "total_orders_count": total_orders_count
        }
    }

    output_file.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    log.info(f"Saved data -> {output_file}")
    log.info(f"Summary: {result['summary']}")

    # optional webhook
    if N8N_WEBHOOK:
        try:
            r = session.post(N8N_WEBHOOK, json=result, timeout=30)
            r.raise_for_status()
            log.info(f"Pushed to N8N webhook (HTTP {r.status_code})")
        except Exception as e:
            log.error(f"N8N webhook failed: {e}", exc_info=True)

    log.info("=" * 80)
    log.info("SCRAPER FINISHED")
    log.info("=" * 80)


if __name__ == "__main__":
    main()

