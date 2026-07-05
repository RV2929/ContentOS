#!/usr/bin/env python3
"""
TikTok video uploader via Playwright browser automation.
Session cookies are saved so login only happens once.

First-time setup (opens visible browser for you to log in manually):
  python tiktok_uploader.py --login

Upload a video:
  python tiktok_uploader.py /path/to/clip.mp4 "My caption #shorts #fyp"

Options:
  --login     Open browser for manual login and save session
  --visible   Show browser window while uploading (for debugging)
  --firefox   Force Firefox instead of Chromium

Outputs a JSON line to stdout: {"ok": true} or {"ok": false, "error": "..."}
"""

import sys, os, json, time, random, argparse
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

# Current Chrome UA on macOS — update periodically
UA_CHROME = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
UA_FIREFOX = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) "
    "Gecko/20100101 Firefox/133.0"
)

# Chromium launch args that suppress automation signals
CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-infobars",
    "--no-first-run",
    "--no-service-autorun",
    "--password-store=basic",
    "--use-mock-keychain",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-ipc-flooding-protection",
    "--window-size=1280,900",
    "--start-maximized",
]

# JS injected before every page load to mask automation properties
_STEALTH_INIT_SCRIPT = """
() => {
    // Hide navigator.webdriver
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // Inject realistic chrome object
    if (!window.chrome) {
        window.chrome = {
            app: { isInstalled: false, InstallState: {}, RunningState: {} },
            runtime: { PlatformOs: {}, PlatformArch: {}, PlatformNaclArch: {}, RequestUpdateCheckStatus: {} },
            loadTimes: function() {},
            csi: function() {},
        };
    }

    // Realistic plugin list (Chrome ships with PDF viewer)
    const pluginData = [
        { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: '' },
        { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: '' },
        { name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: '' },
        { name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer', description: '' },
    ];
    const plugins = Object.create(PluginArray.prototype);
    Object.defineProperty(plugins, 'length', { value: pluginData.length });
    pluginData.forEach((p, i) => {
        const plugin = Object.create(Plugin.prototype);
        Object.defineProperty(plugin, 'name',        { value: p.name });
        Object.defineProperty(plugin, 'filename',    { value: p.filename });
        Object.defineProperty(plugin, 'description', { value: p.description });
        Object.defineProperty(plugin, 'length',      { value: 0 });
        Object.defineProperty(plugins, i, { value: plugin });
    });
    Object.defineProperty(navigator, 'plugins', { get: () => plugins });

    // Realistic languages
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

    // Hardware concurrency (real Macs have 8-12 cores)
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });

    // Device memory
    Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

    // Permissions API — don't reveal automation
    const originalQuery = window.navigator.permissions?.query;
    if (originalQuery) {
        window.navigator.permissions.query = (parameters) =>
            parameters.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : originalQuery(parameters);
    }

    // Realistic screen dimensions
    Object.defineProperty(screen, 'width',       { get: () => 1440 });
    Object.defineProperty(screen, 'height',      { get: () => 900 });
    Object.defineProperty(screen, 'availWidth',  { get: () => 1440 });
    Object.defineProperty(screen, 'availHeight', { get: () => 877 });
    Object.defineProperty(screen, 'colorDepth',  { get: () => 24 });
    Object.defineProperty(screen, 'pixelDepth',  { get: () => 24 });
}
"""

# TikTok element selectors — ordered by reliability
_CAPTION_SELS = [
    '[data-e2e="caption-input"]',
    '.notranslate[contenteditable="true"]',
    '.DraftEditor-root div[contenteditable="true"]',
    'div[contenteditable="true"]',
    'div[role="textbox"]',
]
_POST_BTN_SELS = [
    '[data-e2e="upload-post-btn"]',
    'button:has-text("Post")',
    'div[class*="btn-post"]',
    'button[class*="post"]',
]
_FILE_INPUT_SELS = [
    'input[type="file"][accept*="video"]',
    'input[type="file"]',
]


# ── Human-like helpers ────────────────────────────────────────────────────────

def _pause(lo=0.4, hi=1.1):
    """Random sleep to mimic human reaction time."""
    time.sleep(random.uniform(lo, hi))


def _human_type(element, text: str):
    """Type with variable per-keystroke delay, with occasional micro-pauses."""
    for ch in text:
        element.type(ch, delay=random.randint(45, 160))
        if random.random() < 0.08:          # ~8% chance of a hesitation
            time.sleep(random.uniform(0.15, 0.55))


def _human_click(page, element):
    """Move mouse naturally to element, then click slightly off-centre."""
    try:
        box = element.bounding_box()
        if box:
            # Aim for a random point in the middle 60% of the element
            x = box["x"] + box["width"]  * random.uniform(0.25, 0.75)
            y = box["y"] + box["height"] * random.uniform(0.25, 0.75)
            # Drift the cursor in from a nearby point first
            page.mouse.move(x + random.randint(-30, 30), y + random.randint(-20, 20))
            _pause(0.05, 0.18)
            page.mouse.move(x, y)
            _pause(0.04, 0.12)
            page.mouse.click(x, y)
            return
    except Exception:
        pass
    element.click()


def _scroll_a_bit(page):
    """Scroll slightly — makes the page look like a human is reading."""
    page.mouse.wheel(0, random.randint(80, 250))
    _pause(0.3, 0.8)
    page.mouse.wheel(0, -random.randint(40, 120))
    _pause(0.2, 0.5)


# ── Browser setup ─────────────────────────────────────────────────────────────

def _make_context(p, browser_type_name: str, headless: bool, user_agent: str):
    """Launch a browser and return (browser, context, page)."""
    if browser_type_name == "firefox":
        browser = p.firefox.launch(
            headless=headless,
            firefox_user_prefs={
                "dom.webdriver.enabled": False,
                "useAutomationExtension": False,
                "privacy.trackingprotection.enabled": False,
            },
        )
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=UA_FIREFOX,
            locale="en-US",
            timezone_id="America/New_York",
            color_scheme="light",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
    else:  # chromium
        browser = p.chromium.launch(
            headless=headless,
            args=CHROMIUM_ARGS,
        )
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=UA_CHROME,
            locale="en-US",
            timezone_id="America/New_York",
            color_scheme="light",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"macOS"',
            },
        )

    # Inject stealth JS before every navigation
    ctx.add_init_script(_STEALTH_INIT_SCRIPT)

    page = ctx.new_page()

    # playwright_stealth on top for extra coverage
    if HAS_STEALTH:
        stealth_sync(page)

    return browser, ctx, page


# ── Session helpers ───────────────────────────────────────────────────────────

def _save_cookies(context):
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(context.cookies(), indent=2))


def _load_cookies(context) -> bool:
    if SESSION_FILE.exists():
        try:
            context.add_cookies(json.loads(SESSION_FILE.read_text()))
            return True
        except Exception:
            pass
    return False


def _is_logged_in(page) -> bool:
    try:
        page.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=20_000)
        _pause(2, 3.5)
        _scroll_a_bit(page)
        page.wait_for_selector(
            '[data-e2e="upload-icon"], [data-e2e="profile-icon"], '
            'a[href*="/upload"], [class*="avatar"], [class*="Avatar"]',
            timeout=7_000,
        )
        return True
    except Exception:
        return False


# ── Login ─────────────────────────────────────────────────────────────────────

def _do_credential_login(page, username: str, password: str) -> bool:
    print("[tiktok] Attempting credential login…", flush=True)
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=20_000)
        _pause(2.5, 4)
    except Exception:
        pass

    try:
        u_input = page.locator('input[name="username"], input[placeholder*="Email"], input[placeholder*="email"]').first
        u_input.wait_for(state="visible", timeout=8_000)
        _human_click(page, u_input)
        _pause(0.3, 0.7)
        _human_type(u_input, username)

        _pause(0.5, 1.2)

        p_input = page.locator('input[type="password"]').first
        p_input.wait_for(state="visible", timeout=8_000)
        _human_click(page, p_input)
        _pause(0.3, 0.6)
        _human_type(p_input, password)

        _pause(0.8, 1.5)

        submit = page.locator(
            'button[data-e2e="login-button"], '
            'button[type="submit"]:has-text("Log in"), '
            'button:has-text("Log in")'
        ).first
        _human_click(page, submit)
    except Exception as exc:
        print(f"[tiktok] Could not fill login form: {exc}", flush=True)
        return False

    try:
        page.wait_for_url(lambda u: "/login" not in u, timeout=35_000)
        _pause(1.5, 2.5)
        print("[tiktok] Credential login succeeded", flush=True)
        return True
    except PWTimeout:
        print("[tiktok] Login timed out — likely CAPTCHA. Run: python tiktok_uploader.py --login", flush=True)
        return False


# ── Element finder (searches main page then iframes) ──────────────────────────

def _find_element(page, selector: str, timeout=4_000):
    try:
        el = page.locator(selector).first
        el.wait_for(state="attached", timeout=timeout)
        return el
    except Exception:
        pass
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            el = frame.locator(selector).first
            el.wait_for(state="attached", timeout=timeout)
            return el
        except Exception:
            continue
    return None


# ── Public API ────────────────────────────────────────────────────────────────

def do_manual_login(use_firefox: bool = False):
    """Open a visible browser so the user can log in, then save cookies."""
    print("\n  Opening browser — log in to TikTok, then come back here and press Enter.\n", flush=True)
    with sync_playwright() as p:
        browser_name = "firefox" if use_firefox else "chromium"
        browser, ctx, page = _make_context(p, browser_name, headless=False, user_agent=UA_CHROME)
        page.goto("https://www.tiktok.com/login", wait_until="domcontentloaded")
        input("  [Press Enter once you're logged in] ")
        _save_cookies(ctx)
        browser.close()
    print("  Session saved to tiktok-session.json. Future uploads will reuse it.\n", flush=True)


def upload_video(video_path: str, caption: str, headless: bool = True, use_firefox: bool = False) -> dict:
    """
    Upload video_path to TikTok with the given caption.
    Tries Chromium first; if that is detected or blocked, falls back to Firefox.
    Returns {"ok": True} or {"ok": False, "error": "..."}.
    """
    vpath = Path(video_path)
    if not vpath.exists():
        return {"ok": False, "error": f"Video not found: {vpath}"}

    username = os.environ.get("TIKTOK_USERNAME", "")
    password = os.environ.get("TIKTOK_PASSWORD", "")

    # Determine browser order
    browser_order = ["firefox", "chromium"] if use_firefox else ["chromium", "firefox"]

    last_error = "Unknown error"

    for browser_name in browser_order:
        print(f"[tiktok] Trying {browser_name}…", flush=True)
        result = _attempt_upload(
            browser_name, headless, vpath, caption, username, password
        )
        if result.get("ok"):
            return result
        last_error = result.get("error", last_error)

        # Only fall back to the next browser on detection/login errors, not upload errors
        if "blocked" in last_error.lower() or "detected" in last_error.lower() or "captcha" in last_error.lower():
            print(f"[tiktok] {browser_name} appears blocked — trying next browser", flush=True)
            continue
        # For other errors (e.g. file not found, post button missing), don't retry
        break

    return {"ok": False, "error": last_error}


def _attempt_upload(browser_name: str, headless: bool, vpath: Path, caption: str,
                    username: str, password: str) -> dict:
    with sync_playwright() as p:
        try:
            browser, ctx, page = _make_context(p, browser_name, headless, UA_CHROME)
        except Exception as exc:
            return {"ok": False, "error": f"Could not launch {browser_name}: {exc}"}

        try:
            # ── Session / login ───────────────────────────────────────────
            _load_cookies(ctx)
            logged_in = _is_logged_in(page)

            if not logged_in:
                if username and password:
                    if not _do_credential_login(page, username, password):
                        return {"ok": False, "error": "Login failed — likely CAPTCHA. Run: python tiktok_uploader.py --login"}
                    _save_cookies(ctx)
                else:
                    return {"ok": False, "error": "No TikTok session. Run: python tiktok_uploader.py --login"}

            # ── Navigate to upload page ───────────────────────────────────
            print("[tiktok] Opening upload page…", flush=True)
            page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=30_000)
            _pause(3.5, 5.5)
            _scroll_a_bit(page)
            _pause(1, 2)

            # ── Attach video file ─────────────────────────────────────────
            print(f"[tiktok] Attaching {vpath.name}…", flush=True)
            file_set = False

            for sel in _FILE_INPUT_SELS:
                el = _find_element(page, sel)
                if el:
                    try:
                        el.set_input_files(str(vpath))
                        file_set = True
                        print("[tiktok] File attached via input element", flush=True)
                        break
                    except Exception:
                        continue

            if not file_set:
                try:
                    with page.expect_file_chooser(timeout=12_000) as fc_info:
                        drop_zone = _find_element(
                            page,
                            '[class*="upload-btn"], [class*="drag"], '
                            '[class*="select-video"], [data-e2e="upload-btn"]',
                        )
                        if drop_zone:
                            _human_click(page, drop_zone)
                        else:
                            page.click("body")
                    fc_info.value.set_files(str(vpath))
                    file_set = True
                    print("[tiktok] File attached via file chooser", flush=True)
                except Exception as exc:
                    return {"ok": False, "error": f"Could not attach video: {exc}"}

            # ── Wait for upload & processing ──────────────────────────────
            print("[tiktok] Waiting for video to process…", flush=True)
            deadline = time.time() + 210   # 3.5 min max
            last_pct = ""
            while time.time() < deadline:
                _pause(3, 4)
                try:
                    body = page.inner_text("body")
                except Exception:
                    break
                still_going = (
                    "uploading" in body.lower()
                    or "processing" in body.lower()
                    or ("%" in body and "100%" not in body)
                )
                # Print progress when it changes
                if "%" in body:
                    import re
                    m = re.search(r"(\d+)\s*%", body)
                    if m and m.group(1) != last_pct:
                        last_pct = m.group(1)
                        print(f"[tiktok] Processing… {last_pct}%", flush=True)
                if not still_going:
                    break
            _pause(2.5, 4)

            # ── Set caption ───────────────────────────────────────────────
            print("[tiktok] Setting caption…", flush=True)
            caption_set = False
            for sel in _CAPTION_SELS:
                el = _find_element(page, sel)
                if el:
                    try:
                        _human_click(page, el)
                        _pause(0.3, 0.7)
                        el.press("Meta+a")
                        _pause(0.15, 0.3)
                        el.press("Backspace")
                        _pause(0.2, 0.5)
                        _human_type(el, caption)
                        caption_set = True
                        print("[tiktok] Caption set", flush=True)
                        break
                    except Exception:
                        continue
            if not caption_set:
                print("[tiktok] ⚠ Could not set caption — posting anyway", flush=True)

            _pause(1.5, 2.5)

            # ── Click Post ────────────────────────────────────────────────
            print("[tiktok] Clicking Post…", flush=True)
            post_clicked = False
            for sel in _POST_BTN_SELS:
                el = _find_element(page, sel)
                if el:
                    try:
                        _human_click(page, el)
                        post_clicked = True
                        break
                    except Exception:
                        continue

            if not post_clicked:
                return {"ok": False, "error": "Could not find Post button — TikTok UI may have changed"}

            # ── Confirm success ───────────────────────────────────────────
            success = False
            try:
                page.wait_for_url(
                    lambda u: "upload" not in u or "success" in u,
                    timeout=30_000,
                )
                success = True
            except PWTimeout:
                try:
                    body = page.inner_text("body")
                    success = any(w in body.lower() for w in ["posted", "success", "live", "your video"])
                except Exception:
                    pass

            _save_cookies(ctx)
            if success:
                print("[tiktok] Upload successful!", flush=True)
                return {"ok": True}
            else:
                return {"ok": False, "error": "Post clicked but success not confirmed — check TikTok manually"}

        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            try:
                browser.close()
            except Exception:
                pass


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Upload a video to TikTok")
    parser.add_argument("--login",   action="store_true", help="Open browser for manual login and save session")
    parser.add_argument("--visible", action="store_true", help="Show browser window while uploading")
    parser.add_argument("--firefox", action="store_true", help="Use Firefox instead of Chromium")
    parser.add_argument("video",   nargs="?", help="Path to MP4 file")
    parser.add_argument("caption", nargs="?", default="", help="Caption / title string (include hashtags)")
    args = parser.parse_args()

    if args.login:
        do_manual_login(use_firefox=args.firefox)
        sys.exit(0)

    if not args.video:
        parser.print_help()
        sys.exit(1)

    result = upload_video(
        args.video,
        args.caption,
        headless=not args.visible,
        use_firefox=args.firefox,
    )
    print(json.dumps(result))
    sys.exit(0 if result.get("ok") else 1)
