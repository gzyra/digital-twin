"""Run once to capture your SSO/login session:  python auth_capture.py"""
import yaml
from playwright.sync_api import sync_playwright

with open("config.yaml") as f:
    CFG = yaml.safe_load(f)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, channel="chrome")
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(CFG["start_url"])
    input("Log in in the browser window, then press Enter here to save session...")
    ctx.storage_state(path=CFG["auth_state"])
    print(f"Saved session to {CFG['auth_state']}")
    browser.close()