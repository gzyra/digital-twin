"""Graphical web UI for the browser skill agent.

    chainlit run app.py -w
"""
import asyncio
import json
import os
import re
import threading
from difflib import SequenceMatcher
from pathlib import Path

import chainlit as cl
import yaml

from core.audit import format_audit_summary, log_end, log_start, recent_runs
from core.llm import chat_completion, plan_goal, select_skill_by_intent, stream_chat_completion
from core.memory import age_description, all_outputs_for_llm, is_stale, list_results, load_result, save_result
from core.replay import run_skill
from core.skills import delete_skill, list_skills, load_skill, routing_table, skill_auto_params, skill_catalog, skill_inputs, skill_outputs, skill_parameters
from recorder import record_skill


RUN_VERBS = {"run", "execute", "start", "launch", "open", "play"}


# ---------- dashboard helpers ----------
async def emit_dashboard_update(skill_name: str, outputs: dict, status: str = "") -> None:
    """Emit a parseable dashboard-data message that the JS frontend renders.

    status="loading"   — placeholder card shown before the skill runs.
    status="no-output" — skill finished but produced nothing.
    status=""          — normal card with real outputs.
    """
    if not outputs and not status:
        return
    from datetime import datetime, timezone as _tz
    data = json.dumps({
        "skill": skill_name,
        "outputs": outputs,
        "ts": datetime.now(_tz.utc).isoformat(),
        "status": status,
    })
    await cl.Message(content=f"```dashboard-data\n{data}\n```").send()


# ---------- KPI hero-strip helpers ----------
def _parse_md_table(text: str) -> tuple[list[str], list[list[str]]]:
    """Parse a markdown table string into (headers, rows). Returns ([], []) on failure."""
    if not isinstance(text, str):
        return [], []
    lines = [l.strip() for l in text.split("\n") if l.strip().startswith("|")]
    if len(lines) < 2:
        return [], []

    def cells(line: str) -> list[str]:
        return [c.strip() for c in line.strip().strip("|").split("|")]

    headers = cells(lines[0])
    rows = [cells(l) for l in lines[2:] if l.strip("|").strip()]
    return headers, rows


def _to_number(text: str) -> float | None:
    """Extract a numeric value from a cell like '$1,234.5' or '73%' → float."""
    cleaned = re.sub(r"[^\d.\-]", "", text or "")
    if cleaned in ("", "-", ".", "-."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _format_number(n: float) -> str:
    """Compact human-readable number: 1.2M, 34.5K, 987."""
    absn = abs(n)
    if absn >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if absn >= 1_000:
        return f"{n / 1_000:.1f}K"
    if n == int(n):
        return str(int(n))
    return f"{n:.1f}"


def _compute_kpis() -> list[dict]:
    """Resolve configured dashboard_kpis into [{label, value}] from cached memory."""
    try:
        with open("config.yaml") as _f:
            cfg = yaml.safe_load(_f) or {}
    except Exception:
        return []

    kpis_cfg = cfg.get("dashboard_kpis") or []
    resolved: list[dict] = []
    for entry in kpis_cfg:
        label = entry.get("label", "KPI")
        prefix = entry.get("prefix", "")
        suffix = entry.get("suffix", "")
        value: str

        if "value" in entry:
            value = str(entry["value"])
        else:
            skill = entry.get("skill", "")
            data = load_result(skill) if skill else None
            outputs = (data or {}).get("outputs", {}) if data else {}
            if not outputs:
                value = "—"
            elif entry.get("field"):
                value = str(outputs.get(entry["field"], "—"))
            else:
                # Use the first markdown-table output for column/row operations
                table_val = next(
                    (v for v in outputs.values() if _is_markdown_table(v)), ""
                )
                headers, rows = _parse_md_table(table_val)
                if entry.get("count_rows"):
                    value = str(len(rows))
                elif entry.get("column") and headers:
                    col = entry["column"].lower()
                    idx = next(
                        (i for i, h in enumerate(headers) if col in h.lower()), -1
                    )
                    if idx >= 0:
                        total = sum(
                            _to_number(r[idx]) or 0.0
                            for r in rows
                            if idx < len(r)
                        )
                        value = _format_number(total)
                    else:
                        value = "—"
                else:
                    value = "—"

        resolved.append({"label": label, "value": f"{prefix}{value}{suffix}"})
    return resolved


async def emit_kpis() -> None:
    """Emit a kpi-data message the JS frontend renders as the center hero strip."""
    kpis = _compute_kpis()
    if not kpis:
        return
    from datetime import datetime, timezone as _tz
    data = json.dumps({"kpis": kpis, "ts": datetime.now(_tz.utc).isoformat()})
    await cl.Message(content=f"```kpi-data\n{data}\n```").send()


def _is_markdown_table(value: str) -> bool:
    """Return True if the string looks like a markdown table (≥3 pipe-delimited lines)."""
    if not isinstance(value, str):
        return False
    table_lines = [l for l in value.split("\n") if l.strip().startswith("|")]
    return len(table_lines) >= 3


def _display_value(v: str) -> str:
    """Return a concise display string for an output value (for the chat message)."""
    if _is_markdown_table(v):
        # Count data rows: pipe lines that are not the header or separator
        data_rows = [
            l for l in v.split("\n")
            if l.strip().startswith("|") and not re.match(r"^\|[\s|:|-]+\|$", l.strip())
        ]
        row_count = max(0, len(data_rows) - 1)  # subtract header row
        return f"*(table — {row_count} rows)*"
    if len(v) > 200:
        return v[:197] + "…"
    return v


async def _collect_parsed_fields(out_def: dict, raw_value: str) -> dict[str, str]:
    """Use the LLM to parse a raw output value into named fields. Returns {} on failure."""
    fields: list[dict] = out_def.get("fields", [])
    field_lines = (
        "\n".join(f'- "{f["name"]}": {f["description"]}' for f in fields)
        if fields
        else "Extract all meaningful key/value pairs."
    )
    parse_prompt = [
        {"role": "system", "content": (
            "You are a data extraction assistant. "
            "Parse the following markdown text and extract the requested fields. "
            "Reply ONLY with a valid JSON object. Use null for missing fields. "
            f"Fields to extract:\n{field_lines}"
        )},
        {"role": "user", "content": raw_value},
    ]
    loop = asyncio.get_running_loop()
    parse_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

    def _parse_worker(q=parse_queue) -> None:
        try:
            for chunk in stream_chat_completion(parse_prompt):
                asyncio.run_coroutine_threadsafe(q.put(("token", chunk)), loop)
        except Exception as exc:
            asyncio.run_coroutine_threadsafe(q.put(("error", str(exc))), loop)
        finally:
            asyncio.run_coroutine_threadsafe(q.put(("done", "")), loop)

    threading.Thread(target=_parse_worker, daemon=True).start()
    raw_parts: list[str] = []
    while True:
        etype, payload = await parse_queue.get()
        if etype == "token":
            raw_parts.append(payload)
        elif etype in ("error", "done"):
            break

    json_text = "".join(raw_parts).strip()
    if json_text.startswith("```"):
        json_text = re.sub(r"^```[a-z]*\n?", "", json_text)
        json_text = re.sub(r"\n?```$", "", json_text.rstrip())
    try:
        parsed = json.loads(json_text)
        if isinstance(parsed, dict):
            return {k: str(v) for k, v in parsed.items() if v is not None}
    except Exception:
        pass
    return {}


async def process_skill_outputs(
    skill_name: str,
    skill: dict,
    result: dict,
    stream: bool = True,
) -> dict[str, str]:
    """Extract, display, and return all outputs from a completed skill run.

    When stream=True (interactive run), shows streaming LLM summaries.
    When stream=False (headless/startup run), skips user-facing messages.
    """
    declared = {out["name"]: result["outputs"].get(out["name"], "") for out in skill_outputs(skill)}

    # Parse declared outputs that have parse=true.
    # Skip LLM extraction for table outputs with no explicit fields (preserve structure).
    parsed_fields: dict[str, str] = {}
    for out_def in skill_outputs(skill):
        if not out_def.get("parse"):
            continue
        raw_value = declared.get(out_def["name"], "")
        if not raw_value:
            continue
        if _is_markdown_table(raw_value) and not out_def.get("fields"):
            continue  # Table with no field spec — keep raw; JS renders it
        try:
            fields = await _collect_parsed_fields(out_def, raw_value)
            parsed_fields.update(fields)
        except Exception as exc:
            if stream:
                await cl.Message(content=f"⚠️ Could not parse `{out_def['name']}`: {exc}").send()

    # If no declared outputs, ask LLM to extract key values from page text
    llm_extracted: dict[str, str] = {}
    if result.get("page_text") and not declared:
        extraction_prompt = [
            {"role": "system", "content": (
                f"The user just ran a browser skill called '{skill_name}'. "
                "Extract key output values (like IDs, names, scores, statuses) from the page text below. "
                "Reply ONLY as a compact list, one per line: `key: value`. "
                "If nothing meaningful was found, say 'No extractable output.'"
            )},
            {"role": "user", "content": result["page_text"]},
        ]
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

        def _worker() -> None:
            try:
                for chunk in stream_chat_completion(extraction_prompt):
                    asyncio.run_coroutine_threadsafe(queue.put(("token", chunk)), loop)
            except Exception as e:
                asyncio.run_coroutine_threadsafe(queue.put(("error", str(e))), loop)
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(("done", "")), loop)

        parts: list[str] = []
        if stream:
            summary_msg = cl.Message(content="")
            await summary_msg.send()
            threading.Thread(target=_worker, daemon=True).start()
            while True:
                etype, payload = await queue.get()
                if etype == "token":
                    parts.append(payload)
                    await summary_msg.stream_token(payload)
                elif etype in ("error", "done"):
                    break
            await summary_msg.update()
        else:
            threading.Thread(target=_worker, daemon=True).start()
            while True:
                etype, payload = await queue.get()
                if etype == "token":
                    parts.append(payload)
                elif etype in ("error", "done"):
                    break

        for line in "".join(parts).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                k = k.strip().lstrip("`").lower().replace(" ", "_")
                v = v.strip().rstrip("`")
                if k and v and v.lower() != "no extractable output":
                    llm_extracted[k] = v

    all_outputs = {**declared, **parsed_fields, **llm_extracted}

    if stream:
        if all_outputs:
            display_lines = "\n".join(
                f"- **{k}**: {_display_value(v)}" for k, v in all_outputs.items()
            )
            await cl.Message(
                content=f"📤 **Outputs from `{skill_name}`:**\n{display_lines}\n\nThese are saved in context for the next skill."
            ).send()
        else:
            await cl.Message(content="ℹ️ No structured output captured from this skill.").send()

    return all_outputs


async def _headless_ask_user(ctx: dict) -> dict:
    """Auto-approve all steps for headless startup skill runs (use recorded defaults)."""
    return {"action": "approve"}


async def run_startup_skills() -> None:
    """Run all startup_skills from config headlessly and push their outputs to the dashboard.

    Placeholder cards are emitted immediately for every startup skill so the dashboard
    fills with loading states before any browser runs begin.  Each placeholder is
    replaced with real data (or a no-output marker) once that skill finishes.

    If a cached result exists and is younger than startup_skill_ttl_hours, the skill
    is skipped and the cached data is used instead.
    """
    try:
        with open("config.yaml") as _f:
            _cfg = yaml.safe_load(_f)
    except Exception:
        return

    startup = _cfg.get("startup_skills") or []
    if not startup:
        return

    # Emit loading placeholder cards for every startup skill immediately so the
    # dashboard panel is populated before any skill actually runs.
    for skill_name in startup:
        await emit_dashboard_update(skill_name, {}, status="loading")

    await cl.Message(content=f"⏳ Checking {len(startup)} startup skill(s)…").send()

    for skill_name in startup:
        all_outputs: dict = {}
        # ── Use cached result if still fresh ──
        if not is_stale(skill_name):
            cached = load_result(skill_name)
            all_outputs = cached["outputs"] if cached else {}
            age = age_description(skill_name)
            await cl.Message(
                content=f"📦 Using cached result for `{skill_name}` ({age})"
            ).send()
        else:
            # ── Re-run the skill ──
            try:
                skill = load_skill(skill_name)
                params: dict[str, str] = {}
                for step in skill.get("steps", []):
                    pname = step.get("param_name")
                    if pname and step.get("value"):
                        params[pname] = step["value"]

                result = await run_skill(skill, params, _headless_ask_user)
                all_outputs = await process_skill_outputs(skill_name, skill, result, stream=False)

                if all_outputs:
                    save_result(skill_name, all_outputs)

                await cl.Message(content=f"✅ Startup skill `{skill_name}` done.").send()
            except Exception as exc:
                await cl.Message(content=f"⚠️ Startup skill `{skill_name}` failed: {exc}").send()
                # Fall back to cached data even if stale
                cached = load_result(skill_name)
                all_outputs = cached["outputs"] if cached else {}
                if all_outputs:
                    await cl.Message(
                        content=f"📦 Showing last cached result for `{skill_name}` ({age_description(skill_name)})"
                    ).send()

        if all_outputs:
            context = cl.user_session.get("skill_context") or {}
            context.update(all_outputs)
            cl.user_session.set("skill_context", context)

        # Always emit to replace the loading placeholder — even when there is no output.
        await emit_dashboard_update(
            skill_name,
            all_outputs,
            status="" if all_outputs else "no-output",
        )

    # Refresh LLM system prompt now that memory may have been updated
    skills = cl.user_session.get("skills") or []
    reset_chat_history(skills)

    # Publish KPI hero strip now that startup results are cached
    await emit_kpis()


# ---------- helpers to pause for a button click ----------
async def wait_for_action(message: cl.Message, valid: list[str]) -> dict:
    """Block until the user clicks one of the action buttons on `message`."""
    res = await message.send()
    # Chainlit AskActionMessage handles the wait for us; see below.
    return res


def build_chat_system_prompt(skills: list[str]) -> str:
    available_skills = ", ".join(sorted(skills)) if skills else "none recorded"
    # Auto-generated routing table keeps skill selection guidance in sync with
    # the skills on disk (their descriptions, requires, and provides metadata).
    catalog = skill_catalog()
    routing_block = routing_table(catalog)
    base = (
        "You are the digital-twin assistant. Help the user automate browser workflows, "
        "explain failures, and answer short task-oriented questions. Keep replies concise and practical. "
        f"Available saved skills: {available_skills}\n\n"
        "## Skill routing table\n"
        "Use this to decide which skill answers a request. A skill's 'gives' fields are the\n"
        "data it can retrieve; its 'needs' fields are the inputs it requires. Chain skills so\n"
        "that one skill's output (gives) supplies another skill's input (needs).\n"
        f"{routing_block}\n\n"
        "To run a skill the user can type its name naturally, use /run <skill_name>, or just describe "
        "what they want — you will propose the right skill automatically.\n"
        "For a multi-step objective, the user can type `/goal <objective>` and you will plan and "
        "chain several skills together to achieve it.\n"
        "**Proactive skill suggestion**: if the user asks for a data field listed under a skill's "
        "'gives:' section and that data is not already in memory, immediately propose running "
        "that skill to retrieve it. Example: user asks for CAV_BU_ID → suggest cxaia_did_overview.\n"
        "Type /audit to see the last 10 execution records.\n\n"
        "You also have access to the latest results from automatically-run skills stored in memory. "
        "Use this data to answer questions, highlight insights, and suggest next actions.\n\n"
        "## Domain knowledge\n"
        "- **DID** (Deal ID) is the single unique identifier that links a deal across ALL systems: "
        "Salesforce (param name: `did`), CX AIA (param name: `deal_id`), and Cisco Ready all refer "
        "to the same opportunity with the same numeric DID.\n"
        "- When the user mentions a DID or deal number, it can be used directly in any skill "
        "regardless of which system it targets — no translation needed.\n"
        "- Typical workflow: fetch deal context from CX AIA (cxaia_did_notes / cxaia_did_overview) "
        "then open the same DID in Salesforce (sfdc_search_opportunity) to view or update the record.\n"
        "- DIDs are 8-digit numbers (e.g. 73595369). If the user provides one, pre-fill it as the "
        "`did` / `deal_id` parameter for whichever skill you suggest."
    )
    memory_block = all_outputs_for_llm()
    if memory_block:
        base += f"\n\n{memory_block}"
    return base


def reset_chat_history(skills: list[str]) -> None:
    cl.user_session.set(
        "chat_history",
        [{"role": "system", "content": build_chat_system_prompt(skills)}],
    )


def normalize_skill_name(value: str) -> str:
    parts = re.findall(r"[a-z0-9]+", value.lower())
    return "_".join(parts)


async def broadcast_skills_to_ui(skills: list[str]) -> None:
    """Store skills in session for potential frontend use."""
    cl.user_session.set("current_skills", skills)


def extract_skill_request(content: str, skills: list[str]) -> tuple[str, str] | None:
    """Return (skill_name, inline_hint) or None.

    Supports:
      /run skill_name [hint text]        — explicit prefix, optional hint after name
      run skill_name for DID: X          — natural language with optional hint
      run skill_name X                   — positional hint after name
    """
    stripped = content.strip()

    # ── /run prefix: split skill name from optional hint ──
    if stripped.lower().startswith("/run "):
        rest = stripped[5:].strip()
        words = rest.split()
        if not words:
            return None
        # Try progressively longer prefixes as the skill name, pick longest match
        best: tuple[str, str] | None = None
        for end in range(1, len(words) + 1):
            candidate = normalize_skill_name(" ".join(words[:end]))
            for skill_name in skills:
                if normalize_skill_name(skill_name) == candidate:
                    hint = " ".join(words[end:]).strip()
                    best = (skill_name, hint)
        return best  # longest match wins; None if no skill found

    lowered = stripped.lower()
    if not any(verb in lowered for verb in RUN_VERBS):
        return None

    normalized_content = normalize_skill_name(stripped)
    matches = [
        skill_name
        for skill_name in skills
        if normalize_skill_name(skill_name) in normalized_content
    ]
    if not matches:
        return None
    matched = max(matches, key=len)
    norm_matched = normalize_skill_name(matched)

    # ── Preferred path: find the skill name as a continuous substring ──
    # Try "cxaia did overview" (spaces) or "cxaia_did_overview" form in lowered
    for form in (norm_matched.replace("_", " "), matched.lower(), norm_matched):
        idx = lowered.find(form)
        if idx != -1:
            after = stripped[idx + len(form):].strip()
            # Strip leading stop words
            after = re.sub(r"^(for|with|using|on|to|search)\s+", "", after, flags=re.IGNORECASE)
            # Strip "DID:" / "ID:" prefixes from the hint value itself
            after = re.sub(r"^\s*(?:DID|deal_?id|ID)\s*[:=]\s*", "", after, flags=re.IGNORECASE)
            return matched, after.strip()

    # ── Fallback: token-by-token scan ──
    skill_tokens = set(norm_matched.split("_"))
    words = stripped.split()
    hint_words: list[str] = []
    consuming = False
    for w in words:
        w_norm = normalize_skill_name(w)
        # Match individual skill tokens OR the full skill name written as one word
        if w_norm in skill_tokens or w_norm == norm_matched:
            consuming = True
            continue
        if consuming:
            if w.lower() not in {"for", "with", "using", "on", "to", "search"}:
                hint_words.append(w)
            elif hint_words:
                hint_words.append(w)
    hint = " ".join(hint_words).strip()
    hint = re.sub(r"^\s*(?:DID|deal_?id|ID)\s*[:=]\s*", "", hint, flags=re.IGNORECASE).strip()
    return matched, hint


def is_list_skills_request(content: str) -> bool:
    lowered = content.lower().strip()
    keywords = {"list skills", "show skills", "what skills", "skills?", "all skills"}
    return any(kw in lowered for kw in keywords)


def is_add_skill_request(content: str) -> bool:
    lowered = content.lower().strip()
    keywords = {"add skill", "learn new", "record skill", "new skill", "add new"}
    return any(kw in lowered for kw in keywords)


def is_delete_skill_request(content: str) -> bool:
    lowered = content.lower().strip()
    keywords = {"delete skill", "remove skill", "remove ", "delete ", "kill "}
    return any(kw in lowered for kw in keywords)


def extract_delete_target(content: str, skills: list[str]) -> str | None:
    """Try to extract skill name from delete request.

    Supports exact, partial, and fuzzy matches so commands like
    'delete get_cav' or 'delete get cav id' work.
    """
    if not skills:
        return None

    lowered = content.lower().strip()
    # Remove common delete command prefixes.
    candidate_text = re.sub(r"^(delete|remove|kill)\s+(skill\s+)?", "", lowered).strip()
    if not candidate_text:
        return None

    candidate = normalize_skill_name(candidate_text)
    if not candidate:
        return None

    scored: list[tuple[float, str]] = []
    for skill_name in skills:
        normalized_skill = normalize_skill_name(skill_name)

        # Exact match wins immediately.
        if candidate == normalized_skill:
            return skill_name

        score = 0.0

        # Strong prefix/containment signals.
        if normalized_skill.startswith(candidate):
            score += 1.0
        if candidate in normalized_skill:
            score += 0.8
        if normalized_skill in candidate:
            score += 0.6

        # Token overlap for partial phrase matching.
        cand_tokens = set(candidate.split("_"))
        skill_tokens = set(normalized_skill.split("_"))
        if cand_tokens and skill_tokens:
            overlap = len(cand_tokens & skill_tokens) / len(cand_tokens)
            score += overlap

        # Fuzzy similarity fallback.
        score += 0.7 * SequenceMatcher(None, candidate, normalized_skill).ratio()

        scored.append((score, skill_name))

    if not scored:
        return None

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_skill = scored[0]

    # Require a reasonable confidence so random text doesn't delete a skill.
    return best_skill if best_score >= 1.0 else None


def load_record_urls() -> list[str]:
    cfg_path = Path(os.getenv("DIGITAL_TWIN_CONFIG", "config.yaml"))
    if not cfg_path.exists():
        return []

    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    # First, try to get common_urls dictionary
    common_urls = cfg.get("common_urls", {})
    if common_urls:
        return list(common_urls.values())
    
    # Fallback to old record_urls list
    urls = cfg.get("record_urls")
    if isinstance(urls, list):
        cleaned = [u.strip() for u in urls if isinstance(u, str) and u.strip()]
        if cleaned:
            return cleaned

    fallback = cfg.get("start_url")
    return [fallback] if isinstance(fallback, str) and fallback.strip() else []


def load_common_urls() -> dict[str, str]:
    """Load common URLs from config for reference during recording."""
    cfg_path = Path(os.getenv("DIGITAL_TWIN_CONFIG", "config.yaml"))
    if not cfg_path.exists():
        return {}
    
    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    
    return cfg.get("common_urls", {})


async def show_home(skills: list[str]) -> None:
    await cl.Message(
        content=(
            "### Chat Commands\n"
            "Everything is controlled from chat.\n\n"
            "- `add skill` or `learn new skill` to record a new skill\n"
            "- `list skills` to show all saved skills\n"
            "- `run <skill_name>` to execute a skill (will ask for inputs)\n"
            "- `/goal <objective>` — describe a goal; the agent plans & chains skills to achieve it\n"
            "- `/skill_name [hint]` — shortcut to run any skill directly (e.g. `/cxaia_did_overview 73595369`)\n"
            "- `delete <skill_name>` to remove a skill\n"
            "- `stop recording` to finish an active recording\n"
            "- `/context` to see saved output values from previous skill runs\n"
            "- `/clear context` to reset stored context\n"
            "- `/reset` clears chat history"
        ),
    ).send()

    if skills:
        await cl.Message(
            content=f"Saved skills: {', '.join(sorted(skills))}"
        ).send()


async def show_chat_mode(skills: list[str]) -> None:
    skill_line = ", ".join(sorted(skills)) if skills else "No skills recorded yet."
    await cl.Message(
        content=(
            "### Chat Mode\n"
            "Send a normal message to talk to the LLM. Replies stream as they arrive.\n\n"
            f"Saved skills: {skill_line}\n"
            "Use `/run skill_name` or say `run <skill name>` to execute one."
        ),
    ).send()


async def set_mode(mode: str) -> None:
    resolved = mode if mode in {"home", "execute", "chat", "learn"} else "home"
    cl.user_session.set("mode", resolved)
    skills = cl.user_session.get("skills") or []

    if resolved == "home":
        await show_home(skills)
    elif resolved == "execute":
        await show_execute(skills)
    elif resolved == "chat":
        await show_chat_mode(skills)


async def show_execute(skills: list[str]) -> None:
    if not skills:
        await cl.Message(
            content="No saved skills yet. Use `add skill` to record one first.",
        ).send()
        return

    await cl.Message(
        content=(
            "### Execute Saved Skill\n"
            f"Saved skills: {', '.join(sorted(skills))}\n\n"
            "Use chat: `run <skill_name>`, `list skills`, `add skill`, `delete <name>`."
        ),
    ).send()


def _extract_auto_params_from_hint(hint: str, auto_params: list[dict]) -> dict[str, str]:
    """Extract auto-param values from an inline hint string.

    Tries fast heuristics first (no LLM needed for simple cases), then LLM.

    Fast paths:
      '73595369'           → {deal_id: '73595369'}  (sole number, single param)
      'deal_id: 73595369'  → {deal_id: '73595369'}  (explicit key=value)
    """
    if not hint or not auto_params:
        return {}

    # ── Fast path 1: explicit "key: value" pairs in the hint ──
    fast: dict[str, str] = {}
    for ap in auto_params:
        key_pat = re.escape(ap["name"].replace("_", r"[_ ]")).replace(r"\[\_\ ]", r"[_ ]")
        m = re.search(rf'\b{key_pat}\s*[:=]\s*(\S+)', hint, re.IGNORECASE)
        if m:
            fast[ap["name"]] = m.group(1).rstrip(".,;:")
    if fast:
        return fast

    # ── Fast path 2: hint is a pure number and there is exactly one param ──
    stripped_hint = hint.strip().rstrip(".,;:")
    if re.fullmatch(r"\d+", stripped_hint) and len(auto_params) == 1:
        return {auto_params[0]["name"]: stripped_hint}

    # ── Fast path 3: hint contains exactly one long number (5+ digits) and one param ──
    nums = re.findall(r"\b\d{5,}\b", hint)
    if len(nums) == 1 and len(auto_params) == 1:
        return {auto_params[0]["name"]: nums[0]}

    # ── LLM path: complex hints or multiple params ──
    field_lines = "\n".join(
        f'- "{p["name"]}": {p["description"]}' for p in auto_params
    )
    prompt = [
        {"role": "system", "content": (
            "Extract parameter values from the user's hint text. "
            "Reply ONLY with a valid JSON object mapping param names to extracted string values. "
            "Use null for params that cannot be determined. "
            f"Params to extract:\n{field_lines}"
        )},
        {"role": "user", "content": hint},
    ]
    try:
        raw = chat_completion(prompt).strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw.rstrip())
        parsed = json.loads(raw)
        return {k: str(v) for k, v in parsed.items() if v is not None}
    except Exception:
        if len(auto_params) == 1:
            return {auto_params[0]["name"]: hint}
        return {}


# ---------- batch skill support ----------

def _extract_dids_from_memory(source_skill: str, source_output: str) -> list[str]:
    """Return ordered, deduplicated deal IDs extracted from a cached skill output.

    Handles the markdown-link format produced by cxaia_top_10_dids:
        [73595369](https://ciscosales.lightning.force.com/...)
    Falls back to any bare 5–10-digit number in the text if no links are found.
    """
    data = load_result(source_skill)
    if not data:
        return []
    table_text = data.get("outputs", {}).get(source_output, "")
    if not table_text:
        return []
    # Primary: markdown links [DIGITS](url)
    dids = re.findall(r'\[(\d{5,10})\]\(https?://', table_text)
    if not dids:
        # Fallback: bare long numbers in the table
        dids = re.findall(r'\b(\d{7,10})\b', table_text)
    seen: set[str] = set()
    result: list[str] = []
    for did in dids:
        if did not in seen:
            seen.add(did)
            result.append(did)
    return result


async def run_batch_skill(batch_skill: dict) -> None:
    """Execute a batch skill: run target_skill headlessly for each DID in source memory.

    Each individual result is saved to memory/<target_skill>_<did>.json.
    A combined summary table is emitted to the dashboard as one card.
    """
    source_skill = batch_skill.get("source_skill", "")
    source_output = batch_skill.get("source_output", "")
    target_skill_name = batch_skill.get("target_skill", "")
    target_param = batch_skill.get("target_param", "deal_id")
    batch_card_name = re.sub(r"\s+", "_", batch_skill.get("name", "batch").lower())

    dids = _extract_dids_from_memory(source_skill, source_output)
    if not dids:
        await cl.Message(
            content=(
                f"⚠️ No DIDs found in `{source_skill}` → `{source_output}`.\n"
                f"Run `{source_skill}` first so the dashboard has data to iterate over."
            )
        ).send()
        return

    est_minutes = len(dids) * 2
    await cl.Message(
        content=(
            f"🔄 Running `{target_skill_name}` for **{len(dids)} DIDs** from `{source_skill}`.\n"
            f"Each run takes ~2 min headlessly — estimated **{est_minutes} min** total."
        )
    ).send()

    # Emit loading placeholder immediately so the dashboard card appears
    await emit_dashboard_update(batch_card_name, {}, status="loading")

    try:
        target_skill = load_skill(target_skill_name)
    except Exception as exc:
        await cl.Message(content=f"❌ Could not load skill `{target_skill_name}`: {exc}").send()
        await emit_dashboard_update(batch_card_name, {}, status="no-output")
        return

    summary_rows: list[dict] = []
    for i, did in enumerate(dids, 1):
        await cl.Message(content=f"📋 Notes {i}/{len(dids)}: DID `{did}`…").send()
        try:
            result = await run_skill(target_skill, {target_param: did}, _headless_ask_user)
            outputs = await process_skill_outputs(target_skill_name, target_skill, result, stream=False)
            if outputs:
                save_result(f"{target_skill_name}_{did}", outputs)
            summary_rows.append({
                "did": did,
                "customer": outputs.get("customer_name", "—"),
                "summary": (outputs.get("summary") or "")[:100] or "—",
                "ok": True,
            })
        except Exception as exc:
            await cl.Message(content=f"⚠️ DID {did} failed: {exc}").send()
            summary_rows.append({
                "did": did,
                "customer": "—",
                "summary": f"Error: {str(exc)[:80]}",
                "ok": False,
            })

    # Build summary markdown table for the dashboard card
    lines = [
        "| DID | Customer | Summary |",
        "|-----|----------|---------|",
    ]
    for row in summary_rows:
        lines.append(f"| {row['did']} | {row['customer']} | {row['summary']} |")
    summary_table = "\n".join(lines)

    ok_count = sum(1 for r in summary_rows if r["ok"])
    await cl.Message(
        content=f"✅ Notes fetched for {ok_count}/{len(summary_rows)} DIDs."
    ).send()

    combined = {"did_notes_summary": summary_table}
    save_result(batch_card_name, combined)
    await emit_dashboard_update(batch_card_name, combined)

    # Refresh LLM system prompt so the new data is available in chat
    skills = cl.user_session.get("skills") or []
    reset_chat_history(skills)


# ---------- goal-driven plan execution ----------

def _extract_list_from_text(text: str) -> list[str]:
    """Extract ordered, unique identifier tokens (e.g. DIDs) from a table/text value."""
    if not isinstance(text, str):
        return []
    tokens = re.findall(r'\[(\d{5,10})\]\(https?://', text)  # markdown links first
    if not tokens:
        tokens = re.findall(r'\b(\d{7,10})\b', text)          # bare long numbers
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _lookup_field(ref_skill: str, ref_field: str, plan_outputs: dict[str, dict]) -> str:
    """Resolve a $FROM:skill.field reference from prior plan outputs, then memory."""
    src = plan_outputs.get(ref_skill)
    if src is None:
        data = load_result(ref_skill)
        src = data.get("outputs", {}) if data else {}
    if ref_field and ref_field in src:
        return str(src[ref_field])
    # Fallback: first string value (useful when a skill has a single table output)
    for v in src.values():
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _resolve_input_value(
    raw: str,
    ask_values: dict[str, str],
    context: dict,
    plan_outputs: dict[str, dict],
) -> str:
    """Resolve a single plan input value reference to a concrete string."""
    if not isinstance(raw, str):
        return str(raw)
    val = raw.strip()
    if val == "$ASK":
        # $ASK is resolved by the caller (which knows the param name); nothing to do here.
        return ""
    if val.startswith("$MEMORY:"):
        key = val[len("$MEMORY:"):].strip()
        return str(context.get(key, ""))
    if val.startswith("$FROM:"):
        ref = val[len("$FROM:"):].strip()
        ref_skill, _, ref_field = ref.partition(".")
        return _lookup_field(ref_skill, ref_field, plan_outputs)
    return raw


async def execute_plan(goal: str, plan: list[dict]) -> None:
    """Execute an ordered multi-skill plan produced by core.llm.plan_goal.

    Resolves $ASK / $MEMORY / $FROM input references, runs each skill headlessly,
    caches outputs to memory, and streams progress + dashboard cards. Steps flagged
    for_each repeat once per item produced by their $FROM source.
    """
    if not plan:
        await cl.Message(content="🤔 I couldn't map that goal to any available skills.").send()
        return

    context = cl.user_session.get("skill_context") or {}

    # ── Show the plan and ask for a single confirmation ──
    plan_lines = []
    for i, step in enumerate(plan, 1):
        inp = ", ".join(f"{k}={v}" for k, v in step["inputs"].items()) or "no inputs"
        loop_tag = " _(for each item)_" if step.get("for_each") else ""
        why = f" — {step['reason']}" if step.get("reason") else ""
        plan_lines.append(f"{i}. `{step['skill']}` ({inp}){loop_tag}{why}")
    confirm = await cl.AskActionMessage(
        content=(
            f"🧭 **Plan to achieve:** _{goal}_\n\n" + "\n".join(plan_lines) +
            "\n\nRun this plan?"
        ),
        actions=[
            cl.Action(name="yes", value="yes", label="✅ Run plan", payload={"v": "yes"}),
            cl.Action(name="no", value="no", label="❌ Cancel", payload={"v": "no"}),
        ],
        timeout=120,
    ).send()
    if not confirm or confirm.get("payload", {}).get("v") != "yes":
        await cl.Message(content="Plan cancelled.").send()
        return

    # ── Collect $ASK values up front (one prompt per unique param name) ──
    ask_values: dict[str, str] = {}
    ask_needed: list[str] = []
    for step in plan:
        for pname, pval in step["inputs"].items():
            if isinstance(pval, str) and pval.strip() == "$ASK" and pname not in ask_needed:
                ask_needed.append(pname)
    for pname in ask_needed:
        prefill = context.get(pname, "")
        hint_note = f" _(suggested: `{prefill}`)_" if prefill else ""
        cl.user_session.set("awaiting_manual_input", True)
        try:
            res = await cl.AskUserMessage(
                content=f"Value for **{pname}**{hint_note}:", timeout=300
            ).send()
        finally:
            cl.user_session.set("awaiting_manual_input", False)
        raw = (res.get("output", "").strip() if res else "")
        ask_values[pname] = raw or prefill

    plan_outputs: dict[str, dict] = {}
    cl.user_session.set("skill_running", True)
    try:
        for i, step in enumerate(plan, 1):
            skill_name = step["skill"]
            try:
                skill = load_skill(skill_name)
            except Exception as exc:
                await cl.Message(content=f"❌ Step {i}: could not load `{skill_name}`: {exc}").send()
                continue

            # ── Determine iteration set for for_each steps ──
            iterations: list[dict[str, str]] = []
            if step.get("for_each"):
                # Find the $FROM input that drives the loop
                loop_param = None
                loop_ref = None
                for pname, pval in step["inputs"].items():
                    if isinstance(pval, str) and pval.startswith("$FROM:"):
                        loop_param, loop_ref = pname, pval[len("$FROM:"):].strip()
                        break
                if loop_param and loop_ref:
                    ref_skill, _, ref_field = loop_ref.partition(".")
                    source_text = _lookup_field(ref_skill, ref_field, plan_outputs)
                    items = _extract_list_from_text(source_text)
                    if not items:
                        await cl.Message(
                            content=f"⚠️ Step {i} `{skill_name}`: no items found in `{ref_skill}` to iterate over."
                        ).send()
                        continue
                    for item in items:
                        params = {}
                        for pn, pv in step["inputs"].items():
                            if pn == loop_param:
                                params[pn] = item
                            elif isinstance(pv, str) and pv.strip() == "$ASK":
                                params[pn] = ask_values.get(pn, "")
                            else:
                                params[pn] = _resolve_input_value(pv, ask_values, context, plan_outputs)
                        iterations.append(params)
            if not iterations:
                # Single run
                params = {}
                for pn, pv in step["inputs"].items():
                    if isinstance(pv, str) and pv.strip() == "$ASK":
                        params[pn] = ask_values.get(pn, "")
                    else:
                        params[pn] = _resolve_input_value(pv, ask_values, context, plan_outputs)
                iterations = [params]

            await cl.Message(
                content=f"▶️ **Step {i}/{len(plan)}**: `{skill_name}` × {len(iterations)} run(s)"
            ).send()
            await emit_dashboard_update(skill_name, {}, status="loading")

            last_outputs: dict = {}
            for j, params in enumerate(iterations, 1):
                if len(iterations) > 1:
                    await cl.Message(content=f"  ↳ {j}/{len(iterations)}: {params or 'no inputs'}").send()
                _run_id = log_start(skill_name, display_name=skill.get("name", skill_name), params=params)
                _status, _err = "error", None
                try:
                    result = await run_skill(skill, params, _headless_ask_user)
                    _status = "stopped" if result.get("stopped") else "success"
                    outputs = await process_skill_outputs(skill_name, skill, result, stream=False)
                    last_outputs = outputs or last_outputs
                    if outputs:
                        # Key per-item results so later steps can reference them
                        mem_key = f"{skill_name}_{list(params.values())[0]}" if len(iterations) > 1 and params else skill_name
                        save_result(mem_key, outputs)
                        save_result(skill_name, outputs)
                        context.update(outputs)
                except Exception as exc:
                    _err = str(exc)
                    await cl.Message(content=f"  ⚠️ Run failed: {exc}").send()
                finally:
                    log_end(_run_id, status=_status, outputs=last_outputs, error=_err)

            plan_outputs[skill_name] = last_outputs
            await emit_dashboard_update(
                skill_name, last_outputs, status="" if last_outputs else "no-output"
            )

        cl.user_session.set("skill_context", context)
        await cl.Message(content="✅ **Plan complete.**").send()
    finally:
        cl.user_session.set("skill_running", False)

    await emit_kpis()
    skills = cl.user_session.get("skills") or []
    reset_chat_history(skills)


async def run_selected_skill(skill_name: str, inline_hint: str = "") -> None:
    skill = load_skill(skill_name)

    # Batch skills have no browser steps — delegate to run_batch_skill.
    if skill.get("type") == "batch":
        cl.user_session.set("skill_running", True)
        try:
            await run_batch_skill(skill)
        finally:
            cl.user_session.set("skill_running", False)
        return

    # --- Collect inputs upfront via chat ---
    inputs_spec = skill_inputs(skill)
    context = cl.user_session.get("skill_context") or {}
    params: dict[str, str] = {}

    # Auto-params: steps with param_name but human_in_the_loop=False.
    # Seed from recorded defaults first, then override with inline_hint if provided.
    auto_params = skill_auto_params(skill)
    for ap in auto_params:
        if ap.get("default"):
            params[ap["name"]] = ap["default"]
    if inline_hint and auto_params:
        extracted = _extract_auto_params_from_hint(inline_hint, auto_params)
        params.update(extracted)

    if inputs_spec:
        for inp in inputs_spec:
            name = inp["name"]
            # Skip params already resolved from inline hint or context
            if name in params:
                continue
            label = inp.get("label", name.replace("_", " ").title())
            prefill = context.get(name, inp.get("default", ""))
            hint_note = f" _(suggested: `{prefill}`)_" if prefill else ""
            cl.user_session.set("awaiting_manual_input", True)
            try:
                res = await cl.AskUserMessage(
                    content=f"**{label}**{hint_note}\nEnter value (or press Send to use suggested):",
                    timeout=300,
                ).send()
            finally:
                cl.user_session.set("awaiting_manual_input", False)
            if not res:
                await cl.Message(content="Skill cancelled — no input provided.").send()
                return
            raw = res.get("output", "").strip()
            params[name] = raw if raw else prefill
    else:
        # No declared human-in-the-loop inputs.
        # If params are already populated (from defaults or inline hint), run directly.
        if not params and not inline_hint:
            pass  # No inputs needed — run with empty params
        elif not params and inline_hint:
            params["input"] = inline_hint

    # Also seed any remaining params from skill defaults / context
    for pname, default_val in skill_parameters(skill).items():
        if pname not in params:
            params[pname] = context.get(pname, default_val)

    cl.user_session.set("skill_running", True)
    try:
        # Show what params will be used before launching the browser
        if params:
            param_lines = "\n".join(f"  `{k}` = **{v}**" for k, v in params.items())
            await cl.Message(content=f"🔑 **Parameters resolved:**\n{param_lines}").send()
        await cl.Message(content=f"▶️ Running skill `{skill_name}` with: {params or 'no inputs'}").send()

        # ── Audit: record start ──
        _run_id = log_start(skill_name, display_name=skill.get("name", skill_name), params=params)

        _run_status = "error"
        _run_error: str | None = None
        try:
            result = await run_skill(skill, params, ask_user)
            _run_status = "stopped" if result.get("stopped") else "success"
        except Exception as _exc:
            _run_error = str(_exc)
            raise
        finally:
            # Determine final status before we exit this inner try
            pass

        await cl.Message(content="✅ Skill finished.").send()

        all_outputs = await process_skill_outputs(skill_name, skill, result, stream=True)

        # ── Audit: record completion ──
        log_end(_run_id, status=_run_status, outputs=all_outputs, error=_run_error)

        if all_outputs:
            context.update(all_outputs)
            cl.user_session.set("skill_context", context)
            save_result(skill_name, all_outputs)
            await emit_dashboard_update(skill_name, all_outputs)
            await emit_kpis()

    finally:
        cl.user_session.set("skill_running", False)


async def run_recording_job(skill_name: str, start_url: str, stop_event: threading.Event) -> None:
    try:
        path = await asyncio.to_thread(record_skill, skill_name, start_url, stop_event)
        skills = list_skills()
        cl.user_session.set("skills", skills)
        reset_chat_history(skills)
        await cl.Message(content=f"✅ Saved skill: `{skill_name}` → `{path}`").send()

        # --- Interactively declare outputs ---
        await cl.Message(
            content=(
                "**Declare outputs** (optional) — tell the app what values to capture from the final page.\n\n"
                "One declaration per line, two formats:\n"
                "  `selector as output_name` — read text from a DOM element\n"
                "  `clipboard as output_name` — read from clipboard (e.g. after clicking a Copy button)\n\n"
                "Examples:\n"
                "  `#cav-id-cell as cav_id`\n"
                "  `clipboard as cav_id`\n"
                "  `.deal-status as deal_status`\n\n"
                "Type `done` or press Send empty to skip."
            )
        ).send()

        cl.user_session.set("awaiting_manual_input", True)
        try:
            res = await cl.AskUserMessage(
                content="Enter output declarations (or Send to skip):",
                timeout=120,
            ).send()
        finally:
            cl.user_session.set("awaiting_manual_input", False)

        raw_outputs = (res.get("output", "") if res else "").strip()
        if raw_outputs and raw_outputs.lower() != "done":
            outputs = []
            for line in raw_outputs.splitlines():
                line = line.strip()
                if " as " in line:
                    lhs, _, out_name = line.partition(" as ")
                    lhs = lhs.strip()
                    out_name = out_name.strip().lower().replace(" ", "_")
                    if lhs and out_name:
                        if lhs.lower() == "clipboard":
                            outputs.append({"name": out_name, "type": "clipboard", "label": out_name.replace("_", " ").title()})
                        else:
                            outputs.append({"name": out_name, "type": "selector", "selector": lhs, "label": out_name.replace("_", " ").title()})
            if outputs:
                # Patch the saved skill file with outputs
                skill_file = path
                with open(skill_file) as f:
                    skill_data = json.load(f)
                skill_data["outputs"] = outputs
                with open(skill_file, "w") as f:
                    json.dump(skill_data, f, indent=2)
                out_names = ", ".join(f"`{o['name']}` ({o.get('type','selector')})" for o in outputs)
                await cl.Message(content=f"📤 Declared outputs: {out_names}").send()

        if skills:
            skill_list = ", ".join(sorted(skills))
            await cl.Message(content=f"Saved skills: {skill_list}").send()

        await show_execute(skills)
    except Exception as exc:
        await cl.Message(content=f"❌ Recording failed: {exc}").send()
    finally:
        cl.user_session.set("recording_running", False)
        cl.user_session.set("recording_stop_event", None)
        cl.user_session.set("recording_task", None)


async def stream_assistant_reply(history: list[dict]) -> str:
    reply_message = cl.Message(content="")
    await reply_message.send()

    parts: list[str] = []
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

    def worker() -> None:
        try:
            for chunk in stream_chat_completion(history):
                asyncio.run_coroutine_threadsafe(queue.put(("token", chunk)), loop)
        except Exception as exc:  # noqa: BLE001 - forward provider errors back to the UI
            asyncio.run_coroutine_threadsafe(queue.put(("error", str(exc))), loop)
        finally:
            asyncio.run_coroutine_threadsafe(queue.put(("done", "")), loop)

    threading.Thread(target=worker, daemon=True).start()

    while True:
        event_type, payload = await queue.get()
        if event_type == "token":
            parts.append(payload)
            await reply_message.stream_token(payload)
            continue
        if event_type == "error":
            raise RuntimeError(payload)
        break

    await reply_message.update()
    return "".join(parts)


# ---------- welcome / skill picker ----------
@cl.on_chat_start
async def start():
    skills = list_skills()
    record_urls = load_record_urls()
    reset_chat_history(skills)
    cl.user_session.set("skills", skills)
    cl.user_session.set("record_urls", record_urls)
    cl.user_session.set("awaiting_manual_input", False)
    cl.user_session.set("skill_running", False)
    cl.user_session.set("recording_running", False)
    cl.user_session.set("recording_stop_event", None)
    cl.user_session.set("recording_task", None)
    cl.user_session.set("mode", "home")
    cl.user_session.set("current_skills", skills)
    cl.user_session.set("skill_context", {})

    # Show initial skills list for sidebar to parse
    if skills:
        skill_list = ", ".join(sorted(skills))
        await cl.Message(content=f"Saved skills: {skill_list}").send()

    await show_home(skills)

    # Show KPI strip immediately from any cached data, then refresh after startup skills.
    await emit_kpis()

    # Run startup skills in the background so the UI is responsive immediately.
    asyncio.create_task(run_startup_skills())


@cl.action_callback("enter_chat")
async def on_enter_chat(action: cl.Action):
    await set_mode("chat")


@cl.action_callback("show_execute")
async def on_show_execute(action: cl.Action):
    await set_mode("execute")


@cl.action_callback("show_home")
async def on_show_home(action: cl.Action):
    await set_mode("home")


@cl.action_callback("start_learning")
async def on_start_learning(action: cl.Action):
    if cl.user_session.get("skill_running"):
        await cl.Message(content="A skill is running. Stop or finish it before starting recording.").send()
        return
    if cl.user_session.get("recording_running"):
        await cl.Message(content="Recording is already active. Use **Stop Recording** first.").send()
        return

    cl.user_session.set("mode", "learn")
    cl.user_session.set("awaiting_manual_input", True)
    cl.user_session.set("learning_step", "skill_name")
    
    try:
        name_res = await cl.AskUserMessage(
            content="Enter a name for the new skill (letters, numbers, underscores):",
            timeout=180,
        ).send()
        if not name_res or not str(name_res.get("output", "")).strip():
            await cl.Message(content="Recording cancelled. No skill name provided.").send()
            return
        raw_name = str(name_res["output"]).strip()
        skill_name = normalize_skill_name(raw_name)
        if not skill_name:
            await cl.Message(content="Recording cancelled. The skill name was invalid.").send()
            return

        # Store skill name for later use
        cl.user_session.set("learning_skill_name", skill_name)
        
        # Show common URLs for reference
        common_urls = load_common_urls()
        if common_urls:
            url_list = "\n".join([f"  **{name}**: {url}" for name, url in common_urls.items()])
            await cl.Message(
                content=(
                    f"Common URLs:\n{url_list}\n\n"
                    "Now, **enter the URL** where you want to start recording.\n"
                    "You can paste a URL from above or provide your own."
                )
            ).send()
        else:
            await cl.Message(content="Now, **enter the URL** where you want to start recording.").send()
        
        cl.user_session.set("learning_step", "url")
        cl.user_session.set("awaiting_manual_input", False)
        
    except Exception as e:
        cl.user_session.set("awaiting_manual_input", False)
        await cl.Message(content=f"Error during recording setup: {e}").send()


@cl.action_callback("stop_learning")
async def on_stop_learning(action: cl.Action):
    if not cl.user_session.get("recording_running"):
        await cl.Message(content="No active recording to stop.").send()
        return

    stop_event = cl.user_session.get("recording_stop_event")
    if stop_event:
        stop_event.set()
    await cl.Message(content="Stopping recording and saving the skill...").send()


@cl.on_message
async def on_message(message: cl.Message):
    if cl.user_session.get("awaiting_manual_input"):
        return

    content = (message.content or "").strip()
    if not content:
        return
    lowered_content = content.lower()

    if cl.user_session.get("skill_running"):
        await cl.Message(content="A skill is currently running. Use the step controls or stop that run first.").send()
        return
    if cl.user_session.get("recording_running"):
        if lowered_content in {"stop", "stop recording", "finish recording", "done recording", "done"}:
            stop_event = cl.user_session.get("recording_stop_event")
            if stop_event:
                stop_event.set()
            await cl.Message(content="Stopping recording and saving the skill...").send()
            return
        await cl.Message(content="Recording is in progress. Type `stop recording` to finish saving the skill.").send()
        return

    # Handle URL input during skill recording setup
    learning_step = cl.user_session.get("learning_step")
    if learning_step == "url":
        skill_name = cl.user_session.get("learning_skill_name")
        selected_url = content.strip()
        
        # Check if the input matches a common_urls shorthand (case-insensitive)
        common_urls = load_common_urls()
        for url_name, url_value in common_urls.items():
            if selected_url.lower() == url_name.lower():
                selected_url = url_value
                break
        else:
            # Not a shorthand, validate and normalize the URL
            if not selected_url.startswith(("http://", "https://")):
                # Check if it looks like a domain (has a dot) or is clearly invalid
                if "." not in selected_url:
                    await cl.Message(
                        content=(
                            f"❌ Invalid URL: `{selected_url}` does not look like a valid domain.\n\n"
                            "Either:\n"
                            f"- Use a shorthand: {', '.join(common_urls.keys())}\n"
                            "- Provide a full URL like `https://example.com`\n"
                            "- Or try again with a domain that has a period (e.g., `google.com`)"
                        )
                    ).send()
                    cl.user_session.set("learning_step", "url")
                    return
                selected_url = "https://" + selected_url
        
        cl.user_session.set("learning_step", None)
        
        # Start recording
        stop_event = threading.Event()
        cl.user_session.set("recording_running", True)
        cl.user_session.set("recording_stop_event", stop_event)
        await cl.Message(
            content=(
                f"Recording started for `{skill_name}` at `{selected_url}`.\n"
                "Use the browser window to perform the flow, then type `stop recording` in chat."
            ),
        ).send()

        task = asyncio.create_task(run_recording_job(skill_name, selected_url, stop_event))
        cl.user_session.set("recording_task", task)
        return

    skills = cl.user_session.get("skills") or []

    if content == "/reset":
        reset_chat_history(skills)
        await cl.Message(content="Chat history cleared.").send()
        return

    if content in {"/context", "show context", "list context"}:
        context = cl.user_session.get("skill_context") or {}
        if context:
            lines = "\n".join(f"- **{k}**: {_display_value(v)}" for k, v in context.items())
            await cl.Message(content=f"**Current skill context:**\n{lines}").send()
        else:
            await cl.Message(content="No context values stored yet. Run a skill to populate context.").send()
        return

    if content in {"/memory", "show memory", "list memory"}:
        results = list_results()
        if not results:
            await cl.Message(content="No results in memory yet.").send()
        else:
            lines = []
            for r in results:
                age = age_description(r["skill_name"])
                stale = " ⚠️ stale" if r["stale"] else ""
                keys = ", ".join(r["output_keys"]) or "—"
                lines.append(f"- **{r['skill_name']}** — {age}{stale} — keys: `{keys}`")
            await cl.Message(
                content="**Stored memory results:**\n" + "\n".join(lines)
            ).send()
        return

    if content == "/memory clear":
        from pathlib import Path as _Path
        import glob as _glob
        removed = []
        for f in _glob.glob("memory/*.json"):
            _Path(f).unlink(missing_ok=True)
            removed.append(_Path(f).stem)
        await cl.Message(content=f"🗑️ Cleared memory for: {', '.join(removed) or 'nothing'}").send()
        return

    if content == "/clear context":
        cl.user_session.set("skill_context", {})
        await cl.Message(content="✅ Skill context cleared.").send()
        return

    if content.startswith("/audit"):
        # /audit [skill_name] [N]   — show last N executions (default 10)
        parts = content.split()
        skill_filter: str | None = None
        limit = 10
        if len(parts) >= 2:
            # Second token: either a skill name or a number
            if parts[1].isdigit():
                limit = int(parts[1])
            else:
                skill_filter = parts[1]
                if len(parts) >= 3 and parts[2].isdigit():
                    limit = int(parts[2])
        runs = recent_runs(limit=limit, skill_name=skill_filter)
        header = f"**Audit log** ({len(runs)} most recent{f' for `{skill_filter}`' if skill_filter else ''})\n\n"
        await cl.Message(content=header + format_audit_summary(runs)).send()
        return

    # ── Goal-driven multi-skill planning: /goal <objective> or /plan <objective> ──
    if content.lower().startswith(("/goal ", "/plan ")):
        goal_text = content.split(" ", 1)[1].strip()
        if not goal_text:
            await cl.Message(content="Usage: `/goal <what you want to achieve>`").send()
            return
        await cl.Message(content=f"🧠 Planning how to achieve: _{goal_text}_ …").send()
        catalog = skill_catalog()
        plan = await asyncio.to_thread(plan_goal, goal_text, catalog)
        await execute_plan(goal_text, plan)
        return

    # Check if user is confirming delete (do this FIRST, before any other checks)
    awaiting_delete = cl.user_session.get("awaiting_delete_confirmation")
    target_skill = cl.user_session.get("delete_target_skill")
    if awaiting_delete and target_skill:
        if content.lower() in {"yes", "y", "confirm", "ok", "true"}:
            try:
                delete_skill(target_skill)
                skills = list_skills()
                cl.user_session.set("skills", skills)
                reset_chat_history(skills)
                await cl.Message(content=f"✅ Skill `{target_skill}` has been deleted.").send()
            except Exception as exc:
                await cl.Message(content=f"❌ Failed to delete: {exc}").send()
        else:
            await cl.Message(content="Deletion cancelled.").send()
        
        cl.user_session.set("awaiting_delete_confirmation", False)
        cl.user_session.set("delete_target_skill", None)
        return

    # Check for list skills request
    if is_list_skills_request(content):
        if skills:
            skill_list = ", ".join(sorted(skills))
            await cl.Message(content=f"**Saved skills:** {skill_list}").send()
        else:
            await cl.Message(content="No skills saved yet. Use \"add skill\" or \"learn new skill\" to record one.").send()
        return

    # Check for add/learn skill request
    if is_add_skill_request(content):
        await on_start_learning(cl.Action(name="start_learning", payload={"mode": "learn"}, label="Learn"))
        return

    # Check for delete skill request
    if is_delete_skill_request(content):
        # First time delete request - try to extract skill name
        target = extract_delete_target(content, skills)
        if not target:
            if skills:
                skill_list = ", ".join(sorted(skills))
                await cl.Message(content=f"Which skill to delete? Available: {skill_list}\n\nSay: \"delete <skill_name>\"").send()
            else:
                await cl.Message(content="No skills to delete.").send()
            return
        
        # Ask for confirmation
        cl.user_session.set("awaiting_delete_confirmation", True)
        cl.user_session.set("delete_target_skill", target)
        await cl.Message(
            content=f"⚠️ Are you sure you want to permanently delete the skill `{target}`? Reply with **yes** or **no** to confirm."
        ).send()
        return

    # Try to match run request — fuzzy name match first
    run_match = extract_skill_request(content, skills)
    if run_match:
        skill_name, inline_hint = run_match
        cl.user_session.set("mode", "execute")
        await run_selected_skill(skill_name, inline_hint=inline_hint)
        return

    # ── LLM intent fallback: natural-language requests with no explicit skill name ──
    # Only trigger when the message looks like a task request (not a question/chat).
    _task_keywords = {
        "search", "find", "look up", "look for", "get", "fetch", "update", "change",
        "set", "run", "execute", "show", "retrieve", "open", "create", "review",
    }
    if any(kw in lowered_content for kw in _task_keywords):
        catalog = skill_catalog()
        intent_match = select_skill_by_intent(content, catalog)
        if intent_match:
            matched_name = intent_match["file_name"]
            matched_display = intent_match["display_name"]
            confirm_msg = await cl.AskActionMessage(
                content=(
                    f"🤔 I think you want to run **{matched_display}**.\n"
                    f"_{intent_match.get('description', '')}_"
                ),
                actions=[
                    cl.Action(name="yes", value="yes", label="✅ Yes, run it"),
                    cl.Action(name="no", value="no", label="❌ No, just chat"),
                ],
                timeout=60,
            ).send()
            if confirm_msg and confirm_msg.get("value") == "yes":
                cl.user_session.set("mode", "execute")
                await run_selected_skill(matched_name, inline_hint="")
                return

    # /skill_name [hint] — direct slash shortcut for any existing skill.
    # e.g. "/cxaia_did_overview 73595369" or "/sfdc_my_atr"
    if content.startswith("/"):
        rest = content[1:].strip()
        words = rest.split() if rest else []
        if words:
            shortcut: tuple[str, str] | None = None
            # Try progressively shorter prefixes to find the longest matching skill name.
            for end in range(len(words), 0, -1):
                candidate = normalize_skill_name(" ".join(words[:end]))
                for s in skills:
                    if normalize_skill_name(s) == candidate:
                        shortcut = (s, " ".join(words[end:]).strip())
                        break
                if shortcut:
                    break
            if shortcut:
                skill_name, inline_hint = shortcut
                cl.user_session.set("mode", "execute")
                await run_selected_skill(skill_name, inline_hint=inline_hint)
                return

    history = cl.user_session.get("chat_history") or []
    history.append({"role": "user", "content": content})

    try:
        reply = await stream_assistant_reply(history)
    except Exception as exc:  # noqa: BLE001 - surface runtime failure to the user
        history.pop()
        cl.user_session.set("chat_history", history)
        await cl.Message(content=f"LLM error: {exc}").send()
        return

    history.append({"role": "assistant", "content": reply})
    cl.user_session.set("chat_history", history)


# ---------- when a skill is picked ----------
@cl.action_callback("pick_skill")
async def on_pick_skill(action: cl.Action):
    skill_name = action.payload["skill"]
    cl.user_session.set("mode", "execute")
    await run_selected_skill(skill_name)


# ---------- error-recovery callback for skill replay ----------
async def ask_user(ctx: dict) -> dict:
    """Called only when a step raises an exception during replay."""
    if not ctx.get("error"):
        return {"action": "approve"}
    res = await cl.AskActionMessage(
        content=f"⚠️ **Step {ctx.get('index', 0) + 1} failed:** {ctx['error']}\n\nWhat would you like to do?",
        actions=[
            cl.Action(name="approve", label="🔁 Retry", payload={"action": "approve"}),
            cl.Action(name="skip", label="⏭️ Skip", payload={"action": "skip"}),
            cl.Action(name="stop", label="🛑 Stop", payload={"action": "stop"}),
        ],
    ).send()
    return {"action": res["payload"]["action"]} if res else {"action": "stop"}