"""Extract name / phone / email / vehicle / branch from webform notification emails."""
from __future__ import annotations

import html
import re
from email.message import Message
from email.utils import parseaddr
from typing import Any

from src.config import PLACEHOLDER_BRANCH, PRIMARY_BRANCH
from src.odoo_sync.crm import normalize_crm_branch, normalize_phone_digits
from src.web_leads.models import WebLead

# Label → canonical field (Spanish + English CMS variants).
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "name": (
        "nombre",
        "name",
        "nombre completo",
        "full name",
        "cliente",
        "contact name",
        "tu nombre",
    ),
    "phone": (
        "telefono",
        "teléfono",
        "phone",
        "celular",
        "móvil",
        "movil",
        "whatsapp",
        "tel",
        "número",
        "numero",
    ),
    "email": (
        "email",
        "correo",
        "e-mail",
        "mail",
        "correo electrónico",
        "correo electronico",
    ),
    "vehicle": (
        "vehiculo",
        "vehículo",
        "auto",
        "unidad",
        "modelo",
        "interested vehicle",
        "vehicle",
        "auto de interes",
        "auto de interés",
        "producto",
        "sku",
    ),
    "message": (
        "mensaje",
        "message",
        "comentarios",
        "comments",
        "nota",
        "notes",
        "detalle",
        "consulta",
    ),
    "branch": (
        "sucursal",
        "branch",
        "agencia",
        "ubicacion",
        "ubicación",
        "preferencia de sucursal",
    ),
}

_LABEL_VALUE_RE = re.compile(
    r"(?P<label>[A-Za-zÁÉÍÓÚáéíóúÑñüÜ0-9 /_-]{2,40})\s*[:：\-]\s*(?P<value>.+?)(?:\n|$)",
    re.UNICODE,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_PHONE_RE = re.compile(
    r"(?:\+?52[\s\-]*)?(?:1[\s\-]*)?(\d{2,3}[\s\-]?\d{3,4}[\s\-]?\d{4}|\d{10})"
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _strip_html(raw: str) -> str:
    text = html.unescape(raw or "")
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = re.sub(r"(?i)</tr\s*>", "\n", text)
    text = re.sub(r"(?i)</div\s*>", "\n", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _normalize_label(label: str) -> str:
    return re.sub(r"\s+", " ", (label or "").strip().casefold())


def _canonical_field(label: str) -> str | None:
    key = _normalize_label(label)
    key = key.rstrip(".:")
    for field, aliases in _FIELD_ALIASES.items():
        if key in aliases:
            return field
    for field, aliases in _FIELD_ALIASES.items():
        for alias in aliases:
            if alias in key or key in alias:
                return field
    return None


def _extract_labeled_fields(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in _LABEL_VALUE_RE.finditer(body):
        field = _canonical_field(match.group("label"))
        if not field or field in out:
            continue
        value = match.group("value").strip().strip("\"'")
        if value:
            out[field] = value
    return out


def _guess_branch(text: str) -> str:
    low = (text or "").casefold()
    if "san felipe" in low or "san_felipe" in low or "san-felipe" in low:
        return PLACEHOLDER_BRANCH
    if "perifer" in low:
        return PRIMARY_BRANCH
    return PRIMARY_BRANCH


def _message_plaintext(msg: Message) -> str:
    if msg.is_multipart():
        parts: list[str] = []
        for part in msg.walk():
            ctype = (part.get_content_type() or "").lower()
            if ctype not in {"text/plain", "text/html"}:
                continue
            try:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace")
            except Exception:
                continue
            if ctype == "text/html":
                text = _strip_html(text)
            parts.append(text)
        return "\n".join(parts)
    try:
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace")
    except Exception:
        text = str(msg.get_payload() or "")
    if (msg.get_content_type() or "").lower() == "text/html":
        return _strip_html(text)
    return text


def parse_web_lead_email(
    *,
    subject: str = "",
    body: str = "",
    from_addr: str = "",
    message_id: str = "",
    default_branch: str = PRIMARY_BRANCH,
) -> WebLead:
    """Parse a notification subject+body into a ``WebLead``."""
    plain = _strip_html(body) if "<" in (body or "") else (body or "")
    blob = f"{subject}\n{plain}"
    fields = _extract_labeled_fields(plain)

    name = (fields.get("name") or "").strip()
    phone_raw = (fields.get("phone") or "").strip()
    email = (fields.get("email") or "").strip()
    vehicle = (fields.get("vehicle") or "").strip()
    message = (fields.get("message") or "").strip()
    branch_raw = (fields.get("branch") or "").strip()

    if not phone_raw:
        m = _PHONE_RE.search(blob)
        if m:
            phone_raw = m.group(0)
    if not email:
        # Prefer customer email over marketing@ / noreply senders.
        for candidate in _EMAIL_RE.findall(blob):
            low = candidate.casefold()
            if low.startswith(("noreply@", "no-reply@", "mailer-daemon@")):
                continue
            if "autosell.mx" in low and "marketing@" not in low:
                # staff address — skip unless nothing else
                continue
            if low.startswith("marketing@"):
                continue
            email = candidate
            break
    if not name:
        # "Nuevo contacto: Juan Pérez" / "Lead from Ana"
        for pat in (
            r"(?:nuevo\s+(?:contacto|lead)|lead\s+from|solicitud\s+de)\s*[:\-]?\s*([A-Za-zÁÉÍÓÚáéíóúÑñüÜ ]{3,60})",
            r"nombre\s+del\s+cliente\s*[:\-]\s*([^\n]+)",
        ):
            m = re.search(pat, blob, re.IGNORECASE)
            if m:
                name = m.group(1).strip()
                break

    phone = normalize_phone_digits(phone_raw) or re.sub(r"\D", "", phone_raw)
    if len(phone) == 12 and phone.startswith("52"):
        pass
    elif len(phone) == 10:
        phone = f"52{phone}"

    branch_text = branch_raw or blob
    branch = normalize_crm_branch(_guess_branch(branch_text) or default_branch)

    if not vehicle:
        # Subject often carries the unit: "Interés en Mazda CX-5 2020"
        m = re.search(
            r"(?:inter[eé]s(?:ado)?\s+en|solicitud(?:\s+para)?|vehiculo|vehículo)\s*[:\-]?\s*(.+)$",
            subject or "",
            re.IGNORECASE,
        )
        if m:
            vehicle = m.group(1).strip()

    _, parsed_from = parseaddr(from_addr or "")
    return WebLead(
        name=name or "Cliente Web",
        phone=phone,
        email=email,
        vehicle=vehicle,
        message=message,
        branch=branch,
        subject=(subject or "").strip(),
        message_id=(message_id or "").strip(),
        raw_from=parsed_from or (from_addr or "").strip(),
        extras={"fields": fields},
    )


def parse_email_message(msg: Message, *, default_branch: str = PRIMARY_BRANCH) -> WebLead:
    """Parse an ``email.message.Message`` (RFC822)."""
    subject = str(msg.get("Subject") or "")
    from_addr = str(msg.get("From") or "")
    message_id = str(msg.get("Message-ID") or msg.get("Message-Id") or "").strip()
    body = _message_plaintext(msg)
    return parse_web_lead_email(
        subject=subject,
        body=body,
        from_addr=from_addr,
        message_id=message_id,
        default_branch=default_branch,
    )


def parse_webhook_payload(payload: dict[str, Any]) -> WebLead:
    """Mailgun / SendGrid / generic JSON → ``WebLead``."""
    if not isinstance(payload, dict):
        return WebLead(name="", phone="")
    subject = str(
        payload.get("subject")
        or payload.get("Subject")
        or ""
    )
    body = str(
        payload.get("body")
        or payload.get("text")
        or payload.get("stripped-text")
        or payload.get("html")
        or payload.get("body-plain")
        or ""
    )
    from_addr = str(
        payload.get("from")
        or payload.get("sender")
        or payload.get("From")
        or ""
    )
    message_id = str(
        payload.get("message_id")
        or payload.get("Message-Id")
        or payload.get("Message-ID")
        or ""
    )
    return parse_web_lead_email(
        subject=subject,
        body=body,
        from_addr=from_addr,
        message_id=message_id,
    )


__all__ = [
    "parse_email_message",
    "parse_web_lead_email",
    "parse_webhook_payload",
]
