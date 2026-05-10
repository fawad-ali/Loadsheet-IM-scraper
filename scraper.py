"""
PostEx Loadsheet Scraper
========================
Logs into merchant.postex.pk, captures real loadsheet IDs
from API responses, fetches all orders from PostEx APIs,
and saves the data into JSON.

Author: Updated with bulletproof date matching
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


# ───────────────────────────────────────────────────────────────
# Logging - VERBOSE
# ───────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(funcName)s:%(lineno)d | %(message)s",
)

log = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────
# Config
# ───────────────────────────────────────────────────────────────

BASE_URL = "https://merchant.postex.pk"

LOGIN_URL = f"{BASE_URL}/login"

LOADSHEET_URL = f"{BASE_URL}/main/load-sheet-logs"

API_BASE = "https://api.postex.pk/services/merchant/api/load-sheet"

USERNAME = os.environ["POSTEX_USERNAME"]

PASSWORD = os.environ["POSTEX_PASSWORD"]

N8N_WEBHOOK = os.environ.get("N8N_WEBHOOK_URL", "")

OUTPUT_DIR = Path("data")

OUTPUT_DIR.mkdir(exist_ok=True)

log.info(f"BASE_URL: {BASE_URL}")
log.info(f"LOGIN_URL: {LOGIN_URL}")
log.info(f"LOADSHEET_URL: {LOADSHEET_URL}")
log.info(f"API_BASE: {API_BASE}")
log.info(f"OUTPUT_DIR: {OUTPUT_DIR}")


# ───────────────────────────────────────────────────────────────
# Date Handling - BULLETPROOF
# ───────────────────────────────────────────────────────────────

DATE_OVERRIDE = os.environ.get("DATE_OVERRIDE")

log.info(f"DATE_OVERRIDE env var: {DATE_OVERRIDE}")

if DATE_OVERRIDE:

    TARGET_DATE = datetime.strptime(
        DATE_OVERRIDE,
        "%Y-%m-%d"
    )
    log.info(f"Using DATE_OVERRIDE: {DATE_OVERRIDE}")

else:

    TARGET_DATE = datetime.now() - timedelta(days=1)
    log.info(f"Using yesterday's date")

DATE_TAG = TARGET_DATE.strftime("%Y-%m-%d")

# Create target date components for matching
TARGET_MONTH = TARGET_DATE.strftime("%b")  # e.g., "May"
TARGET_DAY = TARGET_DATE.day  # e.g., 9 (as integer)
TARGET_YEAR = TARGET_DATE.year  # e.g., 2026

log.info(f"TARGET_DATE: {TARGET_DATE}")
log.info(f"DATE_TAG: {DATE_TAG}")
log.info(f"Target date components: {TARGET_MONTH} {TARGET_DAY}, {TARGET_YEAR}")


def matches_target_date(date_text):
    """
    Bulletproof date matching.
    Matches dates like "May 9, 2026, 9:16:58 PM" or "May 09, 2026" or variations.
    """
    log.debug(f"Checking if '{date_text}' matches target date...")
    
    # Remove leading/trailing whitespace
    date_text = date_text.strip()
    
    # Extract month, day, year using regex
    # Pattern: "Month DD, YYYY" (supports optional leading zeros on day)
    match = re.search(r"(\w+)\s+(\d{1,2}),\s+(\d{4})", date_text)
    
    if not match:
        log.debug(f"✗ Could not parse date from: '{date_text}'")
        return False
    
    parsed_month = match.group(1)  # e.g., "May"
    parsed_day = int(match.group(2))  # e.g., 9
    parsed_year = int(match.group(3))  # e.g., 2026
    
    log.debug(f"  Parsed from text: month={parsed_month}, day={parsed_day}, year={parsed_year}")
    log.debug(f"  Target:           month={TARGET_MONTH}, day={TARGET_DAY}, year={TARGET_YEAR}")
    
    # Check all three components
    if parsed_month == TARGET_MONTH and parsed_day == TARGET_DAY and parsed_year == TARGET_YEAR:
        log.debug(f"✓ Date MATCHES!")
        return True
    else:
        log.debug(f"✗ Date does not match")
        return False


# ───────────────────────────────────────────────────────────────
# Login
# ───────────────────────────────────────────────────────────────

def login(page):

    log.info(">>> STEP 1: LOGIN START")

    log.debug("Opening login page...")

    page.goto(
        LOGIN_URL,
        wait_until="networkidle"
    )

    log.debug(f"Login page title: {page.title()}")

    log.debug("Filling email field...")

    page.fill(
        'input[type="email"]',
        USERNAME
    )

    log.debug("Filling password field...")

    page.fill(
        'input[type="password"]',
        PASSWORD
    )

    log.debug("Submitting login form...")

    page.click('button[type="submit"]')

    log.debug("Waiting for redirect to main page...")

    page.wait_for_url(
        f"{BASE_URL}/main/**",
        timeout=30000
    )

    log.info(f"✓ LOGIN SUCCESSFUL - Current URL: {page.url}")


# ───────────────────────────────────────────────────────────────
# Auth Session
# ───────────────────────────────────────────────────────────────

def get_auth_session(page):

    log.info(">>> STEP 2: AUTH SESSION START")

    log.debug("Extracting token from localStorage...")

    token = page.evaluate("""
    () => {
        return localStorage.getItem('token')
            || localStorage.getItem('authToken')
            || localStorage.getItem('access_token')
            || sessionStorage.getItem('token')
            || '';
    }
    """)

    log.debug(f"Token extracted: {token[:50] if token else 'NONE'}...")

    if not token:

        raise Exception(
            "JWT token not found after login"
        )

    session = requests.Session()

    session.headers.update({

        "Accept": "application/json, text/plain, */*",

        "Authorization": f"Bearer {token}",

        "Origin": "https://merchant.postex.pk",

        "Referer": "https://merchant.postex.pk/",

        "User-Agent": (
            "Mozilla/5.0 "
            "(Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36"
        )
    })

    # Copy browser cookies

    cookies = page.context.cookies()
    log.debug(f"Browser has {len(cookies)} cookies")

    for c in cookies:

        session.cookies.set(
            c["name"],
            c["value"]
        )
        log.debug(f"  Cookie: {c['name']}")

    log.info(f"✓ AUTH SESSION CREATED with {len(cookies)} cookies")

    return session


# ───────────────────────────────────────────────────────────────
# Loadsheet Fetching with Improved Logic
# ───────────────────────────────────────────────────────────────


def scrape_loadsheet_ids(page):

    log.info(">>> STEP 3: SCRAPE LOADSHEET IDS START")

    log.info(
        f"Opening loadsheet page"
    )

    page.goto(
        LOADSHEET_URL,
        wait_until="domcontentloaded",
        timeout=60000
    )

    log.debug(f"Loadsheet page URL: {page.url}")
    log.debug(f"Page title: {page.title()}")

    # Give Angular time to render
    log.debug("Waiting 10 seconds for Angular to render...")
    time.sleep(10)

    # Wait for table rows to load
    rows = []

    for i in range(30):

        rows = page.query_selector_all("tr.data-item")

        log.debug(f"  Attempt {i+1}/30: Found {len(rows)} rows")

        if rows:
            log.debug(f"✓ Found {len(rows)} rows on attempt {i+1}")
            break

        time.sleep(2)

    if not rows:

        log.error(
            "✗ NO LOADSHEET ROWS FOUND!"
        )

        # Save page HTML for debugging
        html = page.content()

        debug_html_path = OUTPUT_DIR / "debug_page.html"

        with open(
            debug_html_path,
            "w",
            encoding="utf-8"
        ) as f:
            f.write(html)

        log.error(f"✗ Debug HTML saved to: {debug_html_path}")

        return []

    log.info(
        f"✓ Found {len(rows)} table rows total"
    )

    results = []

    for idx, row in enumerate(rows):

        try:

            log.debug(f"\n--- Processing Row {idx+1} ---")

            cells = row.query_selector_all("td")

            log.debug(f"Row has {len(cells)} cells")

            if len(cells) < 7:
                log.debug(f"✗ Row has {len(cells)} cells, need 7+ - SKIPPING")
                continue

            # Extract date from cell 5
            date_text = (
                cells[5]
                .inner_text()
                .strip()
            )

            log.debug(f"Cell 5 (Date): '{date_text}'")

            # Use bulletproof date matching
            if not matches_target_date(date_text):
                continue

            log.info(f"✓✓ DATE MATCHES: '{date_text}'")

            # Extract loadsheet number from cell 0
            loadsheet_number = (
                cells[0]
                .inner_text()
                .strip()
            )

            log.debug(f"Cell 0 (Loadsheet #): {loadsheet_number}")

            # Extract total orders from cell 1
            total_orders_text = (
                cells[1]
                .inner_text()
                .strip()
            )

            log.debug(f"Cell 1 (Total Orders): {total_orders_text}")

            # Extract status from cell 6
            status = (
                cells[6]
                .inner_text()
                .strip()
            )

            log.debug(f"Cell 6 (Status): {status}")

            log.info(
                f"Matched row: {loadsheet_number} "
                f"({total_orders_text} orders, status: {status})"
            )

            captured_sheet_id = []

            def capture_response(response):
                """Capture API response to extract loadsheet ID"""
                log.debug(f"Response event: {response.url}")
                
                if "/load-sheet/" in response.url and "/order" in response.url:
                    log.debug(f"✓ Found matching API response URL: {response.url}")
                    
                    match = re.search(
                        r"/load-sheet/(\d+)/order",
                        response.url
                    )
                    if match:
                        sid = match.group(1)
                        captured_sheet_id.append(sid)
                        log.info(
                            f"✓✓✓ CAPTURED LOADSHEET ID: {sid}"
                        )
                    else:
                        log.debug(f"✗ Regex did not match URL: {response.url}")

            # Listen for API responses
            log.debug("Attaching response listener...")
            page.on(
                "response",
                capture_response
            )

            # Click on the order count span in cell 1 to trigger API call
            log.debug("Looking for order span in cell 1...")
            order_span = cells[1].query_selector("span")
            
            if order_span:
                log.debug(f"✓ Found order span: {order_span.inner_text()}")
                log.info(
                    f"Clicking order count span for "
                    f"{loadsheet_number}"
                )
                order_span.click()
                log.debug("Clicked, waiting 3 seconds for API response...")
                time.sleep(3)
            else:
                log.warning(
                    f"✗ Could not find order span in cell 1 for {loadsheet_number}"
                )

            # Remove response listener
            log.debug("Removing response listener...")
            page.remove_listener(
                "response",
                capture_response
            )

            # If we got an ID from the API response, use it
            if captured_sheet_id:
                sheet_id = captured_sheet_id[0]
                log.info(
                    f"✓✓ Successfully captured ID: {sheet_id}"
                )
            else:
                # Fallback: try to extract from the loadsheet number if it's available
                log.warning(
                    f"✗ No ID captured from API for "
                    f"{loadsheet_number}"
                )
                log.debug(f"Captured sheet ID list was empty")
                continue

            log.info(f"✓ Adding loadsheet to results: {loadsheet_number} => {sheet_id}")

            results.append({

                "loadsheet_number": loadsheet_number,
                "loadsheet_id": sheet_id,
                "total_orders": total_orders_text,
                "date": date_text,
                "status": status

            })

            time.sleep(1)

        except Exception as e:

            log.error(
                f"✗ Failed processing row {idx}: {e}",
                exc_info=True
            )

    log.info(
        f"✓ STEP 3 COMPLETE: Matched {len(results)} loadsheets"
    )

    return results


# ───────────────────────────────────────────────────────────────
# Orders Fetching
# ───────────────────────────────────────────────────────────────

def fetch_orders(
    session,
    sheet_id,
    status="booked"
):

    log.debug(f">>> fetch_orders({sheet_id}, {status})")

    url = (
        f"{API_BASE}/{sheet_id}/order"
    )

    params = {

        "loadSheetId": sheet_id,

        "orderStatusOption": status,

        "direction": "desc"
    }

    log.debug(f"URL: {url}")
    log.debug(f"Params: {params}")

    try:

        log.info(
            f"Fetching {status} orders "
            f"for sheet {sheet_id}..."
        )

        response = session.get(
            url,
            params=params,
            timeout=30
        )

        log.debug(
            f"Response status: {response.status_code}"
        )

        log.info(
            f"[{sheet_id}] {status} -> HTTP {response.status_code}"
        )

        response.raise_for_status()

        data = response.json()

        log.debug(f"Response type: {type(data)}")

        # Debug: log response structure
        if isinstance(data, dict):

            log.debug(
                f"Response is dict with keys: {list(data.keys())}"
            )

        if isinstance(data, list):
            log.info(
                f"✓ Got {len(data)} items as list"
            )
            return data

        # Try different possible response structures
        log.debug("Trying to extract orders from response...")

        result = (
            data.get("data")
            or data.get("result")
            or data.get("orders")
            or data.get("rows")
            or data.get("dist")
            or []
        )

        log.debug(f"Extracted result type: {type(result)}")

        if isinstance(result, list):
            log.info(
                f"✓ Got {len(result)} items from data structure"
            )
            return result

        log.warning(f"Result is not a list, returning empty")

        return []

    except Exception as e:

        log.error(
            f"✗ Failed fetching {status} for sheet {sheet_id}: {e}",
            exc_info=True
        )

        return []


# ───────────────────────────────────────────────────────────────
# Main
# ───────────────────────────────────────────────────────────────

def main():

    log.info("=" * 80)
    log.info("=== POSTEX LOADSHEET SCRAPER - BULLETPROOF DATE MATCHING ===")
    log.info("=" * 80)

    output_file = (
        OUTPUT_DIR
        / f"loadsheet_{DATE_TAG}.json"
    )

    log.info(f"Output file will be: {output_file}")

    with sync_playwright() as pw:

        log.debug("Launching Chromium browser...")

        browser = pw.chromium.launch(

            headless=True,

            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage"
            ]
        )

        log.debug("✓ Browser launched")

        log.debug("Creating new context...")

        context = browser.new_context()

        log.debug("✓ Context created")

        log.debug("Creating new page...")

        page = context.new_page()

        log.debug("✓ Page created")

        try:
            # Login
            login(page)

            # Authenticated requests session
            session = get_auth_session(page)

            # Get loadsheets
            loadsheets = scrape_loadsheet_ids(page)

            log.info(f"Scraping complete. Found {len(loadsheets)} loadsheets")

        except Exception as e:
            log.error(f"✗ Error during scraping: {e}", exc_info=True)
            loadsheets = []

        finally:
            log.debug("Closing browser...")
            browser.close()
            log.debug("✓ Browser closed")

    if not loadsheets:

        log.warning(
            f"✗ No loadsheets found"
        )

        result = {

            "scrape_date": DATE_TAG,

            "target_date": f"{TARGET_MONTH} {TARGET_DAY}, {TARGET_YEAR}",

            "loadsheets": [],

            "error": "No loadsheets matched target date"
        }

        log.debug(f"Writing empty result to {output_file}")

        output_file.write_text(
            json.dumps(
                result,
                indent=2,
                ensure_ascii=False
            )
        )

        log.error(f"✗ Saved empty result to {output_file}")

        return

    log.info(f"✓ Found {len(loadsheets)} loadsheets, fetching orders...")

    # Fetch orders for each loadsheet
    final_data = []

    for sheet in loadsheets:

        sid = sheet["loadsheet_id"]

        log.info(
            f"Processing sheet {sid}"
        )

        sheet_orders = {
            "booked": [],
            "delivered": [],
            "returned": [],
            "reattempt": [],
            "cancelled": []
        }

        # Fetch all order statuses
        for status_key in sheet_orders.keys():
            log.info(
                f"  Fetching {status_key} orders for sheet {sid}"
            )

            orders = fetch_orders(
                session,
                sid,
                status_key
            )

            sheet_orders[status_key] = orders

            log.info(
                f"  ✓ Got {len(orders)} {status_key} orders"
            )

            time.sleep(1)

        sheet["orders_by_status"] = sheet_orders

        final_data.append(sheet)

    result = {

        "scrape_date": DATE_TAG,

        "target_date": f"{TARGET_MONTH} {TARGET_DAY}, {TARGET_YEAR}",

        "loadsheets": final_data,

        "total_loadsheets": len(final_data),

        "summary": {
            "total_booked": sum(
                len(s["orders_by_status"]["booked"])
                for s in final_data
            ),
            "total_delivered": sum(
                len(s["orders_by_status"]["delivered"])
                for s in final_data
            ),
            "total_returned": sum(
                len(s["orders_by_status"]["returned"])
                for s in final_data
            ),
            "total_reattempt": sum(
                len(s["orders_by_status"]["reattempt"])
                for s in final_data
            ),
            "total_cancelled": sum(
                len(s["orders_by_status"]["cancelled"])
                for s in final_data
            )
        }
    }

    log.debug(f"Writing result to {output_file}")

    output_file.write_text(

        json.dumps(
            result,
            indent=2,
            ensure_ascii=False
        )
    )

    log.info(
        f"✓ Saved data -> {output_file}"
    )

    log.info(
        f"✓ Summary: {result['summary']}"
    )

    # Optional webhook

    if N8N_WEBHOOK:

        try:

            log.info("Sending to N8N webhook...")

            r = session.post(
                N8N_WEBHOOK,
                json=result,
                timeout=30
            )

            log.info(
                f"✓ Webhook sent ({r.status_code})"
            )

        except Exception as e:

            log.error(
                f"✗ Webhook failed: {e}",
                exc_info=True
            )

    log.info("=" * 80)
    log.info("=== SCRAPER FINISHED ===")
    log.info("=" * 80)


if __name__ == "__main__":
    main()
