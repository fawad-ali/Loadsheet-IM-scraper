
"""
PostEx Loadsheet Scraper
========================
Logs into merchant.postex.pk, captures real loadsheet IDs
from API responses, fetches all orders from PostEx APIs,
and saves the data into JSON.

Author: Updated API-based stable version
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
# Loadsheet Fetching
# ───────────────────────────────────────────────────────────────

def scrape_loadsheet_ids(page):

    captured_data = []

    def capture_response(response):

        try:

            url = response.url

            if (
                "/load-sheet" in url
                and response.status == 200
            ):

                content_type = response.headers.get(
                    "content-type",
                    ""
                )

                if "application/json" not in content_type:
                    return

                data = response.json()

                if isinstance(data, dict):

                    possible = (
                        data.get("data")
                        or data.get("result")
                        or data.get("rows")
                    )

                    if isinstance(possible, list):

                        captured_data.extend(possible)

                elif isinstance(data, list):

                    captured_data.extend(data)

        except Exception:
            pass

    page.on(
        "response",
        capture_response
    )

    log.info("Opening loadsheet logs page...")

    page.goto(
        LOADSHEET_URL,
        wait_until="networkidle"
    )

    # Wait longer for Angular requests
    time.sleep(10)

    log.info(
        f"Captured {len(captured_data)} "
        f"API objects"
    )

    results = []

    for item in captured_data:

        try:

            item_str = json.dumps(item)

            # Match target date
            if TARGET_LABEL not in item_str:
                continue

            # Try all common ID fields
            loadsheet_id = (
                item.get("id")
                or item.get("loadSheetId")
                or item.get("loadsheetId")
                or item.get("_id")
            )

            if not loadsheet_id:
                continue

            # Try all common code fields
            loadsheet_number = (
                item.get("loadSheetCode")
                or item.get("loadsheetCode")
                or item.get("code")
                or item.get("sheetCode")
                or ""
            )

            # Date
            created_at = (
                item.get("createdAt")
                or item.get("created_at")
                or item.get("date")
                or ""
            )

            total_orders = (
                item.get("totalOrders")
                or item.get("orderCount")
                or item.get("total")
                or 0
            )

            result = {

                "loadsheet_number": loadsheet_number,

                "loadsheet_id": str(loadsheet_id),

                "total_orders": total_orders,

                "date": created_at,

                "status": item.get(
                    "status",
                    ""
                )
            }

            # Prevent duplicates
            already = any(
                x["loadsheet_id"] == result["loadsheet_id"]
                for x in results
            )

            if not already:

                results.append(result)

                log.info(
                    f"Found loadsheet: "
                    f"{loadsheet_number} "
                    f"(ID={loadsheet_id})"
                )

        except Exception as e:

            log.warning(
                f"Failed parsing loadsheet: {e}"
            )

    log.info(
        f"Final matched loadsheets: {len(results)}"
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

    url = (
        f"{API_BASE}/{sheet_id}/order"
    )

    params = {

        "loadSheetId": sheet_id,

        "orderStatusOption": status,

        "direction": "desc"
    }

    try:

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

        # Debug
        if isinstance(data, dict):

            log.info(
                f"Response keys: "
                f"{list(data.keys())}"
            )

        if isinstance(data, list):
            return data

        return (
            data.get("data")
            or data.get("result")
            or data.get("orders")
            or data.get("rows")
            or []
        )

    except Exception as e:

        log.error(
            f"Failed fetching "
            f"{status} "
            f"for sheet "
            f"{sheet_id}: {e}"
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

        # Login
        login(page)

        # Authenticated requests session
        session = get_auth_session(page)

        # Get loadsheets
        loadsheets = scrape_loadsheet_ids(page)

        browser.close()

    if not loadsheets:

        log.warning(
            "No loadsheets found"
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

    # Fetch orders
    final_data = []

    for sheet in loadsheets:

        sid = sheet["loadsheet_id"]

        log.info(
            f"Fetching orders "
            f"for sheet {sid}"
        )

        booked = fetch_orders(
            session,
            sid,
            "booked"
        )

        delivered = fetch_orders(
            session,
            sid,
            "delivered"
        )

        returned = fetch_orders(
            session,
            sid,
            "returned"
        )

        reattempt = fetch_orders(
            session,
            sid,
            "reattempt"
        )

        cancelled = fetch_orders(
            session,
            sid,
            "cancelled"
        )

        sheet["orders_by_status"] = {

            "booked": booked,

            "delivered": delivered,

            "returned": returned,

            "reattempt": reattempt,

            "cancelled": cancelled
        }

        final_data.append(sheet)

    result = {

        "scrape_date": DATE_TAG,

        "target_date": TARGET_LABEL,

        "loadsheets": final_data
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
                f"Webhook failed: {e}"
            )


if __name__ == "__main__":
    main()
```
