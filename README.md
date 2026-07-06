# Whatnot Price Checker

A Chrome extension + local Python backend for Whatnot live streams: press **W** while a card is on screen, it captures the frame straight from the stream's video element, identifies the Pokemon card via OCR, and shows current pricing in a Slabbr-style scan card — thumbnail, big price, condition grid, and tags.

A legacy Qt desktop overlay (OS-level screenshotting) is also still included — see [Legacy desktop app](#legacy-desktop-app) below — but the **browser extension is the recommended way to run this now**: it reads pixel data directly off the page's `<video>` element, so there's no OS screen-recording permission, no Retina/DPI scaling guesswork, and no risk of capturing the wrong window.

## Architecture

```
Whatnot tab (content script)              localhost:8743 (Python)
┌─────────────────────────────┐           ┌───────────────────────────┐
│ "W" press                    │           │ FastAPI /scan endpoint    │
│   → canvas.drawImage(video)  │  fetch    │   → warp_for_ocr          │
│   → PNG data URL              │ ───────▶ │   → EasyOCR                │
│   → overlay UI renders result│ ◀───────  │   → JustTCG / TCGPlayer    │
└─────────────────────────────┘   JSON     └───────────────────────────┘
        (background.js relays the fetch so it isn't subject to the
         page's CORS policy — see extension/background.js)
```

The extension only handles capture + UI; all OCR and price-lookup logic lives in the same Python modules used by the legacy desktop app (`scan_service.py` is the shared, Qt-free pipeline).

## Features

- **Direct video-frame capture** — reads pixels straight from the stream's `<video>` element via canvas; no screen-recording permission, no desktop/window mix-ups
- **Dual-region OCR** — scans the top of the card for the name and the bottom for the collector number, with aggressive upscaling for stream-quality footage
- **Fuzzy name matching** — matches OCR output against a dictionary of 978 Pokemon names so typos like "Moltros" still resolve to "Moltres"
- **TCGPlayer pricing** — pulls current NM market, low, and mid prices from the official TCGPlayer API
- **Real per-condition pricing (JustTCG)** — when `JUSTTCG_API_KEY` is set, the overlay shows real Near Mint / Lightly Played / Moderately Played / Heavily Played / Damaged prices and 24h price change, not just NM
- **Foil / Normal toggle** — manually switch between foil and normal pricing
- **On-demand scans** — press `W` when the card is visible instead of constantly refreshing the stream
- **Card thumbnail** — the overlay shows the actual captured/warped crop of the scanned card
- **Confidence + price tags** — a HIGH/MEDIUM/LOW/NO MATCH confidence pill and a price-tier pill ("Bulk <$5", etc.)
- **Draggable overlay** — injected via Shadow DOM (fully isolated from Whatnot's styles), remembers its position across reloads

## Requirements

- Python 3.10+
- Google Chrome (or any Chromium-based browser that supports Manifest V3 unpacked extensions)

## Installation

```bash
git clone https://github.com/your-username/whatnot-price-checker.git
cd whatnot-price-checker
pip install -e ".[ocr]"
```

The `[ocr]` extra installs EasyOCR and its dependencies (PyTorch, etc.). If you want to install core dependencies first and add OCR later:

```bash
pip install -e .
# then later:
pip install easyocr
```

## Configuration

Create a `.env` file in the project root:

```env
TCGPLAYER_PUBLIC_KEY=your_tcgplayer_public_key_here
TCGPLAYER_PRIVATE_KEY=your_tcgplayer_private_key_here
# or:
# TCGPLAYER_ACCESS_TOKEN=your_existing_bearer_token

# Optional — enables the real NM/LP/MP/HP/DM condition grid:
JUSTTCG_API_KEY=your_justtcg_api_key_here
```

### All environment variables

| Variable | Default | Description |
|---|---|---|
| `TCGPLAYER_ACCESS_TOKEN` | — | Bearer token for TCGPlayer API |
| `TCGPLAYER_PUBLIC_KEY` | — | OAuth client ID for TCGPlayer API |
| `TCGPLAYER_PRIVATE_KEY` | — | OAuth client secret for TCGPlayer API |
| `JUSTTCG_API_KEY` | — | API key from [justtcg.com](https://justtcg.com) — enables real per-condition (NM/LP/MP/HP/DM) prices and 24h price change. Without it, the overlay falls back to a single TCGPlayer market price and the condition grid stays empty. |

## Usage

### 1. Start the local server

```bash
whatnot-price-checker-server
# (equivalent to: python -m whatnot_price_checker.server)
```

Leave this running in a terminal — it listens on `http://127.0.0.1:8743` and only ever talks to your own machine. You should see:

```
Starting on http://127.0.0.1:8743 (extension connects here)
Uvicorn running on http://127.0.0.1:8743 (Press CTRL+C to quit)
```

### 2. Load the extension

1. Open `chrome://extensions` in Chrome
2. Enable **Developer mode** (top-right toggle)
3. Click **Load unpacked** and select this repo's `extension/` folder
4. Pin the extension if you'd like, but no popup interaction is needed — it works automatically on `whatnot.com`

### 3. Scan a card

1. Open any Whatnot stream and make sure the card is visible in the video
2. Press **W** — the overlay appears in the top-right corner showing the scan card: thumbnail, price, name/set/number, confidence tag, and (with `JUSTTCG_API_KEY` set) a clickable NM/LP/MP/HP/DM condition grid
3. Use the **Normal/Foil** button to toggle between foil and normal pricing
4. Click **VIEW ON TCGPLAYER** to open the matched product page
5. Drag the panel by its header to reposition it (remembered across reloads); close with **×**, reopen by pressing **W** again

If you see "Couldn't reach the local server," make sure step 1's server is still running.

## How It Works

```
"W" press → canvas.drawImage(video) → POST /scan → warp_for_ocr → Dual OCR → Fuzzy Match → Price Lookup → Overlay renders
            (content.js)              (FastAPI)     (card.py)    (top 25%    (pokemon      (TCGPlayer +    (content.js)
                                                                  + bottom    names.json)    JustTCG)
                                                                  15%)
```

1. `content.js` draws the current video frame to an off-screen canvas and exports it as a PNG data URL
2. `background.js` forwards it to the local FastAPI server (routed through the background service worker so the request isn't subject to the page's CORS policy)
3. `server.py` decodes the image and resizes it to standard card dimensions (420 x 587 px) — this same crop becomes the overlay's thumbnail
4. Two separate OCR passes run on the top 25% (name) and bottom 15% (collector number)
5. The OCR name is fuzzy-matched against a dictionary of all Pokemon names
6. The matched name + collector number are sent to TCGPlayer for the catalog match/market price, and to JustTCG (if configured) for the real per-condition price grid
7. The JSON result is sent back to `content.js`, which renders it into the draggable overlay

## Project Structure

```
extension/
├── manifest.json         # Manifest V3 config (host permissions for localhost, content script match)
├── background.js         # Service worker — relays fetches to localhost, bypassing page CORS
├── content.js            # Video capture on "W", Shadow-DOM overlay UI, rendering logic
└── overlay.css           # Overlay styling (injected into the Shadow DOM, isolated from Whatnot's CSS)

src/whatnot_price_checker/
├── __init__.py          # Package version
├── __main__.py          # Legacy desktop app entry point
├── server.py            # FastAPI server backing the extension (POST /scan, GET /health)
├── scan_service.py      # Shared, Qt-free OCR + price-lookup pipeline (used by server.py and app.py)
├── app.py               # Legacy Qt UI (Slabbr-style scan card) + scan worker thread
├── capture.py           # Screen region capture via mss (legacy desktop app only)
├── card.py              # Frame resizing/warping to card dimensions
├── config.py            # Settings from env vars / .env
├── foil.py              # Foil detection heuristic (unused, kept for reference)
├── justtcg_client.py    # JustTCG API client (real NM/LP/MP/HP/DM condition prices)
├── ocr_reader.py        # Dual-region OCR with fuzzy matching
├── pokemon_names.json   # Dictionary of 978 Pokemon names
├── region_picker.py     # Fullscreen region selection overlay (legacy desktop app only)
├── region_store.py      # Persists the last-picked scan region across launches (legacy desktop app only)
├── tcgapi_client.py     # Legacy tcgapi.dev API client
├── tcgplayer.py         # Official TCGPlayer API client
└── win_window.py        # Windows window utilities (kept for reference)
```

## Legacy desktop app

The original OS-screenshot-based overlay still works if you'd rather not use the browser extension:

```bash
python -m whatnot_price_checker
```

It requires **macOS Screen Recording permission** (System Settings → Privacy & Security → Screen Recording — enable it for your terminal/IDE, then fully quit and relaunch). Without it, `mss` silently captures your desktop wallpaper instead of the stream. The first run draws a region picker over your screen; press **W** to screenshot that region and scan, same key as the extension.
