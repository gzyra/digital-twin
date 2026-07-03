# PYTHONPATH=. python core/replay.py


# RRA Digital Twin — AI Agent

Browser automation skill recorder and replay UI with an integrated LLM chat assistant, live dashboard, and execution audit log.

The agent lets you:

- capture login/session state once and reuse it across all skills,
- record browser actions as reusable **skills** (JSON files),
- run skills from a Chainlit web UI with step-by-step human approval / edit / skip controls,
- describe what you want in plain English — the agent picks the right skill automatically,
- pass inline parameters to a skill directly from chat without any popups,
- run **startup skills** automatically at launch and view their outputs on a live dashboard panel,
- persist skill outputs locally and have the LLM reference them in conversation,
- review every execution in the **audit log** (who ran what, when, with what params, outcome),
- chat directly with the configured LLM from the same UI.

## Main Files

| File / Dir | Purpose |
|---|---|
| `app.py` | Chainlit UI — skill execution, dashboard, chat, memory integration |
| `recorder.py` | Records browser actions and saves a skill JSON |
| `auth_capture.py` | Saves authenticated browser sessions; filters to SSO-required cookies only |
| `initapp.py` | Launcher — loads env vars then starts the Chainlit app |
| `config.yaml` | Runtime configuration |
| `core/replay.py` | Skill execution engine (async Playwright) |
| `core/memory.py` | Persistent skill result cache (24 h TTL by default) |
| `core/skills.py` | Skill metadata helpers + `skill_catalog()` for LLM intent routing |
| `core/llm.py` | LLM integration (BridgeIT / LiteLLM) + `select_skill_by_intent()` |
| `core/audit.py` | SQLite audit log — every execution recorded with params, duration, outcome |
| `skills/` | Saved skill JSON files |
| `audit.db` | SQLite database of all skill executions (auto-created) |
| `memory/` | Cached skill outputs (auto-created, git-ignored) |
| `public/sidebar.js` | Custom Chainlit frontend — left skill panel + right dashboard panel |
| `public/sidebar.css` | Layout and dashboard styles |

### Included skills

| Skill file | What it does |
|---|---|
| `sfdc_search_opportunity` | Search Salesforce by DID, open the opportunity record, navigate to Details tab |
| `sfdc_update_renewal_stage` | Open a Salesforce opportunity by DID and update its renewal stage |
| `cxaia_top_10_dids` | Fetch top 10 deals by ATR from CX AIA (runs at startup) |
| `cxaia_did_overview` | Full risk/adoption overview for a specific DID in CX AIA |
| `cxaia_did_notes` | Deal Pulse notes and insights for a specific DID |
| `cxaia_all_did_notes` | Runs `cxaia_did_notes` for every DID in the top-10 list |

## Requirements

- Python 3.13 (recommended).
- Playwright browser binaries installed.

```bash
pip install -r requirements.txt
python -m playwright install
```

## Configuration

1. Copy example config and edit:

```bash
cp config.yaml.example config.yaml
```

2. Update at minimum:

- `user.email` — auto-filled into login forms during auth capture.
- `auth_state` — path to the primary saved session file (e.g. `state.json`).
- `start_url` — default URL opened when recording a new skill.
- `llm_provider`: `litellm` or `circuit` (Cisco BridgeIT).
- `startup_skills` — list of skill names run automatically at launch.

3. For BridgeIT, set credentials in `~/.env/greg_ai_env.json`:

```json
{
  "BRIDGEIT_CLIENT_ID": "...",
  "BRIDGEIT_CLIENT_SEC": "...",
  "BRIDGEIT_KEY": "...",
  "BRIDGEIT_USERID": "..."
}
```

## Capturing Auth Sessions

Each site that requires login needs a saved browser session (Playwright storage-state JSON). Sessions are defined under `auth_captures` in `config.yaml`.

```yaml
auth_captures:
  cxaia:
    type: manual              # user completes SSO/MFA manually
    url: "https://cxassistant.cisco.com/"
    state_file: "state.json"
    username_selector: "input[name='identifier']"
    keep_domains: ["cisco.com"]          # strip tracking/analytics cookies
  sf:
    type: sso_derived         # loads 'load_from' session, SSO completes automatically
    url: "https://ciscosales.lightning.force.com"
    state_file: "sf_state.json"
    load_from: "state.json"
    ready_selector: ".slds-global-header"
    keep_domains: ["salesforce.com", "force.com"]
```

**Capture types:**

| type | behaviour |
|---|---|
| `manual` | Browser opens, email auto-filled, user completes MFA, press Enter to save |
| `sso_derived` | Loads `load_from` session, navigates to URL, waits for `ready_selector`, saves automatically |

**`keep_domains`** — after saving the full browser state, the file is filtered to keep only cookies and localStorage for listed domains. This strips analytics/tracking cookies that slow down replay.

```bash
python auth_capture.py             # capture all sessions
python auth_capture.py cxaia       # capture primary Cisco SSO session
python auth_capture.py sf          # capture Salesforce session (requires cxaia first)
```

Sessions typically need re-capturing every 8–24 hours.

## Running

```bash
python initapp.py                                          # start the UI
python recorder.py my_skill                                # record a new skill
python recorder.py sf_skill --auth sf_state.json           # record using SF session
python recorder.py my_skill https://example.com            # with explicit start URL
```

When `--auth` points to a non-default session file it is saved into the skill JSON as `"auth_state"` and used automatically during replay.

## Chat Commands

| Command / phrase | What happens |
|---|---|
| *describe what you want* | Agent picks the best matching skill and asks for confirmation |
| `/goal <objective>` | Agent plans a **multi-skill chain** to achieve the goal, shows it, and runs it on confirmation |
| `/plan <objective>` | Alias for `/goal` |
| `run <skill_name>` | Run a skill; prompts for required inputs |
| `run <skill_name> <hint>` | Run with an inline hint — parameters extracted automatically |
| `/run <skill_name> [hint]` | Same, explicit prefix form |
| `add skill` / `learn new skill` | Start recording a new skill |
| `list skills` / `show skills` | List all saved skills |
| `delete <skill_name>` | Delete a skill (asks for confirmation) |
| `/reset` | Clear chat history |
| `/context` | Show saved output values from previous runs |
| `/clear context` | Reset stored context |
| `/memory` | Show cached skill results and their age |
| `/memory clear` | Delete all cached results (next startup re-runs skills) |
| `/audit` | Show the 10 most recent skill executions |
| `/audit <skill_name>` | Filter audit log to a specific skill |
| `/audit <N>` | Show the last N executions |

**Inline hint examples:**

```
run cxaia_did_overview 73595369
run sfdc_search_opportunity for DID: 73595369
/run sfdc_search_opportunity 73595369
```

All three resolve the `did` parameter without prompting. A **🔑 Parameters resolved** message is shown before the browser opens.

**Left sidebar**: all saved skills — click any to run.
**Right dashboard panel**: live outputs from startup and interactive skill runs. Click **Expand ↗** for wide tables. Press `Escape` or **Close** to collapse.

## Skill Step Types

Each step in a skill JSON has an `action` field:

| action | description |
|---|---|
| `navigate` | Go to a URL |
| `click` | Click an element by CSS selector (light DOM only) |
| `locator_click` | Click using Playwright's native locator — **pierces shadow DOM**; supports `:has-text()` and `{param}` substitution |
| `js_click` | Evaluate a JS expression that returns an element and click it; supports `{param}` substitution |
| `input` | Fill a field (parameterised) |
| `input_text` | Fill a field and press Enter; supports `template` with `{param_name}` placeholders |
| `manual_input` | Type a **fixed value** — never substituted |
| `parameter_input` | Collect a value before the skill runs (or auto-fill from inline hint) |
| `type_into` | Focus element, type via keyboard API — most compatible with SPAs and LWCs |
| `wait` | Pause for `seconds` |
| `wait_for_selector` | Wait until a CSS selector appears / becomes visible |
| `wait_for_text` | Wait until a text string appears anywhere on the page |
| `wait_for_url` | Wait until the page URL matches a glob pattern |
| `wait_and_click_last` | Wait for a selector, then click the **last** matching element |

### `locator_click`

Use for SF Lightning / LWC components or any site with shadow DOM where `click` fails.

```json
{
  "action": "locator_click",
  "label": "Click Details tab",
  "selector": "a[data-label='Details'][role='tab']",
  "click_method": "js",
  "timeout_s": 20
}
```

| field | notes |
|---|---|
| `selector` | Playwright CSS. `:has-text('...')` pierces shadow DOM. Supports `{param_name}` substitution. |
| `click_method` | `"playwright"` (default) — simulated click · `"js"` — `el.click()` via JS eval, bypasses LWC pointer-event interception · `"force"` — Playwright click with `force=True` |
| `wait_selector` | CSS selector to wait for (attached) before running |
| `timeout_s` | seconds (default: `action_timeout_ms` from config) |

### `js_click`

Use when full JavaScript logic is needed to find the element (ancestor exclusion, combined checks).

```json
{
  "action": "js_click",
  "label": "Click result excluding tab bar",
  "wait_selector": "a",
  "timeout_s": 30,
  "js": "Array.from(document.querySelectorAll('a')).find(el => el.textContent.includes('{did}') && !el.closest('.slds-context-bar'))"
}
```

> **Note**: `js_click` uses `document.querySelectorAll` which **cannot pierce shadow DOM**. Use `locator_click` with `:has-text()` when targeting LWC components.

### `parameter_input`

Value collected before the skill runs, then silently typed during execution.

```json
{
  "action": "parameter_input",
  "selector": "input.search-input",
  "param_name": "did",
  "param_description": "DID number to search for",
  "value": "",
  "press_enter": true
}
```

### `wait_for_url`

```json
{ "action": "wait_for_url", "pattern": "**/lightning/r/**", "timeout_s": 20 }
```

### Goal-driven planning & routing metadata

The assistant routes requests using an auto-generated **routing table** built from each
skill's metadata, so guidance stays in sync as you add skills manually. Each skill JSON
may declare optional routing fields (all are derived automatically when omitted):

```json
{
  "name": "CX AIA DID Overview",
  "description": "Full risk/adoption overview for one deal.",
  "requires": ["deal_id"],
  "provides": ["cav_bu_id", "adoption_score", "opportunity_url"],
  "goal_tags": ["deal risk", "adoption score"],
  "system": "cxaia"
}
```

- **`requires`** — inputs the skill needs (defaults to its declared inputs).
- **`provides`** — output fields the skill yields (defaults to its declared outputs).
- **`goal_tags`** — free-text intents the skill satisfies (optional).

When the user types `/goal <objective>`, the planner (`core.llm.plan_goal`) returns an
ordered plan of skill runs and chains them so one skill's `provides` feed another's
`requires`. Input values in a plan may reference:

- `$ASK` — pause and ask the user,
- `$MEMORY:<key>` — reuse a value already captured in context/memory,
- `$FROM:<skill>.<field>` — use a field produced by an earlier plan step.

A step marked `for_each` repeats once per item its `$FROM` source yields (e.g. one run
per deal in a list). The full plan is shown for a single confirmation before it runs.

### Dashboard KPIs (center hero strip)

Big-number cards across the top of the chat are configured under `dashboard_kpis` in
`config.yaml`. Each KPI pulls from a skill's cached memory result:

```yaml
dashboard_kpis:
  - label: "Top-10 ATR"
    skill: cxaia_top_10_dids
    column: "ATR"          # sum this numeric column from the skill's table output
    prefix: "$"
  - label: "Deals Tracked"
    skill: cxaia_top_10_dids
    count_rows: true       # count rows in the skill's table output
  - label: "ATR to Renew"
    value: "—"             # static placeholder
```

Supported value sources: `value` (static), `skill`+`column` (sum a numeric column),
`skill`+`count_rows` (row count), or `skill`+`field` (single named output). Optional
`prefix`/`suffix` format the displayed value.

### Per-skill auth session

A skill can override the default session file:

```json
{
  "name": "My SF Skill",
  "auth_state": "sf_state.json",
  "steps": [ ... ]
}
```

`replay.py` uses this file instead of `config.yaml auth_state`. The recorder sets it automatically when `--auth` is used.

### Inline hint auto-params

Steps with `param_name` are auto-params — filled from the inline hint without prompting:

1. Explicit `key=value`: `deal_id: 73595369`
2. Pure number with single param: `73595369`
3. Single long number (5+ digits): `run overview for DID 73595369`
4. LLM extraction for complex / multi-param hints
5. Fallback to recorded default

## Startup Skills & Dashboard

Skills in `startup_skills` run at launch. Results cached in `memory/` (TTL = `startup_skill_ttl_hours`).

- Cache hit (fresh) → skip re-run, load from file.
- Cache stale / missing → run headlessly.
- Skill fails → use most recent stale cache as fallback.
- After any interactive run → result also cached for LLM access.

Use `/memory` to inspect ages. Use `/memory clear` to force a fresh run.

Dashboard panel features: markdown tables rendered as HTML, **Expand ↗** overlay for wide tables, **↻** force re-render. Every output in `memory/` is injected into the LLM system prompt automatically.

## Audit Log

Every interactive run is recorded in `audit.db` (SQLite, auto-created):

- skill name, parameters, timestamps, duration
- outcome: `success`, `stopped`, or `error`
- error message and captured outputs

Query from chat with `/audit`, or open with DB Browser for SQLite.

## Troubleshooting

| Problem | Fix |
|---|---|
| No skills visible | Record one with `python recorder.py <name>` or add a JSON to `skills/` |
| Intent routing picks wrong skill | Improve the `description` field in the skill JSON |
| Login / session not reused | Run `python auth_capture.py` again; check `auth_state` in config |
| SF skill fails on LWC element | Use `locator_click` with `click_method: "js"` instead of `click` or `js_click` |
| `js_click` returns no element | Element is in shadow DOM — switch to `locator_click` with `:has-text()` |
| SF session expired | Run `python auth_capture.py sf`; sessions last 8–24 h |
| BridgeIT auth errors | Verify values in `~/.env/greg_ai_env.json` |
| Playwright browser errors | Run `python -m playwright install` |
| Dashboard not updating | Open browser console, run `window.dtDebug()` |
| Wrong parameter value | Check **🔑 Parameters resolved** before skill runs; `/memory` shows cached values |