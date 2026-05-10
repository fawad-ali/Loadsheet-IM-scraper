"""
PostEx Loadsheet Scraper
========================
Logs into merchant.postex.pk, finds yesterday's loadsheet(s),
fetches every order from the loadsheet API, and writes the result
to  data/loadsheet_YYYY-MM-DD.json  so N8N (or anything else) can
pick it up whenever it needs it.

Environment variables (set as GitHub Secrets):
  POSTEX_USERNAME   – your merchant login e-mail / username
  POSTEX_PASSWORD   – your merchant password
  N8N_WEBHOOK_URL   – (optional) if set, data is also POSTed to N8N
"""

import os
import json
import time
import logging
import requests
from datetime import datetime, timedelta
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────
BASE_URL        = "https://merchant.postex.pk"
LOGIN_URL       = f"{BASE_URL}/login"
LOADSHEET_URL   = f"{BASE_URL}/main/load-sheet-logs"
API_BASE        = "https://api.postex.pk/services/merchant/api/load-sheet"

USERNAME        = os.environ["POSTEX_USERNAME"]
PASSWORD        = os.environ["POSTEX_PASSWORD"]
N8N_WEBHOOK     = os.environ.get("N8N_WEBHOOK_URL", "")   # optional

OUTPUT_DIR      = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)

# Yesterday's date in the same format PostEx uses: "May 9, 2026"
YESTERDAY       = datetime.now() - timedelta(days=1)
YESTERDAY_STR   = YESTERDAY.strftime("%-m/%-d/%Y")   # used for flexible matching
YESTERDAY_LABEL = YESTERDAY.strftime("%b %-d, %Y")   # e.g. "May 9, 2026"

# ── Helpers ────────────────────────────────────────────────────────────────

def flexible_date_match(cell_text: str) -> bool:
    """
    PostEx shows dates like 'May 9, 2026, 9:16:58 PM'.
    We just need to check the date part matches yesterday.
    """
    # e.g. YESTERDAY_LABEL = "May 9, 2026"
    return YESTERDAY_LABEL in cell_text


def login(page):
    """Login to PostEx merchant portal and wait until dashboard loads."""
    log.info("Navigating to login page …")
    page.goto(LOGIN_URL, wait_until="networkidle")

    # Fill credentials
    page.fill('input[type="email"], input[name="email"], input[placeholder*="mail" i]', USERNAME)
    page.fill('input[type="password"]', PASSWORD)

    log.info("Submitting login form …")
    page.click('button[type="submit"]')

    # Wait until we are past the login page
    page.wait_for_url(f"{BASE_URL}/main/**", timeout=30_000)
    log.info("Login successful ✓")


def get_auth_cookies(page) -> dict:
    """Extract cookies/token from the browser context for API calls."""
    cookies = {c["name"]: c["value"] for c in page.context.cookies()}
    return cookies


def get_auth_headers(page) -> dict:
    """
    PostEx SPA likely uses a JWT stored in localStorage.
    We grab it so we can make direct API calls without the browser.
    """
    token = page.evaluate("""() => {
        return localStorage.getItem('token')
            || localStorage.getItem('authToken')
            || localStorage.getItem('access_token')
            || sessionStorage.getItem('token')
            || '';
    }""")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        log.info("JWT token captured ✓")
    else:
        log.warning("JWT not found in storage – will rely on cookies only")
    return headers


def scrape_loadsheet_ids(page) -> list[dict]:
    """
    Open the loadsheet log page and return all rows whose date
    matches yesterday.  Returns list of dicts with keys:
      loadsheet_number, total_orders, picked, unpicked, rider, date, status
    (loadsheet_id is parsed from loadsheet_number for the API call)
    """
    log.info(f"Opening loadsheet logs page … (looking for {YESTERDAY_LABEL})")
page.goto(LOADSHEET_URL, wait_until="networkidle")

# Wait until Angular renders actual rows into the table
try:
    page.wait_for_selector("table#excel-table tbody tr", timeout=15_000)
except PWTimeout:
    log.warning("Table rows did not appear in 15s — page may still be loading")

time.sleep(2)  # small buffer after rows appear
rows = page.query_selector_all("table#excel-table tbody tr")
    log.info(f"Found {len(rows)} loadsheet rows total")

    yesterday_sheets = []
    for row in rows:
        cells = row.query_selector_all("td")
        if len(cells) < 7:
            continue

        date_text   = (cells[5].inner_text() or "").strip()
        if not flexible_date_match(date_text):
            continue

        lds_number  = (cells[0].inner_text() or "").strip()
        total       = (cells[1].inner_text() or "").strip()
        picked      = (cells[2].inner_text() or "").strip()
        unpicked    = (cells[3].inner_text() or "").strip()
        rider       = (cells[4].inner_text() or "").strip()
        status      = (cells[6].inner_text() or "").strip()

        # Extract numeric ID from the API URL embedded in the page
        # The page builds links like  /load-sheet/5381184/order
        # We can also derive it from the row's DOM data attributes or
        # by clicking "Print" and watching the network – here we look
        # for a data attribute first, then fall back to a network intercept.
        sheet_id = extract_sheet_id(page, row, lds_number)

        yesterday_sheets.append({
            "loadsheet_number": lds_number,
            "loadsheet_id":     sheet_id,
            "total_orders":     int(total)   if total.isdigit()   else total,
            "picked":           int(picked)  if picked.isdigit()  else picked,
            "unpicked":         int(unpicked)if unpicked.isdigit()else unpicked,
            "rider":            rider,
            "date":             date_text,
            "status":           status,
        })
        log.info(f"  Matched loadsheet: {lds_number}  (id={sheet_id})")

    log.info(f"Total loadsheets for yesterday: {len(yesterday_sheets)}")
    return yesterday_sheets


def extract_sheet_id(page, row, lds_number: str) -> str | None:
    """
    Try several strategies to get the numeric loadsheet ID
    that PostEx uses in its API URLs.
    Strategy 1 – data attribute on the row
    Strategy 2 – intercept the network request triggered by clicking Print
    Strategy 3 – regex the page HTML
    """
    import re

    # Strategy 1: data attribute
    for attr in ["data-id", "data-loadsheet-id", "data-sheet-id"]:
        val = row.get_attribute(attr)
        if val and val.isdigit():
            return val

    # Strategy 2: parse from any <a> href in the row
    links = row.query_selector_all("a[href]")
    for link in links:
        href = link.get_attribute("href") or ""
        m = re.search(r"/load-sheet/(\d+)", href)
        if m:
            return m.group(1)

    # Strategy 3: search the whole page source for the LDS number near a numeric ID
    html = page.content()
    # Look for something like  "LDS-ES7BY484060"  followed by a numeric id
    pattern = re.escape(lds_number.replace(" ", "")) + r'.*?(\d{5,10})'
    m = re.search(pattern, html, re.DOTALL)
    if m:
        return m.group(1)

    log.warning(f"Could not extract numeric ID for {lds_number} – will try clicking")

    # Strategy 4: click the action button and intercept the API call
    sheet_id_found = []
    def capture_request(req):
        m = re.search(r"/load-sheet/(\d+)/order", req.url)
        if m:
            sheet_id_found.append(m.group(1))

    page.on("request", capture_request)
    try:
        btn = row.query_selector("a.toggle-tigger")
        if btn:
            btn.click()
            time.sleep(1)
            # close dropdown
            page.keyboard.press("Escape")
    except Exception as e:
        log.warning(f"Click strategy failed: {e}")
    page.remove_listener("request", capture_request)

    return sheet_id_found[0] if sheet_id_found else None


def fetch_orders_for_sheet(sheet_id: str, headers: dict, cookies: dict,
                           status_option: str = "booked") -> list[dict]:
    """
    Call the PostEx API to get all orders for a given loadsheet.
    Handles pagination automatically.
    """
    all_orders = []
    page_num   = 1
    page_size  = 100   # grab large pages

    while True:
        url = (
            f"{API_BASE}/{sheet_id}/order"
            f"?loadSheetId={sheet_id}"
            f"&orderStatusOption={status_option}"
            f"&direction=desc"
            f"&page={page_num}"
            f"&limit={page_size}"
        )
        log.info(f"  Fetching orders page {page_num} from API …")
        try:
            resp = requests.get(url, headers=headers, cookies=cookies, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.error(f"  API call failed: {e}")
            break

        # PostEx wraps results – try common envelope keys
        orders = (
            data.get("data")
            or data.get("orders")
            or data.get("result")
            or (data if isinstance(data, list) else [])
        )
        if not orders:
            break

        all_orders.extend(orders)
        log.info(f"  Got {len(orders)} orders (running total: {len(all_orders)})")

        # Stop if we got a partial page (last page)
        if len(orders) < page_size:
            break
        page_num += 1

    return all_orders


def fetch_all_status_options(sheet_id: str, headers: dict, cookies: dict) -> dict:
    """
    Fetch orders by different status buckets so we capture:
    - booked   (handed over / normal)
    - reattempt / failed / returned  (issues needing reattempt)
    Returns a dict keyed by status.
    """
    statuses = ["booked", "reattempt", "returned", "cancelled", "delivered"]
    result   = {}
    for s in statuses:
        orders = fetch_orders_for_sheet(sheet_id, headers, cookies, status_option=s)
        if orders:
            result[s] = orders
            log.info(f"    [{s}] → {len(orders)} orders")
    return result


def calculate_cheque_summary(orders_by_status: dict) -> dict:
    """
    Estimate COD cheque amounts from delivered orders.
    PostEx typically sends cheques for delivered COD orders.
    """
    delivered = orders_by_status.get("delivered", [])
    total_cod  = 0
    for o in delivered:
        amount = (
            o.get("codAmount")
            or o.get("cod_amount")
            or o.get("amount")
            or o.get("orderAmount")
            or 0
        )
        try:
            total_cod += float(amount)
        except (TypeError, ValueError):
            pass

    return {
        "delivered_orders":  len(delivered),
        "estimated_cod_pkr": round(total_cod, 2),
        "note": "Cheque estimate based on delivered COD orders. Verify with PostEx statement.",
    }


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    date_tag    = YESTERDAY.strftime("%Y-%m-%d")
    output_file = OUTPUT_DIR / f"loadsheet_{date_tag}.json"

    log.info(f"=== PostEx Scraper | Target date: {YESTERDAY_LABEL} ===")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        # 1. Login
        login(page)

        # 2. Capture auth for direct API calls
        auth_headers = get_auth_headers(page)
        auth_cookies = get_auth_cookies(page)

        # 3. Scrape loadsheet list for yesterday
        loadsheets = scrape_loadsheet_ids(page)

        if not loadsheets:
            log.warning(f"No loadsheets found for {YESTERDAY_LABEL}. Exiting.")
            result = {
                "scrape_date":  date_tag,
                "target_date":  YESTERDAY_LABEL,
                "loadsheets":   [],
                "summary":      {},
                "cheque_estimate": {},
            }
            output_file.write_text(json.dumps(result, indent=2, ensure_ascii=False))
            return

        browser.close()  # done with browser – use requests for API calls

    # 4. Fetch orders for each loadsheet via the API
    full_data = []
    grand_summary = {
        "total_orders_handed_over": 0,
        "total_picked":             0,
        "total_unpicked":           0,
        "total_reattempt":          0,
        "total_returned":           0,
        "total_cancelled":          0,
        "total_delivered":          0,
    }

    for sheet in loadsheets:
        sheet_id = sheet.get("loadsheet_id")
        if not sheet_id:
            log.warning(f"Skipping {sheet['loadsheet_number']} – no numeric ID found")
            sheet["orders_by_status"] = {}
            full_data.append(sheet)
            continue

        log.info(f"Processing {sheet['loadsheet_number']} (id={sheet_id}) …")
        orders_by_status = fetch_all_status_options(sheet_id, auth_headers, auth_cookies)
        sheet["orders_by_status"] = orders_by_status

        # Accumulate summary
        grand_summary["total_orders_handed_over"] += sheet.get("total_orders", 0)
        grand_summary["total_picked"]             += sheet.get("picked", 0)
        grand_summary["total_unpicked"]           += sheet.get("unpicked", 0)
        grand_summary["total_reattempt"]          += len(orders_by_status.get("reattempt", []))
        grand_summary["total_returned"]           += len(orders_by_status.get("returned", []))
        grand_summary["total_cancelled"]          += len(orders_by_status.get("cancelled", []))
        grand_summary["total_delivered"]          += len(orders_by_status.get("delivered", []))

        sheet["cheque_estimate"] = calculate_cheque_summary(orders_by_status)
        full_data.append(sheet)

    # Combined cheque estimate across all sheets
    total_cod = sum(
        s.get("cheque_estimate", {}).get("estimated_cod_pkr", 0)
        for s in full_data
    )

    result = {
        "scrape_date":      date_tag,
        "target_date":      YESTERDAY_LABEL,
        "loadsheets":       full_data,
        "grand_summary":    grand_summary,
        "cheque_estimate":  {
            "total_estimated_cod_pkr": round(total_cod, 2),
            "note": "Sum of COD from all delivered orders across all yesterday's loadsheets.",
        },
    }

    # 5. Save to file
    output_file.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    log.info(f"✓ Data saved → {output_file}")

    # 6. Optionally push to N8N webhook
    if N8N_WEBHOOK:
        try:
            r = requests.post(N8N_WEBHOOK, json=result, timeout=30)
            r.raise_for_status()
            log.info(f"✓ Data pushed to N8N webhook (status {r.status_code})")
        except Exception as e:
            log.error(f"N8N webhook push failed: {e}")

    # 7. Print quick summary to GitHub Actions log
    log.info("=== SUMMARY ===")
    for k, v in grand_summary.items():
        log.info(f"  {k}: {v}")
    log.info(f"  Estimated COD cheque: PKR {total_cod:,.2f}")


if __name__ == "__main__":
    main()
