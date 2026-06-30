"""
One-off script: fetch Japanese names from PokeAPI and write pokemon_names_ja_map.json.

Run from repo root:
  py -3 scripts/fetch_pokemon_ja_map.py
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
NAMES_JSON = ROOT / "src" / "whatnot_price_checker" / "pokemon_names.json"
OUT_JSON = ROOT / "src" / "whatnot_price_checker" / "pokemon_names_ja_map.json"

BASE = "https://pokeapi.co/api/v2/pokemon-species"

# Slug overrides where automatic conversion fails
SLUG_OVERRIDES: dict[str, str | list[str]] = {
    "Farfetch'd": "farfetchd",
    "Sirfetch'd": "sirfetchd",
    "Type: Null": "type-null",
    "Porygon-Z": "porygon-z",
    "Ho-Oh": "ho-oh",
    "Mime Jr.": "mime-jr",
    "Mr. Mime": "mr-mime",
    "Mr. Rime": "mr-rime",
    "Nidoran": ["nidoran-f", "nidoran-m"],
    "Flabébé": "flabebe",
}


def to_slug(name: str) -> str:
    if name in SLUG_OVERRIDES:
        v = SLUG_OVERRIDES[name]
        if isinstance(v, list):
            return ""  # handled separately
        return v
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = s.replace("'", "").replace(".", "").replace(":", "")
    s = re.sub(r"\s+", "-", s.strip())
    s = re.sub(r"-+", "-", s)
    return s


def fetch_species(client: httpx.Client, slug: str) -> dict | None:
    r = client.get(f"{BASE}/{slug}", follow_redirects=True)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def ja_names_from_species(data: dict) -> list[str]:
    out: list[str] = []
    for entry in data.get("names") or []:
        lang = (entry.get("language") or {}).get("name") or ""
        if lang in ("ja", "ja-Hrkt", "ja-hrkt"):
            n = (entry.get("name") or "").strip()
            if n and n not in out:
                out.append(n)
    return out


def main() -> None:
    all_names: list[str] = json.loads(NAMES_JSON.read_text(encoding="utf-8"))
    split_at = all_names.index("Ace Trainer")
    species_names = all_names[:split_at]

    ja_to_en: dict[str, str] = {}

    with httpx.Client(timeout=30.0) as client:
        for en in species_names:
            slugs: list[str]
            if en in SLUG_OVERRIDES and isinstance(SLUG_OVERRIDES[en], list):
                slugs = SLUG_OVERRIDES[en]  # type: ignore[assignment]
            else:
                slugs = [to_slug(en)]

            for slug in slugs:
                if not slug:
                    continue
                data = fetch_species(client, slug)
                if data is None:
                    print(f"WARN: 404 for {en!r} slug={slug!r}")
                    continue
                for ja in ja_names_from_species(data):
                    # Prefer first English canonical for duplicates
                    if ja not in ja_to_en:
                        ja_to_en[ja] = en
                time.sleep(0.05)  # be gentle to PokeAPI

    # Sort keys for stable diffs (Unicode order)
    ordered = {k: ja_to_en[k] for k in sorted(ja_to_en.keys())}

    OUT_JSON.write_text(
        json.dumps(ordered, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(ordered)} Japanese keys -> English to {OUT_JSON}")


if __name__ == "__main__":
    main()
