import asyncio
import argparse
from urllib.parse import urlparse
import os
from playwright.async_api import async_playwright
from screeninfo import get_monitors

DEFAULT_ASPECT_RATIO = 502 / 1340

def compute_viewport(user_width=None, user_height=None):
    if user_width and user_height:
        return int(user_width), int(user_height)
    elif user_width:
        return int(user_width), int(user_width * DEFAULT_ASPECT_RATIO)
    else:
        monitor = get_monitors()[0]
        width = monitor.width
        height = int(width * DEFAULT_ASPECT_RATIO)
        return width, height

def extract_id_from_url(url):
    path = urlparse(url).path
    parts = [p for p in path.split("/") if p]
    return parts[-1] if parts else "capture"

async def capture_screenshot(url, dist, width, height, selector):
    viewport_width, viewport_height = compute_viewport(width, height)
    capture_id = extract_id_from_url(url)
    filename = f"screenshot_{capture_id}.png"
    filepath = os.path.join(dist, filename)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": viewport_width, "height": viewport_height})
        await page.goto(url, wait_until="networkidle")

        if selector:
            try:
                await page.wait_for_selector(selector, timeout=10000)
                element = await page.query_selector(selector)
                if element:
                    await element.screenshot(path=filepath)
                    print(f"Saved element screenshot to {filepath}")
                    return
                else:
                    print(f"Selector '{selector}' not found. Saving full page.")
            except Exception as e:
                print(f"Selector error: {e}. Saving full page.")

        await page.screenshot(path=filepath, full_page=True)
        print(f"✅ Saved full-page screenshot to {filepath}")
        await browser.close()

def main():
    parser = argparse.ArgumentParser(description="Capture screenshot from URL.")

    parser.add_argument('--url', required=True, help='URL to capture')
    parser.add_argument('--dist', default='.', help='Directory to save image (default: current dir)')
    parser.add_argument('--width', type=int, help='Viewport width')
    parser.add_argument('--height', type=int, help='Viewport height')
    parser.add_argument('--selector', help='CSS selector for element screenshot')

    args = parser.parse_args()

    os.makedirs(args.dist, exist_ok=True)

    asyncio.run(capture_screenshot(
        url=args.url,
        dist=args.dist,
        width=args.width,
        height=args.height,
        selector=args.selector
    ))

if __name__ == "__main__":
    main()


# Usage Examples:

# Default to current folder, auto-named:
#     python capture_qr.py --url http://localhost:5012/instruments/43/qr
# Output: ./screenshot_123.png

# Custom output folder:
#     python capture_qr.py --url http://localhost:5000/instruments/abc/qr --dist ./captures
# Output: ./captures/screenshot_abc.png

# Capture specific element:
#     python capture_qr.py --url http://localhost:5000/qr --selector "#tag" --dist ./output

# Custom viewport size:
#     python capture_qr.py --url http://localhost:5000/qr --selector "#tag" --dist ./output --width 1340 --height 502