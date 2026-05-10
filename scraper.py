# name=scraper.py

"""
PostEx Loadsheet Scraper
ULTRA VERBOSE FORENSIC DEBUG EDITION

Features:
- Full Playwright tracing
- HAR recording
- Browser console capture
- Request/response interception
- Full HTML dumps
- Full screenshots
- Full API logging
- Full row diagnostics
- Full exception stack traces
- Timing metrics
- Network archive generation
- Retry wrapper
- Storage dump
- Cookie dump
- Request failure capture
- Click-to-network tracing
- Direct API replay logging
"""

import os
import re
import json
import time
import traceback
import logging

from datetime import datetime, timedelta
from pathlib import Path

import requests

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PWTimeout
)


# ---------------------------------------------------------
# Logging
# ---------------------------------------------------------

logging.basicConfig(
    level=logging.NOTSET,
    format="%(asctime)s [%(levelname)s] %(funcName)s:%(lineno)d | %(message)s",
)

log = logging.getLogger("postex-scraper")


# ---------------------------------------------------------
# Ultra Verbose Logger
# ---------------------------------------------------------

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

        log.debug(f"{prefix} {message}\n{pretty}")

    else:

        log.debug(f"{prefix} {message}")


# ---------------------------------------------------------
# Config
# ---------------------------------------------------------

BASE_URL = "https://merchant.postex.pk"

LOGIN_URL = f"{BASE_URL}/login"

LOADSHEET_URL = f"{BASE_URL}/main/load-sheet-logs"

API_BASE = "https://api.postex.pk/services/merchant/api/load-sheet"

USERNAME = os.environ.get("POSTEX_USERNAME", "")

PASSWORD = os.environ.get("POSTEX_PASSWORD", "")

OUTPUT_DIR = Path("data")

OUTPUT_DIR.mkdir(exist_ok=True)

DEBUG_DIR = OUTPUT_DIR / "debug"

DEBUG_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------
# Environment Debug
# ---------------------------------------------------------

trace("Environment variables loaded", {

    "BASE_URL": BASE_URL,

    "LOGIN_URL": LOGIN_URL,

    "LOADSHEET_URL": LOADSHEET_URL,

    "USERNAME_EXISTS": bool(USERNAME),

    "PASSWORD_EXISTS": bool(PASSWORD),

    "OUTPUT_DIR": str(OUTPUT_DIR),
})


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

    trace(
        f"Writing JSON -> {path}"
    )

    path.write_text(

        json.dumps(
            data,
            indent=2,
            ensure_ascii=False,
            default=str
        ),

        encoding="utf-8"
    )


def dump_html(page, name):

    try:

        html = page.content()

        path = DEBUG_DIR / f"{name}.html"

        path.write_text(
            html,
            encoding="utf-8"
        )

        trace(
            f"HTML dumped -> {path}"
        )

    except Exception:

        log.exception("HTML dump failed")


def screenshot(page, name):

    try:

        path = DEBUG_DIR / f"{name}.png"

        page.screenshot(
            path=str(path),
            full_page=True
        )

        trace(
            f"Screenshot saved -> {path}"
        )

    except Exception:

        log.exception("Screenshot failed")


def timed(label):

    start = time.time()

    trace(f"START -> {label}")

    def done():

        duration = time.time() - start

        trace(
            f"END -> {label}",
            {
                "seconds": duration
            }
        )

    return done


def retry(fn, retries=3):

    for attempt in range(retries):

        try:

            trace(
                f"Retry attempt {attempt+1}"
            )

            return fn()

        except Exception:

            log.exception(
                f"Retry {attempt+1} failed"
            )

            time.sleep(2)

    raise Exception("All retries failed")


def matches_target_date(date_text):

    trace(
        "Checking date match",
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
            "Date regex failed",
            date_text
        )

        return False

    month, day_s, year_s = m.groups()

    try:

        day = int(day_s)

        year = int(year_s)

    except Exception:

        log.exception("Date parsing failed")

        return False

    result = (

        month == TARGET_MONTH
        and day == TARGET_DAY
        and year == TARGET_YEAR
    )

    trace(
        "Date comparison result",
        {
            "month": month,
            "day": day,
            "year": year,
            "matched": result
        }
    )

    return result


# ---------------------------------------------------------
# Login
# ---------------------------------------------------------

def login(page):

    trace("Opening login page")

    page.goto(
        LOGIN_URL,
        wait_until="networkidle"
    )

    dump_html(page, "login_page")

    screenshot(page, "login_page")

    trace("Filling email field")

    page.fill(
        'input[type="email"]',
        USERNAME
    )

    trace("Filling password field")

    page.fill(
        'input[type="password"]',
        PASSWORD
    )

    trace("Clicking submit button")

    page.click(
        'button[type="submit"]'
    )

    trace("Waiting for dashboard redirect")

    page.wait_for_url(
        f"{BASE_URL}/main/**",
        timeout=30000
    )

    trace(
        "Login successful",
        {
            "url": page.url
        }
    )

    dump_html(page, "after_login")

    screenshot(page, "after_login")


# ---------------------------------------------------------
# Session
# ---------------------------------------------------------

def get_auth_session(page):

    trace("Dumping browser storage")

    storage_data = page.evaluate("""
    () => {

        const ls = {}
        const ss = {}

        for (let i = 0; i < localStorage.length; i++) {

            const k = localStorage.key(i)

            ls[k] = localStorage.getItem(k)
        }

        for (let i = 0; i < sessionStorage.length; i++) {

            const k = sessionStorage.key(i)

            ss[k] = sessionStorage.getItem(k)
        }

        return {
            localStorage: ls,
            sessionStorage: ss
        }
    }
    """)

    trace(
        "Storage dump",
        storage_data
    )

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
        "Token extracted",
        {
            "exists": bool(token),
            "preview": token[:120]
        }
    )

    session = requests.Session()

    session.headers.update({

        "Accept": "application/json, text/plain, */*",

        "Authorization": f"Bearer {token}",

        "Origin": BASE_URL,

        "Referer": BASE_URL + "/",

        "User-Agent": (
            "Mozilla/5.0 AppleWebKit/537.36"
        )
    })

    cookies = page.context.cookies()

    for c in cookies:

        trace(
            "Cookie copied",
            c
        )

        session.cookies.set(
            c["name"],
            c["value"]
        )

    return session


# ---------------------------------------------------------
# Request Interceptor
# ---------------------------------------------------------

def route_intercept(route, request):

    trace(
        "INTERCEPTED REQUEST",
        {
            "url": request.url,
            "method": request.method,
            "resource_type": request.resource_type,
            "headers": request.headers,
            "post_data": request.post_data
        }
    )

    route.continue_()


# ---------------------------------------------------------
# Capture Requests While Clicking
# ---------------------------------------------------------

def capture_requests_while_click(

    page,
    element,
    timeout=10,
    click_label="unknown_click"
):

    trace(
        "CLICK CAPTURE SESSION STARTED",
        {
            "label": click_label,
            "timeout": timeout
        }
    )

    captured = {

        "click_label": click_label,

        "clicked_element": {},

        "requests": [],

        "responses": [],

        "matching_urls": [],

        "api_replays": []
    }

    # -------------------------------------------------
    # Element details
    # -------------------------------------------------

    try:

        element_html = element.evaluate(
            "(el) => el.outerHTML"
        )

        element_text = element.inner_text()

        bbox = element.bounding_box()

        captured["clicked_element"] = {

            "html": element_html,

            "text": element_text,

            "bounding_box": bbox
        }

        trace(
            "CLICK TARGET DETAILS",
            captured["clicked_element"]
        )

    except Exception:

        log.exception(
            "Failed extracting click target info"
        )

    # -------------------------------------------------
    # Request handler
    # -------------------------------------------------

    def on_request(request):

        try:

            req_data = {

                "timestamp": time.time(),

                "url": request.url,

                "method": request.method,

                "resource_type": request.resource_type,

                "headers": dict(request.headers),

                "post_data": request.post_data
            }

            captured["requests"].append(req_data)

            trace(
                "REQUEST TRIGGERED BY CLICK",
                req_data
            )

            if request.resource_type in ["xhr", "fetch"]:

                trace(
                    "XHR/FETCH DETECTED",
                    {
                        "url": request.url,
                        "method": request.method
                    }
                )

            if (
                "load-sheet" in request.url
                or "/order" in request.url
                or "api.postex.pk" in request.url
            ):

                captured["matching_urls"].append(
                    request.url
                )

                trace(
                    "MATCHING API URL DETECTED",
                    request.url
                )

        except Exception:

            log.exception(
                "Request logging failed"
            )

    # -------------------------------------------------
    # Response handler
    # -------------------------------------------------

    def on_response(response):

        try:

            body = ""

            try:
                body = response.text()
            except Exception:
                pass

            res_data = {

                "timestamp": time.time(),

                "url": response.url,

                "status": response.status,

                "headers": dict(response.headers),

                "body_preview": body[:10000]
            }

            captured["responses"].append(
                res_data
            )

            trace(
                "RESPONSE TRIGGERED BY CLICK",
                {
                    "url": response.url,
                    "status": response.status,
                    "preview": body[:1000]
                }
            )

            safe_name = re.sub(
                r"[^a-zA-Z0-9]",
                "_",
                response.url
            )[:180]

            raw_path = (
                DEBUG_DIR
                / f"response_{safe_name}.txt"
            )

            raw_path.write_text(
                body,
                encoding="utf-8"
            )

        except Exception:

            log.exception(
                "Response logging failed"
            )

    # -------------------------------------------------
    # Attach listeners
    # -------------------------------------------------

    page.on("request", on_request)

    page.on("response", on_response)

    # -------------------------------------------------
    # Perform click
    # -------------------------------------------------

    try:

        trace(
            "PERFORMING CLICK"
        )

        click_start = time.time()

        element.click()

        click_end = time.time()

        trace(
            "CLICK COMPLETED",
            {
                "duration_seconds":
                    click_end - click_start
            }
        )

    except Exception:

        log.exception(
            "Element click failed"
        )

    # -------------------------------------------------
    # Wait for network
    # -------------------------------------------------

    trace(
        "WAITING FOR NETWORK EVENTS",
        {
            "seconds": timeout
        }
    )

    time.sleep(timeout)

    # -------------------------------------------------
    # Cleanup listeners
    # -------------------------------------------------

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

        log.exception(
            "Listener cleanup failed"
        )

    # -------------------------------------------------
    # Replay URLs directly
    # -------------------------------------------------

    try:

        trace(
            "STARTING DIRECT API REPLAY"
        )

        for url in captured["matching_urls"]:

            try:

                trace(
                    "REPLAYING URL",
                    url
                )

                replay_response = requests.get(
                    url,
                    timeout=30
                )

                replay_data = {

                    "url": url,

                    "status_code":
                        replay_response.status_code,

                    "headers":
                        dict(replay_response.headers),

                    "body_preview":
                        replay_response.text[:10000]
                }

                captured["api_replays"].append(
                    replay_data
                )

                trace(
                    "DIRECT API REPLAY RESPONSE",
                    {
                        "url": url,
                        "status":
                            replay_response.status_code
                    }
                )

                safe_name = re.sub(
                    r"[^a-zA-Z0-9]",
                    "_",
                    url
                )[:180]

                replay_path = (
                    DEBUG_DIR
                    / f"replay_{safe_name}.txt"
                )

                replay_path.write_text(
                    replay_response.text,
                    encoding="utf-8"
                )

            except Exception:

                log.exception(
                    "Replay request failed"
                )

    except Exception:

        log.exception(
            "Replay phase failed"
        )

    trace(
        "CLICK CAPTURE SESSION FINISHED",
        {
            "requests_captured":
                len(captured["requests"]),

            "responses_captured":
                len(captured["responses"]),

            "matching_urls":
                len(captured["matching_urls"]),

            "replays":
                len(captured["api_replays"])
        }
    )

    return captured


# ---------------------------------------------------------
# Find Loadsheet Rows
# ---------------------------------------------------------

def find_rows_dom(page):

    trace("Finding loadsheet rows")

    results = []

    selectors = [

        "table tbody tr.data-item",

        "tr.data-item",

        "tbody tr"
    ]

    trace(
        "Total TR count",
        page.locator("tr").count()
    )

    rows = []

    for sel in selectors:

        try:

            trace(
                f"Trying selector: {sel}"
            )

            rows = page.query_selector_all(sel)

            trace(
                "Selector result",
                {
                    "selector": sel,
                    "count": len(rows)
                }
            )

            if rows:
                break

        except Exception:

            log.exception(
                "Selector failed"
            )

    if not rows:

        raise Exception("NO ROWS FOUND")

    for idx, row in enumerate(rows):

        try:

            trace(
                f"Inspecting row {idx}"
            )

            raw_html = row.inner_html()

            trace(
                f"RAW HTML ROW {idx}",
                raw_html
            )

            row_file = DEBUG_DIR / f"row_{idx}.html"

            row_file.write_text(
                raw_html,
                encoding="utf-8"
            )

            cells = row.query_selector_all("td")

            trace(
                f"Cells found in row {idx}",
                len(cells)
            )

            if len(cells) < 7:
                continue

            values = []

            for cidx, cell in enumerate(cells):

                try:

                    val = cell.inner_text().strip()

                    trace(
                        f"Cell [{idx}:{cidx}]",
                        val
                    )

                    values.append(val)

                except Exception:

                    log.exception(
                        "Cell extraction failed"
                    )

                    values.append("")

            loadsheet_number = values[0]

            total_orders = values[1]

            date_text = values[5]

            status = values[6]

            if not matches_target_date(date_text):

                trace(
                    "Date mismatch",
                    {
                        "incoming": date_text,
                        "target": TARGET_LABEL
                    }
                )

                continue

            m = re.search(
                r"more-menu-(\d+)",
                raw_html
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

            trace(
                "MATCHED ROW",
                result
            )

            try:

                clickable = row.query_selector(
                    "span.orders"
                )

                trace(
                    "Clickable orders element exists",
                    bool(clickable)
                )

                if clickable:

                    network = capture_requests_while_click(

                        page,

                        clickable,

                        timeout=10,

                        click_label=f"row_{idx}_orders_click"
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
                                "Captured sheet id",
                                mm.group(1)
                            )

                            break

            except Exception:

                log.exception(
                    "Click capture failed"
                )

            results.append(result)

        except Exception:

            log.exception(
                "Row processing failed"
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

    trace(
        "API REQUEST",
        {
            "url": url,
            "params": params
        }
    )

    try:

        response = session.get(
            url,
            params=params,
            timeout=30
        )

        trace(
            "API RESPONSE",
            {
                "status": response.status_code,
                "preview": response.text[:2000]
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

            trace(
                "API JSON",
                data
            )

        except Exception:

            log.exception(
                "JSON parsing failed"
            )

            data = []

        return {

            "orders": data,

            "status_code":
                response.status_code
        }

    except Exception:

        log.exception(
            "API request failed"
        )

        return {

            "orders": [],

            "status_code": None
        }


# ---------------------------------------------------------
# Main
# ---------------------------------------------------------

def main():

    trace("SCRAPER STARTED")

    final = {

        "scrape_date": DATE_TAG,

        "target_date": TARGET_LABEL,

        "loadsheets": []
    }

    with sync_playwright() as pw:

        trace("Launching browser")

        browser = pw.chromium.launch(

            headless=True,

            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage"
            ]
        )

        trace("Creating browser context")

        context = browser.new_context(

            record_har_path=str(
                DEBUG_DIR / "network.har"
            )
        )

        trace("Starting tracing")

        context.tracing.start(

            screenshots=True,

            snapshots=True,

            sources=True
        )

        page = context.new_page()

        page.route(
            "**/*",
            route_intercept
        )

        # -------------------------------------------------
        # Browser event logs
        # -------------------------------------------------

        page.on(
            "console",
            lambda msg: trace(
                f"BROWSER CONSOLE [{msg.type}]",
                msg.text
            )
        )

        page.on(
            "pageerror",
            lambda exc: trace(
                "PAGE ERROR",
                str(exc)
            )
        )

        page.on(
            "framenavigated",
            lambda frame: trace(
                "FRAME NAVIGATED",
                {
                    "url": frame.url
                }
            )
        )

        page.on(
            "requestfailed",
            lambda req: trace(
                "REQUEST FAILED",
                {
                    "url": req.url,
                    "method": req.method,
                    "failure": str(req.failure)
                }
            )
        )

        end_login = timed("LOGIN")

        retry(
            lambda: login(page)
        )

        end_login()

        session = get_auth_session(page)

        trace(
            "Opening loadsheet page"
        )

        page.goto(
            LOADSHEET_URL,
            wait_until="networkidle"
        )

        time.sleep(10)

        dump_html(page, "loadsheet_page")

        screenshot(page, "loadsheet_page")

        rows = retry(
            lambda: find_rows_dom(page)
        )

        for row in rows:

            try:

                sid = (

                    row.get("captured_sheet_id")
                    or row.get("sheet_id")
                )

                row["final_sheet_id"] = sid

                trace(
                    "Processing loadsheet",
                    {
                        "sheet_id": sid
                    }
                )

                if not sid:

                    trace(
                        "Skipping row due to missing sheet id"
                    )

                    continue

                booked = fetch_orders(

                    session,

                    sid,

                    "booked"
                )

                row["api_result"] = booked

                final["loadsheets"].append(row)

            except Exception:

                log.exception(
                    "Loadsheet processing failed"
                )

        trace("Stopping Playwright tracing")

        context.tracing.stop(

            path=str(
                DEBUG_DIR / "trace.zip"
            )
        )

        browser.close()

    write_json(
        OUTPUT_FILE,
        final
    )

    trace(
        "FINAL SUMMARY",
        {
            "rows_processed": len(rows),
            "loadsheets_saved":
                len(final["loadsheets"]),
            "output_file":
                str(OUTPUT_FILE)
        }
    )

    trace("SCRAPER FINISHED")


if __name__ == "__main__":

    main()
