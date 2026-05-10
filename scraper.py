# name=scraper.py
"""
PostEx Loadsheet Scraper - Full verbose debug edition

What this file does:
- Logs into merchant.postex.pk using Playwright
- Captures JWT + cookies
- Attempts several strategies to find loadsheet rows and numeric load-sheet IDs:
    1) DOM selector on the main page
    2) Regex parse of page.content()
    3) Inspect all frames' content
- For each candidate row it will:
    - Try to click the "orders" span and capture any requests/responses triggered
    - Log every request/response URL (so you can see the exact API URL called by the browser)
- If a numeric sheet_id is found, the script calls the API directly with requests.Session
  (session includes Authorization header + copied cookies)
- Saves final JSON to data/loadsheet_YYYY-MM-DD.json with a debug section
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

# ---------------------------
# Logging - VERY VERBOSE
# ---------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(funcName)s:%(lineno)d | %(message)s",
)
log = logging.getLogger("postex-scraper")

# ---------------------------
# Config
# ---------------------------
BASE_URL = "https://merchant.postex.pk"
LOGIN_URL = f"{BASE_URL}/login"
LOADSHEET_URL = f"{BASE_URL}/main/load-sheet-logs"
API_BASE = "https://api.postex.pk/services/merchant/api/load-sheet"

USERNAME = os.environ.get("POSTEX_USERNAME", "")
PASSWORD = os.environ.get("POSTEX_PASSWORD", "")
N8N_WEBHOOK = os.environ.get("N8N_WEBHOOK_URL", "")

OUTPUT_DIR = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)

# Target date default: yesterday
DATE_OVERRIDE = os.environ.get("DATE_OVERRIDE")
if DATE_OVERRIDE:
    TARGET_DATE = datetime.strptime(DATE_OVERRIDE, "%Y-%m-%d")
else:
    TARGET_DATE = datetime.now() - timedelta(days=1)

DATE_TAG = TARGET_DATE.strftime("%Y-%m-%d")
TARGET_MONTH = TARGET_DATE.strftime("%b")
TARGET_DAY = TARGET_DATE.day
TARGET_YEAR = TARGET_DATE.year
TARGET_LABEL = f"{TARGET_MONTH} {TARGET_DAY}, {TARGET_YEAR}"

OUTPUT_FILE = OUTPUT_DIR / f"loadsheet_{DATE_TAG}.json"

# ---------------------------
# Helpers
# ---------------------------
def matches_target_date(date_text: str) -> bool:
    if not date_text:
        return False
    date_text = date_text.strip()
    m = re.search(r"(\w+)\s+(\d{1,2}),\s+(\d{4})", date_text)
    if not m:
        log.debug(f"matches_target_date: cannot parse date_text='{date_text}'")
        return False
    month, day_s, year_s = m.group(1), m.group(2), m.group(3)
    try:
        day = int(day_s)
        year = int(year_s)
    except ValueError:
        return False
    ok = (month == TARGET_MONTH and day == TARGET_DAY and year == TARGET_YEAR)
    log.debug(f"matches_target_date: parsed {month} {day}, {year} -> ok={ok}")
    return ok

def write_result(result):
    OUTPUT_FILE.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    log.info(f"Wrote output to {OUTPUT_FILE}")

# ---------------------------
# Login & auth capture
# ---------------------------
def login(page):
    log.info("Login: navigating to login page")
    page.goto(LOGIN_URL, wait_until="networkidle")
    log.debug(f"Page title after open: {page.title()}")
    # fill email
    try:
        page.fill('input[type="email"]', USERNAME)
        log.debug("Filled input[type=email]")
    except Exception:
        # try alternatives
        for sel in ('input[name="email"]', 'input[placeholder*="mail" i]'):
            try:
                if page.locator(sel).count() > 0:
                    page.fill(sel, USERNAME)
                    log.debug(f"Filled {sel}")
                    break
            except Exception:
                continue
    # fill password
    page.fill('input[type="password"]', PASSWORD)
    log.debug("Filled password")
    page.click('button[type="submit"]')
    log.debug("Submitted login form, waiting for redirect to /main/**")
    page.wait_for_url(f"{BASE_URL}/main/**", timeout=30_000)
    log.info(f"Login complete - current URL: {page.url}")

def get_auth_session(page):
    log.info("Capturing JWT from localStorage/sessionStorage and copying cookies")
    token = page.evaluate("""() => {
        return localStorage.getItem('token')
            || localStorage.getItem('authToken')
            || localStorage.getItem('access_token')
            || localStorage.getItem('accessToken')
            || sessionStorage.getItem('token')
            || '';
    }""")
    if not token:
        # try to find any entry starting with 'eyJ'
        token = page.evaluate("""() => {
            for (let i=0;i<localStorage.length;i++){
               const k = localStorage.key(i);
               const v = localStorage.getItem(k);
               if (v && v.startsWith('eyJ')) return v;
            }
            return '';
        }""")
    if not token:
        log.error("No JWT token found in browser storage")
        # We continue (some APIs might still allow cookies) but warn strongly
    else:
        log.debug(f"Token found (first 60 chars): {token[:60]}...")

    session = requests.Session()
    if token:
        session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://merchant.postex.pk",
            "Referer": "https://merchant.postex.pk/",
            "User-Agent": "Mozilla/5.0 (Playwright) AppleWebKit/537.36 (KHTML, like Gecko)"
        })

    cookies = page.context.cookies()
    log.debug(f"Browser cookies count: {len(cookies)}")
    for c in cookies:
        session.cookies.set(c["name"], c["value"])
        log.debug(f"Copied cookie: {c['name']}={c['value'][:20]}...")

    return session

# ---------------------------
# Debugging: page / frames state
# ---------------------------
def dump_page_state(page):
    log.info("=== DUMP PAGE STATE ===")
    try:
        url = page.url
        title = page.title()
    except Exception as e:
        url = "<error reading page.url>"
        title = "<error reading title>"
        log.exception(e)
    log.info(f"page.url = {url}")
    log.info(f"page.title = {title}")
    content = page.content()
    log.info(f"page.content length = {len(content)}")
    # save a copy for offline inspection
    debug_path = OUTPUT_DIR / f"debug_page_{int(time.time())}.html"
    try:
        debug_path.write_text(content, encoding="utf-8")
        log.info(f"Wrote debug page HTML to {debug_path}")
    except Exception as e:
        log.warning(f"Failed to write debug HTML: {e}")

    # frames
    frames = page.frames
    log.info(f"Frames count: {len(frames)}")
    for i, f in enumerate(frames):
        try:
            furl = f.url
        except Exception:
            furl = "<unknown>"
        try:
            fcontent = f.content()
            fclen = len(fcontent)
        except Exception:
            fcontent = ""
            fclen = -1
        log.debug(f"Frame[{i}] url={furl} content_len={fclen}")

# ---------------------------
# Strategies to find loadsheet row + ID
# ---------------------------
def find_rows_dom(page):
    """
    Method A: use DOM selectors to find table rows and extract data.
    Returns list of dicts with loadsheet_number, approximate date_text, and dom element handles (or None).
    """

    results = []
    log.info("Method A: Trying DOM selectors on main frame")

    try:
        # try to find table rows (table id may be 'excel-table')
        # We attempt multiple selectors
        selectors = [
            "table#excel-table tbody tr.data-item",
            "table.data-table tbody tr.data-item",
            "tbody tr.data-item",
            "tr.data-item",
        ]
        rows = []
        used_sel = None
        for sel in selectors:
            try:
                found = page.query_selector_all(sel)
                log.debug(f"Selector '{sel}' found {len(found)} elements")
                if found and len(found) > 0:
                    rows = found
                    used_sel = sel
                    break
            except Exception as e:
                log.debug(f"Selector '{sel}' raised: {e}")
                continue

        if not rows:
            log.info("DOM method: no rows found with selectors")
            return results

        log.info(f"DOM method: using selector '{used_sel}' and found {len(rows)} rows")

        for idx, row in enumerate(rows):
            try:
                # try to read TD cells
                cells = row.query_selector_all("td")
                log.debug(f"Row {idx} -> {len(cells)} <td> cells found")
                # try to read cell texts defensively
                def text_of(el):
                    try:
                        return el.inner_text().strip()
                    except Exception:
                        return ""
                lds = text_of(cells[0]) if len(cells) > 0 else ""
                total = text_of(cells[1]) if len(cells) > 1 else ""
                date_text = text_of(cells[5]) if len(cells) > 5 else ""
                status = text_of(cells[6]) if len(cells) > 6 else ""
                # also attempt to find dropdown more-menu class inside this row
                ul = row.query_selector("ul[class*='more-menu-']")
                more_menu_class = ul.get_attribute("class") if ul else None
                m = None
                if more_menu_class:
                    m = re.search(r"more-menu-(\d+)", more_menu_class)
                # fallback: search block outerHTML for more-menu numbers
                if not m:
                    outer = row.inner_html()
                    m = re.search(r"more-menu-(\d+)", outer)
                sheet_id = m.group(1) if m else None

                entry = {
                    "source": "dom",
                    "row_index": idx,
                    "loadsheet_number": lds,
                    "total_orders": total,
                    "date_text": date_text,
                    "status": status,
                    "sheet_id": sheet_id,
                    "row_element": row,  # actual ElementHandle - caller must not serialize
                }
                log.debug(f"DOM candidate: {entry['loadsheet_number']} id={sheet_id} date='{date_text}'")
                results.append(entry)
            except Exception as e:
                log.exception(f"Error reading DOM row {idx}: {e}")
                continue

    except Exception as e:
        log.exception(f"DOM method encountered error: {e}")

    return results

def find_rows_html_parse(content):
    """
    Method B: parse page HTML string for <tr> blocks and more-menu-<id>
    """
    results = []
    log.info("Method B: parsing page HTML for <tr> blocks and more-menu-<id>")

    tr_blocks = re.findall(r"(<tr[^>]*>.*?</tr>)", content, flags=re.DOTALL | re.IGNORECASE)
    log.debug(f"HTML parse found {len(tr_blocks)} <tr> blocks")

    for i, block in enumerate(tr_blocks):
        # look for LDS-... patterns
        m_lds = re.search(r">(LDS[-A-Z0-9_]+)\s*<", block, flags=re.IGNORECASE)
        if not m_lds:
            m_lds = re.search(r"(LDS[-A-Z0-9_]+)", block, flags=re.IGNORECASE)
        if not m_lds:
            continue
        loadsheet_number = m_lds.group(1).strip()
        m_id = re.search(r"more-menu-(\d+)", block)
        sheet_id = m_id.group(1) if m_id else None
        m_date = re.search(r"(\w+\s+\d{1,2},\s+\d{4})", block)
        date_text = m_date.group(1).strip() if m_date else ""
        # total orders: find first numeric span after loadsheet
        m_total = re.search(r"<td[^>]*>\s*<span[^>]*class=[\"'][^\"']*smaller-text[^\"']*[\"'][^>]*>\s*(\d+)\s*</span>", block, flags=re.IGNORECASE)
        total = m_total.group(1) if m_total else ""
        # status
        m_status = re.search(r"dt-detail[^>]*>.*?<span[^>]*>(.*?)</span>", block, flags=re.IGNORECASE|re.DOTALL)
        status = (m_status.group(1).strip() if m_status and m_status.group(1) else "")
        entry = {
            "source": "html_parse",
            "block_index": i,
            "loadsheet_number": loadsheet_number,
            "sheet_id": sheet_id,
            "total_orders": total,
            "date_text": date_text,
            "status": status,
            "block_snippet": block[:300].replace("\n", " "),
        }
        log.debug(f"HTML candidate [{i}]: {loadsheet_number} id={sheet_id} date='{date_text}'")
        results.append(entry)
    return results

def find_rows_in_frames(page):
    """
    Method C: inspect each frame's content for rows
    """
    results = []
    log.info("Method C: scanning frames")
    frames = page.frames
    for idx, f in enumerate(frames):
        try:
            furl = f.url
            log.debug(f"Frame[{idx}] url={furl}")
            content = f.content()
        except Exception as e:
            log.debug(f"Frame[{idx}] content() failed: {e}")
            continue
        # reuse html parser
        r = find_rows_html_parse(content)
        # mark where each came from
        for e in r:
            e["frame_index"] = idx
            e["frame_url"] = furl
        results.extend(r)
    log.info(f"Method C: found {len(results)} candidates across frames")
    return results

# ---------------------------
# Click & capture network that happens when clicking the order count span
# ---------------------------
def capture_requests_while_click(page, element_handle_or_selector, timeout=6):
    """
    Attach request/response listeners, click element (selector string or ElementHandle),
    and wait for network activity for 'timeout' seconds. Returns a dict with captured requests/responses.
    """
    captured = {
        "requests": [],
        "responses": [],
        "matching_urls": [],
    }

    def on_request(request):
        try:
            info = {
                "url": request.url,
               

