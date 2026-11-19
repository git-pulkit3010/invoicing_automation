"""Drives the web UI in a real browser and captures the rendered result.

Usage:  .venv/bin/python tests/ui_check.py [--url http://127.0.0.1:5055]
"""

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
CHROME = "/home/vagrant/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5055")
    parser.add_argument("--name", default="HARISH GIRIRAJU")
    parser.add_argument("--month", default="10")
    parser.add_argument("--year", default="2026")
    parser.add_argument("--out", default=str(ROOT / "_ui_shot.png"))
    args = parser.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROME,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport={"width": 1300, "height": 1200})
        errors = []
        page.on("console", lambda m: errors.append(f"{m.type}: {m.text}")
                if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        page.goto(args.url, wait_until="networkidle")

        # 1 · pick the person
        page.select_option("#contractor", label=args.name)
        # 2 · pick the period
        page.select_option("#month", args.month)
        page.select_option("#year", args.year)

        compute = page.locator("#compute")
        print("compute button disabled before click:", compute.is_disabled())

        # 3 · compute
        compute.click()
        page.wait_for_selector("#result-body:not([hidden])", timeout=15000)
        page.wait_for_timeout(400)

        print("total hours      :", page.locator("#r-total").inner_text())
        print("days recorded    :", page.locator("#r-days").inner_text())
        print("overtime         :", page.locator("#r-ot").inner_text())
        print("benchmark        :", page.locator("#r-benchmark").inner_text())
        print("difference       :", page.locator("#r-diff").inner_text())
        print("compute disabled after click:", compute.is_disabled())
        print("placeholder hidden:", page.locator("#placeholder").is_hidden())
        print("rows in week table:", page.locator("#r-weeks tr").count())
        print("audit rows        :", page.locator("#r-audits tr").count())
        print("notice items      :", page.locator("#r-notice-list li").all_inner_texts())
        print("notice title      :", page.locator("#r-notice-title").inner_text())

        page.screenshot(path=args.out, full_page=True)
        print("screenshot ->", args.out)
        browser.close()

        if errors:
            print("\nBrowser console messages:")
            for e in errors:
                print(" -", e)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
