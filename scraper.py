# name=scraper.py

"""
PostEx Loadsheet Scraper
CLEAN BUSINESS-FLOW DEBUG VERSION

Logs ONLY important business actions:

1. Login
2. Loadsheet page load
3. Rows found
4. Date matching
5. Clicking loadsheet orders
6. Captured API URL
7. Calling loadsheet API
8. API response
9. Orders extracted

This avoids noisy browser asset logs.
"""

import os
import re
import json
import time
import logging

from datetime import datetime, timedelta
from pathlib import Path

import requests

from playwright.sync_api import (
    sync_playwright
)


# ---------------------------------------------------------
# Logging
# ---------------------------------------------------------

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

log = logging.getLogger("postex-scraper")


STEP_COUNTER = 0


def trace(message, data=None):

    global STEP_COUNTER

    STEP_COUNTER += 1

    prefix = f"[STEP {STEP_COUNTER:05d}]"

    if data is not None:

        try:

            pretty = json.dumps(
                data,
                indent=2,
                ensure_ascii=False,
                default=str
            )

        except Exception:

            pretty = str(data)

        log.info(f"{prefix} {message}\n{pretty}")

    else:

        log.info(f"{prefix} {message}")


# ---------------------------------------------------------
# Config
# ---------------------------------------------------------

BASE_URL = "https://merchant.postex.pk"

LOGIN_URL = f"{BASE_URL}/login"

LOADSHEET_URL = f"{BASE_URL}/main/load-sheet-logs"

API_BASE = (
    "https://api.postex.pk/services/merchant/api/load-sheet"
)

USERNAME = os.environ.get("POSTEX_USERNAME", "")

PASSWORD = os.environ.get("POSTEX_PASSWORD", "")

OUTPUT_DIR = Path("data")

OUTPUT_DIR.mkdir(exist_ok=True)

DEBUG_DIR = OUTPUT_DIR / "debug"

DEBUG_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------
# Date
# ---------------------------------------------------------

DATE_OVERRIDE = os.environ.get("DATE_OVERRIDE")

if DATE_OVERRIDE:

    TARGET_DATE = datetime.strptime(
        DATE_OVERRIDE,
        "%Y-%m-%d"
    )

else:

    TARGET_DATE = datetime.now() - timedelta(days=1)

DATE_TAG = TARGET_DATE.strftime("%Y-%m-%d")

TARGET_MONTH = TARGET_DATE.strftime("%b")

TARGET_DAY = TARGET_DATE.day

TARGET_YEAR = TARGET_DATE.year

TARGET_LABEL = (
    f"{TARGET_MONTH} {TARGET_DAY}, {TARGET_YEAR}"
)

OUTPUT_FILE = (
    OUTPUT_DIR
    / f"loadsheet_{DATE_TAG}.json"
)


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def write_json(path, data):

    path.write_text(

        json.dumps(
            data,
            indent=2,
            ensure_ascii=False
        ),

        encoding="utf-8"
    )


def matches_target_date(date_text):

    trace(
        "CHECKING DATE",
        {
            "incoming": date_text,
            "target": TARGET_LABEL
        }
    )

    if not date_text:
        return False

    date_text = date_text.strip()

    m = re.search(
        r"(\w+)\s+(\d{1,2}),\s+(\d{4})",
        date_text
    )

    if not m:

        trace(
            "DATE FORMAT INVALID"
        )

        return False

    month, day_s, year_s = m.groups()

    try:

        day = int(day_s)

        year = int(year_s)

    except Exception:

        return False

    matched = (

        month == TARGET_MONTH
        and day == TARGET_DAY
        and year == TARGET_YEAR
    )

    if matched:

        trace(
            "DATE MATCHED"
        )

    else:

        trace(
            "DATE NOT MATCHED"
        )

    return matched


# ---------------------------------------------------------
# Login
# ---------------------------------------------------------

def login(page):

    trace(
        "OPENING LOGIN PAGE"
    )

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

    trace(
        "CLICKING LOGIN BUTTON"
    )

    page.click(
        'button[type="submit"]'
    )

    page.wait_for_url(
        f"{BASE_URL}/main/**",
        timeout=30000
    )

    trace(
        "LOGIN SUCCESSFUL",
        {
            "url": page.url
        }
    )


# ---------------------------------------------------------
# Session
# ---------------------------------------------------------

def get_auth_session(page):

    token = page.evaluate("""
    () => {
        return localStorage.getItem('token')
            || localStorage.getItem('authToken')
            || localStorage.getItem('access_token')
            || localStorage.getItem('accessToken')
            || sessionStorage.getItem('token')
            || '';
    }
    """)

    trace(
        "TOKEN EXTRACTED",
        {
            "exists": bool(token)
        }
    )

    session = requests.Session()

    session.headers.update({

        "Accept": "application/json, text/plain, */*",

        "Authorization": f"Bearer {token}",

        "Origin": BASE_URL,

        "Referer": BASE_URL + "/",

        "User-Agent": (
            "Mozilla/5.0"
        )
    })

    cookies = page.context.cookies()

    for c in cookies:

        session.cookies.set(
            c["name"],
            c["value"]
        )

    trace(
        "SESSION READY",
        {
            "cookies": len(cookies)
        }
    )

    return session


# ---------------------------------------------------------
# Click + Capture URL
# ---------------------------------------------------------

def capture_requests_while_click(

    page,
    element,
    timeout=8,
    click_label="unknown_click"
):

    trace(
        "CLICK SESSION START",
        {
            "label": click_label
        }
    )

    captured = {

        "matching_urls": []
    }

    # ---------------------------------------------
    # Request listener
    # ---------------------------------------------

    def on_request(request):

        try:

            url = request.url

            if (

                "load-sheet" in url
                and "/order" in url

            ):

                trace(
                    "LOADSHEET API DETECTED",
                    {
                        "url": url,
                        "method": request.method
                    }
                )

                captured["matching_urls"].append(
                    url
                )

        except Exception as e:

            trace(
                "REQUEST CAPTURE ERROR",
                str(e)
            )

    page.on(
        "request",
        on_request
    )

    # ---------------------------------------------
    # Click
    # ---------------------------------------------

    try:

        text = element.inner_text()

        trace(
            "CLICKING ELEMENT",
            {
                "text": text
            }
        )

        element.click()

        trace(
            "ELEMENT CLICKED"
        )

    except Exception as e:

        trace(
            "CLICK FAILED",
            str(e)
        )

    # ---------------------------------------------
    # Wait for requests
    # ---------------------------------------------

    time.sleep(timeout)

    # ---------------------------------------------
    # Cleanup
    # ---------------------------------------------

    try:

        page.remove_listener(
            "request",
            on_request
        )

    except Exception:
        pass

    trace(
        "CLICK SESSION END",
        {
            "urls_found":
                len(captured["matching_urls"])
        }
    )

    return captured


# ---------------------------------------------------------
# Find Rows
# ---------------------------------------------------------

def find_rows_dom(page):

    trace(
        "SEARCHING LOADSHEET ROWS"
    )

    results = []

    selectors = [

        "table tbody tr.data-item",

        "tr.data-item",

        "tbody tr"
    ]

    rows = []

    for sel in selectors:

        rows = page.query_selector_all(sel)

        if rows:

            trace(
                "ROWS FOUND",
                {
                    "selector": sel,
                    "count": len(rows)
                }
            )

            break

    if not rows:

        raise Exception("NO ROWS FOUND")

    for idx, row in enumerate(rows):

        try:

            cells = row.query_selector_all("td")

            if len(cells) < 7:
                continue

            values = []

            for cell in cells:

                try:

                    values.append(
                        cell.inner_text().strip()
                    )

                except Exception:

                    values.append("")

            loadsheet_number = values[0]

            total_orders = values[1]

            date_text = values[5]

            status = values[6]

            trace(
                "ROW FOUND",
                {
                    "loadsheet": loadsheet_number,
                    "orders": total_orders,
                    "date": date_text,
                    "status": status
                }
            )

            if not matches_target_date(
                date_text
            ):
                continue

            outer = row.inner_html()

            m = re.search(
                r"more-menu-(\d+)",
                outer
            )

            sheet_id = (
                m.group(1)
                if m else None
            )

            result = {

                "row_index": idx,

                "loadsheet_number": loadsheet_number,

                "sheet_id": sheet_id,

                "date_text": date_text,

                "status": status,

                "total_orders": total_orders
            }

            # -----------------------------------------
            # Click orders element
            # -----------------------------------------

            clickable = row.query_selector(
                "span.orders"
            )

            if clickable:

                network = capture_requests_while_click(

                    page,

                    clickable,

                    timeout=8,

                    click_label=(
                        f"row_{idx}_orders_click"
                    )
                )

                result["network_capture"] = network

                for url in network["matching_urls"]:

                    mm = re.search(
                        r"/load-sheet/(\d+)/order",
                        url
                    )

                    if mm:

                        result["captured_sheet_id"] = (
                            mm.group(1)
                        )

                        trace(
                            "LOADSHEET ID CAPTURED",
                            {
                                "sheet_id":
                                    mm.group(1)
                            }
                        )

                        break

            results.append(result)

        except Exception as e:

            trace(
                "ROW PROCESSING FAILED",
                str(e)
            )

    return results


# ---------------------------------------------------------
# Fetch Orders
# ---------------------------------------------------------

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

    final_url = (

        f"{url}"
        f"?loadSheetId={sheet_id}"
        f"&orderStatusOption={status}"
        f"&direction=desc"
    )

    trace(
        "CALLING LOADSHEET API",
        {
            "sheet_id": sheet_id,
            "status": status,
            "url": final_url
        }
    )

    try:

        response = session.get(
            url,
            params=params,
            timeout=30
        )

        trace(
            "LOADSHEET API RESPONSE",
            {
                "status_code":
                    response.status_code,

                "response_length":
                    len(response.text)
            }
        )

        raw_file = (

            DEBUG_DIR
            / f"api_{sheet_id}_{status}.txt"
        )

        raw_file.write_text(

            response.text,

            encoding="utf-8"
        )

        try:

            data = response.json()

        except Exception:

            data = []

        trace(
            "ORDERS EXTRACTED",
            {
                "count": len(data)
            }
        )

        return {

            "orders": data,

            "status_code":
                response.status_code
        }

    except Exception as e:

        trace(
            "API REQUEST FAILED",
            str(e)
        )

        return {

            "orders": [],

            "status_code": None
        }


# ---------------------------------------------------------
# Main
# ---------------------------------------------------------

def main():

    trace(
        "SCRAPER STARTED"
    )

    final = {

        "scrape_date": DATE_TAG,

        "target_date": TARGET_LABEL,

        "loadsheets": []
    }

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

        login(page)

        session = get_auth_session(page)

        trace(
            "OPENING LOADSHEET PAGE"
        )

        page.goto(
            LOADSHEET_URL,
            wait_until="networkidle"
        )

        time.sleep(5)

        rows = find_rows_dom(page)

        trace(
            "MATCHED ROWS",
            {
                "count": len(rows)
            }
        )

        for row in rows:

            sid = (

                row.get("captured_sheet_id")
                or row.get("sheet_id")
            )

            row["final_sheet_id"] = sid

            if not sid:

                trace(
                    "SKIPPING ROW - NO SHEET ID"
                )

                continue

            booked = fetch_orders(

                session,

                sid,

                "booked"
            )

            row["api_result"] = booked

            final["loadsheets"].append(row)

        browser.close()

    write_json(
        OUTPUT_FILE,
        final
    )

    trace(
        "SCRAPER FINISHED",
        {
            "output_file":
                str(OUTPUT_FILE),

            "loadsheets_saved":
                len(final["loadsheets"])
        }
    )


if __name__ == "__main__":

    main()
