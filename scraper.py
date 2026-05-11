"""
PostEx Loadsheet Scraper v4 — DIRECT API EDITION
==================================================
ROOT CAUSE OF v2/v3 FAILURE:
  The GitHub Actions runner blocks outbound connections to api.postex.pk
  from the headless browser (net::ERR_ABORTED on every API call).
  Angular never gets its data → table stays empty → 0 rows found.

  However, requests.Session CAN reach api.postex.pk just fine
  (v2 got a 400 back, proving the TCP connection works).

STRATEGY:
  1. Use Playwright ONLY for login (to get the auth token).
  2. Do ALL data fetching via requests.Session directly — no browser clicks.
  3. Call the loadsheet LIST API first to discover real numeric sheet IDs.
  4. Then call the orders API with those real IDs.

LOADSHEET LIST API (reverse-engineered from the browser network tab):
  GET https://api.postex.pk/services/merchant/api/load-sheet
      ?merchantId=<id>&page=0&size=20&direction=desc&sortBy=createdDate

  This returns all loadsheets with their real numeric IDs, dates, statuses, etc.
"""

import os
import re
import json
import time
import logging
import traceback

from datetime import datetime, timedelta
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-8s] %(funcName)s:%(lineno)d | %(message)s",
)
log = logging.getLogger("postex-v4")

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

BASE_URL      = "https://merchant.postex.pk"
LOGIN_URL     = f"{BASE_URL}/login"
LOADSHEET_URL = f"{BASE_URL}/main/load-sheet-logs"

# Direct API endpoints
API_ROOT      = "https://api.postex.pk/services/merchant/api"
API_LS_LIST   = f"{API_ROOT}/load-sheet"          # GET → list of loadsheets
API_LS_ORDERS = f"{API_ROOT}/load-sheet"           # GET /{id}/order

USERNAME    = os.environ.get("POSTEX_USERNAME", "")
PASSWORD    = os.environ.get("POSTEX_PASSWORD", "")
MERCHANT_ID = os.environ.get("POSTEX_MERCHANT_ID", "")  # optional override

OUTPUT_DIR = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)
DEBUG_DIR = OUTPUT_DIR / "debug"
DEBUG_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# Date
# ─────────────────────────────────────────────────────────────────────────────

TESTING_ON = True

if TESTING_ON:
    TARGET_DATE = datetime(2026, 5, 9)
else:
    DATE_OVERRIDE = os.environ.get("DATE_OVERRIDE")
    if DATE_OVERRIDE:
        TARGET_DATE = datetime.strptime(DATE_OVERRIDE, "%Y-%m-%d")
    else:
        TARGET_DATE = datetime.now() - timedelta(days=1)
DATE_TAG     = TARGET_DATE.strftime("%Y-%m-%d")
TARGET_MONTH = TARGET_DATE.strftime("%b")   # "May"
TARGET_DAY   = TARGET_DATE.day              # 9
TARGET_YEAR  = TARGET_DATE.year             # 2026
TARGET_LABEL = f"{TARGET_MONTH} {TARGET_DAY}, {TARGET_YEAR}"
OUTPUT_FILE  = OUTPUT_DIR / f"loadsheet_{DATE_TAG}.json"

# API date filter (ISO format for query params)
TARGET_DATE_ISO_START = TARGET_DATE.strftime("%Y-%m-%d") + "T00:00:00"
TARGET_DATE_ISO_END   = TARGET_DATE.strftime("%Y-%m-%d") + "T23:59:59"

trace("Config", {
    "target":      TARGET_LABEL,
    "date_iso":    TARGET_DATE_ISO_START,
    "output":      str(OUTPUT_FILE),
    "merchant_id": MERCHANT_ID or "(will read from session storage)",
})


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def write_json(path, data):
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def screenshot(page, name):
    try:
        p = DEBUG_DIR / f"{name}.png"
        page.screenshot(path=str(p), full_page=True)
        trace(f"Screenshot -> {p}")
    except Exception:
        log.exception("screenshot failed")


def matches_target_date(date_str):
    """
    Accept any of:
      - ISO: "2026-05-09T16:16:58" / "2026-05-09"
      - Human: "May 9, 2026, 4:16:58 PM"
      - Epoch ms: 1746806218000
    """
    if not date_str:
        return False
    s = str(date_str).strip()

    # Epoch milliseconds
    if re.fullmatch(r"\d{13}", s):
        dt = datetime.fromtimestamp(int(s) / 1000)
        matched = (dt.year == TARGET_YEAR and
                   dt.month == TARGET_DATE.month and
                   dt.day   == TARGET_DAY)
        trace("Date check (epoch ms)", {
            "raw": s, "parsed": str(dt), "matched": matched
        })
        return matched

    # ISO date prefix
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        matched = (y == TARGET_YEAR and
                   mo == TARGET_DATE.month and
                   d == TARGET_DAY)
        trace("Date check (ISO)", {"raw": s, "matched": matched})
        return matched

    # Human-readable: "May 9, 2026"
    m = re.search(r"(\w+)\s+(\d{1,2}),\s+(\d{4})", s)
    if m:
        month, day_s, year_s = m.groups()
        matched = (month == TARGET_MONTH and
                   int(day_s) == TARGET_DAY and
                   int(year_s) == TARGET_YEAR)
        trace("Date check (human)", {"raw": s, "matched": matched})
        return matched

    trace("Date check (no pattern matched)", s)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Login via Playwright, extract token + merchant_id
# ─────────────────────────────────────────────────────────────────────────────

def browser_login():
    """
    Returns (token: str, merchant_id: str, cookies: list[dict])
    Uses Playwright only for the login handshake.
    """
    trace("Starting Playwright for login only")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context()
        page    = context.new_page()

        page.on("console",   lambda m: log.debug(f"BROWSER[{m.type}] {m.text}"))
        page.on("pageerror", lambda e: log.debug(f"PAGE ERROR: {e}"))

        trace("Navigating to login page")
        page.goto(LOGIN_URL, wait_until="networkidle")
        screenshot(page, "01_login")

        trace("Filling credentials")
        page.fill('input[type="email"]',    USERNAME)
        page.fill('input[type="password"]', PASSWORD)
        page.click('button[type="submit"]')
        page.wait_for_url(f"{BASE_URL}/main/**", timeout=30_000)
        screenshot(page, "02_post_login")
        trace("Login OK", {"url": page.url})

        # ── Extract storage ──────────────────────────────────────────────────
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

        # Token
        token = ""
        jwt_re = re.compile(
            r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
        )
        for store in [storage.get("sessionStorage", {}),
                      storage.get("localStorage",  {})]:
            for k in ["token", "authToken", "access_token", "accessToken",
                      "bearerToken", "jwt", "id_token"]:
                v = store.get(k, "") or ""
                if v and v != "undefined":
                    token = v
                    trace(f"Token from sessionStorage['{k}']",
                          {"len": len(token), "preview": token[:60]})
                    break
            if token:
                break
        if not token:
            for store in [storage.get("sessionStorage", {}),
                          storage.get("localStorage",  {})]:
                for k, v in store.items():
                    hit = jwt_re.search(str(v or ""))
                    if hit:
                        token = hit.group(0)
                        trace(f"Token via regex from key '{k}'")
                        break
                if token:
                    break

        # Merchant ID
        ss = storage.get("sessionStorage", {})
        merchant_id = (
            MERCHANT_ID
            or ss.get("merchantId", "")
            or ss.get("postexAccountId", "")
            or ""
        )
        trace("merchant_id", {"value": merchant_id})

        # Cookies
        cookies = context.cookies()
        trace(f"Captured {len(cookies)} cookies")

        browser.close()

    if not token:
        raise RuntimeError("No auth token found — login may have failed")
    if not merchant_id:
        raise RuntimeError(
            "No merchantId found in sessionStorage. "
            "Set POSTEX_MERCHANT_ID env var as fallback."
        )

    return token, merchant_id, cookies


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — Build requests.Session
# ─────────────────────────────────────────────────────────────────────────────

def build_session(token, cookies):
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
    for c in cookies:
        session.cookies.set(c["name"], c["value"], domain=c.get("domain"))
    trace("Session built", {"cookies": len(cookies)})
    return session


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — Fetch loadsheet LIST directly from the API
# ─────────────────────────────────────────────────────────────────────────────

def fetch_loadsheet_list(session, merchant_id, page=0, size=50):
    """
    GET /services/merchant/api/load-sheet
        ?merchantId=62247&page=0&size=50&direction=desc&sortBy=createdDate

    Try several param combinations because PostEx's API is inconsistent.
    Returns the raw JSON response.
    """

    # Candidate param sets to try, in order.
    # The browser sends these — we try each until one returns 200.
    param_sets = [
        # Most likely — what the Angular app sends
        {
            "merchantId": merchant_id,
            "page":       page,
            "size":       size,
            "direction":  "desc",
            "sortBy":     "createdDate",
        },
        # Without sortBy
        {
            "merchantId": merchant_id,
            "page":       page,
            "size":       size,
            "direction":  "desc",
        },
        # With date filter added
        {
            "merchantId":  merchant_id,
            "page":        page,
            "size":        size,
            "direction":   "desc",
            "sortBy":      "createdDate",
            "fromDate":    TARGET_DATE.strftime("%Y-%m-%d"),
            "toDate":      TARGET_DATE.strftime("%Y-%m-%d"),
        },
        # Minimal
        {
            "merchantId": merchant_id,
        },
    ]

    last_resp = None
    for params in param_sets:
        trace(f"Trying loadsheet list params", params)
        try:
            r = session.get(API_LS_LIST, params=params, timeout=30)
            raw = r.text
            trace(f"Response {r.status_code}", {
                "url":     r.url,
                "preview": raw[:1000],
            })
            (DEBUG_DIR / f"ls_list_{hash(str(params))}.json").write_text(
                raw, encoding="utf-8"
            )
            if r.status_code == 200:
                try:
                    return r.json()
                except Exception:
                    trace("JSON parse failed", raw[:500])
            last_resp = r
        except Exception:
            log.exception("loadsheet list request failed")

    # If all fail, raise with the last response body for diagnosis
    body = last_resp.text if last_resp else "(no response)"
    raise RuntimeError(
        f"All loadsheet list attempts failed. "
        f"Last status: {getattr(last_resp, 'status_code', None)}. "
        f"Body: {body[:500]}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — Parse list response and find target-date loadsheets
# ─────────────────────────────────────────────────────────────────────────────

def parse_loadsheet_list(api_response):
    """
    The API may return data in several shapes:
      - { "data": [ {...}, ... ] }
      - { "payload": [ {...}, ... ] }
      - { "content": [ {...}, ... ] }    ← Spring Page
      - [ {...}, ... ]                   ← bare array
      - { "statusCode": "015", ... }     ← error

    Each item likely has keys like:
      id / loadSheetId / loadsheetId     ← the real numeric sheet_id
      createdDate / date / loadSheetDate
      status / loadSheetStatus
      totalOrders / ordersCount
      loadSheetNumber / loadsheetNumber
    """
    trace("Parsing loadsheet list response", api_response)

    # Unwrap envelope
    items = []
    if isinstance(api_response, list):
        items = api_response
    elif isinstance(api_response, dict):
        for key in ["data", "payload", "content", "loadSheets",
                    "loadsheets", "result", "results", "items"]:
            if key in api_response and isinstance(api_response[key], list):
                items = api_response[key]
                trace(f"Unwrapped from key '{key}'", {"count": len(items)})
                break
        if not items:
            # Maybe it IS the item (single-result response)
            if "id" in api_response or "loadSheetId" in api_response:
                items = [api_response]
            else:
                trace("Could not unwrap list — full response", api_response)
                raise RuntimeError(
                    f"Unrecognised loadsheet list shape: {list(api_response.keys())}"
                )

    trace(f"Total loadsheet items in response: {len(items)}")

    # Find date field and id field dynamically
    ID_KEYS   = ["id", "loadSheetId", "loadsheetId", "sheetId",
                 "loadSheet_id", "load_sheet_id"]
    DATE_KEYS = ["createdDate", "date", "loadSheetDate", "createdAt",
                 "created_date", "loadsheetDate", "dateCreated"]
    NUM_KEYS  = ["loadSheetNumber", "loadsheetNumber", "loadSheetNo",
                 "trackingNumber", "number"]
    STATUS_KEYS = ["status", "loadSheetStatus", "loadsheetStatus"]
    ORDERS_KEYS = ["totalOrders", "ordersCount", "total", "orderCount"]

    def first_val(d, keys, default=None):
        for k in keys:
            if k in d:
                return d[k]
        return default

    matched = []
    for item in items:
        date_val = first_val(item, DATE_KEYS)
        if not matches_target_date(date_val):
            trace(f"Skipping item (date mismatch)", {
                "date_val": date_val,
                "id":       first_val(item, ID_KEYS),
            })
            continue

        sheet_id = first_val(item, ID_KEYS)
        matched.append({
            "real_sheet_id":    str(sheet_id) if sheet_id else None,
            "loadsheet_number": first_val(item, NUM_KEYS),
            "status":           str(first_val(item, STATUS_KEYS, "")).upper(),
            "total_orders":     first_val(item, ORDERS_KEYS),
            "date_text":        str(date_val),
            "raw_item":         item,
        })
        trace(f"Matched item", matched[-1])

    trace(f"Matched {len(matched)} loadsheet(s) for {TARGET_LABEL}")
    return matched


# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — Fetch orders for a given real sheet_id
# ─────────────────────────────────────────────────────────────────────────────

STATUS_OPTIONS = {
    "COMPLETED":  ["delivered", "booked", "return", ""],
    "DISPATCHED": ["booked", "delivered", ""],
    "BOOKED":     ["booked", ""],
    "RETURNED":   ["return", ""],
    "CANCELLED":  ["cancelled", ""],
    "":           ["booked", "delivered", "return", ""],
}


def fetch_orders(session, sheet_id, row_status="COMPLETED"):
    url = f"{API_LS_ORDERS}/{sheet_id}/order"
    candidates = STATUS_OPTIONS.get(row_status, ["booked", "delivered", "return", ""])

    all_results = []

    for opt in candidates:
        params = {
            "loadSheetId": sheet_id,
            "direction":   "desc",
        }
        if opt:
            params["orderStatusOption"] = opt

        trace(f"Fetching orders", {"sheet_id": sheet_id, "params": params})

        try:
            r = session.get(url, params=params, timeout=30)
            raw = r.text

            raw_path = DEBUG_DIR / f"orders_{sheet_id}_{opt or 'nooption'}.json"
            raw_path.write_text(raw, encoding="utf-8")

            trace(f"Response {r.status_code}", {
                "url":     r.url,
                "preview": raw[:1000],
            })

            try:
                data = r.json()
            except Exception:
                data = {"raw_text": raw}

            result = {
                "status_option": opt or "(none)",
                "status_code":   r.status_code,
                "url":           r.url,
                "data":          data,
            }

            if r.status_code == 200:
                trace(f"SUCCESS: sheet_id={sheet_id}, opt={opt!r}")
                # Return immediately on first success
                return result

            all_results.append(result)

        except Exception:
            log.exception(f"Order fetch failed for opt={opt!r}")

    # All failed — return last attempt for diagnosis
    trace("All order fetch attempts failed", {
        "sheet_id":   sheet_id,
        "last_result": all_results[-1] if all_results else None,
    })
    return all_results[-1] if all_results else {
        "status_option": "none",
        "status_code":   None,
        "url":           url,
        "data":          {},
        "error":         "All attempts failed",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    trace("SCRAPER v4 STARTED", {
        "target": TARGET_LABEL,
        "output": str(OUTPUT_FILE),
    })

    final = {
        "scrape_date": DATE_TAG,
        "target_date": TARGET_LABEL,
        "loadsheets":  [],
    }

    # ── 1. Login (browser only for auth) ──────────────────────────────────
    token, merchant_id, cookies = browser_login()
    trace("Auth acquired", {
        "merchant_id":  merchant_id,
        "token_length": len(token),
    })

    # ── 2. Build requests session ─────────────────────────────────────────
    session = build_session(token, cookies)

    # ── 3. Fetch loadsheet list ───────────────────────────────────────────
    trace("Fetching loadsheet list via direct API call")
    ls_response = fetch_loadsheet_list(session, merchant_id)
    write_json(DEBUG_DIR / "loadsheet_list_raw.json", ls_response)

    # ── 4. Parse and filter to target date ───────────────────────────────
    matched_sheets = parse_loadsheet_list(ls_response)
    trace(f"{len(matched_sheets)} sheet(s) matched for {TARGET_LABEL}")

    if not matched_sheets:
        trace("No loadsheets found for target date — check loadsheet_list_raw.json")
        write_json(OUTPUT_FILE, final)
        return

    # ── 5. Fetch orders for each matched sheet ────────────────────────────
    for sheet in matched_sheets:
        sheet_id   = sheet.get("real_sheet_id")
        row_status = sheet.get("status", "COMPLETED")

        trace(f"Processing sheet", {
            "id":     sheet_id,
            "number": sheet.get("loadsheet_number"),
            "status": row_status,
        })

        if not sheet_id:
            trace("Skipping — no sheet_id in API response")
            sheet["api_result"] = {"error": "no sheet_id"}
            final["loadsheets"].append(sheet)
            continue

        order_result = fetch_orders(session, sheet_id, row_status)
        sheet["api_result"] = order_result
        final["loadsheets"].append(sheet)

    # ── 6. Save output ────────────────────────────────────────────────────
    write_json(OUTPUT_FILE, final)
    trace("DONE", {
        "sheets_found":  len(matched_sheets),
        "sheets_saved":  len(final["loadsheets"]),
        "output":        str(OUTPUT_FILE),
    })


if __name__ == "__main__":
    main()
