from typing import Awaitable, Callable

from playwright.async_api import async_playwright
import yaml

with open("config.yaml") as f:
    _CFG = yaml.safe_load(f)


ACTION_TIMEOUT_MS = int(_CFG.get("action_timeout_ms", 60000))
LOAD_TIMEOUT_MS = int(_CFG.get("load_timeout_ms", 60000))
DEBUG = bool(_CFG.get("debug", False))


def _dbg(msg: str) -> None:
    """Print msg only when debug mode is enabled."""
    if DEBUG:
        print(f"[replay:debug] {msg}")


def step_label(step: dict) -> str:
    a = step["action"]
    if a == "navigate":
        return f"Open page: {step.get('url', '')}"
    if a == "click":
        return f"Click: {step.get('text') or step.get('selector')}"
    if a == "input":
        return f"Type into: {step.get('selector')}"
    if a == "input_text":
        return f"Type into: {step.get('selector')}"
    if a == "manual_input":
        suffix = " + Enter" if step.get("press_enter", False) else ""
        return f"Enter '{step.get('value', '')}' into: {step.get('selector')}{suffix}"
    if a == "wait":
        seconds = step.get("seconds")
        if seconds is None and step.get("ms") is not None:
            seconds = step.get("ms", 0) / 1000
        return f"Wait: {seconds or 0}s"
    if a == "wait_for_selector":
        return f"Wait for element: {step.get('selector')}"
    if a == "wait_for_text":
        return f"Wait for text: {step.get('text')}"
    if a == "wait_and_click_last":
        return f"Click last: {step.get('selector')}"
    if a == "type_into":
        suffix = " + Enter" if step.get("press_enter", True) else ""
        return f"Type '{step.get('param_name', step.get('value', ''))}' into: {step.get('selector')}{suffix}"
    if a == "parameter_input":
        suffix = " + Enter" if step.get("press_enter", False) else ""
        return f"Enter '{step.get('param_name', '')}' into: {step.get('selector')}{suffix}"
    return a


async def _execute(page, step, params):
    import asyncio

    def _timeout_ms(step: dict, default_ms: int) -> int:
        if step.get("timeout_s") is not None:
            return int(float(step.get("timeout_s")) * 1000)
        if step.get("timeout_ms") is not None:
            return int(step.get("timeout_ms"))
        return default_ms

    a = step["action"]
    if a == "navigate":
        await page.goto(step["url"], timeout=LOAD_TIMEOUT_MS)
        return  # page.goto() already waits for the load event; skip the networkidle wait
    elif a == "click":
        await page.click(step["selector"], timeout=ACTION_TIMEOUT_MS)
        return  # simple click — caller adds explicit waits if navigation follows
    elif a == "input":
        value = params.get(step.get("param_name"), step.get("value", ""))
        await page.fill(step["selector"], value, timeout=ACTION_TIMEOUT_MS)
        return
    elif a == "input_text":
        template = step.get("template")
        if template and params:
            try:
                value = template.format(**params)
            except KeyError:
                value = params.get(step.get("param_name"), step.get("value", ""))
        else:
            value = params.get(step.get("param_name"), step.get("value", ""))
        # Click to focus, then type via keyboard to fire real keystroke events.
        # page.keyboard.type() targets the focused element, which avoids selector
        # mismatches caused by SFDC Lightning shadow-DOM layering.
        await page.click(step["selector"], timeout=ACTION_TIMEOUT_MS)
        await page.keyboard.type(value)
        await page.keyboard.press('Enter')
        return
    elif a == "manual_input":
        # Fixed value typed into a field — always uses the hardcoded step value.
        value = step.get("value", "")
        await page.click(step["selector"], timeout=ACTION_TIMEOUT_MS)
        await page.keyboard.type(value)
        if step.get("press_enter", False):
            await page.keyboard.press('Enter')
        return
    elif a == "parameter_input":
        # Value collected upfront before skill runs; typed silently during execution.
        selector = step.get("selector")
        param_name = step.get("param_name", "")
        template = step.get("template")
        if template and params:
            try:
                value = template.format(**params)
            except KeyError:
                value = params.get(param_name, step.get("value", ""))
        else:
            value = params.get(param_name, step.get("value", ""))
        _dbg(f"[parameter_input] {param_name!r} = {value!r}")
        el = await page.wait_for_selector(selector, state="visible", timeout=ACTION_TIMEOUT_MS)
        await el.click()
        await page.keyboard.type(value)
        if step.get("press_enter", False):
            await page.keyboard.press("Enter")
        return
    elif a == "wait":
        # Fixed pause in seconds — useful after triggering async UI actions.
        seconds = step.get("seconds")
        if seconds is None:
            # Backward compatibility for older skills.
            seconds = float(step.get("ms", 1000)) / 1000
        await asyncio.sleep(float(seconds))
        return  # Skip the networkidle wait below — this is an intentional pause
    elif a == "wait_for_selector":
        # Wait until an element appears in the DOM / becomes visible.
        # state: "attached" (in DOM), "visible" (default), "hidden", "detached"
        timeout = _timeout_ms(step, ACTION_TIMEOUT_MS)
        state = step.get("state", "visible")
        await page.wait_for_selector(step["selector"], timeout=timeout, state=state)
        return  # Element appeared — no need for load state check
    elif a == "wait_for_text":
        # Wait until a given text string appears anywhere on the page.
        text = step.get("text", "")
        timeout = _timeout_ms(step, ACTION_TIMEOUT_MS)
        await page.wait_for_function(
            f"() => document.body.innerText.includes({repr(text)})",
            timeout=timeout,
        )
        return
    elif a == "wait_and_click_last":
        # Wait for elements matching the selector, then click the last one.
        selector = step["selector"]
        settle = float(step.get("settle_seconds", 0))
        await page.wait_for_selector(selector, timeout=ACTION_TIMEOUT_MS)
        elements = await page.query_selector_all(selector)
        if elements:
            last_el = elements[-1]
            if step.get("scroll_into_view"):
                scroll_block = step.get("scroll_block", "center")
                await last_el.evaluate(
                    f"el => el.scrollIntoView({{block: '{scroll_block}'}})"
                )
            await last_el.click()
        if settle > 0:
            await asyncio.sleep(settle)
        return  # Skip networkidle wait after a targeted click

    elif a == "type_into":
        # Focus the element without a click (avoids triggering dropdowns/navigation),
        # then type character-by-character via the keyboard API and optionally press Enter.
        # This is the most compatible approach for SFDC Lightning and similar SPAs.
        selector = step["selector"]
        template = step.get("template")
        if template and params:
            try:
                value = template.format(**params)
            except KeyError:
                value = params.get(step.get("param_name"), step.get("value", ""))
        else:
            value = params.get(step.get("param_name"), step.get("value", ""))

        _dbg(f"[type_into] selector: {selector!r}")
        _dbg(f"[type_into] value   : {value!r}")

        # Check whether the target lives inside an iframe — Okta/SSO pages often do.
        frame = page
        frames = page.frames
        if len(frames) > 1:
            _dbg(f"[type_into] {len(frames)} frames detected, scanning for selector in sub-frames...")
            for f in frames[1:]:
                try:
                    el = await f.query_selector(selector)
                    if el:
                        _dbg(f"[type_into] found element in frame: {f.url!r}")
                        frame = f
                        break
                except Exception as fe:
                    _dbg(f"[type_into] frame scan error: {fe}")

        el = await frame.wait_for_selector(selector, state="visible", timeout=ACTION_TIMEOUT_MS)
        _dbg(f"[type_into] element found: tag={await el.evaluate('e => e.tagName')}, "
             f"name={await el.evaluate('e => e.name || e.id || \"\"')}")

        await frame.focus(selector)
        _dbg(f"[type_into] focused — typing now...")
        await frame.evaluate(f"document.querySelector({selector!r}).value = ''")
        await frame.locator(selector).press_sequentially(value)
        _dbg(f"[type_into] typed ok")
        if step.get("press_enter", True):
            await frame.locator(selector).press("Return")
            _dbg(f"[type_into] pressed Return")
        return  # Skip networkidle wait — caller controls what to wait for next


async def run_skill(
    skill: dict,
    params: dict,
    ask_user: Callable[[dict], Awaitable[dict]],
) -> dict:
    """Replay a skill, pausing only on parameterized steps.
    
    Returns a dict with:
      - 'outputs': {name: value} for each declared skill output
      - 'page_text': raw visible text of the final page for LLM extraction
      - 'page_url': final URL
    """
    result = {"outputs": {}, "page_text": "", "page_url": ""}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, channel="chrome")
        ctx = await browser.new_context(storage_state=_CFG["auth_state"])
        page = await ctx.new_page()
        page.set_default_timeout(ACTION_TIMEOUT_MS)
        page.set_default_navigation_timeout(LOAD_TIMEOUT_MS)

        # Auto-dismiss cookie consent / GDPR overlays on every page load.
        # The MutationObserver watches for consent banners as they appear in the DOM
        # and immediately clicks the reject/decline button.
        await page.add_init_script("""
            const _cookieSelectors = [
                '#onetrust-reject-all-handler',
                'button[id*="reject-all"]',
                'button[id*="declineAll"]',
                'button[id*="decline-all"]',
                'button[title="Reject All"]',
                'button[aria-label*="Reject all"]',
                'button[aria-label*="Decline all"]',
                '[data-id="banner-reject-button"]',
                'button.optanon-deny-accept-group',
            ];
            function _tryDismissCookies() {
                for (const sel of _cookieSelectors) {
                    const el = document.querySelector(sel);
                    if (el && el.offsetParent !== null) { el.click(); return; }
                }
            }
            const _obs = new MutationObserver(_tryDismissCookies);
            const _start = () => _obs.observe(document.body, { childList: true, subtree: true });
            document.readyState === 'loading'
                ? document.addEventListener('DOMContentLoaded', _start)
                : _start();
        """)

        stopped = False
        total_steps = len(skill["steps"])
        skill_name = skill.get("name", "<unnamed>")
        _dbg(f"Starting skill '{skill_name}' — {total_steps} step(s)")

        for i, step in enumerate(skill["steps"]):

            _dbg(f"Step {i + 1}/{total_steps}: action={step['action']!r}  label={step_label(step)!r}")
            if DEBUG:
                detail_fields = {k: v for k, v in step.items()
                                 if k not in ("action", "_step_index")}
                if detail_fields:
                    _dbg(f"  details: {detail_fields}")

            try:
                await _execute(page, step, params)
                _dbg(f"Step {i + 1}/{total_steps}: done")
            except Exception as e:
                retry = await ask_user({
                    "index": i,
                    "label": step_label(step),
                    "error": str(e),
                    "needs_attention": True,
                })
                if retry["action"] == "stop":
                    stopped = True
                    break

        if not stopped:
            # Capture declared outputs
            for out in skill.get("outputs", []):
                name = out.get("name")
                if not name:
                    continue
                out_type = out.get("type", "selector")
                try:
                    if out_type == "clipboard":
                        # Grant clipboard read permission and read it
                        await ctx.grant_permissions(["clipboard-read"])
                        value = await page.evaluate("async () => await navigator.clipboard.readText()")
                        result["outputs"][name] = (value or "").strip()
                    else:
                        # Default: DOM selector
                        selector = out.get("selector")
                        if selector:
                            el = await page.wait_for_selector(selector, timeout=5000)
                            value = (await el.inner_text()).strip() if el else ""
                            result["outputs"][name] = value
                except Exception:
                    result["outputs"][name] = ""

            # Capture full visible page text (trimmed to 4000 chars for LLM)
            try:
                result["page_text"] = (await page.inner_text("body"))[:4000]
            except Exception:
                result["page_text"] = ""

            result["page_url"] = page.url

        await browser.close()

    return result

if __name__ == "__main__":
    
    import asyncio
    from pprint import pprint
    from skills import delete_skill, list_skills, load_skill, skill_inputs, skill_outputs, skill_parameters

    # skill = load_skill("cxaia_did_overview")
    skill = load_skill("cxaia_top_10_dids")
    params = skill_parameters(skill)

    async def ask_user(info: dict) -> dict:
        if info.get("error"):
            print(f"\nStep {info.get('index', '?') + 1}: ERROR — {info['error']}")
            action = input("Choose action (retry/skip/stop): ").strip().lower()
            return {"action": "approve" if action == "retry" else action}
        return {"action": "approve"}

    loop = asyncio.get_event_loop()
    output = loop.run_until_complete(run_skill(skill, params, ask_user))
    print("\nSkill execution completed. Outputs:")
    pprint(output)