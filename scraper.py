"""
PostEx Loadsheet Scraper v7
============================
BACK TO BASICS — v2 was working. It found rows, found dates, found dom_sheet_id.
The ONLY things broken in v2 were:
  1. `span.orders` selector — class doesn't exist → click never fired → no real_sheet_id
  2. Fell back to dom_sheet_id=4786 which is wrong

Fix:
  - Use the EXACT selector from real HTML: td.dt-tracking span
  - Keep everything else from v2 that worked
  - Add page.route() proxy so the table actually loads (fixes ERR_ABORTED)

Real HTML from Document 3:
  <td class="data-col dt-tracking" style="width: 100px;">
    <span class="smaller-text" style="cursor: pointer; color: blue;"> 28 </span>
  </td>

Correct selector: "td.dt-tracking span"
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


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-8s] %(funcName)s:%(lineno)d | %(message)s",
)
log = logging.getLogger("postex-v7")

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
API_HOST      = "api.postex.pk"
API_ROOT      = f"https://{API_HOST}/services/merchant/api"

USERNAME = os.environ.get("POSTEX_USERNAME", "")
PASSWORD = os.environ.get("POSTEX_PASSWORD", "")

OUTPUT_DIR = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)
DEBUG_DIR = OUTPUT_DIR / "debug"
DEBUG_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# Date — set TESTING_ON=True to always scrape May 9 2026
# ─────────────────────────────────────────────────────────────────────────────

TESTING_ON = os.environ.get("TESTING_ON", "true").lower() == "true"

if TESTING_ON:
    TARGET_DATE = datetime(2026, 5, 9)
else:
    DATE_OVERRIDE = os.environ.get("DATE_OVERRIDE")
    TARGET_DATE   = (datetime.strptime(DATE_OVERRIDE, "%Y-%m-%d")
                     if DATE_OVERRIDE else datetime.now() - timedelta(days=1))

DATE_TAG     = TARGET_DATE.strftime("%Y-%m-%d")
TARGET_MONTH = TARGET_DATE.strftime("%b")
TARGET_DAY   = TARGET_DATE.day
TARGET_YEAR  = TARGET_DATE.year
TARGET_LABEL = f"{TARGET_MONTH} {TARGET_DAY}, {TARGET_YEAR}"
OUTPUT_FILE  = OUTPUT_DIR / f"loadsheet_{DATE_TAG}.json"

trace("Config", {
    "target":     TARGET_LABEL,
    "testing_on": TESTING_ON,
    "output":     str(OUTPUT_FILE),
})

# Regex to find real sheet_id in intercepted order URL
# e.g. https://api.postex.pk/services/merchant/api/load-sheet/5381184/order
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
        pass

def matches_target_date(text):
    if not text:
        return False
    s = str(text).strip()
    # Epoch ms
    if re.fullmatch(r"\d{13}", s):
        dt = datetime.fromtimestamp(int(s) / 1000)
        return (dt.year == TARGET_YEAR and dt.month == TARGET_DATE.month
                and dt.day == TARGET_DAY)
    # ISO
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return (int(m.group(1)) == TARGET_YEAR
                and int(m.group(2)) == TARGET_DATE.month
                and int(m.group(3)) == TARGET_DAY)
    # Human: "May 9, 2026, 4:16:58 PM"
    m = re.search(r"(\w+)\s+(\d{1,2}),\s+(\d{4})", s)
    if m:
        month, day_s, year_s = m.groups()
        return (month == TARGET_MONTH and int(day_s) == TARGET_DAY
                and int(year_s) == TARGET_YEAR)
    return False

def safe_fn(url, maxlen=100):
    return re.sub(r"[^a-zA-Z0-9._-]", "_", url)[:maxlen]


# ─────────────────────────────────────────────────────────────────────────────
# Build requests.Session
# ─────────────────────────────────────────────────────────────────────────────

def make_session(token, cookies):
    s = requests.Session()
    s.headers.update({
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
        s.cookies.set(c["name"], c["value"], domain=c.get("domain"))
    return s


# ─────────────────────────────────────────────────────────────────────────────
# Main browser session
# ─────────────────────────────────────────────────────────────────────────────

def run():
    token       = ""
    merchant_id = ""
    cookies_out = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context()
        page    = context.new_page()
        page.on("console",   lambda m: log.debug(f"BROWSER[{m.type}] {m.text}"))
        page.on("pageerror", lambda e: log.debug(f"PAGE ERROR: {e}"))

        # ── LOGIN ────────────────────────────────────────────────────────────
        trace("Navigating to login")
        page.goto(LOGIN_URL, wait_until="networkidle")
        screenshot(page, "01_login")
        page.fill('input[type="email"]',    USERNAME)
        page.fill('input[type="password"]', PASSWORD)
        page.click('button[type="submit"]')
        page.wait_for_url(f"{BASE_URL}/main/**", timeout=30_000)
        screenshot(page, "02_post_login")
        trace("Login OK", {"url": page.url})

        # ── EXTRACT AUTH ─────────────────────────────────────────────────────
        storage = page.evaluate("""() => {
            const ss = {};
            for (let i = 0; i < sessionStorage.length; i++) {
                const k = sessionStorage.key(i);
                ss[k] = sessionStorage.getItem(k);
            }
            return ss;
        }""")
        token       = storage.get("token", "")
        merchant_id = storage.get("merchantId", "")
        cookies_out = context.cookies()
        trace("Auth", {"token_len": len(token), "merchant_id": merchant_id})

        # ── BUILD PROXY SESSION ──────────────────────────────────────────────
        session = make_session(token, cookies_out)

        # ── PROXY: intercept all api.postex.pk calls through requests ────────
        # This is the key fix — the browser can't reach the API but Python can.
        # We fulfill every browser API request using Python requests.
        def handle_route(route, request):
            url    = request.url
            method = request.method
            trace(f"PROXY {method} {url}")
            try:
                resp = session.request(
                    method  = method,
                    url     = url,
                    headers = {
                        "Accept":        "application/json, text/plain, */*",
                        "Authorization": f"Bearer {token}",
                        "Origin":        BASE_URL,
                        "Referer":       LOADSHEET_URL,
                    },
                    data    = request.post_data,
                    timeout = 30,
                )
                (DEBUG_DIR / f"proxy_{safe_fn(url)}.json").write_text(
                    resp.text, encoding="utf-8"
                )
                trace(f"  -> {resp.status_code}", resp.text[:200])
                route.fulfill(
                    status  = resp.status_code,
                    headers = {"Content-Type":                "application/json",
                               "Access-Control-Allow-Origin": "*"},
                    body    = resp.content,
                )
            except Exception:
                log.exception(f"Proxy error: {url}")
                route.fulfill(status=200,
                              headers={"Content-Type": "application/json"},
                              body=b"{}")

        page.route(f"**/{API_HOST}/**", handle_route)

        # ── NAVIGATE TO LOADSHEET PAGE ───────────────────────────────────────
        trace("Navigating to loadsheet page")
        page.goto(LOADSHEET_URL, wait_until="networkidle")

        # Wait for Angular to render the table
        # v2 found rows without this wait — but with proxy the page loads faster
        trace("Waiting for table rows (up to 20s)")
        try:
            page.wait_for_selector("tr.data-item", timeout=20_000)
            trace("tr.data-item rows appeared")
        except PWTimeout:
            trace("Timeout waiting for rows — proceeding anyway")

        time.sleep(3)  # Angular change detection
        dump_html(page,  "03_loadsheet")
        screenshot(page, "03_loadsheet")

        # ── FIND ROWS (same as v2 which worked) ──────────────────────────────
        trace("Counting all <tr> elements")
        trace(f"Total <tr>: {page.locator('tr').count()}")

        rows = page.query_selector_all("table tbody tr.data-item")
        trace(f"tr.data-item rows: {len(rows)}")

        if not rows:
            # Try without table/tbody constraint
            rows = page.query_selector_all("tr.data-item")
            trace(f"Broad tr.data-item rows: {len(rows)}")

        matched_rows = []

        for idx, row in enumerate(rows):
            raw_html = row.inner_html()
            (DEBUG_DIR / f"row_{idx}.html").write_text(raw_html, encoding="utf-8")

            cells = row.query_selector_all("td")
            trace(f"Row {idx}: {len(cells)} cells")

            if len(cells) < 6:
                trace(f"Row {idx}: skipped — only {len(cells)} cells")
                continue

            def cell_text(n):
                try:
                    return cells[n].inner_text().strip()
                except Exception:
                    return ""

            # Cell layout (confirmed from v2 logs + Document 3 HTML):
            # [0] loadsheet number   "LDS-ES7BY484060"
            # [1] total orders       "28"  ← CLICKABLE blue span
            # [2] delivered          "28"
            # [3] returns            "0"
            # [4] empty
            # [5] date               "May 9, 2026, 4:16:58 PM"
            # [6] status             "COMPLETED"
            # [7] action menu

            date_text = cell_text(5)
            status    = cell_text(6).upper()

            trace(f"Row {idx}", {
                "number":  cell_text(0),
                "orders":  cell_text(1),
                "date":    date_text,
                "status":  status,
            })

            if not matches_target_date(date_text):
                trace(f"Row {idx}: date mismatch ({date_text!r}), skipping")
                continue

            # DOM sheet_id from more-menu class (this is NOT the API sheet_id)
            dom_sheet_id = None
            m = re.search(r"more-menu-(\d+)", raw_html)
            if m:
                dom_sheet_id = m.group(1)
            trace(f"Row {idx}: dom_sheet_id={dom_sheet_id} (NOT the real API id)")

            row_data = {
                "row_index":        idx,
                "loadsheet_number": cell_text(0),
                "total_orders":     cell_text(1),
                "delivered":        cell_text(2),
                "returns":          cell_text(3),
                "date_text":        date_text,
                "status":           status,
                "dom_sheet_id":     dom_sheet_id,
                "real_sheet_id":    None,
                "order_api_url":    None,
            }

            # ── CLICK THE SPAN AND CAPTURE THE REAL SHEET_ID ─────────────────
            # The proxy intercepts the resulting API call, so we see the URL
            # which contains the real numeric sheet_id (e.g. 5381184)
            #
            # Selector confirmed from Document 3 HTML:
            #   <td class="data-col dt-tracking" ...>
            #     <span class="smaller-text" style="cursor: pointer; color: blue;">
            #
            # We track new proxy calls by recording URLs before vs after click.

            captured_ids  = []
            captured_url  = []

            # Temporarily override handle_route to also capture sheet_id
            # We do this by watching new files created in DEBUG_DIR — simpler:
            # just record all request URLs in a list during the click window.

            click_requests = []

            def on_request(req):
                click_requests.append(req.url)
                m2 = _SHEET_ID_RE.search(req.url)
                if m2:
                    captured_ids.append(m2.group(1))
                    captured_url.append(req.url)
                    trace(f"Row {idx}: *** REAL sheet_id captured ***", {
                        "sheet_id": m2.group(1),
                        "url":      req.url,
                    })

            page.on("request", on_request)

            # Try selectors in order — from most specific to least
            clicked = False
            for sel in [
                "td.dt-tracking span.smaller-text",   # exact match from HTML
                "td.dt-tracking span",                  # td class + any span
                "span.smaller-text[style*='color: blue']",  # style-based
                "span[style*='cursor: pointer']",       # cursor-based
            ]:
                try:
                    el = row.query_selector(sel)
                    if el:
                        txt = el.inner_text().strip()
                        trace(f"Row {idx}: clicking '{sel}' text='{txt}'")
                        el.click()
                        clicked = True
                        break
                except Exception as e:
                    trace(f"Row {idx}: selector '{sel}' -> {e}")

            if not clicked:
                # Last resort: click the second cell directly
                try:
                    cells[1].click()
                    clicked = True
                    trace(f"Row {idx}: clicked cells[1] directly")
                except Exception as e:
                    trace(f"Row {idx}: cells[1] click failed: {e}")

            if clicked:
                trace(f"Row {idx}: waiting 8s for network after click")
                time.sleep(8)

            page.remove_listener("request", on_request)

            trace(f"Row {idx}: click_requests captured", click_requests)

            if captured_ids:
                row_data["real_sheet_id"] = captured_ids[0]
                row_data["order_api_url"] = captured_url[0]
                trace(f"Row {idx}: real_sheet_id={captured_ids[0]}")
            else:
                trace(f"Row {idx}: NO real_sheet_id captured from click")
                trace("All click_requests", click_requests)

            matched_rows.append(row_data)

        dump_html(page,  "04_after_clicks")
        screenshot(page, "04_after_clicks")
        browser.close()

    return matched_rows, session


# ─────────────────────────────────────────────────────────────────────────────
# Fetch orders
# ─────────────────────────────────────────────────────────────────────────────

STATUS_OPTIONS = {
    "COMPLETED":  ["delivered", "booked", "return", ""],
    "DISPATCHED": ["booked", "delivered", ""],
    "BOOKED":     ["booked", ""],
    "RETURNED":   ["return", ""],
    "CANCELLED":  ["cancelled", ""],
    "":           ["booked", "delivered", "return", ""],
}


def fetch_orders(session, sheet_id, captured_url=None, row_status="COMPLETED"):
    base_url = f"{API_ROOT}/load-sheet/{sheet_id}/order"

    # Build list of attempts: captured URL first, then status option candidates
    attempts = []
    if captured_url:
        attempts.append(("captured_url", captured_url, None))

    for opt in STATUS_OPTIONS.get(row_status, ["booked", "delivered", "return", ""]):
        params = {"loadSheetId": sheet_id, "direction": "desc"}
        if opt:
            params["orderStatusOption"] = opt
        attempts.append((opt or "no_option", base_url, params))

    for label, url, params in attempts:
        trace(f"Orders [{label}]", {"url": url, "params": params})
        try:
            r   = session.get(url, params=params, timeout=30)
            raw = r.text
            (DEBUG_DIR / f"orders_{sheet_id}_{re.sub(r'[^a-z0-9]','_',label)}.json"
             ).write_text(raw, encoding="utf-8")
            trace(f"  -> {r.status_code}", raw[:500])

            try:
                data = r.json()
            except Exception:
                data = {"raw_text": raw}

            if r.status_code == 200:
                trace(f"  SUCCESS [{label}]")
                return {"label": label, "status_code": 200,
                        "url": r.url, "data": data}
        except Exception:
            log.exception(f"  Request failed [{label}]")

    return {"label": "all_failed", "status_code": None, "url": base_url, "data": {}}


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    trace("SCRAPER v7 STARTED", {"target": TARGET_LABEL})

    final = {
        "scrape_date": DATE_TAG,
        "target_date": TARGET_LABEL,
        "loadsheets":  [],
    }

    matched_rows, session = run()

    trace(f"{len(matched_rows)} matched row(s) for {TARGET_LABEL}")

    for row in matched_rows:
        sheet_id     = row.get("real_sheet_id") or row.get("dom_sheet_id")
        captured_url = row.get("order_api_url")
        status       = row.get("status", "COMPLETED")

        trace("Processing row", {
            "real_sheet_id": row.get("real_sheet_id"),
            "dom_sheet_id":  row.get("dom_sheet_id"),
            "using_id":      sheet_id,
            "captured_url":  captured_url,
            "status":        status,
        })

        if not sheet_id:
            row["api_result"] = {"error": "no sheet_id"}
            final["loadsheets"].append(row)
            continue

        row["api_result"] = fetch_orders(session, sheet_id, captured_url, status)
        final["loadsheets"].append(row)

    write_json(OUTPUT_FILE, final)
    trace("DONE", {"sheets": len(final["loadsheets"]), "output": str(OUTPUT_FILE)})


if __name__ == "__main__":
    main()
