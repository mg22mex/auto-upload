"""Beatriz conversation / inventory prompt blocks (Vapi system + WA chat).

Kept as plain strings so ``scripts/configure_vapi_inventory_tool.py`` can
upsert them into the live assistant without embedding prose in the bridge.
"""
from __future__ import annotations

QUALIFY_FIRST_MARKER = "## CALIFICAR ANTES DE INVENTARIO (OBLIGATORIO)"

QUALIFY_FIRST_BLOCK = f"""

{QUALIFY_FIRST_MARKER}
- Si el cliente pregunta de forma ABIERTA por el catálogo SIN tipo de carrocería,
  presupuesto/enganche ni marca/modelo concretos — ejemplos: "¿Qué autos tienen?",
  "¿Qué opciones hay?", "¿Qué autos tienen en San Felipe?", "muéstrame inventario" —
  NO llames query_inventory / get_inventory todavía.
- Responde en UNA sola pregunta: reconoce brevemente la sucursal/disponibilidad y
  califica. Plantilla (adapta la sucursal si la mencionó):
  "Tenemos una gran variedad de unidades listas para entrega inmediata en
  {{branch}}. Para recomendarte las mejores opciones, ¿buscas algún tipo de
  vehículo en especial (como SUV, Sedán, Pickup) o tienes un presupuesto/enganche
  en mente?"
- PROHIBIDO listar autos al azar, inventar stock o vaciar el inventario completo
  ante una pregunta abierta.
- SOLO cuando el cliente dé preferencia (tipo, presupuesto, marca o modelo),
  llama query_inventory filtrado (body_type / max_price / brand / model / branch)
  y presenta como máximo 3 coincidencias con modelo, año y precio.
"""

ANTI_SILENCE_MARKER = "## PROHIBIDO HABLAR ANTES DEL TOOL (CRÍTICO)"

ANTI_SILENCE_BLOCK = f"""

{ANTI_SILENCE_MARKER}
- Esta regla aplica SOLO cuando el cliente ya dio marca+modelo, tipo (SUV/Sedán/
  Pickup), presupuesto/enganche o un vehículo concreto.
- En ese caso: llama INMEDIATAMENTE a query_inventory con los filtros conocidos.
  En el mismo turno del tool call, tu mensaje de texto DEBE estar vacío.
- Frases prohibidas (nunca): "un momento", "dame un momento", "déjame consultar",
  "ahora mismo consulto", "verifico", "consulto el inventario", "espera un segundo".
- Flujo: (1) tool call silencioso → (2) recibes JSON → (3) hablas hasta 3 opciones
  (modelo + precio en palabras + sucursal) + pregunta de cita.
- Si la pregunta es abierta sin filtros, aplica CALIFICAR ANTES DE INVENTARIO —
  habla la pregunta de calificación; NO llames el tool.
"""

INVENTORY_QUALIFY_NEXT_PROMPT = (
    "NO listes vehículos. Reconoce la sucursal si aplica y pregunta UNA sola "
    "vez por tipo (SUV, Sedán, Pickup) o presupuesto/enganche antes de buscar."
)

INVENTORY_TOOL_DESCRIPTION = (
    "Busca hasta 3 vehículos disponibles en inventario Autosell (Odoo). "
    "Úsala SOLO cuando el cliente ya indicó tipo de vehículo, presupuesto, "
    "marca o modelo. Ante preguntas abiertas ('¿qué autos tienen?'), NO la "
    "llames: primero califica. Pasa brand/model juntos si pide un auto "
    "concreto; body_type (SUV/Sedan/Pickup) o max_price si solo dio preferencia; "
    "branch=san_felipe|periferico si mencionó sucursal. Espera el JSON antes "
    "de hablar; no digas frases de espera."
)

WA_QUALIFY_INVENTORY_SNIPPET = (
    "Si el cliente pregunta abierto por inventario ('qué autos tienen', "
    "'qué opciones', 'en San Felipe') SIN tipo/presupuesto/marca/modelo, "
    "NO llames query_inventory: reconoce la sucursal y pregunta tipo "
    "(SUV/Sedán/Pickup) o presupuesto. Solo con preferencia clara, "
    "llama query_inventory filtrado y muestra máximo 3 opciones. "
)


def upsert_prompt_block(system: str, marker: str, block: str) -> str:
    """Insert or replace a marked section inside the assistant system prompt."""
    text = (system or "").rstrip()
    start = text.find(marker)
    if start < 0:
        return text + block
    # Replace from the previous section break (leading newlines + ##) through
    # the next ## heading or end of string.
    # Include any blank line immediately before the marker heading.
    head_start = start
    while head_start > 0 and text[head_start - 1] in "\n":
        head_start -= 1
        if head_start > 0 and text[head_start - 1] == "\n":
            # keep one newline boundary handled by block itself
            break
    # Find start of this ## line
    line_start = text.rfind("\n", 0, start)
    line_start = 0 if line_start < 0 else line_start + 1
    rest = text[start + len(marker) :]
    next_heading = rest.find("\n## ")
    if next_heading < 0:
        return text[:line_start].rstrip() + block
    end = start + len(marker) + next_heading
    return text[:line_start].rstrip() + block + text[end:]


def qualify_prompt_for_branch(branch: str | None) -> str:
    label = (branch or "").strip() or "nuestras sucursales"
    low = label.casefold()
    if "felipe" in low:
        label = "San Felipe"
    elif "perifer" in low:
        label = "Periférico"
    return (
        f"Tenemos una gran variedad de unidades listas para entrega inmediata "
        f"en {label}. Para recomendarte las mejores opciones, ¿buscas algún "
        f"tipo de vehículo en especial (como SUV, Sedán, Pickup) o tienes un "
        f"presupuesto/enganche en mente?"
    )


__all__ = [
    "ANTI_SILENCE_BLOCK",
    "ANTI_SILENCE_MARKER",
    "INVENTORY_QUALIFY_NEXT_PROMPT",
    "INVENTORY_TOOL_DESCRIPTION",
    "QUALIFY_FIRST_BLOCK",
    "QUALIFY_FIRST_MARKER",
    "WA_QUALIFY_INVENTORY_SNIPPET",
    "qualify_prompt_for_branch",
    "upsert_prompt_block",
]
