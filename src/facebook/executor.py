from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from src.facebook.errors import FacebookAutomationError, FacebookSessionError
from src.facebook.poster import create_vehicle_listing
from src.facebook.remover import remove_vehicle_listing
from src.facebook.session import get_page, is_logged_in, open_account_context, page_shows_login_form
from src.facebook.updater import update_vehicle_listing
from src.facebook.util import ensure_log_dir, env_bool, env_float, env_int, env_str, random_delay
from src.models import SyncAction
from src.store.db import SyncStore


@dataclass
class ExecutionResult:
    creates: int = 0
    updates: int = 0
    removals: int = 0
    errors: list[str] = None  # type: ignore[assignment]
    session_expired_accounts: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []
        if self.session_expired_accounts is None:
            self.session_expired_accounts = []


def execute_actions(
    actions: list[SyncAction],
    store: SyncStore,
    config: dict,
    *,
    root: Path,
    account_order: list[str] | None = None,
) -> ExecutionResult:
    if not actions:
        return ExecutionResult()

    fb_config = config.get("facebook", {})
    headless = env_bool("FB_HEADLESS", bool(fb_config.get("headless", True)))
    max_photos = env_int(
        "MAX_PHOTOS_PER_LISTING",
        int(fb_config.get("max_photos_per_listing", 20)),
    )
    delay_min = env_float("FB_ACTION_DELAY_MIN_SEC", 60.0)
    delay_max = env_float("FB_ACTION_DELAY_MAX_SEC", 120.0)
    removal_action = env_str(
        "REMOVAL_ACTION",
        str(config.get("sync", {}).get("removal_action", "mark_sold")),
    )
    log_dir = ensure_log_dir(root / "data" / "logs" / "facebook")

    ordered = _sort_actions(actions)
    by_account: dict[str, list[SyncAction]] = defaultdict(list)
    for action in ordered:
        if not action.account_id:
            continue
        by_account[action.account_id].append(action)

    result = ExecutionResult()

    ordered_accounts = account_order or list(by_account.keys())
    for account_id in ordered_accounts:
        account_actions = by_account.get(account_id)
        if not account_actions:
            continue
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
                    if page_shows_login_form(page):
                        raise FacebookSessionError(
                            f"Session expired for {account_id}. "
                            f"Run: python scripts/fb_login.py --account {account_id}"
                        )
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
            print(
                f"[SKIP] {account_id}: Session expired. "
                f"Run fb_login.py to refresh. ({exc})",
                flush=True,
            )
            result.errors.append(f"FAILED_SESSION_EXPIRED {account_id}: {exc}")
            result.session_expired_accounts.append(account_id)
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
        ok = remove_vehicle_listing(
            page,
            row["fb_listing_url"],
            autosell_id=action.autosell_id,
            removal_action=removal_action,
            log_dir=log_dir,
            store=store,
            account_id=action.account_id or "",
        )
        if ok:
            store.mark_fb_listing_removed(action.autosell_id, action.account_id or "")
            result.removals += 1
            print(f"Removed {action.autosell_id} on {action.account_id}")
        else:
            print(
                f"WARNING: remove {action.autosell_id} on {action.account_id}: "
                f"controls not found / unverified — left live in sync.db; "
                f"continuing queue"
            )
        return

    raise FacebookAutomationError(f"Unknown action: {action.action}")


def _sort_actions(actions: list[SyncAction]) -> list[SyncAction]:
    order = {"remove": 0, "update": 1, "create": 2}
    return sorted(actions, key=lambda item: order.get(item.action, 99))

