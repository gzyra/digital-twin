import base64
import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import litellm
import requests
import yaml
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import AzureChatOpenAI

_TOKEN_BUFFER_SECS = 60
_TOKEN_FETCH_RETRIES = 3
_BRIDGEIT_TOKEN_CACHE: dict = {"token": None, "expires_at": 0.0}
_BRIDGEIT_TOKEN_LOCK = threading.Lock()


def _load_cfg() -> dict:
    cfg_path = Path(os.getenv("DIGITAL_TWIN_CONFIG", "config.yaml"))
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"Missing config file: {cfg_path}. "
            "Create it from config.yaml.example."
        )
    with cfg_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


_CFG = _load_cfg()
MODEL = _CFG.get("model", "gpt-4o-mini")
LLM_PROVIDER = str(_CFG.get("llm_provider", "litellm")).lower()


def _env_or_cfg(cfg: dict, cfg_key: str, env_key: str) -> str | None:
    value = os.getenv(env_key)
    if value:
        return value
    return cfg.get(cfg_key)


def _bridgeit_token_is_stale() -> bool:
    if not _BRIDGEIT_TOKEN_CACHE["token"]:
        return True
    return time.monotonic() >= _BRIDGEIT_TOKEN_CACHE["expires_at"]


def _get_bridgeit_token(circuit_cfg: dict) -> str:
    if not _bridgeit_token_is_stale():
        return _BRIDGEIT_TOKEN_CACHE["token"]

    with _BRIDGEIT_TOKEN_LOCK:
        if not _bridgeit_token_is_stale():
            return _BRIDGEIT_TOKEN_CACHE["token"]

        client_id = _env_or_cfg(circuit_cfg, "client_id", "BRIDGEIT_CLIENT_ID")
        client_secret = _env_or_cfg(circuit_cfg, "client_secret", "BRIDGEIT_CLIENT_SEC")
        if not client_id or not client_secret:
            raise ValueError(
                "Missing BridgeIT credentials. Set BRIDGEIT_CLIENT_ID and "
                "BRIDGEIT_CLIENT_SEC or define circuit.client_id/circuit.client_secret in config.yaml."
            )

        url = "https://id.cisco.com/oauth2/default/v1/token"
        payload = "grant_type=client_credentials"
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("utf-8")
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
        }

        last_exc: Exception | None = None
        for attempt in range(_TOKEN_FETCH_RETRIES):
            try:
                resp = requests.post(url, headers=headers, data=payload, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                access_token = data.get("access_token")
                expires_in = int(data.get("expires_in", 3600))
                if not access_token:
                    raise ValueError("BridgeIT token response is missing access_token")
                _BRIDGEIT_TOKEN_CACHE["token"] = access_token
                _BRIDGEIT_TOKEN_CACHE["expires_at"] = (
                    time.monotonic() + expires_in - _TOKEN_BUFFER_SECS
                )
                return access_token
            except Exception as exc:  # noqa: BLE001 - keep retry logic broad
                last_exc = exc
                if attempt < _TOKEN_FETCH_RETRIES - 1:
                    time.sleep(2**attempt)

        raise RuntimeError(
            f"Failed to fetch BridgeIT token after {_TOKEN_FETCH_RETRIES} attempts: {last_exc}"
        )


def _build_circuit_model(*, json_response: bool = False) -> AzureChatOpenAI:
    circuit_cfg = _CFG.get("circuit", {})
    access_token = _get_bridgeit_token(circuit_cfg)
    app_key = _env_or_cfg(circuit_cfg, "app_key", "BRIDGEIT_KEY")
    user_id = _env_or_cfg(circuit_cfg, "user_id", "BRIDGEIT_USERID")
    if not app_key:
        raise ValueError(
            "Missing BridgeIT app key. Set BRIDGEIT_KEY or circuit.app_key in config.yaml."
        )
    if not user_id:
        user_id = "digital-twin"

    endpoint = circuit_cfg.get("endpoint", "https://chat-ai.cisco.com")
    api_version = circuit_cfg.get("api_version", "2024-12-01-preview")
    model_name = circuit_cfg.get("model", MODEL)

    model_kwargs = {
        "user": f'{{"appkey": "{app_key}", "user": "{user_id}"}}',
    }
    if json_response:
        model_kwargs["response_format"] = {"type": "json_object"}

    return AzureChatOpenAI(
        model=model_name,
        azure_endpoint=endpoint,
        api_version=api_version,
        openai_api_key=access_token,
        model_kwargs=model_kwargs,
    )


def _to_langchain_messages(messages: list[dict]) -> list[SystemMessage | HumanMessage | AIMessage]:
    converted: list[SystemMessage | HumanMessage | AIMessage] = []
    for message in messages:
        role = message.get("role")
        content = str(message.get("content", ""))
        if role == "system":
            converted.append(SystemMessage(content=content))
        elif role == "assistant":
            converted.append(AIMessage(content=content))
        else:
            converted.append(HumanMessage(content=content))
    return converted


def chat_completion(messages: list[dict]) -> str:
    """Return a plain-text chat completion for the supplied conversation."""
    try:
        if LLM_PROVIDER == "circuit":
            model = _build_circuit_model(json_response=False)
            resp = model.invoke(_to_langchain_messages(messages))
            return str(resp.content)

        resp = litellm.completion(model=MODEL, messages=messages)
        return str(resp.choices[0].message.content)
    except Exception as e:
        logging.exception("[llm] chat completion failed")
        raise RuntimeError(f"LLM request failed: {e}") from e


def stream_chat_completion(messages: list[dict]) -> Iterator[str]:
    """Yield plain-text chat completion chunks for the supplied conversation."""
    try:
        if LLM_PROVIDER == "circuit":
            model = _build_circuit_model(json_response=False)
            for chunk in model.stream(_to_langchain_messages(messages)):
                content = getattr(chunk, "content", "")
                if isinstance(content, str) and content:
                    yield content
            return

        resp = litellm.completion(model=MODEL, messages=messages, stream=True)
        for chunk in resp:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
    except Exception as e:
        logging.exception("[llm] streaming chat completion failed")
        raise RuntimeError(f"LLM request failed: {e}") from e


def _extract_steps_from_response(content: str) -> list[dict] | None:
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict) and isinstance(parsed.get("steps"), list):
            return parsed["steps"]
    except json.JSONDecodeError:
        pass

    start = content.find("{")
    end = content.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(content[start : end + 1])
            if isinstance(parsed, dict) and isinstance(parsed.get("steps"), list):
                return parsed["steps"]
        except json.JSONDecodeError:
            return None
    return None


def detect_parameters(steps: list[dict]) -> list[dict]:
    """Use the LLM to mark which input values are user-supplied parameters."""
    prompt = f"""You are labeling a recorded browser flow.
For each step with an input value, decide if the value is a user-supplied
PARAMETER (search term, id, date, name) or a FIXED value.
Add "human_in_the_loop" (bool) and, if true, a snake_case "param_name".
Return ONLY a JSON object: {{"steps": [...]}} preserving all original fields.

Steps:
{json.dumps(steps, indent=2)}"""

    def _with_default_flags() -> list[dict]:
        for step in steps:
            step.setdefault("human_in_the_loop", False)
        return steps

    try:
        if LLM_PROVIDER == "circuit":
            model = _build_circuit_model(json_response=True)
            resp = model.invoke([HumanMessage(content=prompt)])
            parsed_steps = _extract_steps_from_response(resp.content)
            if parsed_steps is None:
                raise ValueError("Circuit response could not be parsed as JSON object with steps")
            return parsed_steps

        resp = litellm.completion(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        parsed_steps = json.loads(resp.choices[0].message.content).get("steps")
        if not isinstance(parsed_steps, list):
            raise ValueError("LiteLLM response missing steps list")
        return parsed_steps
    except Exception as e:
        logging.warning("[llm] parameter detection failed (%s); using raw steps", e)
        return _with_default_flags()


def select_skill_by_intent(user_message: str, catalog: list[dict]) -> dict | None:
    """Use the LLM to pick the best skill for a natural-language request.

    Returns the matching catalog entry dict (with file_name, display_name,
    description, inputs) or None if no confident match is found.

    catalog is a list of dicts as returned by core.skills.skill_catalog().
    """
    if not catalog:
        return None

    catalog_text = "\n".join(
        f'- file_name: "{e["file_name"]}" | name: "{e["display_name"]}" '
        f'| description: "{e["description"]}" | inputs: {e["inputs"]}'
        for e in catalog
    )

    prompt = (
        "You are a skill router for a browser automation agent.\n"
        "Given the user's message and the list of available skills below, "
        "decide which single skill best matches what the user wants to do.\n"
        "Reply ONLY with a JSON object: "
        '{"match": "<file_name>", "confidence": "<high|medium|low>", "reason": "<one sentence>"}\n'
        'If no skill is a reasonable match, return {"match": null, "confidence": "low", "reason": "..."}.\n\n'
        f"Available skills:\n{catalog_text}\n\n"
        f'User message: "{user_message}"'
    )

    try:
        if LLM_PROVIDER == "circuit":
            model = _build_circuit_model(json_response=True)
            resp = model.invoke([HumanMessage(content=prompt)])
            raw = resp.content
        else:
            resp = litellm.completion(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content

        # Parse JSON — strip markdown fences if present
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw.strip())

        parsed = json.loads(raw)
        match_name = parsed.get("match")
        confidence = parsed.get("confidence", "low")

        if not match_name or confidence == "low":
            return None

        for entry in catalog:
            if entry["file_name"] == match_name:
                return entry

        return None

    except Exception as exc:
        logging.warning("[llm] skill intent selection failed (%s)", exc)
        return None