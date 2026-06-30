# Whatnot Price Checker

A desktop overlay for Whatnot live streams that screenshots a selected card area on demand, detects the Pokemon card via OCR, and displays current TCGPlayer pricing.

## Features

- **Region Picker** — draw a rectangle over the card area of any stream on any monitor
- **Dual-region OCR** — scans the top of the card for the name and the bottom for the collector number, with aggressive upscaling for stream-quality footage
- **Fuzzy name matching** — matches OCR output against a dictionary of 978 Pokemon names so typos like "Moltros" still resolve to "Moltres"
- **TCGPlayer pricing** — pulls current NM market, low, and mid prices from the official TCGPlayer API
- **Foil / Normal toggle** — manually switch between foil and normal pricing
- **On-demand scans** — press `S` when the card is visible instead of constantly refreshing the stream
- **Clear API errors** — the overlay reports missing credentials or TCGPlayer request failures
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
TCGPLAYER_PUBLIC_KEY=your_tcgplayer_public_key_here
TCGPLAYER_PRIVATE_KEY=your_tcgplayer_private_key_here
# or:
# TCGPLAYER_ACCESS_TOKEN=your_existing_bearer_token
```

### All environment variables

| Variable | Default | Description |
|---|---|---|
| `TCGPLAYER_ACCESS_TOKEN` | — | Bearer token for TCGPlayer API |
| `TCGPLAYER_PUBLIC_KEY` | — | OAuth client ID for TCGPlayer API |
| `TCGPLAYER_PRIVATE_KEY` | — | OAuth client secret for TCGPlayer API |
| `WPC_UI_REFRESH_MS` | `450` | Overlay text refresh interval (ms) |

Optional image recognition:

| Variable | Description |
|---|---|
| `GOOGLE_VISION_API_KEY` | Enables Google Vision Web Detection as an additional card identification signal |

## Usage

```bash
python -m whatnot_price_checker
```

1. A fullscreen overlay appears — **draw a rectangle** over the area where the card is shown on the stream
2. Click **Use Region**
3. When the card you want is visible, press **S** or click **Scan (S)**
4. The overlay shows the detected card name, set, printing, and NM market price
5. Use the **Normal/Foil** button to toggle between foil and normal pricing
6. Use **Pick Region** to re-select the scan area
7. Close with the **x** button

## How It Works

```
S key → Screen Capture → Resize to Card → Dual OCR → Fuzzy Match → API Lookup → Overlay
        (mss)          (card.py)     (top 25%    (pokemon     (TCGPlayer)  (PySide6)
                                   + bottom    names.json)
                                   15%)
```

1. `mss` captures the user-drawn screen region when you press `S`
2. The frame is resized to standard card dimensions (420 x 587 px)
3. Two separate OCR passes run on the top 25% (name) and bottom 15% (collector number)
4. The OCR name is fuzzy-matched against a dictionary of all Pokemon names
5. The matched name + collector number are sent to TCGPlayer for current pricing
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
├── tcgapi_client.py     # Legacy tcgapi.dev API client
├── tcgplayer.py         # Official TCGPlayer API client
└── win_window.py        # Windows window utilities (kept for reference)
```
