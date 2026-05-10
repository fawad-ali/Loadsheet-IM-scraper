"""
PostEx Loadsheet Scraper v3 — FIXED EDITION
=============================================
Fixes applied vs v2:
  1. Correct clickable span selector  — was `span.orders` (doesn't exist);
     now targets the first blue <span> inside td.dt-tracking (the orders count cell).
  2. Status parameter auto-detection  — derives orderStatusOption from the row
     status badge text instead of hard-coding "booked".
  3. Fallback: also try without orderStatusOption so we always get something.
  4. Explicit wait for the Angular table to finish rendering before scraping.
  5. Intercept network at the *context* level (not page level) so listeners
     survive Angular's router navigation between clicks.
  6. Better dom_sheet_id extraction — checks both more-menu-* and data-id attrs.
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


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-8s] %(funcName)s:%(lineno)d | %(message)s",
)
log = logging.getLogger("postex-scraper-v3")

STEP = 0


def trace(msg, data=None):
    global STEP
    STEP += 1
    prefix = f"[STEP {STEP:06d}]"
    if data is not None:
        try:
            pretty = json.dumps(data, indent=2, ensure_ascii=False, default=str)
        except Exception:
            pretty = str(data)
        log.debug(f"{prefix} {msg}\n{pretty}")
    else:
        log.debug(f"{prefix} {msg}")


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

BASE_URL       = "https://merchant.postex.pk"
LOGIN_URL      = f"{BASE_URL}/login"
LOADSHEET_URL  = f"{BASE_URL}/main/load-sheet-logs"
API_BASE       = "https://api.postex.pk/services/merchant/api/load-sheet"

USERNAME = os.environ.get("POSTEX_USERNAME", "")
PASSWORD = os.environ.get("POSTEX_PASSWORD", "")

OUTPUT_DIR = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)
DEBUG_DIR = OUTPUT_DIR / "debug"
DEBUG_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# Date
# ─────────────────────────────────────────────────────────────────────────────

DATE_OVERRIDE = os.environ.get("DATE_OVERRIDE")
if DATE_OVERRIDE:
    TARGET_DATE = datetime.strptime(DATE_OVERRIDE, "%Y-%m-%d")
else:
    TARGET_DATE = datetime.now() - timedelta(days=1)

DATE_TAG     = TARGET_DATE.strftime("%Y-%m-%d")
TARGET_MONTH = TARGET_DATE.strftime("%b")
TARGET_DAY   = TARGET_DATE.day
TARGET_YEAR  = TARGET_DATE.year
TARGET_LABEL = f"{TARGET_MONTH} {TARGET_DAY}, {TARGET_YEAR}"
OUTPUT_FILE  = OUTPUT_DIR / f"loadsheet_{DATE_TAG}.json"

trace("Config", {
    "target_date": TARGET_LABEL,
    "output_file": str(OUTPUT_FILE),
})

# ─────────────────────────────────────────────────────────────────────────────
# Status → orderStatusOption mapping
# ─────────────────────────────────────────────────────────────────────────────

# Map the badge text from the DOM to the API query param value.
# We'll try these in order; if none works we'll try without the param.
STATUS_MAP = {
    "COMPLETED":   ["delivered", "booked", "return"],
    "DISPATCHED":  ["booked", "delivered"],
    "BOOKED":      ["booked"],
    "RETURNED":    ["return"],
    "CANCELLED":   ["cancelled"],
}

# Regex to capture the real API sheet_id from the intercepted network URL
_SHEET_ID_RE = re.compile(
    r"api\.postex\.pk/services/merchant/api/load-sheet/(\d+)/order"
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def write_json(path, data):
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def dump_html(page, name):
    try:
        html = page.content()
        p = DEBUG_DIR / f"{name}.html"
        p.write_text(html, encoding="utf-8")
        trace(f"HTML -> {p} ({len(html)} chars)")
    except Exception:
        log.exception("html dump failed")


def screenshot(page, name):
    try:
        p = DEBUG_DIR / f"{name}.png"
        page.screenshot(path=str(p), full_page=True)
        trace(f"Screenshot -> {p}")
    except Exception:
        log.exception("screenshot failed")


def safe_fn(url, maxlen=120):
    return re.sub(r"[^a-zA-Z0-9._-]", "_", url)[:maxlen]


def retry(fn, retries=3, delay=2):
    for attempt in range(retries):
        try:
            trace(f"Attempt {attempt+1}/{retries}")
            return fn()
        except Exception:
            log.exception(f"Attempt {attempt+1} failed")
            if attempt < retries - 1:
                time.sleep(delay)
    raise RuntimeError("All retries exhausted")


def matches_target_date(text):
    if not text:
        return False
    m = re.search(r"(\w+)\s+(\d{1,2}),\s+(\d{4})", text.strip())
    if not m:
        return False
    month, day_s, year_s = m.groups()
    try:
        matched = (month == TARGET_MONTH and
                   int(day_s) == TARGET_DAY and
                   int(year_s) == TARGET_YEAR)
    except Exception:
        return False
    trace("Date check", {"raw": text, "matched": matched})
    return matched


# ─────────────────────────────────────────────────────────────────────────────
# Login
# ─────────────────────────────────────────────────────────────────────────────

def login(page):
    trace("Navigating to login")
    page.goto(LOGIN_URL, wait_until="networkidle")
    dump_html(page, "01_login")
    screenshot(page, "01_login")
    page.fill('input[type="email"]',    USERNAME)
    page.fill('input[type="password"]', PASSWORD)
    page.click('button[type="submit"]')
    page.wait_for_url(f"{BASE_URL}/main/**", timeout=30_000)
    trace("Login OK", {"url": page.url})
    dump_html(page,  "02_post_login")
    screenshot(page, "02_post_login")


# ─────────────────────────────────────────────────────────────────────────────
# Build requests.Session from browser auth
# ─────────────────────────────────────────────────────────────────────────────

def build_session(page):
    trace("Extracting auth token from browser storage")
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
    trace("Storage dump", storage)

    token = ""
    jwt_re = re.compile(
        r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    )

    # Try well-known key names first
    for store_dict in [storage.get("sessionStorage", {}),
                       storage.get("localStorage",  {})]:
        for k in ["token", "authToken", "access_token", "accessToken",
                  "bearerToken", "jwt", "id_token"]:
            v = store_dict.get(k, "")
            if v and v != "undefined":
                token = v
                trace(f"Token from key '{k}'", {"preview": token[:60]})
                break
        if token:
            break

    # Regex scan fallback
    if not token:
        for store_dict in [storage.get("sessionStorage", {}),
                           storage.get("localStorage",  {})]:
            for k, v in store_dict.items():
                hit = jwt_re.search(str(v or ""))
                if hit:
                    token = hit.group(0)
                    trace(f"Token via regex from '{k}'")
                    break
            if token:
                break

    if not token:
        trace("WARNING: no token found — API calls will 401")

    session = requests.Session()
    session.headers.update({
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Authorization":   f"Bearer {token}",
        "Origin":          BASE_URL,
        "Referer":         LOADSHEET_URL,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    })

    cookies = page.context.cookies()
    for c in cookies:
        session.cookies.set(c["name"], c["value"], domain=c.get("domain"))

    trace("Session built", {
        "token_length": len(token),
        "cookies":      len(cookies),
    })
    return session, token


# ─────────────────────────────────────────────────────────────────────────────
# Wait for the Angular table to fully render
# ─────────────────────────────────────────────────────────────────────────────

def wait_for_table(page, timeout=30_000):
    """
    Wait until at least one `.data-item` row with real <td> content appears.
    Angular renders asynchronously — networkidle isn't always enough.
    """
    trace("Waiting for Angular table to render")
    try:
        # Wait for any data-item row to appear
        page.wait_for_selector("tr.data-item td", timeout=timeout)
        trace("Table rows with <td> are present")
    except PWTimeout:
        trace("Timed out waiting for table — will proceed anyway")
    # Extra settle time for Angular's change detection
    time.sleep(3)


# ─────────────────────────────────────────────────────────────────────────────
# Click the order-count span and capture the real API sheet_id
# ─────────────────────────────────────────────────────────────────────────────

def capture_real_sheet_id(page, span_element, row_label="row"):
    """
    Click the blue order-count <span> and intercept every network request
    to find the real API sheet_id embedded in the URL.
    Returns (real_sheet_id | None, captured_request_headers | {})
    """
    trace(f"[{row_label}] Attaching network listeners")

    captured_ids  = []
    captured_hdrs = {}
    all_requests  = []
    all_responses = []

    def on_request(req):
        entry = {
            "url":    req.url,
            "method": req.method,
            "type":   req.resource_type,
            "hdrs":   dict(req.headers),
        }
        all_requests.append(entry)

        m = _SHEET_ID_RE.search(req.url)
        if m:
            sid = m.group(1)
            trace(f"[{row_label}] *** REAL sheet_id FOUND ***", {
                "sheet_id": sid,
                "url":      req.url,
            })
            captured_ids.append(sid)
            if not captured_hdrs:
                captured_hdrs.update(dict(req.headers))

    def on_response(res):
        preview = ""
        try:
            preview = res.text()[:2000]
        except Exception:
            pass
        all_responses.append({
            "url":    res.url,
            "status": res.status,
            "body":   preview,
        })
        trace(f"[{row_label}] RESPONSE {res.status}", res.url)

    page.on("request",  on_request)
    page.on("response", on_response)

    try:
        trace(f"[{row_label}] Clicking order-count span")
        span_element.click()
        trace(f"[{row_label}] Click done — waiting 10s for network")
        time.sleep(10)
    except Exception:
        log.exception(f"[{row_label}] Click failed")
    finally:
        try:
            page.remove_listener("request",  on_request)
            page.remove_listener("response", on_response)
        except Exception:
            pass

    # Save click log
    log_path = DEBUG_DIR / f"click_{row_label}.json"
    write_json(log_path, {
        "requests":     all_requests,
        "responses":    all_responses,
        "captured_ids": captured_ids,
    })
    trace(f"[{row_label}] Network log -> {log_path}", {
        "requests":    len(all_requests),
        "captured_ids": captured_ids,
    })

    real_id = captured_ids[0] if captured_ids else None
    return real_id, captured_hdrs


# ─────────────────────────────────────────────────────────────────────────────
# Find and parse loadsheet rows
# ─────────────────────────────────────────────────────────────────────────────

# ── Which <td> index holds each field? ──────────────────────────────────────
#  Based on the actual HTML from Document 3:
#   [0] loadsheet number  (dt-orderRefNum)
#   [1] total orders      (dt-tracking)   ← CLICKABLE blue span
#   [2] delivered count   (dt-type)
#   [3] return count      (dt-status)
#   [4] (empty / merchant name)
#   [5] date              (dt-city)
#   [6] status badge      (dt-detail)
#   [7] action menu       (last td)

CELL_MAP = {
    "loadsheet_number": 0,
    "total_orders":     1,
    "delivered":        2,
    "returns":          3,
    "date":             5,
    "status":           6,
}


def find_rows(page):
    trace("Searching for data-item rows")

    rows = page.query_selector_all("table tbody tr.data-item")
    trace(f"Found {len(rows)} tr.data-item rows")

    if not rows:
        dump_html(page,  "ERROR_no_rows")
        screenshot(page, "ERROR_no_rows")
        raise RuntimeError("No rows found — see HTML dump")

    results = []

    for idx, row in enumerate(rows):
        try:
            raw_html = row.inner_html()
            (DEBUG_DIR / f"row_{idx}.html").write_text(raw_html, encoding="utf-8")

            cells = row.query_selector_all("td")
            trace(f"Row {idx}: {len(cells)} cells")

            if len(cells) < 7:
                trace(f"Row {idx}: skipped — only {len(cells)} cells")
                continue

            def cell_text(n):
                try:
                    return cells[n].inner_text().strip()
                except Exception:
                    return ""

            date_text = cell_text(CELL_MAP["date"])
            if not matches_target_date(date_text):
                trace(f"Row {idx}: date mismatch ({date_text!r})")
                continue

            status = cell_text(CELL_MAP["status"]).upper()

            # Extract dom_sheet_id from the more-menu-* class in the raw HTML
            dom_sid = None
            m = re.search(r"more-menu-(\d+)", raw_html)
            if m:
                dom_sid = m.group(1)
            # Also try data-id attribute as fallback
            if not dom_sid:
                m = re.search(r'data-id="(\d+)"', raw_html)
                if m:
                    dom_sid = m.group(1)

            row_data = {
                "row_index":        idx,
                "loadsheet_number": cell_text(CELL_MAP["loadsheet_number"]),
                "total_orders":     cell_text(CELL_MAP["total_orders"]),
                "delivered":        cell_text(CELL_MAP["delivered"]),
                "returns":          cell_text(CELL_MAP["returns"]),
                "date_text":        date_text,
                "status":           status,
                "dom_sheet_id":     dom_sid,
                "real_sheet_id":    None,
                "click_headers":    {},
            }
            trace(f"Row {idx}: data", row_data)

            # ── Find the clickable span (first blue span in td[1]) ──────────
            # Selector: the <span> inside the dt-tracking <td>
            # From HTML: <span class="smaller-text" style="cursor: pointer; color: blue;">
            clickable = None

            # Try multiple selectors in order of specificity
            for sel in [
                "td.dt-tracking span",          # class-based
                "td:nth-child(2) span",          # positional fallback
                "span[style*='color: blue']",    # style-based fallback
            ]:
                try:
                    clickable = row.query_selector(sel)
                    if clickable:
                        txt = clickable.inner_text().strip()
                        trace(f"Row {idx}: clickable found via '{sel}', text='{txt}'")
                        break
                except Exception:
                    pass

            if clickable:
                real_id, click_hdrs = capture_real_sheet_id(
                    page, clickable, row_label=f"row{idx}"
                )
                row_data["real_sheet_id"] = real_id
                row_data["click_headers"] = click_hdrs
                trace(f"Row {idx}: real_sheet_id={real_id}")
            else:
                trace(f"Row {idx}: no clickable span found — will try dom_sheet_id")
                # Dump the row HTML so we can inspect manually
                trace(f"Row {idx}: raw HTML for debug", raw_html[:800])

            results.append(row_data)

        except Exception:
            log.exception(f"Row {idx} processing failed")

    trace(f"Matched rows for {TARGET_LABEL}: {len(results)}")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Fetch orders from the API
# ─────────────────────────────────────────────────────────────────────────────

def fetch_orders_for_status(session, sheet_id, status_option, merged_headers):
    """Single attempt with one orderStatusOption value."""
    url    = f"{API_BASE}/{sheet_id}/order"
    params = {
        "loadSheetId":       sheet_id,
        "orderStatusOption": status_option,
        "direction":         "desc",
    }
    trace(f"  Trying status_option={status_option!r}", {"url": url, "params": params})

    resp = session.get(url, params=params, headers=merged_headers, timeout=30)
    raw  = resp.text

    # Save raw
    raw_path = DEBUG_DIR / f"api_{sheet_id}_{status_option}.txt"
    raw_path.write_text(raw, encoding="utf-8")

    try:
        data = resp.json()
    except Exception:
        data = {"raw_text": raw}

    trace(f"  Response {resp.status_code}", {
        "body_preview": raw[:500],
        "url":          resp.url,
    })
    return resp.status_code, data


def fetch_orders(session, sheet_id, row_status="COMPLETED",
                 browser_headers=None):
    """
    Try each orderStatusOption candidate for the given row status.
    Also try with no orderStatusOption as a last resort.
    Returns the best successful result, or the last failed one.
    """
    # Merge browser-captured headers (override session defaults)
    merged = dict(session.headers)
    if browser_headers:
        for k, v in browser_headers.items():
            if k.lower() in ("content-length", "host"):
                continue
            if k.lower() == "authorization" and v:
                merged["Authorization"] = v
            else:
                merged[k] = v

    trace("fetch_orders", {
        "sheet_id":   sheet_id,
        "row_status": row_status,
    })

    # Build candidate status options
    candidates = list(STATUS_MAP.get(row_status, ["booked", "delivered"]))
    # Always append a no-filter attempt (empty string skips the param)
    # and a wildcard 'all' in case the API accepts it
    candidates += ["", "all"]

    last_result = None

    for status_option in candidates:
        try:
            if status_option == "":
                # Try without the orderStatusOption param at all
                url    = f"{API_BASE}/{sheet_id}/order"
                params = {
                    "loadSheetId": sheet_id,
                    "direction":   "desc",
                }
                trace("  Trying WITHOUT orderStatusOption", {"url": url})
                resp  = session.get(url, params=params,
                                    headers=merged, timeout=30)
                raw   = resp.text
                (DEBUG_DIR / f"api_{sheet_id}_nooption.txt").write_text(
                    raw, encoding="utf-8"
                )
                try:
                    data = resp.json()
                except Exception:
                    data = {"raw_text": raw}
                code = resp.status_code
            else:
                code, data = fetch_orders_for_status(
                    session, sheet_id, status_option, merged
                )

            last_result = {
                "status_option": status_option or "(none)",
                "status_code":   code,
                "orders":        data,
            }

            if code == 200:
                trace(f"  SUCCESS with status_option={status_option!r}")
                return last_result

        except Exception:
            log.exception(f"  Exception for status_option={status_option!r}")

    trace("  All candidates exhausted", last_result)
    return last_result or {
        "status_option": "none",
        "status_code":   None,
        "orders":        [],
        "error":         "No successful response",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    trace("SCRAPER v3 STARTED", {
        "target": TARGET_LABEL,
        "output": str(OUTPUT_FILE),
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

        page.on("console",       lambda m: trace(f"BROWSER[{m.type}]", m.text))
        page.on("pageerror",     lambda e: trace("PAGE ERROR", str(e)))
        page.on("requestfailed", lambda r: trace("REQ FAILED", {
            "url": r.url, "err": str(r.failure)
        }))

        # ── Login ──────────────────────────────────────────────────────────
        retry(lambda: login(page))

        # ── Build session ──────────────────────────────────────────────────
        session, token = build_session(page)

        # ── Navigate to loadsheets ─────────────────────────────────────────
        trace("Navigating to loadsheet page")
        page.goto(LOADSHEET_URL, wait_until="networkidle")

        # Wait for Angular to finish rendering the table
        wait_for_table(page, timeout=30_000)

        dump_html(page,  "03_loadsheet")
        screenshot(page, "03_loadsheet")

        # ── Scrape rows ────────────────────────────────────────────────────
        rows = retry(lambda: find_rows(page))
        trace(f"Processing {len(rows)} matched row(s)")

        for row in rows:
            try:
                real_id = row.get("real_sheet_id")
                dom_id  = row.get("dom_sheet_id")
                sid     = real_id or dom_id
                row["final_sheet_id"] = sid

                trace("Sheet ID decision", {
                    "real (network)": real_id,
                    "dom  (html)":    dom_id,
                    "using":          sid,
                    "WARNING": (
                        None if real_id
                        else "Using dom_sheet_id — may be wrong! "
                             "Check if click capture succeeded."
                    ),
                })

                if not sid:
                    trace("Skipping row — no sheet_id available")
                    continue

                click_hdrs = row.get("click_headers") or {}
                result = fetch_orders(
                    session,
                    sid,
                    row_status=row.get("status", "COMPLETED"),
                    browser_headers=click_hdrs,
                )
                row["api_result"] = result
                final["loadsheets"].append(row)

            except Exception:
                log.exception("Row processing failed")

        context.tracing.stop(path=str(DEBUG_DIR / "trace.zip"))
        browser.close()

    write_json(OUTPUT_FILE, final)
    trace("DONE", {
        "rows_processed":   len(rows),
        "loadsheets_saved": len(final["loadsheets"]),
        "output":           str(OUTPUT_FILE),
    })


if __name__ == "__main__":
    main()
