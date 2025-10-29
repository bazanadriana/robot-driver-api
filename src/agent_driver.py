import json
from typing import Any, Dict, List, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError


########################
# 1. PLAYWRIGHT HELPERS
########################

def make_env(pw):
    browser = pw.chromium.launch(headless=False, channel="chrome")
    context = browser.new_context(
        viewport={"width": 1366, "height": 900},
        locale="en-US",
    )
    page = context.new_page()
    return browser, context, page


def read_page_state(page) -> Dict[str, Any]:
    """
    Snapshot of the current page that we'll feed to the planner / LLM.
    We try to keep only interactive or meaningful nodes:
    - buttons, links, inputs
    - headings with text
    - price spans
    - product title
    """

    snapshot = []

    locator_candidates = page.locator(
        "button, a, input, select, textarea, "
        "h1, h2, h3, "
        "span.a-price span.a-offscreen, "
        "#priceblock_ourprice, #priceblock_dealprice, "
        "#corePriceDisplay_desktop_feature_div span.a-offscreen, "
        "#productTitle"
    )

    elements = locator_candidates.all()[:200]

    for el in elements:
        # tag
        try:
            tag = el.evaluate("el => el.tagName")
        except Exception:
            tag = None

        # visible text
        try:
            text = el.inner_text().strip()
        except Exception:
            text = ""

        # role
        try:
            role = el.get_attribute("role")
        except Exception:
            role = None

        # placeholder
        try:
            placeholder = el.get_attribute("placeholder")
        except Exception:
            placeholder = None

        # aria-label
        try:
            aria_label = el.get_attribute("aria-label")
        except Exception:
            aria_label = None

        # aria-labelledby -> resolve
        if not aria_label:
            try:
                labelledby_id = el.get_attribute("aria-labelledby")
                if labelledby_id:
                    aria_label = el.evaluate(
                        """(node) => {
                            const id = node.getAttribute('aria-labelledby');
                            if (!id) return null;
                            const labelEl = document.getElementById(id);
                            if (!labelEl) return null;
                            return labelEl.innerText || labelEl.textContent || null;
                        }"""
                    )
            except Exception:
                pass

        # selector_guess
        try:
            selector_guess = el.evaluate(
                """(node) => {
                    if (node.id) {
                        return '#' + node.id;
                    }
                    if (
                        (node.tagName === 'INPUT' ||
                         node.tagName === 'SELECT' ||
                         node.tagName === 'TEXTAREA') &&
                        node.name
                    ) {
                        return node.tagName.toLowerCase() + '[name="' + node.name + '"]';
                    }
                    const classes = (node.className || '')
                        .toString()
                        .trim()
                        .split(/\\s+/)
                        .filter(Boolean)
                        .slice(0,3);
                    if (classes.length > 0) {
                        return node.tagName.toLowerCase() + '.' + classes.join('.');
                    }
                    return node.tagName.toLowerCase();
                }"""
            )
        except Exception:
            selector_guess = None

        # should we keep this node in the summary we send to the planner?
        keep = False
        if tag in ["BUTTON", "A", "INPUT", "SELECT", "TEXTAREA"]:
            keep = True
        if selector_guess and "productTitle" in selector_guess:
            keep = True
        if "price" in (selector_guess or "").lower() or "$" in text:
            keep = True
        if tag in ["H1", "H2", "H3"] and text:
            keep = True

        if not keep:
            continue

        snapshot.append({
            "tag": tag,
            "role": role,
            "text": text[:200],
            "placeholder": placeholder,
            "aria_label": aria_label,
            "selector_guess": selector_guess,
        })

    return {
        "url": page.url,
        "snapshot": snapshot[:60],
    }


def handle_bot_gate(page):
    """
    If Amazon shows the anti-bot interstitial ('Continue shopping'),
    click through it.
    """
    try:
        button = page.locator("text=Continue shopping").first
        if button.is_visible(timeout=2000):
            print("⚠️ Bot gate detected. Clicking 'Continue shopping'...")
            button.click()
            page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass


def find_first_product_link(page) -> Optional[str]:
    """
    Try to guess a clickable product link from the search results page.
    We'll attempt a few patterns, in order.
    """

    # Most common Amazon search result tile format
    loc = page.locator("div.s-main-slot h2 a[href*='/dp/']").first
    if loc.count() > 0:
        return "div.s-main-slot h2 a[href*='/dp/']"

    # Slightly looser: first heading link under results grid
    loc = page.locator("div.s-main-slot h2 a").first
    if loc.count() > 0:
        return "div.s-main-slot h2 a"

    # Total fallback: literally any link with /dp/ in href
    loc = page.locator("a[href*='/dp/']").first
    if loc.count() > 0:
        return "a[href*='/dp/']"

    return None


def extract_price_text(page) -> str:
    """
    Try multiple known Amazon price locations on a product page.
    We do NOT require them to be 'visible', we just grab the text.
    """

    candidate_selectors = [
        "#corePriceDisplay_desktop_feature_div span.a-offscreen",
        "#apex_desktop span.a-offscreen",
        "span#priceblock_ourprice",
        "span#priceblock_dealprice",
        "span.a-price span.a-offscreen",
    ]

    for sel in candidate_selectors:
        try:
            loc = page.locator(sel).first
            # Wait for it to exist in DOM (attached), even if it's hidden
            loc.wait_for(state="attached", timeout=5000)
            raw = loc.inner_text(timeout=2000).strip()
            if raw:
                return raw
        except Exception:
            continue

    return ""


########################
# 2. ACTION EXECUTOR
########################

def do_action(page, step: Dict[str, Any]) -> str:
    """
    Execute one step from the plan.
    Each step is a dict like:
      {"action": "fill", "selector": "#twotabsearchtextbox", "value": "airpods"}
      {"action": "click", "selector": "__FIRST_PRODUCT_LINK__"}
      {"action": "wait_for_selector", "selector": "#productTitle"}
      {"action": "extract_text", "selector": "__PRICE__"}
    """

    action = step.get("action")

    if action == "goto":
        page.goto(step["url"], wait_until="domcontentloaded")
        return "navigated"

    if action == "fill":
        sel = step["selector"]
        val = step["value"]
        page.wait_for_selector(sel, timeout=10000)
        page.locator(sel).fill(val)
        return f"filled:{sel}"

    if action == "click":
        sel = step["selector"]

        # Special virtual selector for "first product link"
        if sel == "__FIRST_PRODUCT_LINK__":
            # small scroll first to trigger lazy render
            try:
                page.mouse.wheel(0, 800)
            except Exception:
                pass

            dyn = find_first_product_link(page)
            if not dyn:
                raise RuntimeError("Could not locate any product link on results page.")
            sel = dyn

        page.wait_for_selector(sel, timeout=10000)
        loc = page.locator(sel).first
        try:
            loc.click(timeout=10000)
        except PWTimeoutError:
            # Scroll and retry (Amazon can delay clickable state)
            loc.scroll_into_view_if_needed(timeout=5000)
            loc.click(timeout=5000)
        return f"clicked:{sel}"

    if action == "wait_for_selector":
        sel = step["selector"]

        # Handle "__FIRST_PRODUCT_LINK__" before we wait
        if sel == "__FIRST_PRODUCT_LINK__":
            # Scroll to encourage lazy-loaded tiles
            try:
                page.mouse.wheel(0, 1000)
            except Exception:
                pass

            dyn = find_first_product_link(page)
            if dyn:
                sel = dyn

        print(f"[wait_for_selector] waiting for {sel!r}")
        try:
            page.wait_for_selector(sel, timeout=15000)
            return f"waited:{sel}"
        except PWTimeoutError:
            # Fallback for throttled/slow result rendering
            if "__FIRST_PRODUCT_LINK__" in step.get("selector", "") or "s-main-slot" in sel or "dp/" in sel:
                try:
                    page.mouse.wheel(0, 1600)
                except Exception:
                    pass

                dyn2 = find_first_product_link(page)
                if dyn2:
                    print(f"[wait_for_selector] retry with dynamic selector {dyn2!r}")
                    page.wait_for_selector(dyn2, timeout=5000)
                    return f"waited:fallback:{dyn2}"

            raise

    if action == "extract_text":
        sel = step["selector"]

        # Special virtual selector for product price
        if sel == "__PRICE__":
            price = extract_price_text(page)
            print(f"[extract_text] __PRICE__ -> {price}")
            return f"extracted:{price}"

        # "normal" selector path
        page.wait_for_selector(sel, timeout=10000)
        txt = page.locator(sel).first.inner_text(timeout=5000).strip()
        print(f"[extract_text] {sel} -> {txt}")
        return f"extracted:{txt}"

    if action == "final_answer":
        return f"FINAL:{step.get('result','')}"

    return f"unknown_action:{action}"


########################
# 3. RULE-BASED "PLANNER"
########################

def ask_llm(goal: str, page_state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Still rule-based (no external API call yet).
    It returns the next action steps for the executor.
    """

    url = page_state.get("url", "")
    snapshot = page_state.get("snapshot", [])

    def has_search_box():
        for node in snapshot:
            sel = (node.get("selector_guess") or "").lower()
            if "#twotabsearchtextbox" in sel or "twotabsearchtextbox" in sel:
                return True
        return False

    def sees_continue_shopping():
        for node in snapshot:
            text = (node.get("text") or "").strip().lower()
            if "continue shopping" in text:
                return True
        return False

    # 0. Interstitial gate page
    if sees_continue_shopping() and not has_search_box():
        return [
            {"action": "click", "selector": "text=Continue shopping"},
            {"action": "wait_for_selector", "selector": "#twotabsearchtextbox"},
        ]

    # 1. Homepage w/ search box
    if (
        "amazon.com" in url
        and "s?k=" not in url
        and "/dp/" not in url
        and has_search_box()
    ):
        return [
            {"action": "wait_for_selector", "selector": "#twotabsearchtextbox"},
            {"action": "fill", "selector": "#twotabsearchtextbox", "value": goal},
            {"action": "click", "selector": "#nav-search-submit-button"},
            # wait for any product link to appear (virtual selector)
            {"action": "wait_for_selector", "selector": "__FIRST_PRODUCT_LINK__"},
        ]

    # 2. Search results page (URL contains s?k=...)
    if "s?k=" in url:
        return [
            {"action": "wait_for_selector", "selector": "__FIRST_PRODUCT_LINK__"},
            {"action": "click", "selector": "__FIRST_PRODUCT_LINK__"},
            {"action": "wait_for_selector", "selector": "#productTitle"},
        ]

    # 3. Product page (/dp/ in URL)
    if "/dp/" in url:
        return [
            # wait until the product title is present (stable anchor for PDP load)
            {"action": "wait_for_selector", "selector": "#productTitle"},

            # pull price using our robust extractor
            {"action": "extract_text", "selector": "__PRICE__"},

            # pull product title normally
            {"action": "extract_text", "selector": "#productTitle"},

            {"action": "final_answer", "result": "Scraped price and title from product page"},
        ]

    # 4. Fallback: go back to homepage and try again
    return [
        {"action": "goto", "url": "https://www.amazon.com/"},
        {"action": "wait_for_selector", "selector": "#twotabsearchtextbox"},
    ]


########################
# 4. ORCHESTRATOR LOOP
########################

def agent_run(user_goal: str):
    """
    Browser-agent loop:
    - handle gate
    - observe
    - plan
    - act
    - repeat
    Until we get a final answer.
    """

    with sync_playwright() as pw:
        browser, context, page = make_env(pw)

        final_result = None

        try:
            page.goto("https://www.amazon.com/", wait_until="domcontentloaded")

            for _ in range(10):
                # try to punch through Amazon's anti-bot 'Continue shopping' wall
                handle_bot_gate(page)

                # read current DOM -> summarized state for the "planner"
                state = read_page_state(page)

                # get next-step plan
                steps = ask_llm(user_goal, state)
                if not steps:
                    print("Planner returned no steps, stopping.")
                    break

                # execute the steps one by one
                for step in steps:
                    # tiny trick: after clicking the search submit button,
                    # Amazon sometimes navigates slowly. give it a breath.
                    if step["action"] == "click" and step.get("selector") == "#nav-search-submit-button":
                        result = do_action(page, step)
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=8000)
                        except Exception:
                            pass
                    else:
                        result = do_action(page, step)

                    # capture final answer when planner says we're done
                    if isinstance(result, str) and result.startswith("FINAL:"):
                        final_result = result[len("FINAL:"):]
                        break

                if final_result is not None:
                    break

            return final_result or "No final answer produced."

        finally:
            # comment these out while debugging if you want
            # to keep the browser window open after run
            context.close()
            browser.close()


########################
# 5. MAIN
########################

if __name__ == "__main__":
    goal = "Apple AirPods Pro"
    answer = agent_run(goal)
    print("AGENT RESULT:", answer)
