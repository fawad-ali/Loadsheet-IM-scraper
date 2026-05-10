"""
PostEx Loadsheet Scraper
========================
DEBUG VERSION

This version:
1. Saves EVERY API URL being called
2. Saves EVERY API response
3. Saves browser-captured request URLs
4. Saves page HTML
5. Saves loadsheet row data
6. Helps identify where scraping fails
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

OUTPUT_DIR = Path("data")

OUTPUT_DIR.mkdir(exist_ok=True)

DEBUG_FILE = OUTPUT_DIR / "debug_output.json"


# ───────────────────────────────────────────────────────────────
# Debug Storage
# ───────────────────────────────────────────────────────────────

DEBUG_DATA = {

    "login": {},

    "loadsheet_page": {},

    "captured_requests": [],

    "table_rows": [],

    "matched_rows": [],

    "api_calls": [],

    "errors": []
}


def save_debug():

    DEBUG_FILE.write_text(
        json.dumps(
            DEBUG_DATA,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    log.info(
        f"Debug data saved -> {DEBUG_FILE}"
    )


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

    page.click('button[type="submit"]')

    page.wait_for_url(
        f"{BASE_URL}/main/**",
        timeout=30000
    )

    current_url = page.url

    DEBUG_DATA["login"] = {

        "success": True,

        "current_url": current_url
    }

    save_debug()

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

    DEBUG_DATA["login"]["token_found"] = bool(token)

    DEBUG_DATA["login"]["token_length"] = len(token)

    save_debug()

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

    for c in page.context.cookies():

        session.cookies.set(
            c["name"],
            c["value"]
        )

    return session


# ───────────────────────────────────────────────────────────────
# Loadsheet Fetching
# ───────────────────────────────────────────────────────────────

def scrape_loadsheet_ids(page):

    log.info(
        f"Opening loadsheet page "
        f"for {TARGET_LABEL}"
    )

    page.goto(
        LOADSHEET_URL,
        wait_until="networkidle",
        timeout=60000
    )

    time.sleep(15)

    DEBUG_DATA["loadsheet_page"] = {

        "url": page.url,

        "title": page.title()
    }

    # Save full HTML
    html = page.content()

    html_file = OUTPUT_DIR / "debug_page.html"

    html_file.write_text(
        html,
        encoding="utf-8"
    )

    DEBUG_DATA["loadsheet_page"]["html_saved"] = str(html_file)

    save_debug()

    # Capture ALL requests
    def capture_all_requests(request):

        req = {

            "url": request.url,

            "method": request.method,

            "resource_type": request.resource_type
        }

        DEBUG_DATA["captured_requests"].append(req)

    page.on(
        "request",
        capture_all_requests
    )

    rows = page.query_selector_all("tr")

    log.info(
        f"Total TR rows found: {len(rows)}"
    )

    DEBUG_DATA["loadsheet_page"]["total_rows"] = len(rows)

    results = []

    for idx, row in enumerate(rows):

        try:

            cells = row.query_selector_all("td")

            row_data = []

            for cell in cells:

                text = cell.inner_text().strip()

                row_data.append(text)

            DEBUG_DATA["table_rows"].append({

                "index": idx,

                "cells": row_data
            })

            save_debug()

            if len(cells) < 7:
                continue

            date_text = row_data[5]

            if TARGET_LABEL not in date_text:
                continue

            loadsheet_number = row_data[0]

            total_orders = row_data[1]

            status = row_data[6]

            log.info(
                f"Matched loadsheet row: "
                f"{loadsheet_number}"
            )

            DEBUG_DATA["matched_rows"].append({

                "loadsheet_number": loadsheet_number,

                "date": date_text,

                "status": status,

                "total_orders": total_orders
            })

            save_debug()

            # Try clicking row buttons
            menu_btn = row.query_selector(
                "a.toggle-tigger"
            )

            if menu_btn:

                menu_btn.click()

                time.sleep(2)

            print_btn = row.query_selector(
                'a[data-target="#r2pPrint"]'
            )

            if print_btn:

                print_btn.click()

                time.sleep(5)

            # Search captured URLs for loadsheet ID
            found_sheet_id = None

            for req in DEBUG_DATA["captured_requests"]:

                match = re.search(
                    r"/load-sheet/(\d+)/order",
                    req["url"]
                )

                if match:

                    found_sheet_id = match.group(1)

                    req["captured_sheet_id"] = found_sheet_id

                    break

            results.append({

                "loadsheet_number": loadsheet_number,

                "loadsheet_id": found_sheet_id,

                "total_orders": total_orders,

                "date": date_text,

                "status": status
            })

            save_debug()

        except Exception as e:

            DEBUG_DATA["errors"].append({

                "stage": "scrape_loadsheet_ids",

                "row": idx,

                "error": str(e)
            })

            save_debug()

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

    full_url = (
        f"{url}"
        f"?loadSheetId={sheet_id}"
        f"&orderStatusOption={status}"
        f"&direction=desc"
    )

    log.info(
        f"CALLING API URL: {full_url}"
    )

    api_debug = {

        "sheet_id": sheet_id,

        "status": status,

        "url": full_url
    }

    try:

        response = session.get(
            url,
            params=params,
            timeout=30
        )

        api_debug["status_code"] = response.status_code

        api_debug["response_text"] = response.text

        api_debug["response_preview"] = response.text[:2000]

        log.info(
            f"HTTP {response.status_code}"
        )

        # Save RAW response
        raw_file = (
            OUTPUT_DIR
            / f"raw_{sheet_id}_{status}.txt"
        )

        raw_file.write_text(
            response.text,
            encoding="utf-8"
        )

        api_debug["raw_response_file"] = str(raw_file)

        try:

            data = response.json()

            api_debug["json_response"] = data

        except Exception as json_error:

            api_debug["json_error"] = str(json_error)

            DEBUG_DATA["api_calls"].append(api_debug)

            save_debug()

            return []

        DEBUG_DATA["api_calls"].append(api_debug)

        save_debug()

        # Return data directly
        if isinstance(data, list):

            return data

        if isinstance(data, dict):

            if "data" in data:

                return data["data"]

            if "result" in data:

                return data["result"]

            if "orders" in data:

                return data["orders"]

        return []

    except Exception as e:

        api_debug["error"] = str(e)

        DEBUG_DATA["api_calls"].append(api_debug)

        DEBUG_DATA["errors"].append({

            "stage": "fetch_orders",

            "sheet_id": sheet_id,

            "status": status,

            "error": str(e)
        })

        save_debug()

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
        f"=== PostEx Scraper DEBUG ==="
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

        login(page)

        session = get_auth_session(page)

        loadsheets = scrape_loadsheet_ids(page)

        browser.close()

    DEBUG_DATA["final_loadsheets"] = loadsheets

    save_debug()

    final_data = []

    for sheet in loadsheets:

        sid = sheet.get("loadsheet_id")

        if not sid:

            continue

        booked = fetch_orders(
            session,
            sid,
            "booked"
        )

        sheet["orders_by_status"] = {

            "booked": booked
        }

        final_data.append(sheet)

        save_debug()

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
        ),
        encoding="utf-8"
    )

    log.info(
        f"Saved -> {output_file}"
    )

    save_debug()


if __name__ == "__main__":
    main()
