"""Autométrica trade-in valuation — guide lookup + mileage → Valor Compra.

Primary path: local fallback table ``data/autometrica_valuations.json`` (no network
in the quote path). Optional live login uses the same Laravel endpoint as
``.github/workflows/get_token.yml`` when ``AUTOMETRICA_TOKEN`` / credentials are set;
live guide fetches are best-effort and never required for amortization.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

TWOPLACES = Decimal("0.01")
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_VALUATIONS_PATH = ROOT / "data" / "autometrica_valuations.json"

ENV_TOKEN = "AUTOMETRICA_TOKEN"
ENV_USER = "AUTOMETRICA_USER"
ENV_PASS = "AUTOMETRICA_PASS"
ENV_LOGIN_URL = "AUTOMETRICA_LOGIN_URL"
DEFAULT_LOGIN_URL = "https://app240.autometrica.mx/api/user/login"


def _q(value: Decimal | int | float | str) -> Decimal:
    return Decimal(str(value)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().casefold())


@dataclass(frozen=True)
class AutometricaValuation:
    """Adjusted trade-in purchase value ready for French amortization engache."""

    year: int
    make: str
    model: str
    version: str
    mileage_km: int
    valor_compra: Decimal
    valor_venta: Decimal
    baseline_km: int
    mileage_adjustment: Decimal
    source: str
    matched: bool
    notes: str = ""

    @property
    def net_trade_in_equity(self) -> Decimal:
        """Alias used by ``calculate_quote(..., net_trade_in_equity=...)``."""
        return self.valor_compra


def load_valuations(path: Path | None = None) -> dict[str, Any]:
    target = path or Path(os.getenv("AUTOMETRICA_VALUATIONS_PATH") or DEFAULT_VALUATIONS_PATH)
    if not target.is_file():
        return {"vehicles": [], "mileage_adjustment_per_10000_km": -3500}
    return json.loads(target.read_text(encoding="utf-8"))


def _score_row(row: dict[str, Any], *, year: int, make: str, model: str, version: str) -> int:
    score = 0
    if int(row.get("year") or 0) == int(year):
        score += 40
    if _norm(str(row.get("make") or "")) == _norm(make):
        score += 30
    if _norm(str(row.get("model") or "")) == _norm(model):
        score += 25
    row_ver = _norm(str(row.get("version") or ""))
    want_ver = _norm(version)
    if want_ver and row_ver == want_ver:
        score += 20
    elif want_ver and want_ver in row_ver:
        score += 10
    elif not want_ver:
        score += 5
    return score


def apply_mileage_adjustment(
    base_valor_compra: Decimal,
    *,
    mileage_km: int,
    baseline_km: int,
    per_10k: Decimal,
) -> tuple[Decimal, Decimal]:
    """Adjust Valor Compra vs baseline km. Returns (adjusted, delta)."""
    delta_km = int(mileage_km) - int(baseline_km)
    steps = Decimal(delta_km) / Decimal(10000)
    delta = _q(steps * per_10k)
    adjusted = _q(base_valor_compra + delta)
    if adjusted < Decimal("0"):
        adjusted = Decimal("0.00")
    return adjusted, delta


def lookup_valor_compra(
    *,
    year: int,
    make: str,
    model: str,
    version: str = "",
    mileage_km: int = 0,
    valuations: dict[str, Any] | None = None,
) -> AutometricaValuation:
    """Find best guide row and apply mileage adjustment → Valor Compra."""
    table = valuations if valuations is not None else load_valuations()
    rows = list(table.get("vehicles") or [])
    per_10k = _q(table.get("mileage_adjustment_per_10000_km") or -3500)

    best: dict[str, Any] | None = None
    best_score = -1
    for row in rows:
        if not isinstance(row, dict):
            continue
        score = _score_row(row, year=year, make=make, model=model, version=version)
        if score > best_score:
            best = row
            best_score = score

    # Require year+make+model match at minimum
    matched = best is not None and best_score >= 95
    if best is None or best_score < 70:
        # Conservative stub so the conversation can continue
        age = max(0, 2026 - int(year))
        stub = _q(Decimal("220000") - Decimal(age) * Decimal("16000"))
        stub = max(stub, Decimal("40000.00"))
        baseline = 80000
        adjusted, delta = apply_mileage_adjustment(
            stub, mileage_km=mileage_km or baseline, baseline_km=baseline, per_10k=per_10k
        )
        return AutometricaValuation(
            year=year,
            make=make,
            model=model,
            version=version or "n/d",
            mileage_km=int(mileage_km or 0),
            valor_compra=adjusted,
            valor_venta=_q(adjusted * Decimal("1.12")),
            baseline_km=baseline,
            mileage_adjustment=delta,
            source="autometrica_stub",
            matched=False,
            notes="No exact Autométrica row — stub Valor Compra",
        )

    base = _q(best.get("valor_compra") or 0)
    baseline = int(best.get("baseline_km") or 80000)
    km = int(mileage_km or baseline)
    adjusted, delta = apply_mileage_adjustment(
        base, mileage_km=km, baseline_km=baseline, per_10k=per_10k
    )
    return AutometricaValuation(
        year=int(best.get("year") or year),
        make=str(best.get("make") or make),
        model=str(best.get("model") or model),
        version=str(best.get("version") or version or ""),
        mileage_km=km,
        valor_compra=adjusted,
        valor_venta=_q(best.get("valor_venta") or adjusted),
        baseline_km=baseline,
        mileage_adjustment=delta,
        source=str(table.get("source") or "autometrica_fallback"),
        matched=matched,
        notes="Autométrica fallback table" if matched else "partial Autométrica match",
    )


def fetch_session_token(
    *,
    username: str | None = None,
    password: str | None = None,
    login_url: str | None = None,
) -> str | None:
    """POST Laravel ``/api/user/login`` (same shape as get_token.yml). Best-effort."""
    user = (username or os.getenv(ENV_USER) or "").strip()
    pwd = (password or os.getenv(ENV_PASS) or "").strip()
    if not user or not pwd:
        existing = (os.getenv(ENV_TOKEN) or "").strip()
        return existing or None
    url = (login_url or os.getenv(ENV_LOGIN_URL) or DEFAULT_LOGIN_URL).strip()
    try:
        import urllib.request

        payload = json.dumps({"username": user, "password": pwd}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"WARN Autométrica login failed: {type(exc).__name__}", flush=True)
        return (os.getenv(ENV_TOKEN) or "").strip() or None

    for key in ("token", "access_token"):
        if isinstance(body.get(key), str) and body[key].strip():
            return body[key].strip()
    data = body.get("data")
    if isinstance(data, dict):
        for key in ("token", "access_token"):
            if isinstance(data.get(key), str) and data[key].strip():
                return data[key].strip()
    return None


__all__ = [
    "AutometricaValuation",
    "DEFAULT_LOGIN_URL",
    "DEFAULT_VALUATIONS_PATH",
    "ENV_LOGIN_URL",
    "ENV_PASS",
    "ENV_TOKEN",
    "ENV_USER",
    "apply_mileage_adjustment",
    "fetch_session_token",
    "load_valuations",
    "lookup_valor_compra",
]
