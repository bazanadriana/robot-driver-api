import os, sys, json, argparse
from typing import Optional, Tuple
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

load_dotenv()

# -----------------------
# ENV / CONFIG
# -----------------------
TARGET_URL = os.getenv("TARGET_URL", "https://www.amazon.com/")
PRODUCT_QUERY = os.getenv("PRODUCT_QUERY", "Apple AirPods Pro")
TIMEOUT_MS = int(os.getenv("TIMEOUT_MS", "45000"))
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"

AMAZON_EMAIL = os.getenv("AMAZON_EMAIL")
AMAZON_PASSWORD = os.getenv("AMAZON_PASSWORD")

# If you want to persist login between runs, set this to a file path
# e.g. AMAZON_STATE_PATH=amazon_state.json in .env
AMAZON_STATE_PATH = os.getenv("AMAZON_STATE_PATH", "").strip() or None

REAL_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# -----------------------
# BROWSER LIFECYCLE
# -----------------------

def make_browser_context(pw):
    """
    Launch a Chromium browser (Chrome channel), spoof some human-ish signals,
    and return browser, context, page so the caller can decide how long to keep it open.
    If AMAZON_STATE_PATH is provided and exists, reuse cookies/session.
    """
    browser = pw.chromium.launch(
        headless=HEADLESS,
        channel="chrome",
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-gpu",
        ],
    )

    # if we have stored session cookies, try to reuse
    storage_state = AMAZON_STATE_PATH if (AMAZON_STATE_PATH and os.path.exists(AMAZON_STATE_PATH)) else None

    context = browser.new_context(
        user_agent=REAL_UA,
        viewport={"width": 1366, "height": 900},
        locale="en-US",
        geolocation={"latitude": 37.7749, "longitude": -122.4194},
        permissions=["geolocation"],
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
        },
        storage_state=storage_state,
    )

    # Hide webdriver flag
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )

    page = context.new_page()
    return browser, context, page


def cleanup_browser(browser, context, persist_state: bool = False):
    """
    Safely close context/browser even if something threw.
    Optionally persist cookies/session to AMAZON_STATE_PATH.
    """
    if persist_state and AMAZON_STATE_PATH:
        try:
            context.storage_state(path=AMAZON_STATE_PATH)
            print(f"💾 Saved session to {AMAZON_STATE_PATH}")
        except Exception as e:
            print(f"⚠️ Could not save session state: {e}")

    try:
        context.close()
    except Exception:
        pass
    try:
        browser.close()
    except Exception:
        pass


# -----------------------
# HELPERS
# -----------------------

def click_if_visible(page, selector: str, timeout: int = 3000):
    try:
        el = page.locator(selector)
        if el.first.is_visible(timeout=timeout):
            el.first.click()
            return True
    except Exception:
        pass
    return False


def handle_consent_and_region(page):
    # Cookie / tracking consent acceptors
    for sel in [
        "#sp-cc-accept",
        "input#sp-cc-accept",
        "input[name='accept']",
        "button[name='accept']",
        "input#csa-c-consent-button",
    ]:
        if click_if_visible(page, sel):
            break

    # Region / ZIP dialogs
    for sel in [
        "button#GLUXConfirmClose",
        "button#a-autoid-0-announce",
        "div#GLUXZipUpdate input",
    ]:
        click_if_visible(page, sel)


def is_block_page(html: str) -> bool:
    text = html.lower()
    cues = [
        "sorry, we just need to make sure you're not a robot",
        "enter the characters you see below",
        "bot detection",
        "to discuss automated access",
        "type the characters",
        "amazon captcha",
    ]
    return any(cue in text for cue in cues)


def maybe_already_signed_in(page) -> bool:
    """
    Quick heuristic: look at the nav account link and see if it still says "Sign in".
    """
    try:
        nav_text = page.locator("#nav-link-accountList").inner_text(timeout=4000)
        # When logged in, that element usually contains account name, not "Sign in"
        if "Sign in" in nav_text or "Sign In" in nav_text:
            return False
        return True
    except Exception:
        # If we can't read nav text (layout changed / slow), assume not signed in.
        return False


def go_to_signin(page):
    """
    Open amazon.com, then click the real "Account & Lists" sign-in link.
    Fallback: direct /ap/signin if link isn't found.
    """
    page.goto("https://www.amazon.com/", wait_until="domcontentloaded")
    handle_consent_and_region(page)

    # try to click "Account & Lists" / sign-in link
    try:
        sign_in_link = page.locator("#nav-link-accountList, a[href*='/signin']")
        if sign_in_link.first.is_visible(timeout=5000):
            sign_in_link.first.click()
            page.wait_for_load_state("domcontentloaded")
            return
    except Exception:
        pass

    # fallback if click didn't work
    page.goto("https://www.amazon.com/ap/signin", wait_until="domcontentloaded")


def ensure_logged_in(page, interactive: bool):
    """
    Try to log in to Amazon if credentials are provided and we're not already logged in.

    Flow:
    - If we already look signed in, skip.
    - Otherwise:
        - Navigate to home
        - Click "Account & Lists" → Sign in
        - Fill email / continue
        - Fill password / submit
    - If CAPTCHA/MFA shows up, we optionally pause (interactive mode)
      so you can solve it manually in the real browser window.

    interactive=True means: pause and let human finish login,
    then press ENTER in terminal to continue.
    """

    # if no credentials at all, just skip
    if not AMAZON_EMAIL or not AMAZON_PASSWORD:
        print("ℹ️ No AMAZON_EMAIL / AMAZON_PASSWORD in env, skipping login.")
        return

    # First: hit homepage and see if nav already shows you're logged in
    page.goto("https://www.amazon.com/", wait_until="domcontentloaded")
    handle_consent_and_region(page)

    if maybe_already_signed_in(page):
        print("✅ Already signed in (detected from nav).")
        return

    print("🔐 Attempting Amazon sign-in...")

    # Go to sign-in screen via navbar
    go_to_signin(page)

    # Fill login form
    try:
        # Email step
        page.wait_for_selector("input#ap_email", timeout=8000)
        page.locator("input#ap_email").fill(AMAZON_EMAIL)
        page.locator("input#continue").click()

        # Password step
        page.wait_for_selector("input#ap_password", timeout=8000)
        page.locator("input#ap_password").fill(AMAZON_PASSWORD)
        page.locator("input#signInSubmit").click()

        # Give Amazon a moment to redirect
        page.wait_for_timeout(4000)

    except Exception as e:
        print(f"⚠️ Login form interaction threw: {e}")

    # Re-check if we're in now
    page.goto("https://www.amazon.com/", wait_until="domcontentloaded")
    handle_consent_and_region(page)

    if maybe_already_signed_in(page):
        print("✅ Login successful (post-submit check).")
        return

    # At this point Amazon might have shown CAPTCHA, OTP, or other challenge.
    print("❌ Not logged in yet. Likely CAPTCHA / MFA / OTP challenge.")

    if interactive and not HEADLESS:
        print(
            "\n🖐 Manual step required.\n"
            "Please solve any CAPTCHA / OTP in the browser window.\n"
            "Then press ENTER here to continue.\n"
        )
        try:
            input()
        except KeyboardInterrupt:
            pass

        # Check again after manual solve
        page.goto("https://www.amazon.com/", wait_until="domcontentloaded")
        handle_consent_and_region(page)

        if maybe_already_signed_in(page):
            print("✅ Login successful after manual intervention.")
        else:
            print("⚠️ Still not logged in. We'll continue anyway.")


def search(page, query: str):
    """
    Perform a product search for the given query.
    """
    search_url = f"https://www.amazon.com/s?k={query.replace(' ', '+')}"
    page.goto(search_url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
    handle_consent_and_region(page)

    try:
        page.wait_for_selector("div.s-main-slot", timeout=15000)
    except PWTimeoutError:
        html = page.content()
        with open("debug.html", "w", encoding="utf-8") as f:
            f.write(html)
        if is_block_page(html):
            raise RuntimeError("Blocked by Amazon (captcha / bot gate). Saved debug.html.")
        raise


def get_first_asin(page) -> Optional[str]:
    """
    Find the first plausible product ASIN from the search result grid.
    """
    containers = page.locator(
        'div.s-main-slot [data-component-type="s-search-result"][data-asin]'
    )
    count = containers.count()
    if count == 0:
        with open("debug.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        return None

    for i in range(min(12, count)):
        asin = containers.nth(i).get_attribute("data-asin")
        if asin and len(asin) >= 10:
            return asin
    return None


def open_product_by_asin(page, asin: str):
    """
    Open a product detail page by ASIN.
    """
    dp = f"https://www.amazon.com/dp/{asin}"
    page.goto(dp, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
    handle_consent_and_region(page)


def open_first_result(page):
    """
    Grab first ASIN from search results and navigate there.
    """
    asin = get_first_asin(page)
    if not asin:
        raise RuntimeError("Could not find any ASINs in results (see debug.html).")
    open_product_by_asin(page, asin)


def extract_price(page) -> Optional[str]:
    """
    Try multiple known price selectors and return the first visible one.
    """
    candidates = [
        "#corePriceDisplay_desktop_feature_div span.a-offscreen",
        "#apex_desktop span.a-price span.a-offscreen",
        "span#priceblock_ourprice",
        "span#priceblock_dealprice",
        "span.a-price.aok-align-center span.a-offscreen",
        "span.a-price span.a-offscreen",
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=2000):
                text = loc.inner_text().strip()
                if text:
                    return text
        except Exception:
            continue
    return None


def get_product_title(page) -> str:
    """
    Extract the product's title text from the detail page.
    Avoid grabbing hidden <input#productTitle>.
    """
    candidates = [
        "h1 span#productTitle",
        "#title span#productTitle",
        "span#productTitle",
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).filter(
                has_not=page.locator("input#productTitle")
            ).first
            if loc.is_visible(timeout=3000):
                return loc.inner_text().strip()
        except Exception:
            continue

    # fallback: first heading on page
    try:
        return page.get_by_role("heading").first.inner_text(timeout=3000).strip()
    except Exception:
        return ""


# -----------------------
# CORE RUN LOGIC
# -----------------------

def run(query: str, stay_open: bool = False, interactive_login: bool = True) -> Tuple[str, Optional[str], str]:
    """
    Does:
      - Launch browser
      - Try to sign in (if creds in .env), with an option to pause so you can solve CAPTCHA/MFA
      - Run product search
      - Open first result
      - Extract title/price/url
      - Optionally keep browser open until ENTER
    Returns (title, price, url_or_err)
    """

    with sync_playwright() as pw:
        browser, context, page = make_browser_context(pw)

        try:
            # 1. Attempt login (will no-op if no creds)
            ensure_logged_in(page, interactive=interactive_login)

            # 2. Perform search and open first result
            search(page, query)
            open_first_result(page)
            handle_consent_and_region(page)

            # 3. Scrape price + title
            price = extract_price(page)
            title = get_product_title(page)
            url = page.url

            # 4. Keep browser open for inspection if requested and we're not headless
            if stay_open and not HEADLESS:
                print("\n🔎 Browser is staying open. Press ENTER here to close it.\n")
                try:
                    input()
                except KeyboardInterrupt:
                    pass

            return title, price, url

        except Exception as e:
            return "", None, f"ERROR: {e.__class__.__name__}: {e}"

        finally:
            # save session cookies if we can
            cleanup_browser(browser, context, persist_state=True)


# -----------------------
# CLI
# -----------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", "-q", default=PRODUCT_QUERY)
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument(
        "--stay-open",
        action="store_true",
        help="Keep browser open until ENTER (only if not headless)",
    )
    parser.add_argument(
        "--no-interactive-login",
        action="store_true",
        help="Don't pause for manual CAPTCHA/MFA solve if login is challenged",
    )
    args = parser.parse_args()

    title, price, url_or_err = run(
        query=args.query,
        stay_open=args.stay_open,
        interactive_login=not args.no_interactive_login,
    )

    if args.json:
        if price:
            print(
                json.dumps(
                    {
                        "status": "SUCCESS",
                        "query": args.query,
                        "title": title,
                        "price": price,
                        "url": url_or_err,
                    }
                )
            )
        else:
            print(
                json.dumps(
                    {
                        "status": "FAILURE",
                        "query": args.query,
                        "reason": url_or_err or "Price not found",
                    }
                )
            )
    else:
        if price:
            print(f"✅ {title}\nPrice: {price}\nURL: {url_or_err}")
        else:
            print(f"❌ FAILURE: {url_or_err or 'Price not found'}")


if __name__ == "__main__":
    main()
