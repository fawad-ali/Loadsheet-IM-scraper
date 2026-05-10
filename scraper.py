"""
PostEx Loadsheet Scraper
========================
Logs into merchant.postex.pk, captures real loadsheet IDs
from API responses, fetches all orders from PostEx APIs,
and saves the data into JSON.

Author: Updated API-based stable version with improved extraction
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
# Logging
# ───────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
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


# ───────────────────────────────────────────────────────────────
# Date Handling
# ───────────────────────────────────────────────────────────────

DATE_OVERRIDE = os.environ.get("DATE_OVERRIDE")

if DATE_OVERRIDE:

    TARGET_DATE = datetime.strptime(
        DATE_OVERRIDE,
        "%Y-%m-%d"
    )

else:

    TARGET_DATE = datetime.now() - timedelta(days=1)

DATE_TAG = TARGET_DATE.strftime("%Y-%m-%d")

TARGET_LABEL = TARGET_DATE.strftime("%b %-d, %Y")


# ───────────────────────────────────────────────────────────────
# Login
# ───────────────────────────────────────────────────────────────

def login(page):

    log.info("Opening login page...")

    page.goto(
        LOGIN_URL,
        wait_until="networkidle"
    )

    page.fill(
        'input[type="email"]',
        USERNAME
    )

    page.fill(
        'input[type="password"]',
        PASSWORD
    )

    log.info("Submitting login form...")

    page.click('button[type="submit"]')

    page.wait_for_url(
        f"{BASE_URL}/main/**",
        timeout=30000
    )

    log.info("Login successful")


# ───────────────────────────────────────────────────────────────
# Auth Session
# ───────────────────────────────────────────────────────────────

def get_auth_session(page):

    token = page.evaluate("""
    () => {
        return localStorage.getItem('token')
            || localStorage.getItem('authToken')
            || localStorage.getItem('access_token')
            || sessionStorage.getItem('token')
            || '';
    }
    """)

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

    for c in page.context.cookies():

        session.cookies.set(
            c["name"],
            c["value"]
        )

    log.info("Authenticated API session created")

    return session


# ───────────────────────────────────────────────────────────────
# Loadsheet Fetching with Improved Logic
# ───────────────────────────────────────────────────────────────


def scrape_loadsheet_ids(page):

    log.info(
        f"Opening loadsheet page for "
        f"{TARGET_LABEL}"
    )

    page.goto(
        LOADSHEET_URL,
        wait_until="domcontentloaded",
        timeout=60000
    )

    # Give Angular time to render
    time.sleep(10)

    # Wait for table rows to load
    rows = []

    for i in range(30):

        rows = page.query_selector_all("tr.data-item")

        if rows:
            break

        log.info(
            f"Waiting for loadsheet rows... "
            f"{i+1}/30"
        )

        time.sleep(2)

    if not rows:

        log.error(
            "No loadsheet rows found!"
        )

        # Save page HTML for debugging
        html = page.content()

        with open(
            "debug_page.html",
            "w",
            encoding="utf-8"
        ) as f:
            f.write(html)

        return []

    log.info(
        f"Found {len(rows)} table rows"
    )

    results = []

    for idx, row in enumerate(rows):

        try:

            cells = row.query_selector_all("td")

            if len(cells) < 7:
                continue

            # Extract date from cell 5
            date_text = (
                cells[5]
                .inner_text()
                .strip()
            )

            log.info(
                f"Row {idx}: Date = {date_text}, "
                f"Target = {TARGET_LABEL}"
            )

            # Check if date matches target
            if TARGET_LABEL not in date_text:
                continue

            # Extract loadsheet number from cell 0
            loadsheet_number = (
                cells[0]
                .inner_text()
                .strip()
            )

            # Extract total orders from cell 1
            total_orders = (
                cells[1]
                .inner_text()
                .strip()
            )

            # Extract status from cell 6
            status = (
                cells[6]
                .inner_text()
                .strip()
            )

            log.info(
                f"Matched row: "
                f"{loadsheet_number} - "
                f"{total_orders} orders"
            )

            captured_sheet_id = []

            def capture_response(response):
                """Capture API response to extract loadsheet ID"""
                if "/load-sheet/" in response.url and "/order" in response.url:
                    match = re.search(
                        r"/load-sheet/(\d+)/order",
                        response.url
                    )
                    if match:
                        sid = match.group(1)
                        captured_sheet_id.append(sid)
                        log.info(
                            f"Captured loadsheet ID from response: {sid}"
                        )

            # Listen for API responses
            page.on(
                "response",
                capture_response
            )

            # Click on the order count span in cell 1 to trigger API call
            order_span = cells[1].query_selector("span")
            
            if order_span:
                log.info(
                    f"Clicking order count for "
                    f"{loadsheet_number}"
                )
                order_span.click()
                time.sleep(3)
            else:
                log.warning(
                    f"Could not find order span for {loadsheet_number}"
                )

            # Remove response listener
            page.remove_listener(
                "response",
                capture_response
            )

            # If we got an ID from the API response, use it
            if captured_sheet_id:
                sheet_id = captured_sheet_id[0]
                log.info(
                    f"Successfully captured ID: {sheet_id}"
                )
            else:
                # Fallback: try to extract from the loadsheet number if it's available
                log.warning(
                    f"No ID captured from API for "
                    f"{loadsheet_number}, attempting fallback"
                )
                continue

            results.append({

                "loadsheet_number": loadsheet_number,
                "loadsheet_id": sheet_id,
                "total_orders": int(total_orders),
                "date": date_text,
                "status": status

            })

            time.sleep(1)

        except Exception as e:

            log.error(
                f"Failed row {idx}: {e}",
                exc_info=True
            )

    log.info(
        f"Matched loadsheets: "
        f"{len(results)}"
    )

    return results


# ───────────────────────────────────────────────────────────────
# Orders Fetching with All Statuses
# ───────────────────────────────────────────────────────────────

def fetch_orders(
    session,
    sheet_id,
    status="booked"
):

    url = (
        f"{API_BASE}/{sheet_id}/order"
    )

    params = {

        "loadSheetId": sheet_id,

        "orderStatusOption": status,

        "direction": "desc"
    }

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

        log.info(
            f"[{sheet_id}] "
            f"{status} "
            f"-> HTTP {response.status_code}"
        )

        response.raise_for_status()

        data = response.json()

        # Debug: log response structure
        if isinstance(data, dict):

            log.info(
                f"Response keys: "
                f"{list(data.keys())}"
            )

        if isinstance(data, list):
            log.info(
                f"Got {len(data)} items as list"
            )
            return data

        # Try different possible response structures
        result = (
            data.get("data")
            or data.get("result")
            or data.get("orders")
            or data.get("rows")
            or data.get("dist")
            or []
        )

        if isinstance(result, list):
            log.info(
                f"Got {len(result)} items from "
                f"data structure"
            )
            return result

        return []

    except Exception as e:

        log.error(
            f"Failed fetching "
            f"{status} "
            f"for sheet "
            f"{sheet_id}: {e}",
            exc_info=True
        )

        return []


# ───────────────────────────────────────────────────────────────
# Main
# ───────────────────────────────────────────────────────────────

def main():

    output_file = (
        OUTPUT_DIR
        / f"loadsheet_{DATE_TAG}.json"
    )

    log.info(
        f"=== PostEx Scraper ==="
    )

    log.info(
        f"Target date: {TARGET_LABEL}"
    )

    with sync_playwright() as pw:

        browser = pw.chromium.launch(

            headless=True,

            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage"
            ]
        )

        context = browser.new_context()

        page = context.new_page()

        try:
            # Login
            login(page)

            # Authenticated requests session
            session = get_auth_session(page)

            # Get loadsheets
            loadsheets = scrape_loadsheet_ids(page)

        finally:
            browser.close()

    if not loadsheets:

        log.warning(
            "No loadsheets found for date "
            f"{TARGET_LABEL}"
        )

        result = {

            "scrape_date": DATE_TAG,

            "target_date": TARGET_LABEL,

            "loadsheets": []
        }

        output_file.write_text(
            json.dumps(
                result,
                indent=2,
                ensure_ascii=False
            )
        )

        return

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
                f"Fetching {status_key} orders "
                f"for sheet {sid}"
            )

            orders = fetch_orders(
                session,
                sid,
                status_key
            )

            sheet_orders[status_key] = orders

            log.info(
                f"Got {len(orders)} {status_key} orders"
            )

            time.sleep(1)

        sheet["orders_by_status"] = sheet_orders

        final_data.append(sheet)

    result = {

        "scrape_date": DATE_TAG,

        "target_date": TARGET_LABEL,

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

    output_file.write_text(

        json.dumps(
            result,
            indent=2,
            ensure_ascii=False
        )
    )

    log.info(
        f"Saved data -> {output_file}"
    )

    log.info(
        f"Summary: {result['summary']}"
    )

    # Optional webhook

    if N8N_WEBHOOK:

        try:

            r = session.post(
                N8N_WEBHOOK,
                json=result,
                timeout=30
            )

            log.info(
                f"Webhook sent "
                f"({r.status_code})"
            )

        except Exception as e:

            log.error(
                f"Webhook failed: {e}",
                exc_info=True
            )


if __name__ == "__main__":
    main()
