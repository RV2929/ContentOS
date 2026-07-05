#!/usr/bin/env python3
"""
TikTok video uploader via Playwright browser automation.
Session cookies are saved so login only happens once.

First-time setup (opens visible browser for you to log in manually):
  python tiktok_uploader.py --login

Upload a video:
  python tiktok_uploader.py /path/to/clip.mp4 "My caption #shorts #fyp"

Outputs a JSON line to stdout: {"ok": true} or {"ok": false, "error": "..."}
"""

import sys, os, json, time, argparse
from pathlib import Path

# ── Load .env from ContentOS root ─────────────────────────────────────────────
_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
try:
    from playwright_stealth import stealth_sync
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

# ── Paths ─────────────────────────────────────────────────────────────────────
DASHBOARD_DIR = Path(__file__).parent / "dashboard"
SESSION_FILE  = DASHBOARD_DIR / "tiktok-session.json"

UPLOAD_URL = "https://www.tiktok.com/tiktok-studio/upload"
LOGIN_URL  = "https://www.tiktok.com/login/phone-or-email/email"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Ordered fallback selectors — TikTok's DOM changes between releases
_CAPTION_SELS = [
    '[data-e2e="caption-input"]',
    '.DraftEditor-root div[contenteditable="true"]',
    'div[contenteditable="true"]',
    'div[role="textbox"]',
]
_POST_BTN_SELS = [
    '[data-e2e="upload-post-btn"]',
    'button:has-text("Post")',
    'div[class*="btn-post"]',
]
_FILE_INPUT_SELS = [
    'input[type="file"][accept*="video"]',
    'input[type="file"]',
]


def _save_cookies(context):
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(context.cookies(), indent=2))


def _load_cookies(context):
    if SESSION_FILE.exists():
        try:
            context.add_cookies(json.loads(SESSION_FILE.read_text()))
            return True
        except Exception:
            pass
    return False


def _is_logged_in(page) -> bool:
    """Navigate to tiktok.com and check for authenticated indicators."""
    try:
        page.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=20_000)
        time.sleep(2)
        # Logged-in: upload button, profile icon, or "For You" feed header present
        page.wait_for_selector(
            '[data-e2e="upload-icon"], [data-e2e="profile-icon"], '
            'a[href*="/upload"], [class*="avatar"]',
            timeout=6_000,
        )
        return True
    except Exception:
        return False


def _do_credential_login(page, username: str, password: str) -> bool:
    print("[tiktok] Attempting credential login…", flush=True)
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=20_000)
        time.sleep(2)

        # Some versions show a "Log in with email" link first
        with page.expect_navigation(timeout=5_000):
            pass
    except Exception:
        pass

    try:
        page.fill('input[name="username"], input[placeholder*="Email"]', username, timeout=8_000)
        time.sleep(0.5)
        page.fill('input[type="password"]', password, timeout=8_000)
        time.sleep(0.8)
        page.click(
            'button[data-e2e="login-button"], '
            'button[type="submit"]:has-text("Log in"), '
            'button:has-text("Log in")',
            timeout=8_000,
        )
    except Exception as exc:
        print(f"[tiktok] Could not fill login form: {exc}", flush=True)
        return False

    # Wait for redirect away from login
    try:
        page.wait_for_url(lambda u: "/login" not in u, timeout=30_000)
        print("[tiktok] Credential login succeeded", flush=True)
        return True
    except PWTimeout:
        print("[tiktok] Login timed out — likely CAPTCHA. Run: python tiktok_uploader.py --login", flush=True)
        return False


def _find_on_page_or_frame(page, selector: str):
    """Try selector on the main page, then each frame."""
    try:
        el = page.locator(selector).first
        el.wait_for(state="attached", timeout=3_000)
        return el
    except Exception:
        pass
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            el = frame.locator(selector).first
            el.wait_for(state="attached", timeout=3_000)
            return el
        except Exception:
            continue
    return None


# ── Public API ────────────────────────────────────────────────────────────────

def do_manual_login():
    """Open a visible browser so the user can log in, then save cookies."""
    print("\n  Opening browser — log in to TikTok, then come back here and press Enter.\n")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=50)
        ctx     = browser.new_context(viewport={"width": 1280, "height": 800}, user_agent=UA)
        page    = ctx.new_page()
        if HAS_STEALTH:
            stealth_sync(page)
        page.goto("https://www.tiktok.com/login", wait_until="domcontentloaded")
        input("  [Press Enter once you're logged in] ")
        _save_cookies(ctx)
        browser.close()
    print("  Session saved. Future uploads will reuse this session.\n")


def upload_video(video_path: str, caption: str, headless: bool = True) -> dict:
    """
    Upload video_path to TikTok with the given caption string.
    Returns {"ok": True} or {"ok": False, "error": "..."}.
    """
    vpath = Path(video_path)
    if not vpath.exists():
        return {"ok": False, "error": f"Video not found: {vpath}"}

    username = os.environ.get("TIKTOK_USERNAME", "")
    password = os.environ.get("TIKTOK_PASSWORD", "")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=UA,
        )
        page = ctx.new_page()
        if HAS_STEALTH:
            stealth_sync(page)

        # ── Session / login ───────────────────────────────────────────────
        _load_cookies(ctx)
        logged_in = _is_logged_in(page)

        if not logged_in:
            if username and password:
                if not _do_credential_login(page, username, password):
                    browser.close()
                    return {"ok": False, "error": "Login failed. Run: python tiktok_uploader.py --login"}
                _save_cookies(ctx)
            else:
                browser.close()
                return {
                    "ok": False,
                    "error": "No TikTok session found. Run: python tiktok_uploader.py --login",
                }

        # ── Navigate to upload page ───────────────────────────────────────
        print("[tiktok] Opening upload page…", flush=True)
        page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=30_000)
        time.sleep(4)

        # ── Set video file ────────────────────────────────────────────────
        print(f"[tiktok] Setting video file: {vpath.name}", flush=True)
        file_set = False

        # Strategy 1: direct file input
        for sel in _FILE_INPUT_SELS:
            el = _find_on_page_or_frame(page, sel)
            if el:
                try:
                    el.set_input_files(str(vpath))
                    file_set = True
                    print("[tiktok] File set via input element", flush=True)
                    break
                except Exception:
                    continue

        # Strategy 2: trigger file chooser via click on drag-drop zone
        if not file_set:
            try:
                with page.expect_file_chooser(timeout=12_000) as fc_info:
                    page.click(
                        '[class*="upload-btn"], [class*="drag"], '
                        '[class*="select-video"], [data-e2e="upload-btn"]',
                        timeout=8_000,
                    )
                fc_info.value.set_files(str(vpath))
                file_set = True
                print("[tiktok] File set via file chooser", flush=True)
            except Exception as exc:
                browser.close()
                return {"ok": False, "error": f"Could not attach video file: {exc}"}

        # ── Wait for upload & processing ──────────────────────────────────
        print("[tiktok] Waiting for video to process…", flush=True)
        # Poll until progress indicators disappear (max 3 min)
        deadline = time.time() + 180
        while time.time() < deadline:
            body = page.inner_text("body")
            uploading = (
                "uploading" in body.lower()
                or "processing" in body.lower()
                or "%" in body
            )
            if not uploading:
                break
            time.sleep(3)
        time.sleep(3)  # extra settle

        # ── Fill caption ──────────────────────────────────────────────────
        print("[tiktok] Setting caption…", flush=True)
        caption_set = False
        for sel in _CAPTION_SELS:
            el = _find_on_page_or_frame(page, sel)
            if el:
                try:
                    el.click(timeout=5_000)
                    time.sleep(0.4)
                    # Select all then overwrite
                    el.press("Meta+a")
                    time.sleep(0.2)
                    el.press("Backspace")
                    el.type(caption, delay=25)
                    caption_set = True
                    print("[tiktok] Caption set", flush=True)
                    break
                except Exception:
                    continue
        if not caption_set:
            print("[tiktok] ⚠ Could not set caption — continuing anyway", flush=True)

        time.sleep(1.5)

        # ── Click Post ────────────────────────────────────────────────────
        print("[tiktok] Clicking Post…", flush=True)
        post_clicked = False
        for sel in _POST_BTN_SELS:
            el = _find_on_page_or_frame(page, sel)
            if el:
                try:
                    el.click(timeout=8_000)
                    post_clicked = True
                    break
                except Exception:
                    continue

        if not post_clicked:
            browser.close()
            return {"ok": False, "error": "Could not find Post button. TikTok UI may have changed."}

        # ── Wait for success ──────────────────────────────────────────────
        success = False
        try:
            # Success typically redirects to manage/profile page
            page.wait_for_url(
                lambda u: "upload" not in u or "success" in u,
                timeout=25_000,
            )
            success = True
        except PWTimeout:
            # Check page text for success indicators
            body = page.inner_text("body")
            success = any(w in body.lower() for w in ["posted", "success", "live", "your video"])

        _save_cookies(ctx)  # refresh session after successful action
        browser.close()

        if success:
            print("[tiktok] Upload successful!", flush=True)
            return {"ok": True}
        else:
            return {"ok": False, "error": "Post clicked but success not confirmed — check TikTok manually"}


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Upload a video to TikTok")
    parser.add_argument("--login", action="store_true", help="Open browser to log in manually and save session")
    parser.add_argument("video", nargs="?", help="Path to MP4 file")
    parser.add_argument("caption", nargs="?", default="", help="Caption / title string")
    parser.add_argument("--visible", action="store_true", help="Show browser window (for debugging)")
    args = parser.parse_args()

    if args.login:
        do_manual_login()
        sys.exit(0)

    if not args.video:
        parser.print_help()
        sys.exit(1)

    result = upload_video(args.video, args.caption, headless=not args.visible)
    print(json.dumps(result))
    sys.exit(0 if result.get("ok") else 1)
