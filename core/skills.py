import json
import os
from datetime import datetime, timezone

from core.config import get_config

_CFG = get_config()

SKILLS_DIR = _CFG["skills_dir"]
os.makedirs(SKILLS_DIR, exist_ok=True)


def save_skill(name: str, steps: list[dict], inputs: list[dict] | None = None, outputs: list[dict] | None = None, auth_state: str | None = None) -> str:
    skill = {
        "name": name,
        "created": datetime.now(timezone.utc).isoformat(),
        "steps": steps,
    }
    if auth_state:
        skill["auth_state"] = auth_state
    if inputs:
        skill["inputs"] = inputs
    if outputs:
        skill["outputs"] = outputs
    path = os.path.join(SKILLS_DIR, f"{name}.json")
    with open(path, "w") as f:
        json.dump(skill, f, indent=2)
    return path


def load_skill(name: str) -> dict:
    with open(os.path.join(SKILLS_DIR, f"{name}.json")) as f:
        return json.load(f)


def list_skills() -> list[str]:
    return [f[:-5] for f in os.listdir(SKILLS_DIR) if f.endswith(".json")]


def skill_parameters(skill: dict) -> dict[str, str]:
    """Return {param_name: default_value} for all parameter_input steps."""
    params = {}
    for step in skill.get("steps", []):
        if step.get("action") == "parameter_input" and step.get("param_name"):
            params[step["param_name"]] = step.get("value", "")
    return params


def skill_inputs(skill: dict) -> list[dict]:
    """Return the declared inputs list, falling back to deriving from parameter_input steps."""
    if "inputs" in skill:
        return skill["inputs"]
    # Derive from parameter_input steps (deduplicated, preserve order)
    seen: set[str] = set()
    inputs = []
    for step in skill.get("steps", []):
        if step.get("action") == "parameter_input":
            pname = step.get("param_name")
            if pname and pname not in seen:
                seen.add(pname)
                inputs.append({
                    "name": pname,
                    "label": step.get("param_description", pname.replace("_", " ").title()),
                    "default": step.get("value", ""),
                })
    return inputs


def skill_auto_params(skill: dict) -> list[dict]:
    """Return param info for all parameter_input steps (used for auto-fill from inline hints)."""
    seen: set[str] = set()
    result = []
    for step in skill.get("steps", []):
        if step.get("action") == "parameter_input":
            pname = step.get("param_name")
            if pname and pname not in seen:
                seen.add(pname)
                result.append({
                    "name": pname,
                    "description": step.get("param_description", pname.replace("_", " ")),
                    "default": step.get("value", ""),
                    "template": step.get("template"),
                })
    return result


def skill_outputs(skill: dict) -> list[dict]:
    """Return the declared outputs list (may be empty for skills without output config)."""
    return skill.get("outputs", [])


def skill_description(skill: dict) -> str:
    """Return the human/LLM-readable description for a skill, falling back to its name."""
    return skill.get("description", skill.get("name", ""))


def skill_catalog() -> list[dict]:
    """Return enriched metadata for all skills: name, description, inputs, outputs.

    The outputs list exposes every named field a skill can produce so the LLM
    can suggest running a skill when the user asks for data the skill provides.

    Routing metadata for the planner:
      - ``requires``   — parameter names the skill needs (declared or derived from inputs)
      - ``provides``   — output field names the skill yields (declared or derived from outputs)
      - ``goal_tags``  — free-text intents the skill satisfies (optional)
      - ``system``     — target system label (e.g. cxaia, salesforce), optional
    """
    catalog = []
    for file_name in list_skills():
        try:
            skill = load_skill(file_name)
            inputs = skill_inputs(skill)
            input_names = [i["name"] for i in inputs] if inputs else []
            # Collect output field names and descriptions from parsed outputs
            output_fields = []
            for out in skill.get("outputs", []):
                if out.get("parse") and out.get("fields"):
                    for f in out["fields"]:
                        output_fields.append({
                            "name": f["name"],
                            "description": f.get("description", ""),
                        })
                else:
                    output_fields.append({
                        "name": out["name"],
                        "description": out.get("label", ""),
                    })
            provides = skill.get("provides") or [o["name"] for o in output_fields]
            requires = skill.get("requires") or input_names
            catalog.append({
                "file_name": file_name,
                "display_name": skill.get("name", file_name),
                "description": skill.get("description", ""),
                "inputs": input_names,
                "outputs": output_fields,
                "requires": requires,
                "provides": provides,
                "goal_tags": skill.get("goal_tags", []),
                "system": skill.get("system", ""),
            })
        except Exception as exc:
            import logging
            logging.warning("[skills] could not load skill %r: %s", file_name, exc)
    return catalog


def routing_table(catalog: list[dict] | None = None) -> str:
    """Build a compact, LLM-friendly routing table from the skill catalog.

    Each line tells the model which skill to use for a given need, what it
    requires as input, and what data it provides — so the assistant can pick
    the right skill and chain skills whose ``provides`` feed another's
    ``requires``. Generated from skill metadata so it stays in sync as skills
    are added manually.
    """
    catalog = catalog if catalog is not None else skill_catalog()
    if not catalog:
        return "No skills available."

    lines: list[str] = []
    for e in catalog:
        desc = e.get("description") or e.get("display_name")
        requires = ", ".join(e.get("requires") or []) or "nothing"
        provides = ", ".join(e.get("provides") or []) or "—"
        tags = ", ".join(e.get("goal_tags") or [])
        line = f"- `{e['file_name']}` — {desc}\n    · needs: {requires}\n    · gives: {provides}"
        if tags:
            line += f"\n    · use when: {tags}"
        lines.append(line)
    return "\n".join(lines)


def delete_skill(name: str) -> None:
    path = os.path.join(SKILLS_DIR, f"{name}.json")
    if os.path.exists(path):
        os.remove(path)