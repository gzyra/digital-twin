# digital-twin — Copilot Instructions

## What this project is

A Chainlit web UI that lets users record browser automation **skills** (JSON files), replay them with Playwright, and chat with an LLM — all from one interface. Skill outputs are cached locally and injected into the LLM's system prompt automatically.

## Running the app

```bash
python initapp.py                           # start Chainlit UI (loads ~/.env/greg_ai_env.json first)
python initapp.py -- python recorder.py <skill_name>   # record a new skill
python auth_capture.py                      # re-capture authenticated browser session
```

`initapp.py` is always the entry point. Running `chainlit run app.py -w` directly will work only if BridgeIT env vars are already exported.

There is no test suite.

## Architecture

```
initapp.py          ─► loads ~/.env/greg_ai_env.json → sets BRIDGEIT_* env vars → runs chainlit app.py
app.py              ─► Chainlit UI: chat, skill execution, startup skills, dashboard updates
core/
  llm.py            ─► LLM abstraction (litellm or Cisco BridgeIT/circuit)
  replay.py         ─► Skill execution engine (Playwright async)
  skills.py         ─► Skill file I/O helpers
  memory.py         ─► Skill result cache (memory/*.json, 24 h TTL by default)
recorder.py         ─► Records browser actions → skill JSON (uses sync Playwright)
skills/             ─► Skill JSON definitions
memory/             ─► Cached skill outputs (auto-created, not committed)
public/             ─► Custom Chainlit frontend (sidebar.js, sidebar.css)
config.yaml         ─► Runtime config (not committed — copy from config.yaml.example)
```

### Data flow

1. `app.py` runs startup skills on load; results cached in `memory/` and emitted as `dashboard-data` fenced blocks.
2. The JS frontend (`public/sidebar.js`) parses `dashboard-data` blocks and renders cards in the right panel.
3. All cached outputs from `memory/` are injected into the LLM system prompt via `core/memory.all_outputs_for_llm()`.

## Configuration

`config.yaml` is loaded at module import time in `core/replay.py`, `core/memory.py`, and `core/skills.py` — changes require a restart. Key fields:

| Field | Notes |
|---|---|
| `llm_provider` | `litellm` or `circuit` (Cisco BridgeIT) |
| `auth_state` | Path to Playwright storage-state file (default: `state.json`) |
| `startup_skills` | List of skill names to run headlessly on startup |
| `startup_skill_ttl_hours` | Cache TTL before a startup skill re-runs (default: 24) |
| `headless` | `false` for interactive runs; startup skills override to headless |

BridgeIT credentials are read from `~/.env/greg_ai_env.json` by `initapp.py` and exported as `BRIDGEIT_CLIENT_ID`, `BRIDGEIT_CLIENT_SEC`, `BRIDGEIT_KEY`, `BRIDGEIT_USERID`. In `core/llm.py`, env vars take precedence over config file values.

## Skill JSON format

Skills live in `skills/<name>.json`. Key fields:

- **`steps`** — array of step objects; each has an `action` field.
- **`outputs`** — array of output capture specs (optional).
- Steps with `param_name` + `human_in_the_loop: true` pause for user input.
- Steps with `param_name` + `human_in_the_loop: false` are **auto-params** filled silently from inline hints in chat (e.g. `run skill_name 73595369`).
- `manual_input` steps always use a fixed `value` — never template-substituted.
- `input_text` steps support a `template` field with `{param_name}` placeholders.

Output types: `selector` (reads a DOM element) or `clipboard` (reads clipboard). Add `"parse": true` and a `fields` array to have the LLM extract structured fields from the raw output.

## LLM providers

`core/llm.py` exposes `chat_completion()` and `stream_chat_completion()`. Both dispatch to either:
- **litellm**: straightforward — uses `model` from config.
- **circuit** (BridgeIT): uses `AzureChatOpenAI` from LangChain with an OAuth2 bearer token fetched from `https://id.cisco.com/oauth2/default/v1/token`. The token is cached in-process with a 60-second safety buffer.

`detect_parameters()` asks the LLM to annotate recorded steps with `human_in_the_loop` and `param_name` — it uses `json_response=True` mode and falls back gracefully.

## Dashboard communication

`app.py` emits dashboard updates by sending a Chainlit message with a `dashboard-data` fenced code block containing JSON `{skill, outputs, ts}`. The custom JS frontend in `public/sidebar.js` intercepts these messages and renders output cards — it never appears as visible chat text.

## Key conventions

- All `core/` modules open `config.yaml` at import time; pass the config path via `DIGITAL_TWIN_CONFIG` env var when running outside the project root.
- `run_skill()` in `core/replay.py` takes an async `ask_user` callback — pass `_headless_ask_user` (in `app.py`) for unattended runs, or an interactive callback for human-in-the-loop runs.
- Skill outputs are merged in order: declared outputs → LLM-parsed fields → LLM-extracted key/values from page text. Later layers only fill gaps.
- `memory/` files are plain JSON; each has `skill_name`, `captured_at` (UTC ISO-8601), and `outputs` keys.
- `recorder.py` uses synchronous Playwright; `core/replay.py` uses async Playwright — do not mix them.
