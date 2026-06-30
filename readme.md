# https://github.com/Gerry-Aballa/Playwright-Py-Cheatsheet

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
| `auth_capture.py` | Saves authenticated browser session to storage-state file |
| `initapp.py` | Launcher — loads env vars then starts the Chainlit app |
| `config.yaml` | Runtime configuration |
| `core/replay.py` | Skill execution engine (Playwright) |
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
| `sfdc_search_opportunity` | Search Salesforce for a deal or opportunity by name |
| `sfdc_update_renewal_stage` | Open a Salesforce opportunity and update its renewal stage |
| `cxaia_top_10_dids` | Fetch top 10 deals by ATR from CX AIA (runs at startup) |
| `cxaia_did_overview` | Full risk/adoption overview for a specific DID in CX AIA |
| `cxaia_did_notes` | Deal Pulse notes and insights for a specific DID |
| `cxaia_all_did_notes` | Runs `cxaia_did_notes` for every DID in the top-10 list |

## Requirements

- Python 3.13 (recommended, based on this environment).
- Playwright browser binaries installed.

Install dependencies:

```bash
pip install -r requirements.txt
python -m playwright install
```

## Configuration

1. Copy example config and edit values:

```bash
cp config.yaml.example config.yaml
```

2. Update at least:

- `start_url` — target app URL.
- `common_urls` (optional) — dictionary of common URLs for quick reference during skill recording:
  ```yaml
  common_urls:
    SalesForce: "https://ciscosales.lightning.force.com/lightning"
    "CX AIA": "https://cxassistant.cisco.com/"
    Google: "https://www.google.com"
  ```
- `auth_state` — file path for saved login session (e.g. `state.json`).
- `llm_provider`:
  - `litellm` for standard LiteLLM path, or
  - `circuit` for Cisco BridgeIT.
- `startup_skills` — list of skill names to run automatically at startup:
  ```yaml
  startup_skills:
    - cxaia_top_10_dids
  ```
- `memory_dir` — directory for cached skill outputs (default: `memory`).
- `startup_skill_ttl_hours` — how long a cached result is considered fresh before re-running the skill (default: `24`).

3. For BridgeIT, set credentials in `~/.env/greg_ai_env.json`.

The launcher (`initapp.py`) reads this file and exports:

- `BRIDGEIT_CLIENT_ID`
- `BRIDGEIT_CLIENT_SEC`
- `BRIDGEIT_KEY`
- `BRIDGEIT_USERID`
- `BRIDGEIT_MODEL`

## Quick Start

1. Capture authenticated session once:

```bash
python auth_capture.py
```

2. Run UI with env loading:

```bash
python initapp.py
```

Then open the Chainlit URL shown in terminal.

## Chat Commands & Chat Tips

| Command / phrase | What happens |
|---|---|
| *describe what you want* | Agent picks the best matching skill and asks for confirmation before running |
| `run <skill_name>` | Run a skill by name; prompts for any required inputs |
| `run <skill_name> <hint>` | Run a skill with an inline hint — parameter values are extracted automatically (e.g. a deal ID, a search term) |
| `/run <skill_name> [hint]` | Same as above, explicit prefix form |
| `add skill` / `learn new skill` | Start recording a new skill |
| `list skills` / `show skills` | List all saved skills in chat |
| `delete <skill_name>` | Delete a skill (asks for confirmation) |
| `/reset` | Clear chat history |
| `/context` | Show saved output values from previous skill runs |
| `/clear context` | Reset stored context |
| `/memory` | Show cached skill results and their age |
| `/memory clear` | Delete all cached skill results (next startup will re-run skills) |
| `/audit` | Show the 10 most recent skill executions with status and duration |
| `/audit <skill_name>` | Filter audit log to a specific skill |
| `/audit <N>` | Show the last N executions (e.g. `/audit 20`) |
| `/audit <skill_name> <N>` | Filter + limit (e.g. `/audit sfdc_search_opportunity 5`) |

**Inline hint examples:**

```
run cxaia_did_overview for DID: 73595369
run cxaia_did_overview 73595369
/run cxaia_did_overview 73595369
```

All three resolve `deal_id = 73595369` without prompting the user. A **🔑 Parameters resolved** message is shown before the browser opens so you can verify the extracted value.

**Left sidebar**: shows all saved skills — click any to run it.  
**Right dashboard panel**: shows live outputs from startup and interactive skill runs. Click **Expand ↗** to view wide tables in a centred overlay. Press `Escape` or **Close** to collapse.

## Skill Step Types

Each step in a skill JSON has an `action` field. Available types:

| action | description |
|---|---|
| `navigate` | Go to a URL |
| `click` | Click an element by CSS selector |
| `input` | Fill a field (parameterized — prompts user if `human_in_the_loop: true`) |
| `input_text` | Fill a field and press Enter (supports `template` with `{param_name}` placeholders) |
| `manual_input` | Type a **fixed/predefined value** — value never changes; human confirmation optional |
| `wait` | Pause for `seconds` |
| `wait_for_selector` | Wait until a CSS selector appears in the DOM |
| `wait_for_text` | Wait until a text string appears anywhere on the page |
| `wait_and_click_last` | Wait for a selector to appear, then click the **last** matching element |

### `manual_input` step

Use `manual_input` when a step should always enter the same fixed text, with an optional human confirmation before execution.

```json
{
  "action": "manual_input",
  "selector": "input[name='search']",
  "value": "Cisco Catalyst 9300",
  "press_enter": true,
  "human_in_the_loop": true
}
```

Fields:

- `selector` *(required)* — CSS selector of the target input element.
- `value` *(required)* — Fixed text to type. Never substituted with template variables.
- `press_enter` *(optional, default `false`)* — Press Enter after filling the field.
- `human_in_the_loop` *(optional, default `true`)* — When `true`, pauses for user approval before executing. The user can confirm, edit the value for this run, skip, or stop.

### `wait_and_click_last` step

Use when multiple elements match the same selector (e.g. several copy buttons rendered by a component) and you want to interact with the last one.

```json
{
  "action": "wait_and_click_last",
  "selector": "button.copy-btn",
  "timeout": 10000
}
```

Fields:

- `selector` *(required)* — CSS selector.
- `timeout` *(optional, default `5000`)* — Milliseconds to wait for the element to appear.

### Skill auto-params (inline hint extraction)

Steps with `param_name` and `human_in_the_loop: false` are **auto-params** — their value is filled automatically from the inline hint the user provides in chat. No popup appears.

The extraction order is:

1. **Explicit key=value** in hint: `deal_id: 73595369`
2. **Pure number** with single param: `73595369`
3. **Single long number** (5+ digits) anywhere in hint: `run overview for DID 73595369`
4. **LLM extraction** for complex or multi-param hints
5. **Fallback** to the recorded default value

## Startup Skills & Dashboard

Skills listed under `startup_skills` in `config.yaml` run automatically at app launch. Their outputs are cached locally and displayed on the **Dashboard panel**.

### Caching (24 h TTL)

- On startup, if a cached result for that skill exists and is **less than `startup_skill_ttl_hours` old**, it is loaded from `memory/` and the skill is **not re-run** (faster startup).
- If the cache is stale or missing, the skill runs headlessly.
- If the skill fails at runtime, the most recent stale cache is used as a fallback with an age label.
- After any **interactive skill run**, the result is also cached so the LLM can reference it later.

Use `/memory` in chat to inspect cache ages. Use `/memory clear` to force a fresh run on next startup.

### Dashboard panel

The right-side panel shows a card for each skill that has produced outputs. Cards auto-update as skills finish.

- **Markdown tables** (e.g. top-10 ATR deal tables) are rendered as proper HTML tables with horizontal scroll.
- **Key/value outputs** are shown as labelled rows.
- Click **Expand ↗** to open the card in a full-screen centred overlay — useful for wide tables.
- Press `Escape` or **Close ✕** to return to normal.
- Click **↻** in the panel header to force a re-render.

### LLM access to stored results

Every skill output saved in `memory/` is injected into the LLM's system prompt automatically. The LLM can reference stored data in chat (e.g. explain a deal from the ATR table, suggest next actions) without re-running the skill.

## Useful Commands

Run default UI launcher:

```bash
python initapp.py
```

Optional: run recorder directly from terminal (env vars are loaded first):

```bash
python initapp.py -- python recorder.py another_skill
```

## Adding a new skill

Skills are JSON files in the `skills/` directory. You can create one by recording a browser flow (`python recorder.py <name>`) or by writing the JSON directly.

Every skill should include a `description` field — this is what the LLM reads when deciding which skill to run from a natural-language request. Without it, intent routing won't suggest the skill.

```json
{
  "name": "My Skill Display Name",
  "description": "One sentence: what this skill does and when to use it.",
  "created": "2026-06-29",
  "steps": [ ... ],
  "inputs": [ ... ],
  "outputs": [ ... ]
}
```

For parameterised steps (where the user provides a value), set `"human_in_the_loop": true` and give the step a `"param_name"`. This causes the agent to pause and confirm the value before executing.

After saving the JSON, the skill appears in the left panel immediately — no restart needed.

## Audit Log

Every interactive skill run is recorded in `audit.db` (SQLite, auto-created on first run). Each row captures:

- skill file name and display name
- parameters passed in
- start and end timestamps
- wall-clock duration in seconds
- outcome: `success`, `stopped`, or `error`
- error message if applicable
- captured outputs

Query it from chat with `/audit`, or open `audit.db` directly with any SQLite viewer (e.g. DB Browser for SQLite).

## Troubleshooting

- **No skills visible in UI**: record at least one skill with `python recorder.py <name>`, or add a JSON file to `skills/`.
- **Intent routing picks the wrong skill**: add or improve the `description` field in the skill JSON — more specific descriptions produce better routing.
- **Login/session not reused**: run `python auth_capture.py` again and verify `auth_state` path in `config.yaml`.
- **BridgeIT auth errors**: verify values exist in `~/.env/greg_ai_env.json` and are non-empty.
- **Playwright browser errors**: run `python -m playwright install`.
- **Dashboard not updating**: open browser console and run `window.dtDebug()` to inspect parsed dashboard state.
- **Skill uses wrong parameter value**: check the **🔑 Parameters resolved** message shown before the skill runs; use `/memory` to see what is cached.
- **SFDC selectors not matching**: Salesforce Lightning selectors can vary by org configuration. If a step fails, use the browser DevTools to find the correct CSS selector and update the skill JSON.