<p align="center">
  <img src="https://img.shields.io/badge/duck.ai-session%20relay-e8b64c?style=flat-square" alt="duck.ai relay">
  &nbsp;
  <img src="https://img.shields.io/badge/OpenAI-compatible-7fb069?style=flat-square" alt="OpenAI compatible">
  &nbsp;
  <img src="https://img.shields.io/badge/streaming-SSE-242c33?style=flat-square" alt="SSE streaming">
</p>

<h1 align="center">DuckAI Web-to-API</h1>

<p align="center">
  <strong>One OpenAI endpoint for the entire duck.ai catalog.</strong><br>
  A real Chrome browser behind the scenes. No API key. No token cost.<br>
  English | <a href="./README.id.md">Bahasa Indonesia</a>
</p>

---

## What this is

duck.ai does not offer a public API — only its web app. This project takes
that as the interface: a real Chrome browser stays alive on your server,
prompts are typed into the composer the way a person would, and every answer
comes back out as a standard `POST /v1/chat/completions` endpoint.

What sets this project apart is not a feature checklist but three design
decisions:

**1. Capacity first, not just "it works".**
One session per model means the second request waits for the first to
finish. Here, every model gets its own pool of browser tabs — requests route
to the idlest tab, a flagged tab steps back without taking the others down.

**2. Streams are tapped, not re-read.**
Answers are not scraped from the DOM. A `window.fetch` hook catches duck.ai's
native SSE at the network layer, so tokens arrive as the model produces them —
pure deltas, no polling, no reflow.

**3. Failure is explicit.**
No silent retries, no empty answers that look like success. A tab duck.ai
flags is recorded, cooled down, and reported. When every tab is flagged the
API answers `429` with concrete advice. When the page breaks, `502`. Your
client always knows what happened.

> **Status:** unofficial project. Not affiliated with duck.ai or DuckDuckGo.
> Automating the web app may violate their terms; your IP can be permanently
> blocked (`418 ERR_BN_LIMIT`); page changes can break this service at any
> time without notice. Operate at a scale you can account for.

> **Note:** this project is published for **learning and educational
> purposes only** — studying how browser automation, SSE interception, and
> API gateway design work together. It is not intended for production traffic
> or commercial use. What you run and where you run it is your own
> responsibility.

## Getting started

```bash
git clone https://github.com/T3Crypt/DuckAI-Web-to-API.git
cd DuckAI-Web-to-API
pip install -r requirements.txt
python3 main.py
```

Done. `http://127.0.0.1:8080` is now live.

- `GET /health` → readiness (status, pools, catalog)
- `GET /` → live status dashboard
- `GET /docs` → interactive OpenAPI (FastAPI built-in)

Straight from curl:

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.4-mini",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": true
  }'
```

Or from any SDK:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key="x")
stream = client.chat.completions.create(
    model="claude-haiku-4-5",
    messages=[{"role": "user", "content": "Hi"}],
    stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="")
```

## Requirements

| | |
|---|---|
| Python | 3.10+ |
| Google Chrome | must be installed (default `/usr/bin/google-chrome`) |
| RAM | ~60–100 MB + ~10 processes per active tab |

Playwright's bundled Chromium **will not work** — duck.ai fingerprints it and
blocks it on the first request. Real Chrome only.

## Configuration

Everything through environment variables; the defaults are sane untouched:

| Variable | Default | Purpose |
|---|---|---|
| `DUCKAI_POOL_SIZE` | `3` | Browser tabs per model |
| `DUCKAI_API_KEY` | — | Set to require `Authorization: Bearer` |
| `DUCKAI_PROXIES` | — | Proxy pool (residential/SOCKS5), reused when a launch fails |
| `DUCKAI_MODEL` | `gpt-5.6-luna` | Fallback model |
| `DUCKAI_BAN_COOLDOWN` | `300` | Seconds a flagged tab sits out |
| `DUCKAI_REQ_TIMEOUT` | `180` | Per-request deadline, retries included |
| `DUCKAI_CATALOG_TTL` | `3600` | Catalog refresh interval |
| `DUCKAI_WARM_MIN` / `DUCKAI_WARM_MAX` | `2` / `7` | Page-readiness poll window |
| `DUCKAI_PROMPT_LIMIT` | `12000` | Head+tail clamp for long prompts |
| `DUCKAI_PREWARM` | `1` | Warm the pool at boot |
| `DUCKAI_CHROME_PATH` | `/usr/bin/google-chrome` | Chrome binary location |
| `PORT` / `HOST` | `8080` / `0.0.0.0` | Bind address |

## Endpoints

| Route | Purpose |
|---|---|
| `POST /v1/chat/completions` | Chat, streaming or buffered |
| `GET /v1/models` | Live catalog + tiers |
| `GET /health` | Liveness + pool state |
| `GET /` | Dashboard |
| `POST /v1/images/generations` | Image generation (via chat-side tool) |

Errors follow the OpenAI convention — `{"error": {"message": "...", "type": "..."}}`
with the right HTTP status (`404` unknown model, `401` auth, `400` bad
payload, `429` all tabs flagged, `502` upstream failure, `504` timeout).

## Model catalog

Pulled straight from duck.ai's own models endpoint
(`GET https://duck.ai/duckchat/v1/models`), refreshed periodically in the
background. The tier column mirrors the `accessTier` field in their response —
not this project's assumption.

| Model | Tier (from their API) |
|---|---|
| `gpt-5.4-mini` | free |
| `claude-haiku-4-5` | free |
| `tinfoil/gpt-oss-120b` | free |
| `gpt-5.6-luna` | no tier data |
| `mistral-small-2603` | no tier data |
| `tinfoil/gemma4-31b` | no tier data |
| `gpt-5.6-terra` | plus/pro |
| `gpt-5.6-sol` | plus/pro |
| `claude-sonnet-4-6` | plus/pro |
| `claude-opus-4-8` | pro |

Models tiered `plus`/`pro` require a paid duck.ai session. The list changes
without notice — `GET /v1/models` always reflects the current truth.

## Dashboard

Open `http://127.0.0.1:8080/` — the status board lives in
[`dashboard.html`](dashboard.html), one static file with no framework and no
build step. Dark terminal aesthetic: monospace, one accent color, section
headers styled like code comments. Polls `/health` every 5 seconds, the
catalog every 30. Edit that file to restyle; the server picks it up on
restart.

```
.
├── main.py           # FastAPI server, endpoints, config
├── duckai.py         # Playwright client: tab pool, SSE tee, typing
├── dashboard.html    # live status board served at /
└── requirements.txt
```

### Custom port

The server reads `PORT` and `HOST` at startup:

```bash
PORT=9000 python3 main.py                    # listen on :9000
PORT=9000 HOST=127.0.0.1 python3 main.py     # localhost only
```

Or make it permanent in the systemd unit:

```ini
[Service]
Environment=PORT=9000
Environment=HOST=0.0.0.0
```

then `systemctl daemon-reload && systemctl restart duckai`. Update every
client's base URL afterward (`http://172.17.0.1:9000/v1` from containers).

## Deployment

**systemd** (recommended):

```ini
[Unit]
Description=DuckAI Web-to-API
After=network.target

[Service]
WorkingDirectory=/opt/duckai-web-to-api
ExecStart=/usr/bin/python3 /opt/duckai-web-to-api/main.py
Environment=DUCKAI_POOL_SIZE=1
MemoryMax=900M
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`MemoryMax` is not optional on small machines — every Chrome tab is ~10
processes, and a ballooning browser must get killed before the host does.

**From other containers** (API routers, n8n, etc.): `127.0.0.1` inside a
container is the container itself. Use `http://172.17.0.1:8080/v1`.

## License

Released under the [MIT License](LICENSE) — free to use, modify, and
redistribute, including commercially, as long as the copyright notice stays
intact. The license covers this repository's code only; duck.ai itself
remains a third-party service with its own terms, and nothing here grants
permission to automate it.
