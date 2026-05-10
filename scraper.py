"""
PostEx Loadsheet Scraper
ULTRA VERBOSE FORENSIC DEBUG EDITION v2
=========================================
KEY FIX: The DOM-extracted sheet_id (e.g. "4786" from more-menu-XXXX)
         is NOT the same as the API sheet_id (e.g. "5381184") that
         appears in the network request URL when the orders span is clicked.
         This version:
           1. Captures ALL XHR/fetch URLs triggered by the click
           2. Logs every single one in full detail
           3. Extracts the REAL sheet_id from the captured network URL
           4. Mirrors the exact headers/params seen in the browser DevTools
           5. Logs every step of the outgoing API call and its response
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
    TimeoutError as PWTimeout,
)


# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.NOTSET,
    format="%(asctime)s [%(levelname)-8s] %(funcName)s:%(lineno)d | %(message)s",
)
log = logging.getLogger("postex-scraper")

STEP_COUNTER = 0


def trace(message, data=None):
    global STEP_COUNTER
    STEP_COUNTER += 1
    prefix = f"[STEP {STEP_COUNTER:06d}]"
    if data is not None:
        try:
            pretty = json.dumps(data, indent=2, ensure_ascii=False, default=str)
        except Exception:
            pretty = str(data)
        log.debug(f"{prefix} {message}\n{pretty}")
    else:
        log.debug(f"{prefix} {message}")


# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

BASE_URL        = "https://merchant.postex.pk"
LOGIN_URL       = f"{BASE_URL}/login"
LOADSHEET_URL   = f"{BASE_URL}/main/load-sheet-logs"

# The API base — note: no trailing /order here, that's appended per call
API_ORDER_BASE  = "https://api.postex.pk/services/merchant/api/load-sheet"

USERNAME = os.environ.get("POSTEX_USERNAME", "")
PASSWORD = os.environ.get("POSTEX_PASSWORD", "")

OUTPUT_DIR = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)
DEBUG_DIR = OUTPUT_DIR / "debug"
DEBUG_DIR.mkdir(exist_ok=True)

trace("Config loaded", {
    "BASE_URL":        BASE_URL,
    "LOADSHEET_URL":   LOADSHEET_URL,
    "API_ORDER_BASE":  API_ORDER_BASE,
    "USERNAME_EXISTS": bool(USERNAME),
    "PASSWORD_EXISTS": bool(PASSWORD),
})


# ─────────────────────────────────────────────
# Date
# ─────────────────────────────────────────────

DATE_OVERRIDE = os.environ.get("DATE_OVERRIDE")
if DATE_OVERRIDE:
    TARGET_DATE = datetime.strptime(DATE_OVERRIDE, "%Y-%m-%d")
else:
    TARGET_DATE = datetime.now() - timedelta(days=1)

DATE_TAG      = TARGET_DATE.strftime("%Y-%m-%d")
TARGET_MONTH  = TARGET_DATE.strftime("%b")
TARGET_DAY    = TARGET_DATE.day
TARGET_YEAR   = TARGET_DATE.year
TARGET_LABEL  = f"{TARGET_MONTH} {TARGET_DAY}, {TARGET_YEAR}"
OUTPUT_FILE   = OUTPUT_DIR / f"loadsheet_{DATE_TAG}.json"

trace("Date config", {
    "DATE_TAG":     DATE_TAG,
    "TARGET_LABEL": TARGET_LABEL,
})


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def write_json(path, data):
    trace(f"Writing JSON -> {path}")
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def dump_html(page, name):
    try:
        html = page.content()
        path = DEBUG_DIR / f"{name}.html"
        path.write_text(html, encoding="utf-8")
        trace(f"HTML dumped -> {path} ({len(html)} chars)")
    except Exception:
        log.exception("HTML dump failed")


def screenshot(page, name):
    try:
        path = DEBUG_DIR / f"{name}.png"
        page.screenshot(path=str(path), full_page=True)
        trace(f"Screenshot saved -> {path}")
    except Exception:
        log.exception("Screenshot failed")


def safe_filename(url, maxlen=160):
    return re.sub(r"[^a-zA-Z0-9._-]", "_", url)[:maxlen]


def retry(fn, retries=3, delay=2):
    for attempt in range(retries):
        try:
            trace(f"Attempt {attempt + 1}/{retries}")
            return fn()
        except Exception:
            log.exception(f"Attempt {attempt + 1} failed")
            if attempt < retries - 1:
                time.sleep(delay)
    raise RuntimeError("All retries exhausted")


def matches_target_date(date_text):
    if not date_text:
        return False
    date_text = date_text.strip()
    m = re.search(r"(\w+)\s+(\d{1,2}),\s+(\d{4})", date_text)
    if not m:
        trace("Date regex failed", {"raw": date_text})
        return False
    month, day_s, year_s = m.groups()
    try:
        day  = int(day_s)
        year = int(year_s)
    except Exception:
        log.exception("Date int conversion failed")
        return False
    matched = (month == TARGET_MONTH and day == TARGET_DAY and year == TARGET_YEAR)
    trace("Date check", {
        "raw":     date_text,
        "parsed":  f"{month} {day} {year}",
        "target":  TARGET_LABEL,
        "matched": matched,
    })
    return matched


# ─────────────────────────────────────────────
# Login
# ─────────────────────────────────────────────

def login(page):
    trace("Navigating to login page")
    page.goto(LOGIN_URL, wait_until="networkidle")
    dump_html(page, "01_login_page")
    screenshot(page, "01_login_page")

    trace("Filling credentials")
    page.fill('input[type="email"]',    USERNAME)
    page.fill('input[type="password"]', PASSWORD)

    trace("Submitting login form")
    page.click('button[type="submit"]')
    page.wait_for_url(f"{BASE_URL}/main/**", timeout=30_000)

    trace("Login success", {"current_url": page.url})
    dump_html(page,  "02_after_login")
    screenshot(page, "02_after_login")


# ─────────────────────────────────────────────
# Build authenticated requests.Session
# ─────────────────────────────────────────────

def build_session(page):
    """
    Mirror the exact headers the browser sends.
    The most important one is the Authorization Bearer token
    stored in localStorage / sessionStorage.
    """
    trace("Dumping browser storage to find token")

    storage = page.evaluate("""() => {
        const ls = {}, ss = {};
        for (let i = 0; i < localStorage.length; i++) {
            const k = localStorage.key(i);
            ls[k] = localStorage.getItem(k);
        }
        for (let i = 0; i < sessionStorage.length; i++) {
            const k = sessionStorage.key(i);
            ss[k] = sessionStorage.getItem(k);
        }
        return { localStorage: ls, sessionStorage: ss };
    }""")

    trace("Full storage dump", storage)

    # ── Try every common key name ──
    token_keys = [
        "token", "authToken", "access_token", "accessToken",
        "bearerToken", "jwt", "id_token", "idToken",
    ]
    token = ""
    for store_name, store_dict in [
        ("localStorage",  storage.get("localStorage",  {})),
        ("sessionStorage", storage.get("sessionStorage", {})),
    ]:
        for k in token_keys:
            if k in store_dict and store_dict[k]:
                token = store_dict[k]
                trace(f"Token found in {store_name}['{k}']", {
                    "preview": token[:120]
                })
                break
        if token:
            break

    # ── Also check if it's nested inside a JSON blob ──
    if not token:
        trace("Direct token key not found — scanning all storage values for JWT pattern")
        jwt_re = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
        for store_dict in [storage.get("localStorage", {}), storage.get("sessionStorage", {})]:
            for k, v in store_dict.items():
                if not v:
                    continue
                hit = jwt_re.search(str(v))
                if hit:
                    token = hit.group(0)
                    trace(f"JWT extracted via regex from key '{k}'", {
                        "preview": token[:120]
                    })
                    break
            if token:
                break

    if not token:
        trace("WARNING: No bearer token found — API calls will likely return 401/400")
    else:
        trace("Token secured", {"length": len(token), "preview": token[:60] + "..."})

    # ── Build session with browser-matching headers ──
    session = requests.Session()
    session.headers.update({
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Authorization":   f"Bearer {token}",
        "Origin":          BASE_URL,
        "Referer":         f"{LOADSHEET_URL}",
        "User-Agent":      (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    })

    # ── Copy all browser cookies ──
    cookies = page.context.cookies()
    trace(f"Copying {len(cookies)} browser cookies to session")
    for c in cookies:
        trace("Cookie", {k: v for k, v in c.items() if k != "value"})
        session.cookies.set(c["name"], c["value"], domain=c.get("domain"))

    trace("Session built", {
        "headers": dict(session.headers),
        "cookie_count": len(session.cookies),
    })
    return session


# ─────────────────────────────────────────────
# Click + Network capture
# Returns the REAL API sheet_id from the network
# ─────────────────────────────────────────────

# Regex that matches the real loadsheet ID in the API URL:
#   https://api.postex.pk/services/merchant/api/load-sheet/5381184/order
_SHEET_ID_RE = re.compile(
    r"api\.postex\.pk/services/merchant/api/load-sheet/(\d+)/order"
)


def capture_real_sheet_id(page, element, row_label="row"):
    """
    Click the orders span and eavesdrop on every network request.
    Returns the sheet_id string found in the API URL, or None.
    Also returns the full captured auth headers from the request
    so we can replay the call with identical headers.
    """
    trace(f"[{row_label}] Starting click-capture to find real sheet_id")

    captured_ids   = []
    captured_hdrs  = {}   # headers from the matching request
    all_requests   = []
    all_responses  = []

    def on_request(req):
        entry = {
            "url":           req.url,
            "method":        req.method,
            "resource_type": req.resource_type,
            "headers":       dict(req.headers),
            "post_data":     req.post_data,
            "timestamp":     time.time(),
        }
        all_requests.append(entry)
        trace(f"[{row_label}] REQUEST", {
            "url":    req.url,
            "method": req.method,
            "type":   req.resource_type,
        })

        m = _SHEET_ID_RE.search(req.url)
        if m:
            sid = m.group(1)
            trace(f"[{row_label}] *** REAL SHEET_ID FOUND IN REQUEST ***", {
                "sheet_id":   sid,
                "full_url":   req.url,
                "headers":    dict(req.headers),
            })
            captured_ids.append(sid)
            # Save the headers from this exact browser request
            if not captured_hdrs:
                captured_hdrs.update(dict(req.headers))

    def on_response(res):
        body_preview = ""
        try:
            body_preview = res.text()[:4000]
        except Exception:
            pass
        entry = {
            "url":          res.url,
            "status":       res.status,
            "headers":      dict(res.headers),
            "body_preview": body_preview,
            "timestamp":    time.time(),
        }
        all_responses.append(entry)
        trace(f"[{row_label}] RESPONSE", {
            "url":    res.url,
            "status": res.status,
            "body":   body_preview[:500],
        })

        # Save raw response to disk for inspection
        fname = DEBUG_DIR / f"response_{safe_filename(res.url)}.txt"
        try:
            fname.write_text(body_preview, encoding="utf-8")
        except Exception:
            pass

    page.on("request",  on_request)
    page.on("response", on_response)

    try:
        trace(f"[{row_label}] Clicking orders span")
        element.click()
        trace(f"[{row_label}] Click complete — waiting 8s for network")
        time.sleep(8)
    except Exception:
        log.exception(f"[{row_label}] Click failed")
    finally:
        try:
            page.remove_listener("request",  on_request)
            page.remove_listener("response", on_response)
        except Exception:
            pass

    # Dump full network log for this click
    click_log_path = DEBUG_DIR / f"click_network_{row_label}.json"
    write_json(click_log_path, {
        "requests":      all_requests,
        "responses":     all_responses,
        "captured_ids":  captured_ids,
        "captured_hdrs": captured_hdrs,
    })
    trace(f"[{row_label}] Click network log saved", {
        "path":          str(click_log_path),
        "requests":      len(all_requests),
        "responses":     len(all_responses),
        "captured_ids":  captured_ids,
    })

    real_id = captured_ids[0] if captured_ids else None
    return real_id, captured_hdrs


# ─────────────────────────────────────────────
# Find Loadsheet Rows
# ─────────────────────────────────────────────

def find_rows_dom(page):
    trace("Searching for loadsheet table rows")

    selectors = [
        "table tbody tr.data-item",
        "tr.data-item",
        "tbody tr",
    ]

    trace("Total <tr> elements on page", page.locator("tr").count())

    rows = []
    for sel in selectors:
        try:
            rows = page.query_selector_all(sel)
            trace(f"Selector '{sel}' -> {len(rows)} rows")
            if rows:
                break
        except Exception:
            log.exception(f"Selector '{sel}' failed")

    if not rows:
        dump_html(page, "ERROR_no_rows_found")
        screenshot(page, "ERROR_no_rows_found")
        raise RuntimeError("No table rows found — check HTML dump")

    results = []

    for idx, row in enumerate(rows):
        try:
            raw_html = row.inner_html()
            (DEBUG_DIR / f"row_{idx}.html").write_text(raw_html, encoding="utf-8")

            cells = row.query_selector_all("td")
            trace(f"Row {idx}: {len(cells)} cells")

            if len(cells) < 7:
                trace(f"Row {idx}: skipped (too few cells)")
                continue

            values = []
            for cidx, cell in enumerate(cells):
                val = ""
                try:
                    val = cell.inner_text().strip()
                except Exception:
                    pass
                trace(f"  Cell[{idx}:{cidx}]", val)
                values.append(val)

            loadsheet_number = values[0]
            total_orders     = values[1]
            date_text        = values[5]
            status           = values[6]

            if not matches_target_date(date_text):
                trace(f"Row {idx}: date mismatch, skipping", {
                    "cell_5": date_text,
                })
                continue

            # DOM-extracted sheet_id (may differ from API sheet_id!)
            dom_sheet_id = None
            m = re.search(r"more-menu-(\d+)", raw_html)
            if m:
                dom_sheet_id = m.group(1)
            trace(f"Row {idx}: DOM sheet_id (from more-menu-*)", dom_sheet_id)

            result = {
                "row_index":        idx,
                "loadsheet_number": loadsheet_number,
                "dom_sheet_id":     dom_sheet_id,   # renamed to make role clear
                "date_text":        date_text,
                "status":           status,
                "total_orders":     total_orders,
                "real_sheet_id":    None,           # filled in after click
                "click_headers":    {},             # auth headers from click
            }
            trace(f"Row {idx}: matched row data", result)

            # ── Click the orders span to capture real sheet_id ──
            clickable = row.query_selector("span.orders")
            trace(f"Row {idx}: orders span found", bool(clickable))

            if clickable:
                real_id, click_hdrs = capture_real_sheet_id(
                    page,
                    clickable,
                    row_label=f"row{idx}",
                )
                result["real_sheet_id"]  = real_id
                result["click_headers"]  = click_hdrs
                trace(f"Row {idx}: real_sheet_id from network", real_id)
                trace(f"Row {idx}: auth headers from network", click_hdrs)
            else:
                trace(f"Row {idx}: no orders span — will fall back to dom_sheet_id")

            results.append(result)

        except Exception:
            log.exception(f"Row {idx} processing failed")

    trace(f"Rows matched for target date: {len(results)}")
    return results


# ─────────────────────────────────────────────
# Fetch Orders — ultra-verbose
# ─────────────────────────────────────────────

def fetch_orders(session, sheet_id, browser_headers=None, status="booked"):
    """
    Replicate the exact browser request:
      GET https://api.postex.pk/services/merchant/api/load-sheet/{id}/order
          ?loadSheetId={id}&orderStatusOption=booked&direction=desc
    """
    # ── Build URL exactly as browser does ──
    url = f"{API_ORDER_BASE}/{sheet_id}/order"
    params = {
        "loadSheetId":       sheet_id,
        "orderStatusOption": status,
        "direction":         "desc",
    }

    trace("=" * 60)
    trace("FETCH ORDERS — PRE-FLIGHT CHECK", {
        "sheet_id":       sheet_id,
        "status":         status,
        "constructed_url": url,
        "params":          params,
        "note": (
            "sheet_id MUST match the number in the API URL captured "
            "from the browser network tab, NOT the DOM more-menu-* id"
        ),
    })

    # ── Optionally override session headers with exact browser headers ──
    merged_headers = dict(session.headers)
    if browser_headers:
        trace("Merging browser-captured headers into session headers", browser_headers)
        for k, v in browser_headers.items():
            # Always prefer browser's own Authorization header if present
            if k.lower() == "authorization" and v:
                merged_headers["Authorization"] = v
                trace("Authorization header overridden from browser capture", {
                    "preview": v[:80]
                })
            elif k.lower() not in ("content-length", "host"):
                merged_headers[k] = v

    trace("Final outgoing headers", merged_headers)
    trace("Final outgoing params",  params)

    # ── Full URL that will be sent (for copy-paste verification) ──
    req = requests.Request("GET", url, headers=merged_headers, params=params)
    prepared = req.prepare()
    trace("EXACT URL BEING SENT (copy into browser to test)", {
        "url":     prepared.url,
        "headers": dict(prepared.headers),
    })

    # ── Fire request ──
    try:
        trace("Sending GET request...")
        t0 = time.time()
        response = session.get(
            url,
            params=params,
            headers=merged_headers,
            timeout=30,
        )
        elapsed = time.time() - t0

        trace("Response received", {
            "status_code":     response.status_code,
            "elapsed_seconds": round(elapsed, 3),
            "response_url":    response.url,
            "response_headers": dict(response.headers),
        })

        # ── Save raw response ──
        raw_path = DEBUG_DIR / f"api_raw_{sheet_id}_{status}.txt"
        raw_path.write_text(response.text, encoding="utf-8")
        trace(f"Raw response saved -> {raw_path}")

        # ── Log response body ──
        trace("Response body preview (first 3000 chars)", response.text[:3000])

        # ── Parse JSON ──
        try:
            data = response.json()
        except Exception:
            log.exception("JSON parse failed")
            trace("Non-JSON body", response.text[:2000])
            data = {"raw_text": response.text}

        # ── Diagnose errors ──
        if response.status_code != 200:
            trace("ERROR — Non-200 response", {
                "status_code":   response.status_code,
                "body":          data,
                "DIAGNOSIS": (
                    "400 + statusCode 015 usually means wrong sheet_id "
                    "OR missing/expired Authorization token. "
                    "Check real_sheet_id vs dom_sheet_id in row data."
                ),
            })
        else:
            if isinstance(data, dict):
                trace("SUCCESS — Response keys", list(data.keys()))
            elif isinstance(data, list):
                trace("SUCCESS — Response is list", {"length": len(data)})

        return {
            "orders":      data,
            "status_code": response.status_code,
            "elapsed":     elapsed,
            "request_url": prepared.url,
        }

    except requests.exceptions.RequestException:
        log.exception("HTTP request exception")
        return {
            "orders":      [],
            "status_code": None,
            "error":       traceback.format_exc(),
        }


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    trace("SCRAPER v2 STARTED", {
        "target_date": TARGET_LABEL,
        "output_file": str(OUTPUT_FILE),
    })

    final = {
        "scrape_date": DATE_TAG,
        "target_date": TARGET_LABEL,
        "loadsheets":  [],
    }

    with sync_playwright() as pw:

        trace("Launching Chromium")
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

        context = browser.new_context(
            record_har_path=str(DEBUG_DIR / "network.har")
        )
        context.tracing.start(screenshots=True, snapshots=True, sources=True)

        page = context.new_page()

        # ── Console / error logging ──
        page.on("console",      lambda m: trace(f"BROWSER [{m.type}]", m.text))
        page.on("pageerror",    lambda e: trace("PAGE ERROR", str(e)))
        page.on("requestfailed", lambda r: trace("REQUEST FAILED", {
            "url": r.url, "failure": str(r.failure)
        }))

        # ── Login ──
        retry(lambda: login(page))

        # ── Build session with auth ──
        session = build_session(page)

        # ── Navigate to loadsheet page ──
        trace("Navigating to loadsheet page")
        page.goto(LOADSHEET_URL, wait_until="networkidle")
        time.sleep(10)
        dump_html(page,  "03_loadsheet_page")
        screenshot(page, "03_loadsheet_page")

        # ── Find rows ──
        rows = retry(lambda: find_rows_dom(page))

        trace(f"Processing {len(rows)} matched row(s)")

        for row in rows:
            try:
                # ── Prefer real_sheet_id (from network capture) ──
                # Fall back to dom_sheet_id only as last resort
                real_id = row.get("real_sheet_id")
                dom_id  = row.get("dom_sheet_id")

                trace("Sheet ID selection", {
                    "real_sheet_id (from network)": real_id,
                    "dom_sheet_id  (from HTML)":    dom_id,
                    "using": real_id or dom_id,
                    "WARNING": (
                        None if real_id else
                        "real_sheet_id missing! Falling back to dom_sheet_id "
                        "which is KNOWN to be wrong. Click capture may have failed."
                    ),
                })

                sid = real_id or dom_id
                row["final_sheet_id"] = sid

                if not sid:
                    trace("Skipping row — no sheet_id available at all")
                    continue

                # ── Fetch orders with click-captured headers ──
                click_hdrs = row.get("click_headers") or {}
                result = fetch_orders(
                    session,
                    sid,
                    browser_headers=click_hdrs,
                    status="booked",
                )
                row["api_result"] = result
                final["loadsheets"].append(row)

            except Exception:
                log.exception("Loadsheet processing failed")

        # ── Save trace ──
        context.tracing.stop(path=str(DEBUG_DIR / "trace.zip"))
        browser.close()

    write_json(OUTPUT_FILE, final)

    trace("SCRAPER FINISHED", {
        "rows_found":       len(rows),
        "loadsheets_saved": len(final["loadsheets"]),
        "output_file":      str(OUTPUT_FILE),
    })


if __name__ == "__main__":
    main()
