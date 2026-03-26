from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from whatnot_price_checker.config import Settings

POKEMON_CATEGORY_ID = 3


def _payload_ok(payload: dict) -> bool:
    v = payload.get("success")
    if v is None:
        v = payload.get("Success")
    return bool(v)


def _result_rows(payload: dict) -> list:
    return list(payload.get("results") or payload.get("Results") or [])


def _row_get(row: dict, *keys: str):
    for k in keys:
        if k in row:
            return row[k]
    return None


@dataclass(frozen=True)
class PriceQuote:
    product_id: int
    name: str
    market_price: float | None
    low_price: float | None
    sub_type_name: str | None


class TcgClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._root = f"https://api.tcgplayer.com/v{settings.tcg_api_version}"
        self._http = httpx.Client(timeout=30.0)
        self._bearer: str | None = settings.tcgplayer_access_token
        self._bearer_expires: float = time.time() + 86400.0 * 365.0

    def close(self) -> None:
        self._http.close()

    def _refresh_bearer(self) -> None:
        pub = self._settings.tcgplayer_public_key
        priv = self._settings.tcgplayer_private_key
        if not pub or not priv:
            raise RuntimeError("Set TCGPLAYER_PUBLIC_KEY and TCGPLAYER_PRIVATE_KEY (or TCGPLAYER_ACCESS_TOKEN).")
        r = self._http.post(
            "https://api.tcgplayer.com/token",
            data={
                "grant_type": "client_credentials",
                "client_id": pub,
                "client_secret": priv,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        r.raise_for_status()
        data = r.json()
        token = data.get("access_token") or data.get("Access_Token")
        if not token:
            raise RuntimeError("Token response missing access_token.")
        self._bearer = token
        expires_in = data.get("expires_in") or data.get("Expires_In") or 3600
        self._bearer_expires = time.time() + float(expires_in) - 60.0

    def _auth_header(self) -> dict[str, str]:
        if self._bearer and time.time() < self._bearer_expires:
            return {"Authorization": f"bearer {self._bearer}"}
        if self._settings.tcgplayer_access_token:
            self._bearer = self._settings.tcgplayer_access_token
            self._bearer_expires = time.time() + 86400.0 * 365.0
            return {"Authorization": f"bearer {self._bearer}"}
        self._refresh_bearer()
        assert self._bearer is not None
        return {"Authorization": f"bearer {self._bearer}"}

    def search_pokemon_by_name(self, name: str, *, limit: int = 5) -> list[int]:
        headers = {
            **self._auth_header(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        body = {
            "sort": "Relevance",
            "limit": limit,
            "offset": 0,
            "filters": [{"name": "ProductName", "values": [name.strip()]}],
        }
        r = self._http.post(
            f"{self._root}/catalog/categories/{POKEMON_CATEGORY_ID}/search",
            headers=headers,
            json=body,
        )
        r.raise_for_status()
        payload = r.json()
        if not _payload_ok(payload):
            return []
        results = _result_rows(payload)
        out: list[int] = []
        for x in results:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                continue
        return out

    def product_names(self, product_ids: list[int]) -> dict[int, str]:
        if not product_ids:
            return {}
        csv_ids = ",".join(str(i) for i in product_ids)
        r = self._http.get(
            f"{self._root}/catalog/products/{csv_ids}",
            headers={**self._auth_header(), "Accept": "application/json"},
            params={"getExtendedFields": "false"},
        )
        r.raise_for_status()
        payload = r.json()
        out: dict[int, str] = {}
        for row in _result_rows(payload):
            pid = _row_get(row, "productId", "ProductId")
            if pid is None:
                continue
            out[int(pid)] = str(_row_get(row, "name", "Name") or "")
        return out

    def prices_for(self, product_ids: list[int]) -> dict[int, list[dict]]:
        if not product_ids:
            return {}
        csv_ids = ",".join(str(i) for i in product_ids)
        r = self._http.get(
            f"{self._root}/pricing/product/{csv_ids}",
            headers={**self._auth_header(), "Accept": "application/json"},
        )
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        payload = r.json()
        by_id: dict[int, list[dict]] = {}
        for row in _result_rows(payload):
            pid_raw = _row_get(row, "productId", "ProductId")
            if pid_raw is None:
                continue
            pid = int(pid_raw)
            by_id.setdefault(pid, []).append(row)
        return by_id

    def quote_best_match(self, ocr_name: str) -> PriceQuote | None:
        ids = self.search_pokemon_by_name(ocr_name)
        if not ids:
            return None
        names = self.product_names(ids[:3])
        prices = self.prices_for(ids[:3])
        primary = ids[0]
        rows = prices.get(primary) or []
        pick = _pick_normal_row(rows)
        return PriceQuote(
            product_id=primary,
            name=names.get(primary, ocr_name),
            market_price=_as_float(_row_get(pick, "marketPrice", "MarketPrice")),
            low_price=_as_float(_row_get(pick, "lowPrice", "LowPrice")),
            sub_type_name=_row_get(pick, "subTypeName", "SubTypeName"),
        )


def _as_float(v: object) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pick_normal_row(rows: list[dict]) -> dict:
    for row in rows:
        st = str(_row_get(row, "subTypeName", "SubTypeName") or "")
        if st.lower() == "normal":
            return row
    return rows[0] if rows else {}
