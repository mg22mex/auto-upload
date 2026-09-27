"""Persist last inquired vehicle across WA qualification + Vapi chat meta.

When Beatriz runs ``query_inventory`` / ``calculate_financing``, the active
session must point at the NEW vehicle — never keep a prior selection for
appointment / Odoo opportunity binding.
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def normalize_vehicle_label(raw: str | None) -> str:
    """Compact vehicle title for session / CRM (strip chat fluff when present)."""
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        from src.pdf_engine.generator import sanitize_vehicle_title

        return sanitize_vehicle_title(text)
    except Exception:
        return re.sub(r"\s+", " ", text)[:120].strip()


def inventory_vehicle_label(
    *,
    brand: str | None = None,
    model: str | None = None,
    year: int | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> str:
    """Best label after an inventory lookup: single hit wins, else query tokens."""
    rows = rows or []
    if len(rows) == 1:
        name = str(rows[0].get("name") or rows[0].get("model") or "").strip()
        label = normalize_vehicle_label(name)
        if label:
            return label
    parts = [p for p in (brand, model) if (p or "").strip()]
    label = " ".join(str(p).strip() for p in parts)
    if year is not None:
        label = f"{label} {int(year)}".strip()
    label = normalize_vehicle_label(label)
    if label:
        return label
    if rows:
        name = str(rows[0].get("name") or rows[0].get("model") or "").strip()
        return normalize_vehicle_label(name)
    return ""


def remember_interested_vehicle(
    vehicle: str | None,
    *,
    phone: str | None = None,
    instance: str | None = None,
    price: float | int | None = None,
    vehicle_year: int | None = None,
    qualification_store: Any | None = None,
    chat_store: Any | None = None,
    clear_tradein: bool = False,
) -> str:
    """Write *vehicle* into qualification.db + vapi chat meta. Returns stored label.

    When ``clear_tradein`` is True (new inventory evaluation / session reset),
    wipes sticky Autométrica trade-in fields so they cannot override the unit.
    """
    label = normalize_vehicle_label(vehicle)
    if not label:
        return ""
    digits = re.sub(r"\D", "", phone or "")
    meta: dict[str, Any] = {
        "vehicle_name": label,
        "interested_vehicle": label,
    }
    if clear_tradein:
        meta.update(
            {
                "tradein_summary": "",
                "trade_in_label": "",
                "valor_compra": "",
                "net_trade_in_equity": "",
                "tradein_make": "",
                "tradein_model": "",
                "tradein_year": "",
                "tradein_mileage_km": "",
            }
        )
    if price is not None:
        try:
            meta["vehicle_price"] = float(price)
        except (TypeError, ValueError):
            pass
    year = vehicle_year
    if year is None:
        try:
            from src.quote_engine.term_limits import extract_model_year

            year = extract_model_year(label)
        except Exception:
            year = None
    if year is not None:
        try:
            meta["vehicle_year"] = int(year)
        except (TypeError, ValueError):
            pass

    if digits:
        try:
            if qualification_store is None:
                from src.whatsapp_worker.inbound import QualificationStore

                store = QualificationStore()
            else:
                store = qualification_store
            sessions = store.list_by_phone(digits)
            if instance is not None:
                matched = [s for s in sessions if s.instance == (instance or "")]
                if matched:
                    sessions = matched
            for sess in sessions:
                changed = False
                if (sess.vehicle_interest or "").strip() != label:
                    sess.vehicle_interest = label
                    changed = True
                if clear_tradein and (
                    (sess.trade_in_vehicle or "").strip()
                    or (sess.down_payment or "").strip()
                ):
                    sess.trade_in_vehicle = ""
                    sess.down_payment = ""
                    changed = True
                if changed:
                    store.save(sess)
                    logger.info(
                        "session vehicle_interest phone=%s instance=%s → %r clear_tradein=%s",
                        digits,
                        sess.instance,
                        label,
                        clear_tradein,
                    )
        except Exception:
            logger.exception(
                "remember_interested_vehicle qualification update failed phone=%s",
                digits,
            )

        try:
            if chat_store is None:
                from src.voice_gateway.vapi_chat import get_chat_store

                cs = get_chat_store()
            else:
                cs = chat_store
            cs.update_meta(digits, instance or "", **meta)
        except Exception:
            logger.exception(
                "remember_interested_vehicle vapi meta update failed phone=%s",
                digits,
            )
    else:
        logger.debug("remember_interested_vehicle skipped — no phone for %r", label)

    return label


def resolve_interested_vehicle(
    phone: str | None,
    *,
    instance: str | None = None,
    fallback: str | None = None,
    qualification_store: Any | None = None,
    chat_store: Any | None = None,
) -> str:
    """Last stored vehicle for *phone*: Vapi meta → qualification → fallback."""
    digits = re.sub(r"\D", "", phone or "")
    if digits:
        try:
            if chat_store is None:
                from src.voice_gateway.vapi_chat import get_chat_store

                cs = get_chat_store()
            else:
                cs = chat_store
            meta = cs.get_meta(digits, instance or "")
            for key in ("interested_vehicle", "vehicle_name"):
                label = normalize_vehicle_label(str(meta.get(key) or ""))
                if label:
                    return label
            if instance:
                meta = cs.get_meta(digits, "")
                for key in ("interested_vehicle", "vehicle_name"):
                    label = normalize_vehicle_label(str(meta.get(key) or ""))
                    if label:
                        return label
        except Exception:
            logger.exception("resolve_interested_vehicle meta read failed")

        try:
            if qualification_store is None:
                from src.whatsapp_worker.inbound import QualificationStore

                store = QualificationStore()
            else:
                store = qualification_store
            sessions = store.list_by_phone(digits)
            if instance is not None:
                matched = [s for s in sessions if s.instance == (instance or "")]
                if matched:
                    sessions = matched
            sessions_sorted = sorted(
                sessions,
                key=lambda s: s.updated_at or "",
                reverse=True,
            )
            for sess in sessions_sorted:
                label = normalize_vehicle_label(sess.vehicle_interest)
                if label:
                    return label
        except Exception:
            logger.exception("resolve_interested_vehicle qualification read failed")

    return normalize_vehicle_label(fallback)
