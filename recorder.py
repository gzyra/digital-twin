"""Record a browser flow → skill JSON:  python recorder.py my_skill_name"""
import sys
import time
from threading import Event

import yaml
from playwright.sync_api import sync_playwright

from core.llm import detect_parameters
from core.skills import save_skill

with open("config.yaml") as f:
    CFG = yaml.safe_load(f)

RECORDER_JS = r"""
function cssPath(el) {
  if (el.dataset && el.dataset.testid) return `[data-testid="${el.dataset.testid}"]`;
  if (el.id) return `#${el.id}`;
  if (el.getAttribute && el.getAttribute('name')) return `[name="${el.getAttribute('name')}"]`;
  return el.tagName ? el.tagName.toLowerCase() : 'unknown';
}

// Keep recorded events in page memory to avoid async bridge races during close.
window.__dtRecordedEvents = window.__dtRecordedEvents || [];
function dtRecord(ev) {
  try {
    window.__dtRecordedEvents.push(ev);
  } catch (err) {}
}

// Capture clicks
document.addEventListener('click', e => {
  dtRecord({
    action: 'click',
    selector: cssPath(e.target),
    value: null,
    text: (e.target.innerText || '').slice(0, 60),
    url: location.href
  });
}, true);

// Capture every keystroke so we never miss a typed value.
// Python side deduplicates, keeping only the final value per selector.
document.addEventListener('input', e => {
  const el = e.target;
  const tag = (el.tagName || '').toLowerCase();
  if (tag !== 'input' && tag !== 'textarea' && !el.isContentEditable) return;
  dtRecord({
    action: 'input',
    selector: cssPath(el),
    value: el.value !== undefined ? el.value : (el.innerText || ''),
    text: (el.placeholder || el.getAttribute('aria-label') || el.getAttribute('name') || '').slice(0, 60),
    url: location.href
  });
}, true);

window.__dtDrainEvents = () => {
  const out = window.__dtRecordedEvents.slice();
  window.__dtRecordedEvents = [];
  return out;
};
"""


def _record_urls() -> list[str]:
    urls = CFG.get("record_urls")
    if not isinstance(urls, list):
        return []
    return [u.strip() for u in urls if isinstance(u, str) and u.strip()]


def _resolve_start_url(start_url: str | None) -> str:
    if start_url:
        return start_url
    configured_urls = _record_urls()
    if configured_urls:
        return configured_urls[0]
    return CFG["start_url"]


def _dedup_input_steps(steps: list[dict]) -> list[dict]:
    """For consecutive input events on the same selector, keep only the last.
    This collapses keystroke-by-keystroke recording into a single final value.
    """
    result = []
    i = 0
    while i < len(steps):
        step = steps[i]
        if step.get("action") == "input":
            sel = step["selector"]
            # Scan forward while same selector and action
            j = i + 1
            while j < len(steps) and steps[j].get("action") == "input" and steps[j].get("selector") == sel:
                j += 1
            # Keep the last (most complete) value
            result.append(steps[j - 1])
            i = j
        else:
            result.append(step)
            i += 1
    return result


def record_skill(name: str, start_url: str | None = None, stop_event: Event | None = None) -> str:
    target_url = _resolve_start_url(start_url)
    steps = []

    with sync_playwright() as p:
      browser = p.chromium.launch(headless=False, channel="chrome")
      ctx = browser.new_context(storage_state=CFG["auth_state"])
      page = ctx.new_page()
      page.add_init_script(RECORDER_JS)
      steps.append({"action": "navigate", "url": target_url})
      page.goto(target_url)

      if stop_event is None:
        input("Perform your flow, then press Enter to stop recording...")
      else:
        while not stop_event.is_set():
          time.sleep(0.2)

        # Pull buffered events before closing browser/context to avoid races.
        try:
          captured = page.evaluate("() => (window.__dtDrainEvents ? window.__dtDrainEvents() : [])")
          if isinstance(captured, list):
            steps.extend(captured)
        except Exception:
          # If page is gone already, continue with what we already captured.
          pass

        browser.close()

    print(f"Captured {len(steps)} raw steps, deduplicating inputs...")
    steps = _dedup_input_steps(steps)
    print(f"Reduced to {len(steps)} steps. Detecting parameters via LLM...")
    steps = detect_parameters(steps)
    path = save_skill(name, steps)
    print(f"Saved skill -> {path}")
    return path


def main():
  name = sys.argv[1] if len(sys.argv) > 1 else "untitled_skill"
  start_url = sys.argv[2] if len(sys.argv) > 2 else None
  record_skill(name, start_url)


if __name__ == "__main__":
  main()