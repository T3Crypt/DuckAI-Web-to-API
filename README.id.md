<p align="center">
  <img src="https://img.shields.io/badge/duck.ai-session%20relay-e8b64c?style=flat-square" alt="duck.ai relay">
  &nbsp;
  <img src="https://img.shields.io/badge/OpenAI-compatible-7fb069?style=flat-square" alt="OpenAI compatible">
  &nbsp;
  <img src="https://img.shields.io/badge/streaming-SSE-242c33?style=flat-square" alt="SSE streaming">
</p>

<h1 align="center">DuckAI Web-to-API</h1>

<p align="center">
  <strong>Satu endpoint OpenAI untuk seluruh katalog duck.ai.</strong><br>
  Chrome asli di belakang layar. Tanpa API key. Tanpa biaya token.<br>
  <a href="./README.md">English</a> | Bahasa Indonesia
</p>

---

## Ini apa

duck.ai menutup akses API-nya. Yang dibuka cuma web app-nya. Project ini
mengambil sisi itu: satu Chrome asli dibiarkan hidup di server, prompt
diketik ke composer seperti manusia, dan semua jawaban dialirkan ulang
sebagai endpoint `POST /v1/chat/completions` standar.

Yang membedakan project ini bukan daftar fitur, tapi tiga keputusan desain:

**1. Kapasitas dulu, bukan sekadar jalan.**
Satu sesi per model berarti request kedua menunggu request pertama selesai.
Di sini setiap model punya kolam tab browser — request di-routing ke tab
paling sepi, tab yang kena blok mundur sementara tanpa menjatuhkan yang lain.

**2. Stream disadap, bukan dibaca ulang.**
Jawaban tidak di-scrape dari DOM. Hook `window.fetch` menangkap SSE asli
duck.ai di lapisan jaringan, jadi token sampai saat model mengeluarkannya —
delta murni, tanpa polling, tanpa reflow.

**3. Kegagalan itu eksplisit.**
Tidak ada retry diam-diam, tidak ada jawaban kosong yang terlihat sukses.
Tab yang ditandai duck.ai dicatat, di-cooldown, dilaporkan. Kalau semua tab
terkena, API menjawab `429` dengan saran konkret. Kalau halaman rusak, `502`.
Client Anda selalu tahu apa yang terjadi.

> **Status:** project tidak resmi. Tidak ada hubungannya dengan duck.ai atau
> DuckDuckGo. Mengotomasi web app bisa melanggar ketentuan mereka; IP bisa
> diblokir permanen (`418 ERR_BN_LIMIT`); perubahan halaman bisa merusak
> layanan ini kapan saja tanpa pemberitahuan. Gunakan pada skala yang bisa
> Anda pertanggungjawabkan.

> **Catatan:** project ini dipublikasikan hanya untuk **bahan belajar dan
> edukasi** — memahami cara browser automation, penyadapan SSE, dan desain
> API gateway bekerja bersama. Tidak dimaksudkan untuk trafik produksi atau
> penggunaan komersial. Apa yang Anda jalankan dan di mana Anda menjalankannya
> adalah tanggung jawab Anda sendiri.

## Cara pakai

```bash
git clone https://github.com/T3Crypt/DuckAI-Web-to-API.git
cd DuckAI-Web-to-API
pip install -r requirements.txt
python3 main.py
```

Selesai. `http://127.0.0.1:8080` sudah hidup.

- `GET /health` → readiness (status, pool, katalog)
- `GET /` → dashboard status live
- `GET /docs` → OpenAPI interaktif (bawaan FastAPI)

Contoh langsung:

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.4-mini",
    "messages": [{"role": "user", "content": "Halo"}],
    "stream": true
  }'
```

Atau dari SDK mana pun:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key="x")
stream = client.chat.completions.create(
    model="claude-haiku-4-5",
    messages=[{"role": "user", "content": "Hai"}],
    stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="")
```

## Kebutuhan

| | |
|---|---|
| Python | 3.10+ |
| Google Chrome | wajib terpasang (default `/usr/bin/google-chrome`) |
| RAM | ±60–100 MB + ±10 proses per tab aktif |

Chromium bawaan Playwright **tidak bisa dipakai** — duck.ai mengenali
fingerprint-nya dan memblokir di request pertama. Hanya Chrome asli.

## Konfigurasi

Semua lewat environment variable, default masuk akal tanpa diset:

| Variable | Default | Fungsi |
|---|---|---|
| `DUCKAI_POOL_SIZE` | `3` | Tab browser per model |
| `DUCKAI_API_KEY` | — | Set untuk mewajibkan `Authorization: Bearer` |
| `DUCKAI_PROXIES` | — | Pool proxy (residential/SOCKS5), dipakai ulang saat launch gagal |
| `DUCKAI_MODEL` | `gpt-5.6-luna` | Model fallback |
| `DUCKAI_BAN_COOLDOWN` | `300` | Detik cooldown tab kena blokir |
| `DUCKAI_REQ_TIMEOUT` | `180` | Deadline per request termasuk retry |
| `DUCKAI_CATALOG_TTL` | `3600` | Interval refresh katalog model |
| `DUCKAI_WARM_MIN` / `DUCKAI_WARM_MAX` | `2` / `7` | Jendela poll kesiapan halaman |
| `DUCKAI_PROMPT_LIMIT` | `12000` | Clamp prompt panjang (kepala+ekor) |
| `DUCKAI_PREWARM` | `1` | Hangatkan pool saat boot |
| `DUCKAI_CHROME_PATH` | `/usr/bin/google-chrome` | Lokasi binary Chrome |
| `PORT` / `HOST` | `8080` / `0.0.0.0` | Alamat bind |

## Endpoint

| Route | Fungsi |
|---|---|
| `POST /v1/chat/completions` | Chat, streaming atau buffered |
| `GET /v1/models` | Katalog live + tier |
| `GET /health` | Liveness + state pool |
| `GET /` | Dashboard |
| `POST /v1/images/generations` | Image generation (via tool sisi chat) |

Format error mengikuti konvensi OpenAI — `{"error": {"message": "...", "type": "..."}}`
dengan HTTP status yang tepat (`404` model tak dikenal, `401` auth, `400`
payload rusak, `429` semua tab diblokir, `502` upstream gagal, `504` timeout).

## Katalog model

Diambil langsung dari endpoint resmi duck.ai
(`GET https://duck.ai/duckchat/v1/models`), di-refresh berkala di background.
Kolom tier mengikuti field `accessTier` dari respons mereka — bukan asumsi
project ini.

| Model | Tier (dari API mereka) |
|---|---|
| `gpt-5.4-mini` | free |
| `claude-haiku-4-5` | free |
| `tinfoil/gpt-oss-120b` | free |
| `gpt-5.6-luna` | tanpa data tier |
| `mistral-small-2603` | tanpa data tier |
| `tinfoil/gemma4-31b` | tanpa data tier |
| `gpt-5.6-terra` | plus/pro |
| `gpt-5.6-sol` | plus/pro |
| `claude-sonnet-4-6` | plus/pro |
| `claude-opus-4-8` | pro |

Model dengan tier `plus`/`pro` butuh sesi duck.ai berbayar. Daftar berubah
tanpa pemberitahuan — `GET /v1/models` selalu mencerminkan kondisi terkini.

## Dashboard

Buka `http://127.0.0.1:8080/` — papan status ada di
[`dashboard.html`](dashboard.html), satu file statis tanpa framework dan tanpa
build step. Estetika terminal gelap: monospace, satu warna aksen, section
header gaya komentar kode. Polling `/health` tiap 5 detik, katalog tiap 30
detik. Edit file itu untuk restyle; server membacanya ulang saat restart.

```
.
├── main.py           # Server FastAPI, endpoint, konfigurasi
├── duckai.py         # Klien Playwright: tab pool, SSE tee, typing
├── dashboard.html    # Papan status live yang disajikan di /
└── requirements.txt
```

### Port kustom

Server membaca `PORT` dan `HOST` saat start:

```bash
PORT=9000 python3 main.py                    # listen di :9000
PORT=9000 HOST=127.0.0.1 python3 main.py     # localhost saja
```

Atau permanen di unit systemd:

```ini
[Service]
Environment=PORT=9000
Environment=HOST=0.0.0.0
```

lalu `systemctl daemon-reload && systemctl restart duckai`. Jangan lupa
perbarui base URL di semua client (`http://172.17.0.1:9000/v1` dari
container).

## Deployment

**systemd** (disarankan):

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

`MemoryMax` bukan opsional di mesin kecil — setiap tab Chrome ≈ 10 proses,
dan browser yang membesar harus kena kill dulu sebelum host-nya.

**Dari container lain** (router API, n8n, dsb): `127.0.0.1` di dalam container
adalah container itu sendiri. Gunakan `http://172.17.0.1:8080/v1`.

## Lisensi

Dirilis di bawah [Lisensi MIT](LICENSE) — bebas dipakai, dimodifikasi, dan
didistribusikan ulang, termasuk untuk komersial, selama notice copyright
tetap disertakan. Lisensi ini mencakup kode repositori ini saja; duck.ai
tetap layanan pihak ketiga dengan ketentuannya sendiri, dan tidak ada di sini
yang memberi izin untuk mengotomasinya.
