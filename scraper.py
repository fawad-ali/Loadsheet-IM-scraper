        trace("Route interceptor active — navigating to loadsheet page")

        # ── Step E: Navigate to loadsheet page ───────────────────────────────
        # Now Angular CAN load because our proxy fulfills all API calls
        page.goto(LOADSHEET_URL, wait_until="networkidle")
        
        # Ensure we're actually on the loadsheet page
        trace(f"Current URL after goto: {page.url}")
        
        # Wait for the URL to be correct
        if "/load-sheet-logs" not in page.url:
            trace("Not on loadsheet page yet, waiting and retrying")
            time.sleep(3)
            page.goto(LOADSHEET_URL, wait_until="networkidle")
            trace(f"Retried navigation, current URL: {page.url}")

        trace("Waiting for load sheet table to fully appear")
