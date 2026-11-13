"""
The one place DataTrust talks to a language model (LLM_PROVIDER in .env: gemini, anthropic, ollama; none = off).

Rules every AI feature follows
  * the model only sees evidence DataTrust measured itself (check results, runs, errors, catalog facts), never raw access
  * it must answer from that evidence; numbers it writes are checked against the evidence and unverified ones are flagged
  * answers are cached by evidence: the same evidence is never paid for twice (app.ai_notes)
  * without a model every feature still works with its deterministic version
"""
from __future__ import annotations

import hashlib
import json
import os
import re

import appdb

DDL = """
CREATE SCHEMA IF NOT EXISTS app;
CREATE TABLE IF NOT EXISTS app.ai_notes (
    note_key      TEXT PRIMARY KEY,               -- kind + subject + evidence hash
    kind          TEXT NOT NULL,                  -- explain / suggest / briefing
    subject       TEXT,                           -- incident id, table, scope ...
    content       JSONB NOT NULL,
    model         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class Unavailable(Exception):
    """No model configured, or it did not answer: callers fall back to their deterministic version."""


def ensure_tables() -> None:
    with appdb.connect() as conn:
        conn.execute(DDL)
        conn.commit()


def provider() -> str:
    p = os.getenv("LLM_PROVIDER", "none").strip().lower()
    return p if p in ("gemini", "anthropic", "ollama") else "none"


def model_name() -> str | None:
    return {"gemini": os.getenv("GEMINI_MODEL", "gemini-2.5-flash"), "anthropic": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
            "ollama": os.getenv("OLLAMA_MODEL", "qwen2.5:7b")}.get(provider())


def complete_json(system: str, prompt: str, timeout: int = 90) -> dict:
    """One call that must return a JSON object. A busy or briefly failing model is retried once."""
    import time
    try:
        return _complete_json(system, prompt, timeout)
    except Unavailable as e:
        if provider() == "none" or "not valid JSON" in str(e):
            raise
        time.sleep(2)
        return _complete_json(system, prompt, timeout)


def _complete_json(system: str, prompt: str, timeout: int) -> dict:
    import requests
    p = provider()
    try:
        if p == "gemini":
            import llm_gemini
            text = llm_gemini.complete_json(system, prompt)
        elif p == "anthropic":
            r = requests.post("https://api.anthropic.com/v1/messages", timeout=timeout, headers={
                "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": model_name(), "max_tokens": 2000, "temperature": 0,
                      "system": system + "\nReply with one JSON object only, no other text.",
                      "messages": [{"role": "user", "content": prompt}]})
            r.raise_for_status()
            text = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
        elif p == "ollama":
            r = requests.post(os.getenv("OLLAMA_URL", "http://localhost:11434") + "/api/chat", timeout=max(timeout, 180), json={
                "model": model_name(), "stream": False, "format": "json", "options": {"temperature": 0},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]})
            r.raise_for_status()
            text = r.json()["message"]["content"]
        else:
            raise Unavailable("No AI model is configured (LLM_PROVIDER in .env).")
    except Unavailable:
        raise
    except Exception as e:                                            # noqa: BLE001  network, quota, bad key ...
        raise Unavailable(describe_error(e)) from e
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        return json.loads(m.group(0) if m else text)
    except (ValueError, AttributeError) as e:
        raise Unavailable("The AI model's answer was not valid JSON.") from e


def describe_error(e: Exception) -> str:
    """What went wrong with the model call, in words a user can act on."""
    resp = getattr(e, "response", None)
    code = getattr(resp, "status_code", None)
    body = (getattr(resp, "text", "") or "").lower()
    if code == 429 and "per day" in body:
        m = re.search(r"limit: (\d+)", body)
        return (f"The AI model's daily request limit is used up{f' ({m.group(1)} requests a day on the free tier)' if m else ''}. "
                "It resets within 24 hours; enable billing on the model's account for more, or switch LLM_PROVIDER.")
    if code == 429:
        return "The AI model is rate-limiting requests right now. Try again in a minute."
    if code in (401, 403):
        return "The AI model rejected the API key (check GEMINI_API_KEY / ANTHROPIC_API_KEY in .env)."
    if code and code >= 500:
        return "The AI model's service is having problems right now. Try again shortly."
    return f"The AI model did not answer ({type(e).__name__}: {str(e)[:160]})"


# ============================================================================ grounding: numbers must come from the evidence
_NUM = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")


def _norm(n: str) -> str:
    n = n.replace(",", "")
    try:
        f = float(n)
    except ValueError:
        return n
    return str(int(f)) if f.is_integer() else f"{f:.4f}".rstrip("0").rstrip(".")


def unverified_numbers(text: str, evidence) -> list:
    """Numbers in the model's text that appear nowhere in the evidence (small counts like 1-10 and years are allowed)."""
    known = {_norm(x) for x in _NUM.findall(json.dumps(evidence, default=str))}
    out = []
    for n in _NUM.findall(text or ""):
        k = _norm(n)
        try:
            small = abs(float(k)) <= 10 or 1990 <= float(k) <= 2100
        except ValueError:
            small = False
        if not small and k not in known and k not in out:
            out.append(n)
    return out


def text_of(obj) -> str:
    if isinstance(obj, dict):
        return " ".join(text_of(v) for v in obj.values())
    if isinstance(obj, list):
        return " ".join(text_of(v) for v in obj)
    return str(obj)


# ============================================================================ cache
def _key(kind: str, subject: str, evidence) -> str:
    h = hashlib.sha256(json.dumps(evidence, sort_keys=True, default=str).encode()).hexdigest()[:24]
    return f"{kind}:{subject}:{model_name() or 'none'}:{h}"


def cached(kind: str, subject: str, evidence):
    if not appdb.table_exists("app.ai_notes"):
        return None
    row = appdb.one("SELECT content, created_at FROM app.ai_notes WHERE note_key = %s", (_key(kind, subject, evidence),))
    return {**row["content"], "generated_at": row["created_at"], "cached": True} if row else None


def remember(kind: str, subject: str, evidence, content: dict) -> None:
    from psycopg.types.json import Jsonb
    with appdb.connect() as conn:
        conn.execute("""INSERT INTO app.ai_notes (note_key, kind, subject, content, model) VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (note_key) DO UPDATE SET content = EXCLUDED.content, created_at = now()""",
                     (_key(kind, subject, evidence), kind, subject, Jsonb(content), model_name()))
        conn.commit()


def grounded(kind: str, subject: str, evidence, system: str, prompt: str, refresh: bool = False) -> dict:
    """Cached JSON answer for this exact evidence, with any number not found in the evidence listed under 'unverified'."""
    if not refresh:
        hit = cached(kind, subject, evidence)
        if hit:
            return hit
    out = complete_json(system, prompt)
    # numbers may come from the evidence or from the instructions themselves ("last 24 hours")
    out["unverified"] = unverified_numbers(text_of({k: v for k, v in out.items() if k != "unverified"}), {"e": evidence, "s": system})
    out["model"] = f"{provider()} · {model_name()}"
    remember(kind, subject, evidence, out)
    return {**out, "cached": False}
