from __future__ import annotations

import copy
import json
import re
import uuid
from pathlib import Path
from typing import Any

from src.facebook.graphql.errors import FacebookGraphqlFallback
from src.facebook.util import env_str

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def default_graphql_dir(root: Path | None = None) -> Path:
    base = root if root is not None else Path.cwd()
    override = env_str("FB_GQL_TEMPLATE_DIR", "")
    if override:
        path = Path(override)
        return path if path.is_absolute() else (base / path)
    return base / "data" / "graphql"


def load_template(
    name: str,
    *,
    root: Path | None = None,
    required: bool = True,
) -> dict[str, Any] | None:
    """Load ``data/graphql/{name}.json`` (or env override path).

    Env shortcuts:
      FB_GQL_DELETE_DOC_ID / FB_GQL_CREATE_DOC_ID — inject doc_id when file missing fields.
    """
    directory = default_graphql_dir(root)
    path = directory / f"{name}.json"
    data: dict[str, Any] | None = None
    if path.is_file():
        with path.open(encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            raise FacebookGraphqlFallback(f"Template {path} must be a JSON object")
        data = loaded
    elif required:
        raise FacebookGraphqlFallback(
            f"Missing GraphQL template {path}. Capture with "
            f"scripts/fb_capture_graphql.py or copy from *.example.json"
        )
    else:
        data = None

    if data is None:
        data = {}

    env_doc = ""
    if name.startswith("delete"):
        env_doc = env_str("FB_GQL_DELETE_DOC_ID", "")
    elif name.startswith("create"):
        env_doc = env_str("FB_GQL_CREATE_DOC_ID", "")
    if env_doc and not data.get("doc_id"):
        data["doc_id"] = env_doc

    if required and not str(data.get("doc_id") or "").strip():
        raise FacebookGraphqlFallback(
            f"Template {name} has empty doc_id — set FB_GQL_*_DOC_ID or capture one"
        )
    return data


def render_template_value(node: Any, mapping: dict[str, Any]) -> Any:
    """Replace ``{{placeholders}}`` in strings; recurse into dict/list."""
    if isinstance(node, str):
        return _render_string(node, mapping)
    if isinstance(node, list):
        return [render_template_value(item, mapping) for item in node]
    if isinstance(node, dict):
        return {key: render_template_value(value, mapping) for key, value in node.items()}
    return node


def _render_string(text: str, mapping: dict[str, Any]) -> Any:
    # Whole-string placeholder → preserve non-string types (lists/ints).
    whole = _PLACEHOLDER_RE.fullmatch(text.strip())
    if whole:
        key = whole.group(1)
        if key not in mapping:
            raise FacebookGraphqlFallback(f"Missing template value for {{{{{key}}}}}")
        return mapping[key]

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in mapping:
            raise FacebookGraphqlFallback(f"Missing template value for {{{{{key}}}}}")
        value = mapping[key]
        if isinstance(value, (dict, list)):
            return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        return str(value)

    return _PLACEHOLDER_RE.sub(repl, text)


def build_variables(template: dict[str, Any], mapping: dict[str, Any]) -> dict[str, Any]:
    variables = template.get("variables")
    if variables is None:
        raise FacebookGraphqlFallback("Template missing 'variables' object")
    rendered = render_template_value(copy.deepcopy(variables), mapping)
    if not isinstance(rendered, dict):
        raise FacebookGraphqlFallback("Rendered variables must be a JSON object")
    return rendered


def base_mapping(
    *,
    c_user: str,
    item_id: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mapping: dict[str, Any] = {
        "c_user": c_user,
        "actor_id": c_user,
        "item_id": item_id,
        "listing_id": item_id,
        "for_sale_item_id": item_id,
        "client_mutation_id": str(uuid.uuid4()),
    }
    if extra:
        mapping.update(extra)
    return mapping


_ID_FIELD_TO_MAPPING_KEY = {
    "for_sale_item_id": "item_id",
    "listing_id": "item_id",
    "marketplace_listing_id": "item_id",
    "product_item_id": "item_id",
    "item_id": "item_id",
    "actor_id": "c_user",
    "client_mutation_id": "client_mutation_id",
}


def substitute_known_ids(node: Any, mapping: dict[str, Any]) -> Any:
    """Overwrite known id fields in a captured mutation even without ``{{placeholders}}``.

    Captured templates often hardcode the sample ``for_sale_item_id`` / ``actor_id``.
    """
    data = copy.deepcopy(node)

    def walk(current: Any) -> None:
        if isinstance(current, dict):
            for key, value in list(current.items()):
                map_key = _ID_FIELD_TO_MAPPING_KEY.get(key)
                if map_key and map_key in mapping and mapping[map_key] not in (None, ""):
                    current[key] = (
                        str(mapping[map_key])
                        if map_key != "client_mutation_id"
                        else str(mapping[map_key])
                    )
                else:
                    walk(value)
        elif isinstance(current, list):
            for item in current:
                walk(item)

    walk(data)
    return data


def prepare_mutation_variables(
    template: dict[str, Any], mapping: dict[str, Any]
) -> dict[str, Any]:
    """Render placeholders when present; otherwise substitute known id fields."""
    variables = template.get("variables")
    if variables is None:
        raise FacebookGraphqlFallback("Template missing 'variables' object")
    blob = json.dumps(variables)
    if "{{" in blob:
        return build_variables(template, mapping)
    rendered = substitute_known_ids(variables, mapping)
    if not isinstance(rendered, dict):
        raise FacebookGraphqlFallback("Rendered variables must be a JSON object")
    return rendered


def save_captured_template(
    name: str,
    *,
    doc_id: str,
    variables: dict[str, Any] | str,
    root: Path | None = None,
    friendly_name: str = "",
    meta: dict[str, Any] | None = None,
) -> Path:
    directory = default_graphql_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    if isinstance(variables, str):
        try:
            variables_obj: Any = json.loads(variables)
        except json.JSONDecodeError:
            variables_obj = variables
    else:
        variables_obj = variables
    payload = {
        "doc_id": str(doc_id),
        "friendly_name": friendly_name,
        "variables": variables_obj,
        "captured_at": meta or {},
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
