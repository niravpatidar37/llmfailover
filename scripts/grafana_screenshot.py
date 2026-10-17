"""Headless screenshot of the provisioned Grafana dashboard.

Logs in as the local admin with the password from the gitignored .env (never printed), then captures
/d/llm-gateway. Requires the optional ``screenshot`` dependency group and ``playwright install chromium``.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from dotenv import dotenv_values
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:13001")
    ap.add_argument("--out", default=str(ROOT / "docs" / "grafana_dashboard.png"))
    ap.add_argument("--range", default="now-10m", help="Grafana 'from' value (e.g. now-10m or epoch ms)")
    ap.add_argument("--to", default="now", help="Grafana 'to' value")
    ap.add_argument("--wait", type=float, default=8.0)
    args = ap.parse_args()
    password = dotenv_values(ROOT / ".env").get("GRAFANA_ADMIN_PASSWORD") or ""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1680, "height": 1250})
        page.goto(f"{args.url}/login")
        page.fill("input[name='user']", "admin")
        page.fill("input[name='password']", password)
        page.click("button[type='submit']")
        page.wait_for_url(lambda u: "/login" not in u, timeout=20_000)
        page.goto(f"{args.url}/d/llm-gateway/self-healing-llm-gateway?orgId=1&from={args.range}&to={args.to}")
        time.sleep(args.wait)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=args.out, full_page=True)
        browser.close()
    print(f"saved {args.out}")


if __name__ == "__main__":
    main()
