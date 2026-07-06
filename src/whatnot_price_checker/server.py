"""Local HTTP API backing the browser extension.

The extension captures pixel-perfect frames directly from the page's
<video> element (no OS screen-recording permission, no DPI/scaling
ambiguity) and POSTs them here for OCR + price lookup, reusing every fix
made to the EasyOCR/JustTCG/TCGPlayer pipeline. Runs on localhost only —
this never needs to be exposed to the network.
"""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from whatnot_price_checker.config import Settings
from whatnot_price_checker.justtcg_client import JustTcgClient
from whatnot_price_checker.scan_service import run_scan
from whatnot_price_checker.tcgplayer import TcgClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("wpc.server")

PORT = 8743

settings = Settings.from_env()

_tcgplayer: TcgClient | None = None
if settings.tcgplayer_access_token or (
    settings.tcgplayer_public_key and settings.tcgplayer_private_key
):
    _tcgplayer = TcgClient(settings)

_justtcg: JustTcgClient | None = None
if settings.justtcg_api_key:
    _justtcg = JustTcgClient(settings.justtcg_api_key)

if _tcgplayer is None and _justtcg is None:
    log.warning(
        "No price API configured — set JUSTTCG_API_KEY (recommended, gives "
        "real NM/LP/MP/HP/DM prices) or TCGPLAYER_* in .env. Scans will "
        "still run OCR but won't return prices."
    )

app = FastAPI(title="Whatnot Price Checker (local)")

# The extension's content/background scripts run from a chrome-extension://
# origin; CORS is wide open here since this only ever listens on localhost.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ScanRequest(BaseModel):
    image: str  # data URL (data:image/png;base64,...) or raw base64
    preferFoil: bool = False


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "hasTcgplayer": _tcgplayer is not None,
        "hasJusttcg": _justtcg is not None,
    }


@app.post("/scan")
def scan(req: ScanRequest) -> dict:
    raw = req.image.split(",", 1)[-1] if "," in req.image else req.image
    try:
        data = base64.b64decode(raw)
    except Exception:
        return {"status": "error", "detail": "Invalid image payload."}

    arr = np.frombuffer(data, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)  # cv2 decodes straight to true BGR
    if frame is None:
        return {"status": "error", "detail": "Could not decode image."}

    if os.environ.get("WPC_DEBUG_SAVE_CAPTURES"):
        try:
            debug_path = Path.home() / ".whatnot_price_checker" / "debug_last_capture.png"
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(debug_path), frame)
            log.info("Saved raw capture to %s (%dx%d)", debug_path, frame.shape[1], frame.shape[0])
        except Exception:
            log.exception("Failed to save debug capture")

    try:
        return run_scan(
            frame,
            settings=settings,
            prefer_foil=req.preferFoil,
            tcgplayer=_tcgplayer,
            justtcg=_justtcg,
        )
    except Exception as e:  # noqa: BLE001 - surfaced to the extension as `detail`
        log.exception("Scan failed")
        return {"status": "error", "detail": str(e)}


def main() -> None:
    import uvicorn

    log.info("Starting on http://127.0.0.1:%d (extension connects here)", PORT)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
