"""WhatsApp worker — open-wa / Evolution API wrapper.

Isolated from Playwright `sessions/`. Secrets from env only.
"""
from __future__ import annotations

import mimetypes
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import requests


class WhatsAppWorkerError(RuntimeError):
    """API or validation failure."""


class QuoteLike(Protocol):
    """Minimal quote surface for message formatting."""

    vehicle_price: Decimal
    term_months: int
    down_payment: Decimal
    cash_down_payment: Decimal
    net_trade_in_equity: Decimal
    base_monthly_payment: Decimal
    estimated_monthly_payment: Decimal
    monthly_auto_insurance: Decimal
    monthly_life_insurance: Decimal
    average_monthly_iva: Decimal
    origination_fee: Decimal
    financed_principal: Decimal


def _money(value: Decimal | int | float | str) -> str:
    amount = Decimal(str(value)).quantize(Decimal("0.01"))
    # 1234567.89 → $1,234,567.89
    sign = "-" if amount < 0 else ""
    whole, _, frac = f"{abs(amount):.2f}".partition(".")
    grouped = ",".join(
        reversed([whole[max(0, i - 3) : i] for i in range(len(whole), 0, -3)])
    )
    return f"{sign}${grouped}.{frac}"


def normalize_phone_number(phone_number: str, *, default_country: str = "52") -> str:
    """Digits only; prepend MX 52 when given a 10-digit local number."""
    digits = re.sub(r"\D", "", phone_number or "")
    if not digits:
        raise WhatsAppWorkerError("phone_number is required")
    if len(digits) == 10:
        digits = f"{default_country}{digits}"
    if len(digits) < 11 or len(digits) > 15:
        raise WhatsAppWorkerError(f"invalid phone_number length: {digits}")
    return digits


def format_quote_message(
    lead_name: str,
    vehicle_name: str,
    quote_result: QuoteLike,
) -> str:
    """Clean WhatsApp text: down payment, term, monthly breakdown."""
    name = (lead_name or "Cliente").strip()
    vehicle = (vehicle_name or "vehículo").strip()
    q = quote_result
    trade = getattr(q, "net_trade_in_equity", Decimal("0")) or Decimal("0")
    cash = getattr(q, "cash_down_payment", q.down_payment)

    lines = [
        f"Hola {name} 👋",
        "",
        f"Cotización Autosell — *{vehicle}*",
        f"Precio: {_money(q.vehicle_price)}",
        "",
        "*Enganche*",
        f"• Total aplicado: {_money(q.down_payment)}",
        f"• Efectivo: {_money(cash)}",
    ]
    if trade and Decimal(str(trade)) > 0:
        lines.append(f"• Trade-in (equity): {_money(trade)}")

    lines += [
        "",
        f"*Plazo:* {q.term_months} meses",
        f"Monto a financiar: {_money(q.financed_principal)}",
        f"Comisión apertura: {_money(q.origination_fee)}",
        "",
        "*Pago mensual estimado*",
        f"• Base (amortización): {_money(q.base_monthly_payment)}",
        f"• Seguro auto (mensual): {_money(q.monthly_auto_insurance)}",
        f"• Seguro vida (mensual): {_money(q.monthly_life_insurance)}",
        f"• IVA intereses (prom.): {_money(q.average_monthly_iva)}",
        f"• *Total estimado: {_money(q.estimated_monthly_payment)}*",
        "",
        "Cotización informativa; sujeta a aprobación crediticia.",
        "¿Te agendo con un asesor?",
    ]
    return "\n".join(lines)


class WhatsAppWorkerClient:
    """HTTP client for Evolution API or open-wa.

    Env:
      WHATSAPP_PROVIDER=evolution|openwa  (default evolution)
      WHATSAPP_BASE_URL=https://...
      WHATSAPP_API_KEY=...
      WHATSAPP_INSTANCE=autosell          (Evolution instance name)
    """

    ENV_PROVIDER = "WHATSAPP_PROVIDER"
    ENV_BASE_URL = "WHATSAPP_BASE_URL"
    ENV_API_KEY = "WHATSAPP_API_KEY"
    ENV_INSTANCE = "WHATSAPP_INSTANCE"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        instance: str | None = None,
        provider: str | None = None,
        session: requests.Session | None = None,
        timeout_sec: float = 30.0,
    ) -> None:
        self.provider = (
            provider or os.getenv(self.ENV_PROVIDER, "evolution")
        ).strip().lower()
        self.base_url = (base_url or os.getenv(self.ENV_BASE_URL, "")).rstrip("/")
        self.api_key = api_key or os.getenv(self.ENV_API_KEY, "")
        self.instance = instance or os.getenv(self.ENV_INSTANCE, "autosell")
        self.timeout_sec = timeout_sec
        self._session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            # Evolution uses apikey; open-wa often Authorization Bearer / x-api-key
            if self.provider == "openwa":
                headers["Authorization"] = f"Bearer {self.api_key}"
            else:
                headers["apikey"] = self.api_key
        return headers

    def _require_config(self) -> None:
        if not self.base_url:
            raise WhatsAppWorkerError(f"Missing {self.ENV_BASE_URL}")
        if not self.api_key:
            raise WhatsAppWorkerError(f"Missing {self.ENV_API_KEY}")

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_config()
        url = f"{self.base_url}{path}"
        resp = self._session.post(
            url,
            json=payload,
            headers=self._headers(),
            timeout=self.timeout_sec,
        )
        if resp.status_code >= 400:
            raise WhatsAppWorkerError(
                f"WhatsApp API {resp.status_code}: {resp.text[:300]}"
            )
        if not resp.content:
            return {"ok": True}
        try:
            data = resp.json()
        except ValueError:
            return {"ok": True, "raw": resp.text}
        return data if isinstance(data, dict) else {"ok": True, "data": data}

    def _post_multipart(
        self,
        path: str,
        data: dict[str, str],
        file_field: str,
        pdf_path: Path,
    ) -> dict[str, Any]:
        self._require_config()
        url = f"{self.base_url}{path}"
        mime = mimetypes.guess_type(str(pdf_path))[0] or "application/pdf"
        headers = {}
        if self.api_key:
            if self.provider == "openwa":
                headers["Authorization"] = f"Bearer {self.api_key}"
            else:
                headers["apikey"] = self.api_key
        with pdf_path.open("rb") as handle:
            files = {file_field: (pdf_path.name, handle, mime)}
            resp = self._session.post(
                url,
                data=data,
                files=files,
                headers=headers,
                timeout=self.timeout_sec,
            )
        if resp.status_code >= 400:
            raise WhatsAppWorkerError(
                f"WhatsApp API {resp.status_code}: {resp.text[:300]}"
            )
        if not resp.content:
            return {"ok": True}
        try:
            body = resp.json()
        except ValueError:
            return {"ok": True, "raw": resp.text}
        return body if isinstance(body, dict) else {"ok": True, "data": body}

    @staticmethod
    def format_quote_message(
        lead_name: str,
        vehicle_name: str,
        quote_result: QuoteLike,
    ) -> str:
        return format_quote_message(lead_name, vehicle_name, quote_result)

    def send_text_message(self, phone_number: str, text_body: str) -> dict[str, Any]:
        """Send WhatsApp text. Returns provider response dict."""
        text = (text_body or "").strip()
        if not text:
            raise WhatsAppWorkerError("text_body is required")
        phone = normalize_phone_number(phone_number)

        if self.provider == "openwa":
            payload = {
                "chatId": f"{phone}@c.us",
                "message": text,
            }
            return self._post("/sendText", payload)

        # Evolution API
        payload = {
            "number": phone,
            "text": text,
        }
        return self._post(f"/message/sendText/{self.instance}", payload)

    def send_quote_pdf(
        self,
        phone_number: str,
        pdf_path: str | Path,
        caption: str = "",
    ) -> dict[str, Any]:
        """Dispatch quote PDF to WhatsApp."""
        path = Path(pdf_path)
        if not path.is_file():
            raise WhatsAppWorkerError(f"PDF not found: {path}")
        phone = normalize_phone_number(phone_number)
        caption_text = (caption or path.name).strip()

        if self.provider == "openwa":
            return self._post_multipart(
                "/sendFile",
                {
                    "chatId": f"{phone}@c.us",
                    "caption": caption_text,
                    "filename": path.name,
                },
                "file",
                path,
            )

        # Evolution: media message with local file upload
        return self._post_multipart(
            f"/message/sendMedia/{self.instance}",
            {
                "number": phone,
                "mediatype": "document",
                "mimetype": "application/pdf",
                "caption": caption_text,
                "fileName": path.name,
            },
            "file",
            path,
        )
