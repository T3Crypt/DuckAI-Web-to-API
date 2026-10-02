# DuckAI Web-to-API

> OpenAI-compatible API wrapper untuk **duck.ai** (DuckDuckGo AI Chat) — pakai browser Chrome asli, gratis, 10 model (termasuk Claude Opus/Sonnet 4.x & GPT-5.6), streaming SSE token-per-token.

Python/FastAPI/Playwright. Dibangun lebih baik dari referensi hirotomasato/duckapi.

---

## Fitur vs Referensi

| Fitur | Referensi (duckapi) | Ini |
|---|---|---|
| Session | 1 tab/model, lock serial | **Tab pool round-robin** (`DUCKAI_POOL_SIZE`), lock per-tab |
| Image instruction | Hardcode bahasa China | **English** |
| `/v1/models` | Statis | **Live catalog** `duckchat/v1/models` + TTL refresh + fallback |
| Ban 418/ERR_BN_LIMIT | Mati total | **Cooldown per-tab + retry queue + rotasi proxy → HTTP 429 kalau semua ban** |
| Dashboard | 660 lines HTML | **Tidak ada (YAGNI)** — log stdout |
| Kode | ~2.400 lines | **~870 lines** (2 file) |
| Max context | 24K "never replies" | **24K jalan** (clamp pakai `maxlength` UI asli) |

## Arsitektur

```
Client (OpenAI SDK/apa pun)
   │  POST /v1/chat/completions (stream/non-stream)
   ▼
FastAPI (main.py)  ──►  ModelPool (duckai.py)
   │                       │  N tab Chrome per model (round-robin, least-waiters)
   │                       │  per-tab: mutex + ban cooldown + lastPrompt (delta typing)
   ▼                       ▼
Chrome (channel="chrome", headless) ──► duck.ai
   │  fetch hook: r.clone().body.getReader() → tangkap SSE asli
   ▼
SSE tee → parse event → OpenAI-format chunk
```

Teknik inti:
- **Real Chrome wajib** — Chromium bundled Playwright langsung kena ban (`ERR_BN_LIMIT`); Chrome asli cuma kena challenge yang bisa lewat via UI-driven send.
- **UI-driven send** — isi textarea + klik Send (bukan POST internal API), fingerprint tetap "manusia".
- **SSE tee** — hook `window.fetch` sebelum halaman jalan, `clone()` response, baca stream mentah tanpa ganggu UI.
- **Delta typing** — prompt baru yang diawali prompt terakhir cukup ngetik selisihnya (~3 detik vs ~10 detik per turn).
- **Warmup poll** — tunggu first-token dengan poll `WARM_MIN..WARM_MAX`, bukan sleep mati.
- **Prompt clamp** — ikuti atribut `maxlength` textarea; fallback potong head+tail 12K.

## Instalasi

### 1. Persiapan

```bash
# Ubuntu/Debian — Chrome asli (WAJIB, bukan chromium)
wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | sudo gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" | sudo tee /etc/apt/sources.list.d/google-chrome.list
sudo apt update && sudo apt install -y google-chrome-stable

# Python 3.10+ + deps
pip install -r requirements.txt   # fastapi uvicorn playwright
playwright install chromium       # hanya untuk driver-nya; browser yang dipakai tetap Chrome asli
```

### 2. Jalankan

```bash
cd duckai-web-to-api

# minimal
python3 main.py

# produksi (disarankan)
DUCKAI_POOL_SIZE=2 DUCKAI_PREWARM=1 python3 main.py
# → http://127.0.0.1:8080
```

### 3. systemd (auto-start + RAM guard)

```ini
# /etc/systemd/system/duckai.service
[Unit]
Description=DuckAI Web-to-API
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/duckai-web-to-api
ExecStart=/usr/bin/python3 /opt/duckai-web-to-api/main.py
Environment=DUCKAI_POOL_SIZE=1
Restart=on-failure
RestartSec=5
# WAJIB di VPS kecil (< 2GB) — service kena kill dulu sebelum VPS OOM
MemoryMax=900M
MemoryHigh=700M

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now duckai
```

> ⚠️ **VPS 1.9GB jangan pakai pool > 1** dan **jangan jalankan dua server bareng**.
> Setiap tab Chrome = ~10 proses + 60–100MB. Pool 2 × 10 model = 20+ tab ≈ 2GB+ → OOM → VPS freeze.

## Environment Variables

| Var | Default | Arti |
|---|---|---|
| `DUCKAI_POOL_SIZE` | `3` | Jumlah tab per model (round-robin) |
| `DUCKAI_PREWARM` | `0` | `1` = hangatkan tab model pertama saat boot |
| `DUCKAI_BASE` | `https://duck.ai` | Base URL target |
| `DUCKAI_CHROME_PATH` | auto | Path Chrome (mis. `/usr/bin/google-chrome`) |
| `DUCKAI_WARM_MIN` / `DUCKAI_WARM_MAX` | `2.0` / `7.0` | Rentang poll warmup (detik) |
| `DUCKAI_BAN_COOLDOWN` | `300` | Cooldown tab kena ban (detik) |
| `DUCKAI_PROMPT_LIMIT` | `12000` | Clamp prompt (fallback) |
| `DUCKAI_CATALOG_TTL` | `3600` | Refresh katalog model (detik) |
| `DUCKAI_REQ_TIMEOUT` | `180` | Timeout request (detik) |
| `DUCKAI_API_KEY` | *(kosong)* | Kalau diisi, wajib `Authorization: Bearer <key>` |
| `DUCKAI_PROXIES` | *(kosong)* | Daftar proxy dipisah koma (rotasi saat ban) |
| `PORT` | `8080` | Port listen |

## Endpoint

| Endpoint | Keterangan |
|---|---|
| `POST /v1/chat/completions` | OpenAI-compatible, `stream: true/false` |
| `GET /v1/models` | Katalog live dari duck.ai (+ fallback statis, TTL 1 jam) |
| `GET /health` | Status + pools + `catalog_live` |
| `POST /v1/images/generations` | Stub — baru return revised prompt |

### Contoh

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5.4-mini","messages":[{"role":"user","content":"Halo!"}],"stream":false}'
```

```python
from openai import OpenAI
c = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key="x")
for ch in c.chat.completions.create(model="claude-haiku-4-5",
        messages=[{"role":"user","content":"Hai"}], stream=True):
    print(ch.choices[0].delta.content or "", end="")
```

### Pakai dari Docker (mis. 10router/9router di container)

`127.0.0.1` dari dalam container = container itu sendiri → **fetch failed**.
Pakai IP bridge Docker host:

```
Base URL: http://172.17.0.1:8080/v1
```

## Benchmark (hasil nyata, semua via API ini)

| Model | Tier | Reasoning | Intel 3/3 | TTFT | Tok/s | Instr |
|---|---|---|---|---|---|---|
| gpt-5.6-terra | paid | ✅ | ✅ | 10.3s | 259.1 | ✅ |
| tinfoil/gemma4-31b | free | ✅ | ✅ | 15.1s | 195.3 | ✅ |
| tinfoil/gpt-oss-120b | free | ✅ | ✅ | 15.3s | 188.0 | ✅ |
| mistral-small-2603 | free | ✅ | ✅ | 12.7s | 155.5 | ✅ |
| gpt-5.4-mini | free | ✅ | ✅ | 8.3s | 153.1 | ✅ |
| claude-sonnet-4-6 | paid | ✅ | ✅ | 9.6s | 242.5 | ✅ |
| claude-opus-4-8 | paid | ✅ | ✅ | 10.3s | 118.9 | ✅ |
| gpt-5.6-sol | paid | ✅ | ✅ | 11.2s | 235.1 | ✅ |
| gpt-5.6-luna | free | ✅ | ✅ | 8.2s | 82.9 | ✅ |
| claude-haiku-4-5 | free | ✅ | ✅ | 12.7s | 120.9 | ✅ |

10/10 PASS (math multi-step, puzzle deduksi, trivia, instruction-following).
Jalankan ulang: `python3 benchmark.py` → `benchmark_results.json`.

## Testing

```bash
python3 test_all_models.py     # 10/10 model + timing
python3 benchmark.py           # benchmark lengkap
```

## Troubleshooting

| Gejala | Sebab → Solusi |
|---|---|
| `ERR_BN_LIMIT` / 418 | Pakai Chromium bundled → wajib `channel="chrome"` / Chrome asli |
| `ERR_CHALLENGE` berulang | Headless kedetek → coba `headless=False` + Xvfb |
| First request 60–90s | Normal (cold start + challenge) — bukan service mati |
| Semua tab ban → HTTP 429 | Cooldown `DUCKAI_BAN_COOLDOWN` habis → tunggu/rotasi proxy |
| VPS freeze/OOM | Pool terlalu besar atau 2 server jalan → pool 1 + systemd `MemoryMax` |
| `fetch failed` dari container | Container ≠ host → pakai `http://172.17.0.1:8080/v1` |

## Lisensi & Kredit

MIT. Teknik inti (UI-driven send, SSE tee, delta typing) diinspirasi
[hirotomasato/duckapi](https://github.com/hirotomasato/duckapi) — ditulis ulang
lebih ringan dengan perbaikan di atas.
