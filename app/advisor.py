"""
Sheepin AI - Gemini advisor.

Takes the payload produced by detect.to_gemini_payload() and asks Gemini
(via Vertex AI) for a short advisory in Bahasa Indonesia, using the system
prompt validated in AI Studio (prompts/advisor_system.txt).

Gemini never decides whether an animal has a problem. It receives the alert
the rules produced and explains it. Guardrails live in the prompt; this module
adds JSON validation and a cheap in-memory cache.

Run locally (after `gcloud auth application-default login`):
  python -m app.advisor 0931        # detect + advise one animal from BigQuery
  python -m app.advisor --test      # run prompts/test_cases.json through Gemini
"""

import json
import os
import sys
import time
from pathlib import Path

from google import genai
from google.genai import types

PROJECT = os.environ.get("GCP_PROJECT", "sheepin-ai")
LOCATION = os.environ.get("VERTEX_LOCATION", "global")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "advisor_system.txt"
TEST_CASES_PATH = Path(__file__).resolve().parent.parent / "prompts" / "test_cases.json"

REQUIRED_KEYS = {"ringkasan", "cek_hari_ini", "lakukan", "hubungi_dokter_hewan_jika", "tingkat"}
VALID_LEVELS = {"normal", "waspada", "segera"}

ADVICE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "ringkasan": {"type": "STRING"},
        "cek_hari_ini": {"type": "ARRAY", "items": {"type": "STRING"}},
        "lakukan": {"type": "ARRAY", "items": {"type": "STRING"}},
        "hubungi_dokter_hewan_jika": {"type": "STRING"},
        "tingkat": {"type": "STRING", "enum": ["normal", "waspada", "segera"]},
    },
    "required": ["ringkasan", "cek_hari_ini", "lakukan",
                 "hubungi_dokter_hewan_jika", "tingkat"],
}

_client: genai.Client | None = None
_system_prompt: str | None = None
_cache: dict[str, dict] = {}


def client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
    return _client


def system_prompt() -> str:
    global _system_prompt
    if _system_prompt is None:
        _system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    return _system_prompt


def advise(payload: dict, use_cache: bool = True) -> dict:
    """Return Gemini's advisory for one animal payload, as a validated dict."""
    key = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    if use_cache and key in _cache:
        return _cache[key]

    t0 = time.time()
    response = client().models.generate_content(
        model=MODEL,
        contents=json.dumps(payload, ensure_ascii=False, indent=2),
        config=types.GenerateContentConfig(
            system_instruction=system_prompt(),
            response_mime_type="application/json",
            temperature=0.2,
            max_output_tokens=2048,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )

    finish = response.candidates[0].finish_reason if response.candidates else None
    if finish and "MAX_TOKENS" in str(finish):
        usage = getattr(response, "usage_metadata", None)
        raise ValueError(f"Gemini output terpotong (MAX_TOKENS). usage={usage}")

    advice = parse_advice(response.text)
    advice["_meta"] = {
        "model": MODEL,
        "latency_ms": int((time.time() - t0) * 1000),
        "finish_reason": str(finish),
    }
    if use_cache:
        _cache[key] = advice
    return advice


def parse_advice(text: str) -> dict:
    """Parse and validate Gemini's JSON; raise ValueError if it is unusable."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"Gemini returned non-JSON output: {e}\n{text[:300]}") from e

    missing = REQUIRED_KEYS - set(data)
    if missing:
        raise ValueError(f"Gemini output missing keys: {sorted(missing)}")
    if data["tingkat"] not in VALID_LEVELS:
        raise ValueError(f"Gemini returned unknown tingkat: {data['tingkat']!r}")
    for k in ("cek_hari_ini", "lakukan"):
        if not isinstance(data[k], list):
            data[k] = [str(data[k])]
    return data


def load_test_cases() -> list[dict]:
    """Accepts either a bare list of inputs or a list of {"input": ..., ...}."""
    raw = json.loads(TEST_CASES_PATH.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("cases", list(raw.values()))
    return [c["input"] if isinstance(c, dict) and "input" in c else c for c in raw]


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        for payload in load_test_cases():
            print(f"\n=== {payload.get('ear_tag')} ===")
            print(json.dumps(advise(payload, use_cache=False), ensure_ascii=False, indent=2))
        return

    from app.detect import run, to_gemini_payload
    records = run()
    if len(sys.argv) > 1:
        records = [r for r in records if r["ear_tag"] == sys.argv[1]]
    for r in records[:1] if len(sys.argv) == 1 else records:
        print(json.dumps({"deteksi": r, "saran": advise(to_gemini_payload(r))},
                         ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()