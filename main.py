"""
Sheepin AI - API and web app.

Endpoints:
  GET /                              -> farmer-facing web app (static/index.html)
  GET /api/health                    -> service status
  GET /api/animals                   -> all animals with rule results, flagged first
  GET /api/animals/{ear_tag}         -> one animal's rule result
  GET /api/animals/{ear_tag}/advice  -> rule result + Gemini advisory

Local run:  uvicorn main:app --reload --port 8080
"""

import os
import time

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from app.advisor import MODEL, advise
from app.detect import run, to_gemini_payload

app = FastAPI(title="Sheepin AI", version="0.1.0")

CACHE_TTL_SECONDS = int(os.environ.get("DETECT_CACHE_TTL", "300"))
_detect_cache: dict = {"at": 0.0, "records": []}


def records() -> list[dict]:
    """Rule results for all animals, cached briefly to keep BigQuery calls low."""
    now = time.time()
    if now - _detect_cache["at"] > CACHE_TTL_SECONDS:
        _detect_cache["records"] = run()
        _detect_cache["at"] = now
    return _detect_cache["records"]


def find(ear_tag: str) -> dict:
    for r in records():
        if r["ear_tag"] == ear_tag:
            return r
    raise HTTPException(status_code=404, detail=f"ear_tag {ear_tag} tidak ditemukan")


STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/", include_in_schema=False)
def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok", "model": MODEL, "animals_cached": len(_detect_cache["records"])}


@app.get("/api/animals")
def list_animals():
    rs = records()
    summary = {lvl: sum(1 for r in rs if r["tingkat_sistem"] == lvl)
               for lvl in ("segera", "waspada", "normal")}
    # the list stays light; /api/animals/{ear_tag} carries the 30-day history
    slim = [{k: v for k, v in r.items() if k != "riwayat_bobot"} for r in rs]
    return {"ringkasan": summary, "ternak": slim}


@app.get("/api/animals/{ear_tag}")
def get_animal(ear_tag: str):
    return find(ear_tag)


@app.get("/api/animals/{ear_tag}/advice")
def get_advice(ear_tag: str):
    record = find(ear_tag)
    try:
        advice = advise(to_gemini_payload(record))
    except ValueError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"deteksi": record, "saran": advice}