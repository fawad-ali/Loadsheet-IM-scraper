"""
PostEx Loadsheet Scraper
========================
Logs into merchant.postex.pk, captures loadsheet data
from the modal table that opens when clicking rows,
and saves the data into JSON.

Author: FIXED - Extracts data from modal instead of API
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
# ───────────────────────────────────────────��───────────────────

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
# Extract Orders from Modal
# ───────────────────────────────────────────────────────────────

def extract_orders_from_modal(page):
    """
    Extract orders from the modal table that's currently displayed.
    Modal HTML structure:
    <table id="export-excel-table">
      <tbody>
        <tr>
          <td>Sr.No</td>
          <td>Order Ref (IM-xxxxx)</td>
          <td>Tracking #</td>
          <td>Date</td>
          <td>Status</td>
          <td>Name</td>
          <td>Phone</td>
          <td>Amount</td>
        </tr>
      </tbody>
    </table>
    """
    
    log.debug("Extracting orders from modal table...")
    
    try:
        # Wait for the modal table to be visible
        page.wait_for_selector("table#export-excel-table tbody tr", timeout=5000)
        log.debug("✓ Modal table found")
    except Exception as e:
        log.warning(f"✗ Could not find modal table: {e}")
        return []
    
    time.sleep(1)  # Give it a moment to render
    
    rows = page.query_selector_all("table#export-excel-table tbody tr")
    
    log.debug(f"Found {len(rows)} order rows in modal")
    
    orders = []
    
    for idx, row in enumerate(rows):
        try:
            cells = row.query_selector_all("td")
            
            if len(cells) < 8:
                log.debug(f"Row {idx} has only {len(cells)} cells, skipping")
                continue
            
            sr_no = cells[0].inner_text().strip()
            order_ref = cells[1].inner_text().strip()
            tracking = cells[2].inner_text().strip()
            date_time = cells[3].inner_text().strip()
            status = cells[4].inner_text().strip()
            name = cells[5].inner_text().strip()
            phone = cells[6].inner_text().strip()
            amount = cells[7].inner_text().strip()
            
            log.debug(f"  Order {sr_no}: {order_ref} - {amount}")
            
            orders.append({
                "sr_no": sr_no,
                "order_ref": order_ref,
                "tracking": tracking,
                "date": date_time,
                "status": status,
                "name": name,
                "phone": phone,
                "amount": amount
            })
        
        except Exception as e:
            log.warning(f"✗ Failed to extract row {idx}: {e}")
    
    log.info(f"✓ Extracted {len(orders)} orders from modal")
    return orders


# ───────────────────────────────────────────────────────────────
# Loadsheet Fetching
# ───────────────────────────────────────────────────────────────


def scrape_loadsheet_rows(page):

    log.info(">>> STEP 3: SCRAPE LOADSHEET ROWS START")

    log.info(f"Opening loadsheet page: {LOADSHEET_URL}")

    page.goto(LOADSHEET_URL, wait_until="domcontentloaded", timeout=60000)

    log.debug(f"Loadsheet page URL: {page.url}")
    log.debug(f"Page title: {page.title()}")

    # Wait for the actual loadsheet page to load
    log.debug("Waiting for loadsheet table to load...")
    
    for attempt in range(15):
        try:
            page.wait_for_selector("table#excel-table tbody tr.data-item", timeout=2000)
            log.debug(f"✓ Table found on attempt {attempt + 1}")
            break
        except:
            log.debug(f"Attempt {attempt + 1}: table not found yet, waiting...")
            time.sleep(2)
    else:
        log.error("✗ Table never appeared!")
        return []

    time.sleep(3)  # Extra wait for Angular to fully render

    rows = page.query_selector_all("table#excel-table tbody tr.data-item")

    log.info(f"✓ Found {len(rows)} loadsheet rows")

    if not rows:
        log.error("✗ No loadsheet rows found!")
        html = page.content()
        debug_html_path = OUTPUT_DIR / "debug_page.html"
        with open(debug_html_path, "w", encoding="utf-8") as f:
            f.write(html)
        log.error(f"✗ Debug HTML saved to: {debug_html_path}")
        return []

    results = []

    for idx, row in enumerate(rows):

        try:

            log.debug(f"\n--- Processing Loadsheet Row {idx+1} ---")

            cells = row.query_selector_all("td")

            log.debug(f"Row has {len(cells)} cells")

            if len(cells) < 7:
                log.debug(f"✗ Row has {len(cells)} cells, need 7+ - SKIPPING")
                continue

            # Cell structure from the HTML you provided:
            # 0: LoadSheet #
            # 1: Total Orders (as span with click handler)
            # 2: Picked
            # 3: Unpicked
            # 4: Rider Name
            # 5: Date
            # 6: Status
            # 7: Action (dropdown menu)

            loadsheet_number = cells[0].inner_text().strip()
            total_orders_text = cells[1].inner_text().strip()
            date_text = cells[5].inner_text().strip()
            status = cells[6].inner_text().strip()

            log.debug(f"Loadsheet #: {loadsheet_number}")
            log.debug(f"Total Orders: {total_orders_text}")
            log.debug(f"Date: {date_text}")
            log.debug(f"Status: {status}")

            # Check if date matches
            if not matches_target_date(date_text):
                continue

            log.info(f"✓✓ DATE MATCHES: {loadsheet_number}")

            # Click on the total orders count to open the modal
            log.info(f"Clicking on orders count for {loadsheet_number}...")
            
            order_span = cells[1].query_selector("span")
            if not order_span:
                log.warning(f"✗ Could not find order span")
                continue
            
            order_span.click()
            
            log.debug("Waiting for modal to appear...")
            time.sleep(2)

            # Extract orders from the modal
            orders = extract_orders_from_modal(page)

            # Close the modal
            log.debug("Closing modal...")
            page.keyboard.press("Escape")
            time.sleep(1)

            # Add to results
            results.append({
                "loadsheet_number": loadsheet_number,
                "total_orders": total_orders_text,
                "date": date_text,
                "status": status,
                "orders": orders
            })

            log.info(f"✓ Added loadsheet: {loadsheet_number}")

        except Exception as e:

            log.error(
                f"✗ Failed processing row {idx}: {e}",
                exc_info=True
            )

    log.info(f"✓ STEP 3 COMPLETE: Matched {len(results)} loadsheets")

    return results


# ───────────────────────────────────────────────────────────────
# Main
# ───────────────────────────────────────────────────────────────

def main():

    log.info("=" * 80)
    log.info("=== POSTEX LOADSHEET SCRAPER - MODAL EXTRACTION ===")
    log.info("=" * 80)

    output_file = OUTPUT_DIR / f"loadsheet_{DATE_TAG}.json"

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

        context = browser.new_context()

        log.debug("✓ Context created")

        page = context.new_page()

        log.debug("✓ Page created")

        try:
            # Login
            login(page)

            # Authenticated requests session
            session = get_auth_session(page)

            # Get loadsheets and extract orders from modals
            loadsheets = scrape_loadsheet_rows(page)

            log.info(f"Scraping complete. Found {len(loadsheets)} loadsheets")

        except Exception as e:
            log.error(f"✗ Error during scraping: {e}", exc_info=True)
            loadsheets = []

        finally:
            log.debug("Closing browser...")
            browser.close()
            log.debug("✓ Browser closed")

    if not loadsheets:

        log.warning(f"✗ No loadsheets found")

        result = {
            "scrape_date": DATE_TAG,
            "target_date": f"{TARGET_MONTH} {TARGET_DAY}, {TARGET_YEAR}",
            "loadsheets": [],
            "error": "No loadsheets matched target date"
        }

        output_file.write_text(
            json.dumps(result, indent=2, ensure_ascii=False)
        )

        log.error(f"✗ Saved empty result to {output_file}")
        return

    log.info(f"✓ Found {len(loadsheets)} loadsheets with orders")

    result = {
        "scrape_date": DATE_TAG,
        "target_date": f"{TARGET_MONTH} {TARGET_DAY}, {TARGET_YEAR}",
        "loadsheets": loadsheets,
        "total_loadsheets": len(loadsheets),
        "summary": {
            "total_orders": sum(
                len(ls["orders"]) for ls in loadsheets
            ),
            "total_amount_pkr": sum(
                float(o["amount"].replace(",", "")) 
                for ls in loadsheets 
                for o in ls["orders"]
                if o["amount"] and o["amount"] != "0.00"
            )
        }
    }

    log.debug(f"Writing result to {output_file}")

    output_file.write_text(
        json.dumps(result, indent=2, ensure_ascii=False)
    )

    log.info(f"✓ Saved data -> {output_file}")
    log.info(f"✓ Summary: {result['summary']}")

    # Optional webhook
    if N8N_WEBHOOK:
        try:
            log.info("Sending to N8N webhook...")
            r = session.post(N8N_WEBHOOK, json=result, timeout=30)
            log.info(f"✓ Webhook sent ({r.status_code})")
        except Exception as e:
            log.error(f"✗ Webhook failed: {e}", exc_info=True)

    log.info("=" * 80)
    log.info("=== SCRAPER FINISHED ===")
    log.info("=" * 80)


if __name__ == "__main__":
    main()
