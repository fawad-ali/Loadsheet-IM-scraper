# name=scraper.py

"""
PostEx Loadsheet Scraper - Full verbose debug edition

FIXED VERSION

What this file does:
- Logs into merchant.postex.pk using Playwright
- Captures JWT + cookies
- Finds loadsheet rows
- Captures ALL browser API calls
- Logs exact API URLs
- Saves raw API responses
- Calls PostEx API directly
- Saves everything into JSON debug output

FIXES INCLUDED:
- Completed broken function capture_requests_while_click()
- Fixed incomplete code ending
- Fixed serialization issues with ElementHandle
- Fixed page listener cleanup
- Fixed request/response capture
- Added safe JSON handling
- Added direct API debug logging
- Added browser network capture
"""

import os
import re
import json
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


# ---------------------------------------------------------
# Logging
# ---------------------------------------------------------

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(funcName)s:%(lineno)d | %(message)s",
)

log = logging.getLogger("postex-scraper")


# ---------------------------------------------------------
# Config
# ---------------------------------------------------------

BASE_URL = "https://merchant.postex.pk"

LOGIN_URL = f"{BASE_URL}/login"

LOADSHEET_URL = f"{BASE_URL}/main/load-sheet-logs"

API_BASE = "https://api.postex.pk/services/merchant/api/load-sheet"

USERNAME = os.environ.get("POSTEX_USERNAME", "")

PASSWORD = os.environ.get("POSTEX_PASSWORD", "")

N8N_WEBHOOK = os.environ.get("N8N_WEBHOOK_URL", "")

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

TARGET_LABEL = f"{TARGET_MONTH} {TARGET_DAY}, {TARGET_YEAR}"

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

    if not date_text:
        return False

    date_text = date_text.strip()

    m = re.search(
        r"(\w+)\s+(\d{1,2}),\s+(\d{4})",
        date_text
    )

    if not m:
        return False

    month, day_s, year_s = m.groups()

    try:

        day = int(day_s)

        year = int(year_s)

    except Exception:

        return False

    return (

        month == TARGET_MONTH
        and day == TARGET_DAY
        and year == TARGET_YEAR
    )


# ---------------------------------------------------------
# Login
# ---------------------------------------------------------

def login(page):

    log.info("Opening login page")

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

    page.click(
        'button[type="submit"]'
    )

    page.wait_for_url(
        f"{BASE_URL}/main/**",
        timeout=30000
    )

    log.info(
        f"Login success -> {page.url}"
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

    log.info(
        f"Token found: {bool(token)}"
    )

    session = requests.Session()

    session.headers.update({

        "Accept": "application/json, text/plain, */*",

        "Authorization": f"Bearer {token}",

        "Origin": "https://merchant.postex.pk",

        "Referer": "https://merchant.postex.pk/",

        "User-Agent": (
            "Mozilla/5.0 "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko)"
        )
    })

    cookies = page.context.cookies()

    for c in cookies:

        session.cookies.set(
            c["name"],
            c["value"]
        )

    log.info(
        f"Copied {len(cookies)} cookies"
    )

    return session


# ---------------------------------------------------------
# Capture Network Requests
# ---------------------------------------------------------

def capture_requests_while_click(

    page,

    element,

    timeout=6
):

    captured = {

        "requests": [],

        "responses": [],

        "matching_urls": []
    }

    def on_request(request):

        try:

            req = {

                "url": request.url,

                "method": request.method,

                "resource_type": request.resource_type,

                "headers": dict(request.headers)
            }

            captured["requests"].append(req)

            log.info(
                f"REQUEST -> {request.url}"
            )

            if "load-sheet" in request.url:

                captured["matching_urls"].append(
                    request.url
                )

        except Exception as e:

            log.error(
                f"Request capture error: {e}"
            )

    def on_response(response):

        try:

            body = ""

            try:
                body = response.text()
            except Exception:
                pass

            res = {

                "url": response.url,

                "status": response.status,

                "headers": dict(response.headers),

                "body_preview": body[:3000]
            }

            captured["responses"].append(res)

            log.info(
                f"RESPONSE -> "
                f"{response.status} "
                f"{response.url}"
            )

        except Exception as e:

            log.error(
                f"Response capture error: {e}"
            )

    page.on(
        "request",
        on_request
    )

    page.on(
        "response",
        on_response
    )

    try:

        if hasattr(element, "click"):

            element.click()

        else:

            page.click(element)

    except Exception as e:

        log.error(
            f"Click failed: {e}"
        )

    time.sleep(timeout)

    try:

        page.remove_listener(
            "request",
            on_request
        )

        page.remove_listener(
            "response",
            on_response
        )

    except Exception:
        pass

    return captured


# ---------------------------------------------------------
# Find Loadsheet Rows
# ---------------------------------------------------------

def find_rows_dom(page):

    results = []

    selectors = [

        "table tbody tr.data-item",

        "tr.data-item",

        "tbody tr"
    ]

    rows = []

    for sel in selectors:

        try:

            rows = page.query_selector_all(sel)

            if rows:

                log.info(
                    f"Using selector: {sel}"
                )

                break

        except Exception:
            pass

    log.info(
        f"Rows found: {len(rows)}"
    )

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

            log.info(
                f"Matched row -> "
                f"{result}"
            )

            # Try clicking order count
            try:

                clickable = row.query_selector(
                    "span.orders"
                )

                if clickable:

                    network = capture_requests_while_click(
                        page,
                        clickable
                    )

                    result["network_capture"] = network

                    # Search for ID from URLs
                    for url in network["matching_urls"]:

                        mm = re.search(
                            r"/load-sheet/(\d+)/order",
                            url
                        )

                        if mm:

                            result["captured_sheet_id"] = (
                                mm.group(1)
                            )

                            break

            except Exception as e:

                result["network_error"] = str(e)

            results.append(result)

        except Exception as e:

            log.error(
                f"Row parse failed: {e}"
            )

    return results


# ---------------------------------------------------------
# API Fetch
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

    log.info(
        f"API URL -> {final_url}"
    )

    debug = {

        "sheet_id": sheet_id,

        "status": status,

        "url": final_url
    }

    try:

        response = session.get(
            url,
            params=params,
            timeout=30
        )

        debug["status_code"] = (
            response.status_code
        )

        debug["response_text"] = (
            response.text[:10000]
        )

        log.info(
            f"HTTP {response.status_code}"
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

            debug["json"] = data

        except Exception as e:

            debug["json_error"] = str(e)

            return {

                "debug": debug,

                "orders": []
            }

        return {

            "debug": debug,

            "orders": data
        }

    except Exception as e:

        debug["error"] = str(e)

        return {

            "debug": debug,

            "orders": []
        }


# ---------------------------------------------------------
# Main
# ---------------------------------------------------------

def main():

    final = {

        "scrape_date": DATE_TAG,

        "target_date": TARGET_LABEL,

        "loadsheets": [],

        "debug": {}
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

        page.goto(
            LOADSHEET_URL,
            wait_until="networkidle"
        )

        time.sleep(10)

        html = page.content()

        debug_html = (
            DEBUG_DIR
            / "page.html"
        )

        debug_html.write_text(

            html,

            encoding="utf-8"
        )

        rows = find_rows_dom(page)

        final["debug"]["rows_found"] = rows

        for row in rows:

            sid = (

                row.get("captured_sheet_id")
                or row.get("sheet_id")
            )

            row["final_sheet_id"] = sid

            if not sid:
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

    log.info(
        f"Finished -> {OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()
