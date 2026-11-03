"""Dev check: screenshot /@tiktok in both themes and verify proxied <video> playback.

Run while the app is up:  uv run python scripts_dev/shots_check.py [base_url]
"""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
OUT = Path(__file__).parent / "shots"

PLAY_JS = """async (sel) => {
  const v = document.querySelector(sel);
  if (!v) return {error: 'no video'};
  v.muted = true;
  v.preload = 'auto';
  try { await v.play(); } catch (e) { return {error: String(e)}; }
  const t0 = Date.now();
  while (v.readyState < 2 && Date.now() - t0 < 20000) await new Promise(r => setTimeout(r, 200));
  await new Promise(r => setTimeout(r, 1500));
  return {readyState: v.readyState, currentTime: v.currentTime, paused: v.paused,
          src: (v.currentSrc || '').slice(0, 60), error: v.error && v.error.code};
}"""


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    ok = True
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        for theme in ("default", "card"):
            ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
            await ctx.add_cookies([{"name": "theme", "value": theme, "url": BASE}])
            page = await ctx.new_page()
            await page.goto(f"{BASE}/@tiktok", wait_until="networkidle", timeout=60000)
            await page.screenshot(path=str(OUT / f"user_{theme}.png"))
            if theme == "default":
                res = await page.evaluate(PLAY_JS, "article video")
            else:
                await page.click(".clickable-img >> nth=0")
                await page.wait_for_selector("#modal.is-active", timeout=10000)
                res = await page.evaluate(PLAY_JS, "#video")
            await page.screenshot(path=str(OUT / f"user_{theme}_playing.png"))
            print(theme, res)
            ok = ok and res.get("readyState", 0) >= 2 and not res.get("error")
            await ctx.close()
        await browser.close()
    print("OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
