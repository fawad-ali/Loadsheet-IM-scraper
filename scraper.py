"""
PostEx Loadsheet Scraper v6
============================
KEY INSIGHT from v5 logs:
  - The proxy works perfectly — we CAN call api.postex.pk from Python
  - The loadsheet page never fires its data API call on load (we see 0 load-sheet URLs)
  - The table rows have 0 <td> cells — Angular uses virtual/component rows
  - DOM parsing is unreliable for Angular apps

NEW STRATEGY — Don't touch the DOM at all:
  1. Login via browser → get token + merchantId
  2. Use the proxy to find what URL the loadsheet page calls
     by navigating to it and watching intercepted URLs.
     If we still don't see it, we TRIGGER it via page.evaluate()
     by calling fetch() from inside the page with the token.
  3. The loadsheet page data API (from real browser DevTools) is:
       GET https://api.postex.pk/services/merchant/api/load-sheet-logs/{merchantId}
                                                          ^^^^^^^^^^^^ note: load-sheet-LOGS not load-sheet
     or possibly:
       GET /services/merchant/api/load-sheet/merchant/{merchantId}
  4. We call ALL plausible load-sheet list endpoints directly from Python
     until one returns 200 with data.
  5. Parse the response to find target-date loadsheets + their real IDs.
  6. Call the orders endpoint with the real ID.

  The key we were missing: the list endpoint path is NOT /load-sheet
  it is something different — we find it by watching the browser's
  actual network calls when on the load-sheet-logs page.
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
log = logging.getLogger("postex-v6")

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
# Date
# ─────────────────────────────────────────────────────────────────────────────

# TESTING MODE
# True  = always scrape 8 May 2026
# False = scrape yesterday's date automatically
TESTING_ON = True

if TESTING_ON:
    TARGET_DATE = datetime(2026, 5, 8)
else:
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

trace("Config", {"target": TARGET_LABEL, "output": str(OUTPUT_FILE)})


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
        pass

def matches_target_date(text):
    if not text:
        return False
    s = str(text).strip()
    if re.fullmatch(r"\d{13}", s):
        dt = datetime.fromtimestamp(int(s) / 1000)
        return (dt.year == TARGET_YEAR and
                dt.month == TARGET_DATE.month and
                dt.day == TARGET_DAY)
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return (int(m.group(1)) == TARGET_YEAR and
                int(m.group(2)) == TARGET_DATE.month and
                int(m.group(3)) == TARGET_DAY)
    m = re.search(r"(\w+)\s+(\d{1,2}),\s+(\d{4})", s)
    if m:
        month, day_s, year_s = m.groups()
        return (month == TARGET_MONTH and
                int(day_s) == TARGET_DAY and
                int(year_s) == TARGET_YEAR)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Step 1: Browser login + intercept to find the REAL loadsheet list URL
# ─────────────────────────────────────────────────────────────────────────────

def browser_login_and_discover():
    """
    Login, proxy all API calls, navigate to loadsheet page,
    capture every intercepted URL, return auth + all URLs seen.
    """
    token       = ""
    merchant_id = ""
    cookies_out = []
    all_urls    = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context()
        page    = context.new_page()
        page.on("console",   lambda m: log.debug(f"BROWSER[{m.type}] {m.text}"))
        page.on("pageerror", lambda e: log.debug(f"PAGE ERROR: {e}"))

        # ── Login ────────────────────────────────────────────────────────────
        trace("Login")
        page.goto(LOGIN_URL, wait_until="networkidle")
        screenshot(page, "01_login")
        page.fill('input[type="email"]',    USERNAME)
        page.fill('input[type="password"]', PASSWORD)
        page.click('button[type="submit"]')
        page.wait_for_url(f"{BASE_URL}/main/**", timeout=30_000)
        screenshot(page, "02_post_login")

        # ── Extract auth ─────────────────────────────────────────────────────
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

        # ── Build proxy session ───────────────────────────────────────────────
        proxy_session = _make_session(token, cookies_out)

        # ── Route handler: proxy + record ─────────────────────────────────────
        def handle_route(route, request):
            url    = request.url
            method = request.method
            all_urls.append(url)
            trace(f"INTERCEPT {method} {url}")
            try:
                resp = proxy_session.request(
                    method  = method,
                    url     = url,
                    headers = {
                        "Accept":          "application/json, text/plain, */*",
                        "Authorization":   f"Bearer {token}",
                        "Origin":          BASE_URL,
                        "Referer":         LOADSHEET_URL,
                    },
                    data    = request.post_data,
                    timeout = 30,
                )
                # Save response for inspection
                safe = re.sub(r"[^a-zA-Z0-9._-]", "_", url)[:100]
                (DEBUG_DIR / f"intercept_{safe}.json").write_text(
                    resp.text, encoding="utf-8"
                )
                trace(f"  -> {resp.status_code}", resp.text[:300])
                route.fulfill(
                    status  = resp.status_code,
                    headers = {"Content-Type": "application/json",
                               "Access-Control-Allow-Origin": "*"},
                    body    = resp.content,
                )
            except Exception:
                log.exception(f"Proxy error for {url}")
                route.fulfill(status=200,
                              headers={"Content-Type": "application/json"},
                              body=b"{}")

        page.route(f"**/{API_HOST}/**", handle_route)

        # ── Navigate to loadsheet page and wait ───────────────────────────────
        trace("Navigating to loadsheet page (with proxy active)")
        page.goto(LOADSHEET_URL, wait_until="networkidle")
        time.sleep(5)

        screenshot(page, "03_loadsheet")
        trace(f"Total intercepted URLs so far: {len(all_urls)}")

        # ── Manual trigger ───────────────────────────────────────────────────
        ls_trigger_result = page.evaluate(f"""
            async () => {{
                try {{
                    const token = sessionStorage.getItem('token');
                    const merchantId = sessionStorage.getItem('merchantId');
                    const url = `https://api.postex.pk/services/merchant/api/load-sheet-logs/${{merchantId}}`;
                    const r = await fetch(url, {{
                        headers: {{
                            'Authorization': `Bearer ${{token}}`,
                            'Accept': 'application/json'
                        }}
                    }});
                    const text = await r.text();
                    return {{ status: r.status, url: url, body: text.substring(0, 2000) }};
                }} catch(e) {{
                    return {{ error: String(e) }};
                }}
            }}
        """)
        trace("Manual fetch result", ls_trigger_result)

        browser.close()

    write_json(DEBUG_DIR / "all_intercepted_urls.json", all_urls)
    return token, merchant_id, cookies_out, all_urls


def _make_session(token, cookies):
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
