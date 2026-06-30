# Whatnot Price Checker

A desktop overlay for Whatnot live streams that automatically detects Pokemon cards via OCR and displays real-time TCGPlayer pricing.

## Features

- **Region Picker** — draw a rectangle over the card area of any stream on any monitor
- **Dual-region OCR** — scans the top of the card for the name and the bottom for the collector number, with aggressive upscaling for stream-quality footage
- **Fuzzy name matching** — matches OCR output against a dictionary of 978 Pokemon names so typos like "Moltros" still resolve to "Moltres"
- **TCGPlayer pricing** — pulls NM market price, low price, and set info via tcgapi.dev
- **Foil / Normal toggle** — manually switch between foil and normal pricing
- **Smart caching** — 24-hour in-memory cache to minimize API calls (tcgapi.dev has a 100/day limit)
- **Network resilience** — automatic retry with backoff on transient failures; clear overlay messages when rate-limited
- **Draggable overlay** — frameless always-on-top window you can move anywhere

## Requirements

- Python 3.10+
- Windows 10/11 (uses `mss` for screen capture and `pywin32` for optional window utilities)

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
TCGAPI_KEY=your_tcgapi_dev_key_here
```

### All environment variables

| Variable | Default | Description |
|---|---|---|
| `TCGAPI_KEY` | — | API key for tcgapi.dev (required for pricing) |
| `WPC_FPS` | `3.0` | Scan rate in frames per second |
| `WPC_TCGAPI_PER_PAGE` | `25` | Results per API search query |
| `WPC_PRICE_CACHE_TTL_SEC` | `86400` | How long to cache prices (seconds) |
| `WPC_UI_REFRESH_MS` | `450` | Overlay text refresh interval (ms) |

Optional TCGPlayer official API (fallback if tcgapi.dev is not configured):

| Variable | Description |
|---|---|
| `TCGPLAYER_ACCESS_TOKEN` | Bearer token for TCGPlayer API |
| `TCGPLAYER_PUBLIC_KEY` | OAuth client ID |
| `TCGPLAYER_PRIVATE_KEY` | OAuth client secret |

## Usage

```bash
python -m whatnot_price_checker
```

1. A fullscreen overlay appears — **draw a rectangle** over the area where the card is shown on the stream
2. Click **Start Scanning**
3. The overlay window appears showing detected card name, set, printing, and NM market price
4. Use the **Normal/Foil** button to toggle between foil and normal pricing
5. Use **Pick Region** to re-select the scan area
6. Close with the **x** button

## How It Works

```
Screen Capture → Resize to Card → Dual OCR → Fuzzy Match → API Lookup → Overlay
     (mss)          (card.py)     (top 25%    (pokemon     (tcgapi.dev)  (PySide6)
                                   + bottom    names.json)
                                   15%)
```

1. `mss` captures the user-drawn screen region every frame
2. The frame is resized to standard card dimensions (420 x 587 px)
3. Two separate OCR passes run on the top 25% (name) and bottom 15% (collector number)
4. The OCR name is fuzzy-matched against a dictionary of all Pokemon names
5. The matched name + collector number are sent to tcgapi.dev for pricing
6. Results are displayed in the draggable overlay window

## Project Structure

```
src/whatnot_price_checker/
├── __init__.py          # Package version
├── __main__.py          # Entry point
├── app.py               # Main UI + scan worker thread
├── capture.py           # Screen region capture via mss
├── card.py              # Frame resizing to card dimensions
├── config.py            # Settings from env vars / .env
├── foil.py              # Foil detection heuristic (unused, kept for reference)
├── ocr_reader.py        # Dual-region OCR with fuzzy matching
├── pokemon_names.json   # Dictionary of 978 Pokemon names
├── region_picker.py     # Fullscreen region selection overlay
├── tcgapi_client.py     # tcgapi.dev API client with retry logic
├── tcgplayer.py         # Official TCGPlayer API client (fallback)
└── win_window.py        # Windows window utilities (kept for reference)
```
