from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from src.facebook.browser_health import is_browser_dead
from src.facebook.errors import FacebookAutomationError, FacebookPostingError, FacebookSessionError
from src.facebook.poster import create_vehicle_listing
from src.facebook.remover import remove_vehicle_listing
from src.facebook.session import get_page, is_logged_in, open_account_context, page_shows_login_form
from src.facebook.util import ensure_log_dir, env_bool, env_float, env_int, env_str, random_delay
from src.models import SyncAction
from src.store.db import SyncStore


@dataclass
class RepostResult:
    reposts: int = 0
    errors: list[str] = None  # type: ignore[assignment]
    browser_reopens: int = 0

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
    repost_cfg = config.get("sync", {}).get("repost", {}) or {}
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
    restart_every = env_int(
        "FB_REPOST_BROWSER_EVERY",
        int(repost_cfg.get("restart_browser_every", 3)),
    )
    max_reopens = env_int(
        "FB_BROWSER_REOPEN_MAX",
        int(repost_cfg.get("max_browser_reopens", 5)),
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
        remaining = list(by_account.get(account_id) or [])
        if not remaining:
            continue
        print(f"Repost: processing {len(remaining)} listing(s) for {account_id}")
        reopens = 0

        while remaining:
            reopen_requested = False
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

                    done_in_session = 0
                    while remaining:
                        action = remaining[0]
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
                            remaining.pop(0)
                            done_in_session += 1
                        except Exception as exc:
                            if is_browser_dead(exc):
                                print(
                                    f"WARN: repost {action.autosell_id} on {account_id}: "
                                    f"browser died ({exc}); will reopen and retry"
                                )
                                reopen_requested = True
                                break
                            msg = f"repost {action.autosell_id} on {account_id}: {exc}"
                            print(f"ERROR: {msg}")
                            result.errors.append(msg)
                            remaining.pop(0)

                        if remaining:
                            random_delay(delay_min, delay_max)

                        if (
                            restart_every > 0
                            and done_in_session >= restart_every
                            and remaining
                        ):
                            print(
                                f"Repost: restarting browser after {done_in_session} "
                                f"listing(s) for {account_id} "
                                f"({len(remaining)} left)"
                            )
                            break
            except FacebookSessionError as exc:
                result.errors.append(str(exc))
                break
            except Exception as exc:
                if is_browser_dead(exc):
                    reopen_requested = True
                    print(
                        f"WARN: repost on {account_id}: browser died ({exc}); "
                        f"will reopen and retry"
                    )
                else:
                    result.errors.append(f"{account_id}: {exc}")
                    break

            if not remaining:
                break
            if not reopen_requested and restart_every > 0:
                # Proactive chunk restart — do not count against reopen budget.
                continue
            if reopen_requested:
                reopens += 1
                result.browser_reopens += 1
                if reopens > max_reopens:
                    result.errors.append(
                        f"{account_id}: too many browser reopens ({reopens}); "
                        f"{len(remaining)} listing(s) left"
                    )
                    break
                print(
                    f"Repost: reopening browser for {account_id} "
                    f"({reopens}/{max_reopens}, {len(remaining)} left)"
                )
                continue
            break

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

    removed = False
    try:
        remove_vehicle_listing(
            page,
            old_url,
            autosell_id=action.autosell_id,
            removal_action=removal_action,
            log_dir=log_dir,
        )
        removed = True
        new_url = create_vehicle_listing(
            page,
            action.vehicle,
            fb_config=fb_config,
            max_photos=max_photos,
            log_dir=log_dir,
        )
    except Exception as exc:
        if removed and not is_browser_dead(exc):
            raise FacebookPostingError(
                f"NEEDS_RECREATE: removed/sold old listing but create failed "
                f"for {action.autosell_id}: {exc}"
            ) from exc
        raise

    store.record_repost(
        action.autosell_id,
        action.account_id or "",
        fb_listing_url=new_url,
        content_hash=action.vehicle.content_hash(),
    )
    result.reposts += 1
    print(f"Reposted {action.autosell_id} on {action.account_id}: {new_url}")
