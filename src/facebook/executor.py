from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from src.facebook.errors import FacebookAutomationError, FacebookSessionError
from src.facebook.poster import create_vehicle_listing
from src.facebook.remover import remove_vehicle_listing
from src.facebook.session import get_page, is_logged_in, open_account_context
from src.facebook.updater import update_vehicle_listing
from src.facebook.util import ensure_log_dir, random_delay
from src.models import SyncAction
from src.store.db import SyncStore


@dataclass
class ExecutionResult:
    creates: int = 0
    updates: int = 0
    removals: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


def execute_actions(
    actions: list[SyncAction],
    store: SyncStore,
    config: dict,
    *,
    root: Path,
) -> ExecutionResult:
    if not actions:
        return ExecutionResult()

    fb_config = config.get("facebook", {})
    headless = _env_bool("FB_HEADLESS", fb_config.get("headless", True))
    max_photos = int(
        os.getenv(
            "MAX_PHOTOS_PER_LISTING",
            fb_config.get("max_photos_per_listing", 20),
        )
    )
    delay_min = float(os.getenv("FB_ACTION_DELAY_MIN_SEC", "60"))
    delay_max = float(os.getenv("FB_ACTION_DELAY_MAX_SEC", "120"))
    removal_action = os.getenv(
        "REMOVAL_ACTION",
        config.get("sync", {}).get("removal_action", "mark_sold"),
    )
    log_dir = ensure_log_dir(root / "data" / "logs" / "facebook")

    ordered = _sort_actions(actions)
    by_account: dict[str, list[SyncAction]] = defaultdict(list)
    for action in ordered:
        if not action.account_id:
            continue
        by_account[action.account_id].append(action)

    result = ExecutionResult()

    for account_id, account_actions in by_account.items():
        print(f"Facebook: processing {len(account_actions)} action(s) for {account_id}")
        try:
            with open_account_context(
                config,
                account_id,
                root=root,
                headless=headless,
            ) as context:
                page = get_page(context)
                if not is_logged_in(page):
                    raise FacebookSessionError(
                        f"Not logged in for {account_id}. Run: python scripts/fb_login.py --account {account_id}"
                    )

                for action in account_actions:
                    try:
                        _execute_one(
                            page,
                            action,
                            store,
                            fb_config=fb_config,
                            max_photos=max_photos,
                            removal_action=removal_action,
                            log_dir=log_dir,
                            result=result,
                        )
                    except Exception as exc:
                        msg = f"{action.action} {action.autosell_id} on {account_id}: {exc}"
                        print(f"ERROR: {msg}")
                        result.errors.append(msg)
                    random_delay(delay_min, delay_max)
        except FacebookSessionError as exc:
            result.errors.append(str(exc))
        except Exception as exc:
            result.errors.append(f"{account_id}: {exc}")

    return result


def _execute_one(
    page,
    action: SyncAction,
    store: SyncStore,
    *,
    fb_config: dict,
    max_photos: int,
    removal_action: str,
    log_dir: Path,
    result: ExecutionResult,
) -> None:
    if action.action == "create":
        if not action.vehicle:
            raise FacebookAutomationError("Create action missing vehicle payload")
        url = create_vehicle_listing(
            page,
            action.vehicle,
            fb_config=fb_config,
            max_photos=max_photos,
            log_dir=log_dir,
        )
        store.upsert_fb_listing(
            action.autosell_id,
            action.account_id or "",
            fb_listing_url=url,
            content_hash=action.vehicle.content_hash(),
            status="live",
        )
        result.creates += 1
        print(f"Created {action.autosell_id} on {action.account_id}: {url}")
        return

    if action.action == "update":
        if not action.vehicle:
            raise FacebookAutomationError("Update action missing vehicle payload")
        row = store.get_fb_listing(action.autosell_id, action.account_id or "")
        if not row or not row["fb_listing_url"]:
            raise FacebookAutomationError("No fb_listing_url in database for update")
        update_vehicle_listing(
            page,
            row["fb_listing_url"],
            action.vehicle,
            log_dir=log_dir,
        )
        store.upsert_fb_listing(
            action.autosell_id,
            action.account_id or "",
            fb_listing_url=row["fb_listing_url"],
            content_hash=action.vehicle.content_hash(),
            status="live",
        )
        result.updates += 1
        print(f"Updated {action.autosell_id} on {action.account_id}")
        return

    if action.action == "remove":
        row = store.get_fb_listing(action.autosell_id, action.account_id or "")
        if not row or not row["fb_listing_url"]:
            store.mark_fb_listing_removed(action.autosell_id, action.account_id or "")
            result.removals += 1
            print(f"Removed {action.autosell_id} on {action.account_id} (no URL; marked in DB)")
            return
        remove_vehicle_listing(
            page,
            row["fb_listing_url"],
            autosell_id=action.autosell_id,
            removal_action=removal_action,
            log_dir=log_dir,
        )
        store.mark_fb_listing_removed(action.autosell_id, action.account_id or "")
        result.removals += 1
        print(f"Removed {action.autosell_id} on {action.account_id}")
        return

    raise FacebookAutomationError(f"Unknown action: {action.action}")


def _sort_actions(actions: list[SyncAction]) -> list[SyncAction]:
    order = {"remove": 0, "update": 1, "create": 2}
    return sorted(actions, key=lambda item: order.get(item.action, 99))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
