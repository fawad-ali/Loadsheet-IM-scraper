# PostEx Loadsheet Scraper

Automated daily scraper that logs into the PostEx merchant portal,
extracts yesterday's loadsheet data, and saves it as JSON for your N8N
BI machine to consume.

---

## Repository Structure

```
postex-scraper/
├── scraper.py                          ← main scraper
├── requirements.txt
├── data/                               ← auto-created, JSON files go here
│   └── loadsheet_YYYY-MM-DD.json
└── .github/
    └── workflows/
        └── postex_scraper.yml          ← GitHub Actions schedule
```

---

## What It Collects

| Field | Description |
|---|---|
| `total_orders_handed_over` | Total orders across all yesterday's loadsheets |
| `total_picked` | Orders picked up by riders |
| `total_unpicked` | Orders riders did not pick |
| `total_reattempt` | Orders needing a reattempt |
| `total_returned` | Orders returned to you |
| `total_delivered` | Successfully delivered orders |
| `cheque_estimate.total_estimated_cod_pkr` | Estimated COD cheque amount this week |

---

## Setup (One-Time)

### 1. Add GitHub Secrets

Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**

| Secret Name | Value |
|---|---|
| `POSTEX_USERNAME` | Your PostEx login email |
| `POSTEX_PASSWORD` | Your PostEx password |
| `N8N_WEBHOOK_URL` | *(optional)* Your N8N webhook URL |

> ✅ Your credentials **never appear** in code or logs.

### 2. Enable GitHub Actions

Make sure Actions is enabled in your repo settings.
The workflow runs automatically every day at **6:00 AM PKT** (1:00 AM UTC).

### 3. Manual Test Run

Go to **Actions → PostEx Daily Loadsheet Scraper → Run workflow**.
You can optionally pass a specific date like `2026-05-08` to backfill.

---

## How N8N Reads the Data

### Option A – GitHub Raw URL (Recommended, Free)

After each run, the JSON is committed to the repo. N8N can fetch it directly:

```
https://raw.githubusercontent.com/YOUR_ORG/YOUR_REPO/main/data/loadsheet_YYYY-MM-DD.json
```

In N8N:
1. **HTTP Request node** → GET the above URL
2. Replace `YYYY-MM-DD` dynamically using N8N's `{{ $today.minus(1, 'days').toFormat('yyyy-MM-dd') }}`

Full dynamic URL expression in N8N:
```
https://raw.githubusercontent.com/YOUR_ORG/YOUR_REPO/main/data/loadsheet_{{ $today.minus(1, 'days').toFormat('yyyy-MM-dd') }}.json
```

### Option B – N8N Webhook (Push)

Set `N8N_WEBHOOK_URL` secret to your N8N workflow's webhook URL.
The scraper will POST data directly to N8N right after scraping.

N8N Workflow 2 structure:
```
[Webhook Trigger] → [Set / Transform node] → [Execute Workflow → BI Machine]
```

---

## Output JSON Structure

```json
{
  "scrape_date": "2026-05-09",
  "target_date": "May 9, 2026",
  "grand_summary": {
    "total_orders_handed_over": 45,
    "total_picked": 45,
    "total_unpicked": 0,
    "total_reattempt": 3,
    "total_returned": 1,
    "total_cancelled": 0,
    "total_delivered": 38
  },
  "cheque_estimate": {
    "total_estimated_cod_pkr": 142500.00,
    "note": "Sum of COD from all delivered orders across all yesterday's loadsheets."
  },
  "loadsheets": [
    {
      "loadsheet_number": "LDS-ES7BY484060",
      "loadsheet_id": "5381184",
      "total_orders": 28,
      "picked": 28,
      "unpicked": 0,
      "date": "May 9, 2026, 9:16:58 PM",
      "status": "COMPLETED",
      "cheque_estimate": {
        "delivered_orders": 22,
        "estimated_cod_pkr": 88000.00
      },
      "orders_by_status": {
        "booked":    [ { ...order objects... } ],
        "reattempt": [ { ...order objects... } ],
        "returned":  [ { ...order objects... } ],
        "delivered": [ { ...order objects... } ]
      }
    }
  ]
}
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Login fails | Check `POSTEX_USERNAME` / `POSTEX_PASSWORD` secrets |
| No loadsheets found | PostEx may have changed their date format – check `YESTERDAY_LABEL` in scraper.py |
| API returns 401 | JWT capture logic may need updating – PostEx may use a different localStorage key |
| Numeric ID not found | PostEx changed their DOM – update `extract_sheet_id()` in scraper.py |

---

## Architecture Diagram

```
GitHub Actions (daily 6 AM PKT)
         │
         ▼
   scraper.py (Playwright)
   ┌──────────────────────┐
   │ 1. Login PostEx      │
   │ 2. Find yesterday's  │
   │    loadsheets        │
   │ 3. Call PostEx API   │
   │    for each sheet    │
   │ 4. Save JSON to      │
   │    data/ folder      │
   └──────────────────────┘
         │                    (optional)
         ├──────────────────────────────► N8N Webhook (push)
         │
         ▼
   Git commit → main branch
         │
         ▼
   raw.githubusercontent.com/…/data/loadsheet_YYYY-MM-DD.json
         │
         ▼
   N8N HTTP Request Node (pull)
         │
         ▼
   N8N BI Machine Workflow
```
