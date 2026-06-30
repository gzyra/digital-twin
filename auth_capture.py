"""Capture browser sessions for all sites that need authentication.

Usage:
    python auth_capture.py              # capture every session in config order
    python auth_capture.py cxaia        # capture only the 'cxaia' session
    python auth_capture.py sf           # capture only Salesforce
    python auth_capture.py cxaia sf     # capture two named sessions

Sessions are defined in config.yaml under the 'auth_captures' key.

Capture types
-------------
manual
    Browser opens, user email is auto-filled into the login form, then the
    user completes MFA / password / SSO manually.  Press Enter in the terminal
    to save the session once the target page is loaded.

sso_derived
    Loads an existing session file ('load_from'), navigates to the target URL,
    and waits for the SSO redirect to complete automatically.  No user
    interaction required unless the session has expired.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright, Page

# -- Config -------------------------------------------------------------------

with open("config.yaml") as _f:
    CFG = yaml.safe_load(_f)

USER_EMAIL: str = CFG.get("user", {}).get("email", "")
AUTH_CAPTURES: dict = CFG.get("auth_captures", {})

# -- Cookie-accept script (injected into every new page) ----------------------
# Clicks common "Accept all cookies" buttons as soon as they appear in the DOM.

_COOKIE_ACCEPT_JS = """
(function () {
    const SELECTORS = [
        '#onetrust-accept-btn-handler',
        'button[id*="accept-all"]',
        'button[id*="acceptAll"]',
        'button[id*="AcceptAll"]',
        'button[title="Accept All"]',
        'button[title="Accept all"]',
        'button[aria-label*="Accept all"]',
        'button[aria-label*="Accept All"]',
        '.cookie-accept-all',
        'button.cc-accept',
    ];
    function tryAccept() {
        for (const sel of SELECTORS) {
            const el = document.querySelector(sel);
            if (el && el.offsetParent !== null) { el.click(); return; }
        }
    }
    const obs = new MutationObserver(tryAccept);
    const start = () => obs.observe(document.body, { childList: true, subtree: true });
    document.readyState === 'loading'
        ? document.addEventListener('DOMContentLoaded', start)
        : start();
    tryAccept();
})();
"""


# -- Helpers ------------------------------------------------------------------

def _autofill_email(page: Page, selector: str) -> None:
    """Try to fill the username/email field if it appears within 8 seconds."""
    if not USER_EMAIL or not selector:
        return
    try:
        page.wait_for_selector(selector, timeout=8_000, state="visible")
        page.fill(selector, USER_EMAIL)
        print(f"  -> Auto-filled email: {USER_EMAIL}")
    except Exception:
        pass  # Field not found or page already past the username step


def _report_cookies(state_file: str, domain_hint: str) -> None:
    state = json.loads(Path(state_file).read_text())
    cookies = state.get("cookies", [])
    if domain_hint:
        relevant = [c for c in cookies if domain_hint in c.get("domain", "")]
        print(f"  saved {state_file} -- {len(relevant)} {domain_hint} cookies")
    else:
        print(f"  saved {state_file} -- {len(cookies)} total cookies")


def _filter_state(state_file: str, keep_domains: list) -> tuple[int, int]:
    """Strip cookies and localStorage origins not matching keep_domains.

    Returns (cookies_removed, origins_removed) for reporting.
    """
    if not keep_domains:
        return 0, 0
    path = Path(state_file)
    state = json.loads(path.read_text())

    original_cookies = state.get("cookies", [])
    original_origins = state.get("origins", [])

    state["cookies"] = [
        c for c in original_cookies
        if any(d in c.get("domain", "") for d in keep_domains)
    ]
    state["origins"] = [
        o for o in original_origins
        if any(d in o.get("origin", "") for d in keep_domains)
    ]

    path.write_text(json.dumps(state, indent=2))
    return (
        len(original_cookies) - len(state["cookies"]),
        len(original_origins) - len(state["origins"]),
    )


# -- Capture handlers ---------------------------------------------------------

def _capture_manual(name: str, cfg: dict) -> None:
    """Open browser, auto-fill email, wait for user to complete login."""
    url = cfg["url"]
    state_file = cfg["state_file"]
    username_selector = cfg.get("username_selector", "")
    domain_hint = cfg.get("domain_hint", "")

    print(f"\n[{name}] Opening browser -> {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="chrome")
        ctx = browser.new_context()
        page = ctx.new_page()
        page.add_init_script(_COOKIE_ACCEPT_JS)
        page.goto(url)
        _autofill_email(page, username_selector)
        input("  Complete login in the browser, then press Enter to save the session... ")
        ctx.storage_state(path=state_file)
        browser.close()

    keep_domains = cfg.get("keep_domains", [])
    if keep_domains:
        removed_c, removed_o = _filter_state(state_file, keep_domains)
        print(f"  -> Stripped {removed_c} cookie(s), {removed_o} localStorage origin(s) outside {keep_domains}")
    _report_cookies(state_file, domain_hint)


def _capture_sso_derived(name: str, cfg: dict) -> None:
    """Load an existing session, navigate to the target URL, wait for SSO to complete."""
    url = cfg["url"]
    state_file = cfg["state_file"]
    load_from = cfg.get("load_from", CFG.get("auth_state", "state.json"))
    ready_selector = cfg.get("ready_selector", "body")
    domain_hint = cfg.get("domain_hint", "")

    if not Path(load_from).exists():
        raise FileNotFoundError(
            f"[{name}] Prerequisite session '{load_from}' not found.\n"
            f"       Capture it first:  python auth_capture.py cxaia"
        )

    print(f"\n[{name}] Loading '{load_from}' -> navigating to {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="chrome")
        ctx = browser.new_context(storage_state=load_from)
        page = ctx.new_page()
        page.add_init_script(_COOKIE_ACCEPT_JS)
        page.goto(url)

        try:
            page.wait_for_selector(ready_selector, timeout=60_000)
            print(f"  -> Page ready ({ready_selector})")
        except Exception:
            print("  SSO timed out. Complete login manually in the browser window.")
            input("  Press Enter once the page is fully loaded... ")

        ctx.storage_state(path=state_file)
        browser.close()

    keep_domains = cfg.get("keep_domains", [])
    if keep_domains:
        removed_c, removed_o = _filter_state(state_file, keep_domains)
        print(f"  -> Stripped {removed_c} cookie(s), {removed_o} localStorage origin(s) outside {keep_domains}")
    _report_cookies(state_file, domain_hint)


_HANDLERS = {
    "manual": _capture_manual,
    "sso_derived": _capture_sso_derived,
}


# -- Entry point --------------------------------------------------------------

def _run(name: str) -> None:
    if name not in AUTH_CAPTURES:
        print(f"ERROR: No auth_capture named '{name}' in config.yaml")
        print(f"       Available: {list(AUTH_CAPTURES.keys())}")
        sys.exit(1)
    cfg = AUTH_CAPTURES[name]
    capture_type = cfg.get("type", "manual")
    handler = _HANDLERS.get(capture_type)
    if handler is None:
        raise ValueError(f"Unknown capture type '{capture_type}' for '{name}'")
    handler(name, cfg)


def main() -> None:
    if not AUTH_CAPTURES:
        print("ERROR: No 'auth_captures' section found in config.yaml")
        sys.exit(1)
    targets = sys.argv[1:] or list(AUTH_CAPTURES.keys())
    for name in targets:
        _run(name)
    print("\nDone.")


if __name__ == "__main__":
    main()
