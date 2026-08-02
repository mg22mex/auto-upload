"""CRM-facing helpers (re-export surface for modular layout).

Heavy CRM logic still lives on ``OdooCRMClient`` in ``client.py`` for
backward compatibility; this module documents the crm slice of the package.
"""
from __future__ import annotations

# Intentionally thin — calendar/activity/lead APIs remain on OdooCRMClient.
__all__: list[str] = []
