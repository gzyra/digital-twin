"""Load environment variables, then run digital-twin.

Default command:
    python initapp.py

Custom command:
    python initapp.py -- python recorder.py my_task
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import certifi


# Keep macOS SSL behavior consistent with greg_personal_assistant/initapp.py.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_JSON = Path.home() / ".env" / "greg_ai_env.json"


def _pick_first_non_empty(data: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _load_env_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"Environment file not found: {path}. "
            "Create it first, e.g. ~/.env/greg_ai_env.json"
        )
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_runtime_env(env_json_path: Path) -> None:
    cfg = _load_env_json(env_json_path)

    llm = cfg.get("llm", {}) if isinstance(cfg.get("llm"), dict) else {}
    llm_tools = cfg.get("llm_tools", {}) if isinstance(cfg.get("llm_tools"), dict) else {}

    bridgeit_llm = llm.get("bridgeit", {}) if isinstance(llm.get("bridgeit"), dict) else {}
    bridgeit_tools = (
        llm_tools.get("bridgeit", {}) if isinstance(llm_tools.get("bridgeit"), dict) else {}
    )

    merged_bridgeit = {**bridgeit_tools, **bridgeit_llm}

    mapped_env = {
        "BRIDGEIT_CLIENT_ID": _pick_first_non_empty(
            merged_bridgeit, ("client_id", "clientId")
        ),
        "BRIDGEIT_CLIENT_SEC": _pick_first_non_empty(
            merged_bridgeit, ("client_sec", "client_secret", "clientSecret")
        ),
        "BRIDGEIT_KEY": _pick_first_non_empty(
            merged_bridgeit, ("api_key", "app_key", "key")
        ),
        "BRIDGEIT_USERID": _pick_first_non_empty(
            merged_bridgeit, ("userid", "user_id", "user")
        ),
        "BRIDGEIT_MODEL": _pick_first_non_empty(merged_bridgeit, ("model",)),
    }

    for env_name, env_value in mapped_env.items():
        if env_value:
            os.environ[env_name] = env_value

    os.environ.setdefault("DIGITAL_TWIN_CONFIG", str(PROJECT_DIR / "config.yaml"))
    os.environ.setdefault("CHAINLIT_LANGUAGE", "en-US")


def build_command(argv: list[str]) -> list[str]:
    if "--" in argv:
        idx = argv.index("--")
        custom = argv[idx + 1 :]
        if custom:
            return custom
    return [sys.executable, "-m", "chainlit", "run", "app.py", "-w"]


def main() -> int:
    try:
        load_runtime_env(DEFAULT_ENV_JSON)
    except Exception as exc:  # noqa: BLE001 - explicit startup feedback
        print(f"Failed to load environment variables: {exc}", file=sys.stderr)
        return 1

    cmd = build_command(sys.argv[1:])
    proc = subprocess.run(cmd, cwd=PROJECT_DIR)
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
