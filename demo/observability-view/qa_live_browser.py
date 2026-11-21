"""Optional independent real-browser acceptance for public ProjectFlow LIVE SSE.

Run with a local Playwright installation and Chromium browser; no API secrets,
login state or project mutation. Does not belong to the required pytest gate.
"""
from pathlib import Path
from playwright.sync_api import sync_playwright

URL = "https://hungtranbkit.github.io/projectflow-workspace-manager/"


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        for width in (390, 320):
            page = browser.new_page(viewport={"width": width, "height": 844})
            problems = []
            page.on("pageerror", lambda e: problems.append(str(e)[:160]))
            response = page.goto(URL, wait_until="domcontentloaded", timeout=25000)
            assert response and response.status == 200
            page.wait_for_function(
                "() => document.getElementById('live-transport')?.textContent.includes('SSE')",
                timeout=20000)
            health = page.locator("#live-health").inner_text()
            transport = page.locator("#live-transport").inner_text()
            nodes = page.locator("#live-nodes").inner_text()
            opened = page.locator("#live-open").inner_text()
            ttl = page.locator("#live-ttl").inner_text()
            acceptance = page.locator("#live-acceptance").inner_text()
            sessions = page.locator("#live-sessions li").count()
            assert "LIVE" in health and "SSE" in transport
            assert "/" in nodes and opened.isdigit()
            assert ttl == "UNKNOWN" and acceptance == "UNKNOWN"
            assert sessions > 0, "anonymous session detail missing"
            assert not problems, problems
            assert not page.evaluate("() => document.documentElement.scrollWidth > innerWidth")
            at = page.locator("#live-time").inner_text()
            page.wait_for_function(
                "(old) => document.getElementById('live-time')?.textContent !== old",
                arg=at, timeout=12000)
            page.screenshot(path="/tmp/projectflow-live-sse-browser-%d.png" % width, full_page=True)
            print("BROWSER_PASS",width,"NODES",nodes,"OPEN",opened,"SESSIONS",sessions,
                  "STREAM",transport,"TTL",ttl)
            page.close()
        browser.close()


if __name__ == "__main__":
    main()
