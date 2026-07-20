from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from src.facebook.errors import FacebookAutomationError, FacebookSessionError
from src.facebook.poster import create_vehicle_listing
from src.facebook.remover import remove_vehicle_listing
from src.facebook.session import get_page, is_logged_in, open_account_context, page_shows_login_form
from src.facebook.util import ensure_log_dir, env_float, env_int, env_str, random_delay
from src.models import SyncAction
from src.store.db import SyncStore


@dataclass
class RepostResult:
    reposts: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


def execute_reposts(
    actions: list[SyncAction],
    store: SyncStore,
    config: dict,
    *,
    root: Path,
    account_order: list[str] | None = None,
) -> RepostResult:
    if not actions:
        return RepostResult()

    fb_config = config.get("facebook", {})
    headless = _env_bool("FB_HEADLESS", fb_config.get("headless", True))
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

    by_account: dict[str, list[SyncAction]] = defaultdict(list)
    for action in actions:
        if action.action != "repost" or not action.account_id:
            continue
        by_account[action.account_id].append(action)

    result = RepostResult()
    ordered_accounts = account_order or list(by_account.keys())

    for account_id in ordered_accounts:
        account_actions = by_account.get(account_id)
        if not account_actions:
            continue
        print(f"Repost: processing {len(account_actions)} listing(s) for {account_id}")
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
                        f"Not logged in for {account_id}. "
                        f"Run: python scripts/fb_login.py --account {account_id}"
                    )

                for action in account_actions:
                    if page_shows_login_form(page):
                        raise FacebookSessionError(
                            f"Session expired for {account_id}. "
                            f"Run: python scripts/fb_login.py --account {account_id}"
                        )
                    try:
                        _repost_one(
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
                        msg = f"repost {action.autosell_id} on {account_id}: {exc}"
                        print(f"ERROR: {msg}")
                        result.errors.append(msg)
                    random_delay(delay_min, delay_max)
        except FacebookSessionError as exc:
            result.errors.append(str(exc))
        except Exception as exc:
            result.errors.append(f"{account_id}: {exc}")

    return result


def _repost_one(
    page,
    action: SyncAction,
    store: SyncStore,
    *,
    fb_config: dict,
    max_photos: int,
    removal_action: str,
    log_dir: Path,
    result: RepostResult,
) -> None:
    if not action.vehicle:
        raise FacebookAutomationError("Repost action missing vehicle payload")
    old_url = action.fb_listing_url
    if not old_url:
        row = store.get_fb_listing(action.autosell_id, action.account_id or "")
        old_url = row["fb_listing_url"] if row else None
    if not old_url:
        raise FacebookAutomationError("No fb_listing_url for repost")

    remove_vehicle_listing(
        page,
        old_url,
        autosell_id=action.autosell_id,
        removal_action=removal_action,
        log_dir=log_dir,
    )
    new_url = create_vehicle_listing(
        page,
        action.vehicle,
        fb_config=fb_config,
        max_photos=max_photos,
        log_dir=log_dir,
    )
    store.record_repost(
        action.autosell_id,
        action.account_id or "",
        fb_listing_url=new_url,
        content_hash=action.vehicle.content_hash(),
    )
    result.reposts += 1
    print(f"Reposted {action.autosell_id} on {action.account_id}: {new_url}")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
