"""DuckAI Web-to-API v2 - browser client.

Perbaikan vs referensi (analisa kelemahan, bukan tiruan):
  1. Single session per model (1 asyncio.Lock, request saling blokir)
     -> TAB POOL: N tab Chrome dalam 1 browser per model, round-robin,
     tiap tab lock sendiri. DUCKAI_POOL_SIZE mengatur N.
  2. Image instruction bahasa China -> ENGLISH (+ size hint EN).
  3. ERR_BN_LIMIT (418) -> tab cooldown + retry queue dengan backoff
     sampai deadline request; referensi langsung fail.
  4. Delta typing (session reuse) tetap, sekarang per-tab.
  5. Warmup poll WARM_MIN..WARM_MAX (bukan sleep tetap).
  6. Clamp prompt: maxlength UI kalau ada, else DUCKAI_PROMPT_LIMIT (12K).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.request
from typing import AsyncIterator, Optional

from playwright.async_api import async_playwright

logger = logging.getLogger("duckai")

# --- Config env ---
BASE = os.getenv("DUCKAI_BASE", "https://duck.ai")
CHROME_PATH = os.getenv("DUCKAI_CHROME_PATH", "/usr/bin/google-chrome")
POOL_SIZE = max(1, int(os.getenv("DUCKAI_POOL_SIZE", "3")))
WARM_MIN = float(os.getenv("DUCKAI_WARM_MIN", "2.0"))
WARM_MAX = float(os.getenv("DUCKAI_WARM_MAX", "7.0"))
BAN_COOLDOWN = float(os.getenv("DUCKAI_BAN_COOLDOWN", "300"))
PROMPT_LIMIT = int(os.getenv("DUCKAI_PROMPT_LIMIT", "12000"))
REQ_TIMEOUT = float(os.getenv("DUCKAI_REQ_TIMEOUT", "180"))
STREAM_POLL_MS = int(os.getenv("DUCKAI_STREAM_POLL_MS", "200"))

# Real desktop Chrome UA (tanpa HeadlessChrome).
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)

DEFAULT_MODEL = "gpt-5.6-luna"

# Alias -> id backend. Live catalog menimpa lewat note_live_models().
MODEL_ALIASES = {
    "gpt-5.6": "gpt-5.6-luna",
    "gpt-5.6-luna": "gpt-5.6-luna",
    "gpt-5.4": "gpt-5.4-mini",
    "gpt-4o": "gpt-5.4-mini",
    "gpt-4o-mini": "gpt-5.4-mini",
    "o3-mini": "gpt-5.4-mini",
    "claude-3-5-sonnet": "claude-sonnet-4-6",
    "claude-sonnet": "claude-sonnet-4-6",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-3-haiku": "claude-haiku-4-5",
    "claude-haiku": "claude-haiku-4-5",
    "claude-haiku-4-5": "claude-haiku-4-5",
    "claude-3-opus": "claude-opus-4-8",
    "claude-opus": "claude-opus-4-8",
    "claude-opus-4-8": "claude-opus-4-8",
    "mistral-small": "mistral-small-2603",
    "mistral-small-2603": "mistral-small-2603",
    "gpt-oss-120b": "tinfoil/gpt-oss-120b",
    "tinfoil/gpt-oss-120b": "tinfoil/gpt-oss-120b",
    "gemma4-31b": "tinfoil/gemma4-31b",
    "tinfoil/gemma4-31b": "tinfoil/gemma4-31b",
    # Image generation lewat GenerateImage tool Luna.
    "gpt-image-1": "gpt-5.6-luna",
    "gpt-image-2": "gpt-5.6-luna",
    "dall-e-3": "gpt-5.6-luna",
}

# Fallback statis kalau live catalog gagal (best-effort saja).
FALLBACK_MODELS = [
    {"id": "gpt-5.6-luna", "name": "GPT-5.6 Luna"},
    {"id": "gpt-5.4-mini", "name": "GPT-5.4 mini"},
    {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
    {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5"},
    {"id": "claude-opus-4-8", "name": "Claude Opus 4.8"},
    {"id": "mistral-small-2603", "name": "Mistral Small 4"},
    {"id": "tinfoil/gpt-oss-120b", "name": "gpt-oss 120B"},
    {"id": "tinfoil/gemma4-31b", "name": "Gemma 4 31B"},
]

LIVE_MODELS: dict = {}  # lower -> canonical id, diisi fetch_model_catalog()


def note_live_models(models: list) -> None:
    out = {}
    for m in models:
        mid = (m.get("id") or "").strip()
        if mid:
            out[mid.lower()] = mid
    if out:
        LIVE_MODELS.clear()
        LIVE_MODELS.update(out)


def resolve_model(name: Optional[str]) -> str:
    if not name:
        return DEFAULT_MODEL
    n = name.strip()
    low = n.lower()
    if low in LIVE_MODELS:
        return LIVE_MODELS[low]
    if n in MODEL_ALIASES:
        return MODEL_ALIASES[n]
    return MODEL_ALIASES.get(low, n)


def fetch_model_catalog(timeout: float = 10.0) -> list:
    """Public GET /duckchat/v1/models (no auth). Sinkron; panggil via to_thread."""
    url = f"{BASE}/duckchat/v1/models"
    req = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    models = data.get("models") or []
    note_live_models(models)
    return models


class DuckAIError(Exception):
    pass


class DuckAIBan(DuckAIError):
    """ERR_BN_LIMIT (418): ban IP/fingerprint. Retry queue + proxy rotation."""


# --- Browser-side JS: SSE tee via fetch hook (pattern referensi) ------------
_INIT_SCRIPT = r"""
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
window.__duckai = {chunks:[], done:false, err:null};
(function(){
  const O = window.fetch;
  window.fetch = async function(u,o){
    o = o || {};
    if(typeof u === 'string' && u.includes('/duckchat/v1/chat')){
      const D = window.__duckai;
      D.chunks = []; D.done = false; D.err = null;
      const r = await O.call(this, u, o);
      try{
        const reader = r.clone().body.getReader();
        const dec = new TextDecoder();
        (async function pump(){
          try{
            while(true){
              const {value, done} = await reader.read();
              if(done){ D.chunks.push(dec.decode()); D.done = true; break; }
              const s = dec.decode(value, {stream:true});
              D.chunks.push(s);
              if(s.indexOf('"action":"error"') !== -1) D.err = s;
            }
          }catch(e){ D.err = String(e); D.done = true; }
        })();
      }catch(e){ D.err = String(e); D.done = true; }
      return r;
    }
    return O.apply(this, arguments);
  };
})();
"""

_RESET_JS = r"() => { window.__duckai = {chunks:[], done:false, err:null}; }"

_POLL_JS = r"""
(n) => {
  const D = window.__duckai || {chunks:[], done:false, err:null};
  const text = D.chunks.slice(n).join('');
  return {n: D.chunks.length, text, done: D.done, err: D.err};
}
"""


def _parse_event(line: str) -> Optional[dict]:
    line = line.strip()
    if not line.startswith("{"):
        if line.startswith("data:"):
            line = line[5:].strip()
        else:
            return None
    if line == "[DONE]" or not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _clamp(prompt: str, limit: int) -> str:
    """Keep head (system preamble) + tail (pertanyaan hidup), buang tengah."""
    marker = "\n...[context truncated]...\n"
    if limit <= len(marker) + 40:
        return prompt[:limit]
    head = max(1, limit // 10)
    tail = limit - head - len(marker)
    return prompt[:head] + marker + prompt[-tail:]


def _norm_proxy(p: str) -> dict:
    m = re.match(
        r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*)://(?:(?P<user>[^:@]+):(?P<pwd>[^@]*)@)?(?P<host>.+)$",
        p.strip(),
    )
    if not m:
        return {"server": f"http://{p.strip()}"}
    out = {"server": f"{m.group('scheme')}://{m.group('host')}"}
    if m.group("user"):
        out["username"] = m.group("user")
        out["password"] = m.group("pwd") or ""
    return out


class Tab:
    """Satu tab Chrome = satu worker dengan lock sendiri (concurrency unit)."""

    def __init__(self, ctx, model: str) -> None:
        self.ctx = ctx
        self.model = model
        self.page = None
        self._last_prompt: Optional[str] = None
        self.lock = asyncio.Lock()
        self.banned_until = 0.0

    async def _new_page(self):
        page = await self.ctx.new_page()
        await page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
        opened = time.monotonic()
        deadline = opened + WARM_MAX
        while time.monotonic() < deadline:
            if time.monotonic() - opened >= WARM_MIN:
                try:
                    if await page.query_selector("textarea") is not None:
                        break
                except Exception:
                    break
            await page.wait_for_timeout(200)
        return page

    async def _open_warm(self) -> None:
        if self.page is not None and not self.page.is_closed():
            return
        self.page = await self._new_page()
        self._last_prompt = None

    async def _drop_page(self) -> None:
        if self.page is not None:
            try:
                await self.page.close()
            except Exception:
                pass
            self.page = None
        self._last_prompt = None

    def _continuation(self, prompt: str) -> Optional[str]:
        """Delta typing: prompt ekstensinya prompt lama + 1 turn Human baru."""
        if self._last_prompt is None or self.page is None:
            return None
        if not prompt.startswith(self._last_prompt):
            return None
        tail = prompt[len(self._last_prompt):].strip()
        marks = list(re.finditer(r"(?m)^Human: ", tail))
        if len(marks) != 1:
            return None
        return tail[marks[0].end():].strip() or None

    def _raise_err(self, text: str) -> None:
        if "ERR_BN_LIMIT" in text:
            self.banned_until = time.monotonic() + BAN_COOLDOWN
            raise DuckAIBan("ERR_BN_LIMIT (418)")
        if "ERR_CHALLENGE" in text:
            raise DuckAIError("ERR_CHALLENGE (challenge required)")
        m = re.search(r'"type"\s*:\s*"([^"]+)"', text)
        raise DuckAIError(f"Duck.ai error: {m.group(1) if m else 'unknown'}")

    async def _iter_events(self, prompt: str, timeout: float) -> AsyncIterator[dict]:
        text = self._continuation(prompt) if self.page is not None else None
        if text is None:
            await self._drop_page()
            await self._open_warm()
            text = prompt
        page = self.page

        await page.evaluate(_RESET_JS)
        try:
            await self._type_and_send(page, text)
        except Exception:
            await self._drop_page()
            raise

        buf = ""
        idx = 0
        saw_data = False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = await page.evaluate(_POLL_JS, idx)
            idx = st["n"]
            if st.get("err"):
                await self._drop_page()
                self._raise_err(st["err"])
            new = st.get("text") or ""
            if new:
                saw_data = True
                buf += new
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    ev = _parse_event(line)
                    if ev is not None:
                        yield ev
            if st.get("done"):
                if buf.strip():
                    ev = _parse_event(buf)
                    if ev is not None:
                        yield ev
                if not saw_data:
                    await self._drop_page()
                    raise DuckAIError("empty stream")
                self._last_prompt = prompt
                return
            await page.wait_for_timeout(STREAM_POLL_MS if saw_data else 500)
        await self._drop_page()
        raise DuckAIError("timed out waiting for Duck.ai reply")

    async def _type_and_send(self, page, prompt: str) -> None:
        ta = await page.query_selector("textarea")
        if ta is None:
            raise DuckAIError("textarea not found (UI changed)")
        raw = await ta.get_attribute("maxlength")
        limit = int(raw) if raw and raw.isdigit() and int(raw) > 0 else PROMPT_LIMIT
        if len(prompt) > limit:
            logger.warning("prompt %d chars > limit %d, clamping head+tail", len(prompt), limit)
            prompt = _clamp(prompt, limit)
        await ta.click()
        await ta.fill(prompt)
        await page.wait_for_timeout(300)
        sent = False
        for btn in await page.query_selector_all("button"):
            t = (await btn.inner_text() or "").strip()
            a = await btn.get_attribute("aria-label") or ""
            if t in ("Ask", "Send") or a in ("Ask", "Send", "Ask AI", "Submit"):
                await btn.click(force=True)
                sent = True
                break
        if not sent:
            await ta.press("Enter")

    async def stream(self, prompt: str, timeout: float) -> AsyncIterator[str]:
        async with self.lock:
            async for ev in self._iter_events(prompt, timeout):
                msg = ev.get("message")
                if msg:
                    yield msg

    async def close(self) -> None:
        await self._drop_page()


# Image instruction: ENGLISH (referensi: bahasa China). Size hint EN juga.
IMAGE_INSTRUCTION = (
    "Generate an image directly using your image generation tool based on the "
    "following description. Do not ask questions, do not explain the process. "
    "Image description: {prompt}{size_hint}"
)


def build_image_prompt(prompt: str, size: Optional[str] = None) -> str:
    hint = ""
    if size:
        dims = re.split(r"[x*]", str(size))
        try:
            w, h = int(dims[0]), int(dims[1])
            hint = " (landscape composition)" if w > h else " (portrait composition)" if h > w else " (square composition)"
        except (ValueError, IndexError):
            pass
    return IMAGE_INSTRUCTION.format(prompt=prompt, size_hint=hint)


class ModelPool:
    """N tab dalam 1 browser per model, round-robin + ban-aware.

    Kelemahan referensi: 1 page per model dengan 1 asyncio.Lock global ->
    request paralel antre semua. Fix: POOL_SIZE tab, tiap tab lock sendiri.
    Ban (ERR_BN_LIMIT=418) -> tab cooldown BAN_COOLDOWN detik + retry queue:
    request coba tab sehat lain dengan backoff sampai deadline; kalau semua
    tab/proxy ban, raise DuckAIBan (HTTP 429).
    """

    def __init__(self, model: str, proxies: Optional[list] = None) -> None:
        self.model = model
        self.proxies = [p.strip() for p in (proxies or []) if p and p.strip()] or [None]
        self._pw = None
        self.browser = None
        self.ctx = None
        self.tabs: list[Tab] = []
        self._pidx = 0

    async def _ensure(self) -> None:
        if self.ctx is None or self.browser is None or not self.browser.is_connected():
            await self._launch(self._pidx)
        while len(self.tabs) < POOL_SIZE:
            self.tabs.append(Tab(self.ctx, self.model))

    async def _launch(self, pidx: int) -> None:
        """Launch browser dengan proxy index pidx; rotasi proxy kalau gagal."""
        if self._pw is None:
            self._pw = await async_playwright().start()
        launch: dict = {"headless": True, "channel": "chrome"}
        if CHROME_PATH and os.path.exists(CHROME_PATH):
            launch["executable_path"] = CHROME_PATH
        launch["args"] = ["--disable-blink-features=AutomationControlled"]
        if self.proxies[pidx]:
            launch["proxy"] = _norm_proxy(self.proxies[pidx])
        try:
            browser = await self._pw.chromium.launch(**launch)
        except Exception as e:
            if len(self.proxies) > 1:
                nxt = (pidx + 1) % len(self.proxies)
                logger.warning("launch with proxy[%d] failed (%s); rotating to [%d]", pidx, e, nxt)
                self._pidx = nxt
                raise DuckAIError(f"proxy launch failed: {e}")
            raise
        ctx = await browser.new_context(user_agent=UA)
        await ctx.add_init_script(_INIT_SCRIPT)
        # Ganti browser lama (kalau ada) tanpa orphan.
        if self.browser is not None:
            try:
                await self.browser.close()
            except Exception:
                pass
        self.browser = browser
        self.ctx = ctx
        self.tabs = []  # tab lama mati bersama browser

    def _waiters(self, t: Tab) -> int:
        w = t.lock._waiters
        return len(w) if w else 0

    async def stream(self, prompt: str, timeout: Optional[float] = None) -> AsyncIterator[str]:
        """Round-robin tab paling sepi; ban -> cooldown tab + retry tab lain."""
        timeout = timeout or REQ_TIMEOUT
        deadline = time.monotonic() + timeout
        last_ban: Optional[DuckAIBan] = None
        backoff = 0.5
        while True:
            now = time.monotonic()
            if now > deadline:
                if last_ban:
                    raise last_ban
                raise DuckAIError("timed out (all tabs busy/banned)")
            await self._ensure()
            cands = [t for t in self.tabs if t.banned_until <= now]
            if not cands:
                wait = min(t.banned_until for t in self.tabs) - now
                left = deadline - now
                if wait < left:
                    # Retry queue: cooldown selesai dalam sisa deadline -> tunggu.
                    logger.info("all tabs banned, waiting %.0fs (retry queue)", wait)
                    await asyncio.sleep(wait + 0.5)
                    continue
                logger.info("all %d tabs banned, cooldown %.0fs > deadline %.0fs",
                            len(self.tabs), wait, left)
                raise last_ban or DuckAIBan(f"all tabs banned, cooldown {wait:.0f}s")
            tab = min(cands, key=self._waiters)
            try:
                got = False
                async for tok in tab.stream(prompt, max(1.0, deadline - time.monotonic())):
                    got = True
                    yield tok
                return
            except DuckAIBan as e:
                last_ban = e
                tab.banned_until = time.monotonic() + BAN_COOLDOWN
                await tab._drop_page()
                logger.warning("tab banned (ERR_BN_LIMIT=418), cooldown %.0fs, retrying", BAN_COOLDOWN)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)
                continue
            except DuckAIError:
                raise
            except Exception as e:
                # Playwright transport mati -> rebuild browser, surface 502.
                try:
                    if self.browser:
                        await self.browser.close()
                except Exception:
                    pass
                self.browser = None
                self.ctx = None
                self.tabs = []
                raise DuckAIError(f"browser transport failed: {e}")

    async def prewarm(self) -> bool:
        try:
            await self._ensure()
            t = self.tabs[0]
            async with t.lock:
                await t._open_warm()
            return True
        except Exception as e:
            logger.warning("prewarm failed (%s); lazy start instead", e)
            return False

    async def close(self) -> None:
        for t in self.tabs:
            await t.close()
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self.browser = None
        self.ctx = None
        self.tabs = []
