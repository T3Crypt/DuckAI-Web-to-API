"""DuckAI Web-to-API v2 - OpenAI-compatible relay over duck.ai web UI.

Usage:
    python3 main.py                       # or: uvicorn main:app --port 8080
    curl -s localhost:8080/v1/models | head
    curl -s localhost:8080/health
    curl -N localhost:8080/v1/chat/completions -H 'Content-Type: application/json' \
      -d '{"model":"gpt-5.6-luna","messages":[{"role":"user","content":"ping"}],"stream":true}'

Kelemahan referensi (/root/ref-duckapi) yang dianalisa dan diperbaiki:
  1. Single session per model - 1 page + 1 asyncio.Lock -> semua request
     paralel antre (blocking). v2: TAB POOL, N tab Chrome (DUCKAI_POOL_SIZE,
     default 3) dalam 1 browser per model, round-robin ke tab paling sepi,
     tiap tab lock sendiri -> benar2 N concurrent request per model.
  2. Image instruction hardcode bahasa China -> ENGLISH (IMAGE_INSTRUCTION +
     size hint EN di duckai.py), dipakai endpoint /v1/images/generations.
  3. /v1/models live-refresh: referensi fetch on-demand TTL 3600s - kalau
     endpoint tak pernah dipanggil, catalog basi. v2: background task
     periodic fetch https://duck.ai/duckchat/v1/models (public, no auth)
     tiap DUCKAI_CATALOG_TTL detik; fallback statis kalau gagal.
  4. Ban (ERR_BN_LIMIT=418): referensi langsung raise, no retry. v2: tab
     kena ban -> cooldown DUCKAI_BAN_COOLDOWN detik, request retry ke tab
     sehat lain dengan exponential backoff (retry queue) sampai deadline;
     semua ban -> HTTP 429 + Retry-After. PROXIES dirotasi saat launch fail.
  5. Dashboard HTML dihapus total; log ke stdout saja.
  6. Prompt clamp head+tail (12K default) tetap; maxlength UI dipakai kalau
     ada. Delta typing (session reuse) tetap, sekarang per-tab.

Tetap dipertahankan dari referensi (pattern proven):
  - Real Chrome via channel="chrome" (bukan bundled chromium - fingerprint
    ERR_BN_LIMIT di request pertama), path /usr/bin/google-chrome.
  - UI-driven send: ketik textarea + klik Send (fetch manual kena
    ERR_CHALLENGE; x-fe-signals hanya di-generate app sendiri).
  - SSE tee fetch hook: r.clone().body.getReader() -> token-by-token.
  - Warmup poll WARM_MIN..WARM_MAX (bukan sleep tetap 7s).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, List, Optional, Union
from uuid import uuid4

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from duckai import (
    DEFAULT_MODEL,
    DuckAIBan,
    DuckAIError,
    FALLBACK_MODELS,
    ModelPool,
    build_image_prompt,
    fetch_model_catalog,
    resolve_model,
)

logging.basicConfig(level=os.getenv("DUCKAI_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("duckai2api")

# --- Config env ---
API_KEY = os.getenv("DUCKAI_API_KEY", "").strip()
PROXIES = [p.strip() for p in os.getenv("DUCKAI_PROXIES", "").split(",") if p.strip()]
DEFAULT = os.getenv("DUCKAI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
PORT = int(os.getenv("PORT", "8080"))
HOST = os.getenv("HOST", "0.0.0.0")
CATALOG_TTL = float(os.getenv("DUCKAI_CATALOG_TTL", "3600"))

POOLS: dict = {}


def _pool(model: str) -> ModelPool:
    if model not in POOLS:
        POOLS[model] = ModelPool(model=model, proxies=PROXIES)
    return POOLS[model]


def _id() -> str:
    return f"chatcmpl-{uuid4().hex[:24]}"


def _created() -> int:
    return int(time.time())


def require_key(authorization: str = Header(default="")) -> None:
    if not API_KEY:
        return
    if not authorization.startswith("Bearer ") or authorization.split(" ", 1)[1] != API_KEY:
        raise HTTPException(status_code=401, detail="invalid API key")


# --- Flatten conversation (duck.ai satu textarea; context full re-send) -----
def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                t = b.get("type")
                if t == "text":
                    parts.append(b.get("text", ""))
                elif t == "tool_result":
                    inner = b.get("content", "")
                    if isinstance(inner, list):
                        inner = " ".join(
                            x.get("text", "") if isinstance(x, dict) else str(x) for x in inner
                        )
                    parts.append(f"[tool_result {b.get('tool_use_id', '')}]: {inner}")
                elif t == "tool_use":
                    parts.append(
                        f'[tool_call name="{b.get("name", "")}"] '
                        f"{json.dumps(b.get('input', {}), ensure_ascii=False)}"
                    )
        return "\n".join(p for p in parts if p)
    return str(content)


def flatten(system: Any, messages: List[dict]) -> str:
    parts: List[str] = []
    sys_text = _text(system)
    if sys_text.strip():
        parts.append(f"System: {sys_text}")
    for m in messages:
        role = m.get("role", "user")
        label = {"user": "Human", "assistant": "Assistant"}.get(role, "Human")
        if role == "tool":
            label = f"[tool_result {m.get('tool_call_id', '')}]"
        text = _text(m.get("content", ""))
        if text.strip():
            parts.append(f"{label}: {text}")
    return "\n\n".join(parts).strip()


# --- Catalog: live refresh periodik di background ---------------------------
CATALOG: list = list(FALLBACK_MODELS)
CATALOG_OK = False


async def _catalog_refresher() -> None:
    """Fetch https://duck.ai/duckchat/v1/models (public GET, no auth) tiap TTL."""
    global CATALOG, CATALOG_OK
    while True:
        try:
            models = await asyncio.to_thread(fetch_model_catalog)
            if models:
                CATALOG = models
                CATALOG_OK = True
                logger.info("model catalog refreshed: %d models", len(models))
        except Exception as e:  # noqa: BLE001
            logger.warning("catalog refresh failed (keeping previous): %s", e)
        await asyncio.sleep(CATALOG_TTL)


app = FastAPI(title="DuckAI Web-to-API")


async def _prewarm() -> None:
    try:
        await _pool(DEFAULT).prewarm()
        logger.info("prewarmed pool: %s", DEFAULT)
    except Exception as e:  # noqa: BLE001
        logger.warning("prewarm skipped: %s", e)


@app.on_event("startup")
async def _startup() -> None:
    asyncio.create_task(_catalog_refresher())
    if os.getenv("DUCKAI_PREWARM", "1") not in ("0", "false", "False"):
        asyncio.create_task(_prewarm())


@app.on_event("shutdown")
async def _shutdown() -> None:
    for model, pool in list(POOLS.items()):
        try:
            await pool.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("close failed %s: %s", model, e)


# --- OpenAI protocol --------------------------------------------------------
class ChatMessage(BaseModel):
    role: str
    content: Union[str, List[Any]]


class ChatCompletionRequest(BaseModel):
    model: str = DEFAULT
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    top_p: Optional[float] = None


def _err_sse(message: str) -> str:
    return f"data: {json.dumps({'error': {'message': message, 'type': 'server_error'}})}\n\n"


async def _openai_stream(pool: ModelPool, prompt: str, model: str, chat_id: str):
    def chunk(delta: dict, finish):
        return {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": _created(),
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    t0 = time.time(); tout = 0; ok = False; banned = False
    try:
        yield f"data: {json.dumps(chunk({'role': 'assistant', 'content': ''}, None))}\n\n"
        async for token in pool.stream(prompt):
            tout += len(token)
            yield f"data: {json.dumps(chunk({'content': token}, None))}\n\n"
        yield f"data: {json.dumps(chunk({}, 'stop'))}\n\n"
        yield "data: [DONE]\n\n"
        ok = True
    except DuckAIBan as e:
        banned = True
        yield _err_sse(f"{e} (HTTP 429; set DUCKAI_PROXIES to rotate past bans)")
    except DuckAIError as e:
        yield _err_sse(str(e))
    except Exception as e:  # noqa: BLE001
        yield _err_sse(f"stream failed: {e}")
    finally:
        _rec(model, len(prompt), tout, (time.time()-t0)*1000, ok, banned)


@app.post("/v1/chat/completions", dependencies=[Depends(require_key)])
async def chat_completions(req: ChatCompletionRequest):
    system = None
    turns = []
    for m in req.messages:
        if m.role == "system":
            system = m.content
        else:
            turns.append({"role": m.role, "content": m.content})
    prompt = flatten(system, turns)
    if not prompt.strip():
        raise HTTPException(status_code=400, detail="empty prompt")
    model = resolve_model(req.model)
    logger.info("REQ chat model=%s->%s stream=%s msgs=%d prompt_len=%d",
                req.model, model, req.stream, len(turns), len(prompt))

    pool = _pool(model)
    if req.stream:
        return StreamingResponse(
            _openai_stream(pool, prompt, model, _id()), media_type="text/event-stream"
        )

    t0 = time.time()
    try:
        result = "".join([tok async for tok in pool.stream(prompt)])
    except DuckAIBan as e:
        _rec(model, len(prompt), 0, (time.time()-t0)*1000, False, True)
        raise HTTPException(status_code=429, detail=f"{e}. Set DUCKAI_PROXIES to rotate past bans.")
    except DuckAIError as e:
        _rec(model, len(prompt), 0, (time.time()-t0)*1000, False, False)
        raise HTTPException(status_code=502, detail=str(e))
    _rec(model, len(prompt), len(result), (time.time()-t0)*1000, True, False)
    return {
        "id": _id(),
        "object": "chat.completion",
        "created": _created(),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": result.strip()},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


# --- Images (English instruction; GenerateImage tool via chat turn) ---------
class ImageRequest(BaseModel):
    model: str = DEFAULT
    prompt: str
    n: Optional[int] = 1
    size: Optional[str] = None
    response_format: Optional[str] = "url"


@app.post("/v1/images/generations", dependencies=[Depends(require_key)])
async def images_generations(req: ImageRequest):
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="empty prompt")
    model = resolve_model(req.model)
    prompt = build_image_prompt(req.prompt, req.size)
    pool = _pool(model)
    data = []
    count = max(1, min(int(req.n or 1), 4))
    for _ in range(count):
        try:
            text = "".join([tok async for tok in pool.stream(prompt)])
        except DuckAIBan as e:
            raise HTTPException(status_code=429, detail=str(e))
        except DuckAIError as e:
            raise HTTPException(status_code=502, detail=str(e))
        data.append({"b64_json": None, "revised_prompt": text.strip()[:200]})
        # ponytail: b64 extraction dari SSE ui-component events belum dipakai
        # di v2 pool stream (text path only); upgrade: reuse ref tools.py
        # parser utk ui-component b64Image saat image delivery dibutuhkan.
    return {"created": _created(), "data": data}


@app.get("/v1/models", dependencies=[Depends(require_key)])
async def list_models():
    data = []
    for m in CATALOG:
        mid = m.get("id")
        if not mid:
            continue
        tiers = m.get("accessTier") or []
        data.append({
            "id": mid,
            "object": "model",
            "created": _created(),
            "owned_by": "duck.ai",
            "display_name": m.get("name") or mid,
            "access": "free" if "free" in tiers else "paid",
        })
    return {"object": "list", "data": data}


_BOOT = time.time()

# --- Usage tracking (in-memory ring buffer + totals) ---
import collections
from datetime import datetime, timezone

_USAGE_LOCK = asyncio.Lock()
_REQS: collections.deque = collections.deque(maxlen=60)   # recent requests
_TOTALS = {"requests": 0, "in": 0, "out": 0, "ok": 0, "err": 0, "banned": 0}
_PER_MODEL: dict = {}   # model -> {requests, in, out, ok, err, ms_sum}


def _rec(model: str, tin: int, tout: int, ms: float, ok: bool, banned: bool) -> None:
    _TOTALS["requests"] += 1
    _TOTALS["in"] += tin
    _TOTALS["out"] += tout
    _TOTALS["ok" if ok else "err"] += 1
    _TOTALS["banned"] += int(banned)
    m = _PER_MODEL.setdefault(model, {"requests": 0, "in": 0, "out": 0, "ok": 0, "err": 0, "ms_sum": 0.0})
    m["requests"] += 1
    m["in"] += tin
    m["out"] += tout
    m["ok" if ok else "err"] += 1
    m["ms_sum"] += ms
    _REQS.appendleft({
        "model": model, "in": tin, "out": tout, "ms": round(ms),
        "ok": ok, "banned": banned, "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
    })


@app.get("/stats")
async def stats():
    now = time.time()
    per_model = []
    for m, d in _PER_MODEL.items():
        total = d["ok"] + d["err"]
        health = round(100 * d["ok"] / total, 2) if total else 0.0
        ms_kt = round(d["ms_sum"] / max(d["out"], 1) * 1000) if d["out"] else 0
        per_model.append({"model": m, "health": health, "ms_per_ktoken": ms_kt,
                          "requests": d["requests"], "errors": d["err"],
                          "in": d["in"], "out": d["out"]})
    per_model.sort(key=lambda x: -x["requests"])
    total = _TOTALS
    rate = total["ok"] / total["requests"] * 100 if total["requests"] else 0.0
    return {
        "boot": _BOOT,
        "uptime_s": int(now - _BOOT),
        "totals": total,
        "success_rate": round(rate, 2),
        "avg_ms": round((now - _BOOT) and sum(r["ms"] for r in _REQS) / max(len(_REQS), 1), 0),
        "per_model": per_model,
        "recent": list(_REQS)[:15],
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "models": len(CATALOG),
        "catalog_live": CATALOG_OK,
        "uptime_s": int(time.time() - _BOOT),
        "pools": {
            m: {"tabs": len(p.tabs),
                "connected": bool(p.browser and p.browser.is_connected())}
            for m, p in POOLS.items()
        },
    }


# --- Dashboard (single-file, no build) --------------------------------------
from fastapi.responses import HTMLResponse

_DASH = open(os.path.join(os.path.dirname(__file__), "dashboard.html"), encoding="utf-8").read()


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(_DASH)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
