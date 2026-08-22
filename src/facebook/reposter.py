from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from src.facebook.browser_health import is_browser_dead
from src.facebook.errors import FacebookAutomationError, FacebookDeferredError, FacebookPostingError, FacebookSessionError
from src.facebook.poster import create_vehicle_listing
from src.facebook.remover import extract_item_id, ensure_no_matching_shelf_listings, remove_vehicle_listing
from src.facebook.ui import dismiss_overlays
from src.facebook.session import (
    format_session_login_error,
    get_page,
    is_logged_in,
    open_account_context,
    page_shows_login_form,
    resolve_session_dir,
    session_health_report,
)
from src.facebook.util import (
    ensure_log_dir,
    env_bool,
    env_float,
    env_int,
    env_str,
    random_delay,
)
from src.models import SyncAction
from src.store.db import SyncStore


@dataclass
class RepostResult:
    reposts: int = 0
    errors: list[str] = None  # type: ignore[assignment]
    browser_reopens: int = 0
    session_expired_accounts: list[str] = None  # type: ignore[assignment]
    accounts_ok: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []
        if self.session_expired_accounts is None:
            self.session_expired_accounts = []
        if self.accounts_ok is None:
            self.accounts_ok = []


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
    # Relist: hard delete by default (avoids "publicación duplicada").
    # REPOST_REMOVAL_ACTION > sync.repost.removal_action > delete
    removal_action = env_str(
        "REPOST_REMOVAL_ACTION",
        str(repost_cfg.get("removal_action") or "delete"),
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
            result.accounts_ok.append(account_id)
            continue
        print(f"Repost: processing {len(remaining)} listing(s) for {account_id}", flush=True)
        try:
            session_dir = resolve_session_dir(config, account_id, root)
        except FacebookSessionError:
            session_dir = (root / "sessions" / account_id).resolve()
        health = session_health_report(session_dir)
        print(
            f"Repost: session health {account_id}: "
            f"exists={health['exists']} files={health['file_count']} "
            f"cookies_file={health['has_cookies_file']} "
            f"looks_empty={health['looks_empty']} path={health['path']}",
            flush=True,
        )
        if health["looks_empty"]:
            msg = format_session_login_error(account_id, session_dir)
            print(f"WARN: {msg}", flush=True)

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
                            format_session_login_error(account_id, session_dir)
                        )

                    done_in_session = 0
                    while remaining:
                        action = remaining[0]
                        if page_shows_login_form(page):
                            raise FacebookSessionError(
                                format_session_login_error(account_id, session_dir)
                                + " (session expired mid-run)"
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
                        except FacebookDeferredError as exc:
                            print(
                                f"DEFERRED_FAILED: {action.autosell_id} on {account_id}: {exc}",
                                flush=True,
                            )
                            result.errors.append(
                                f"DEFERRED_FAILED {action.autosell_id} on {account_id}: {exc}"
                            )
                            remaining.pop(0)
                            reopen_requested = True
                            break
                        except Exception as exc:
                            if is_browser_dead(exc):
                                print(
                                    f"WARN: repost {action.autosell_id} on {account_id}: "
                                    f"browser died ({exc}); will reopen and retry",
                                    flush=True,
                                )
                                reopen_requested = True
                                break
                            msg = f"repost {action.autosell_id} on {account_id}: {exc}"
                            print(f"ERROR: {msg}", flush=True)
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
                                f"({len(remaining)} left)",
                                flush=True,
                            )
                            break
            except FacebookSessionError as exc:
                print(
                    f"[SKIP] {account_id}: Session expired. "
                    f"Run fb_login.py to refresh. ({exc})",
                    flush=True,
                )
                result.errors.append(f"FAILED_SESSION_EXPIRED {account_id}: {exc}")
                result.session_expired_accounts.append(account_id)
                remaining.clear()
                break
            except Exception as exc:
                if is_browser_dead(exc):
                    reopen_requested = True
                    print(
                        f"WARN: repost on {account_id}: browser died ({exc}); "
                        f"will reopen and retry",
                        flush=True,
                    )
                else:
                    result.errors.append(f"{account_id}: {exc}")
                    break

            if not remaining:
                break
            if not reopen_requested and restart_every > 0:
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
                    f"({reopens}/{max_reopens}, {len(remaining)} left)",
                    flush=True,
                )
                continue
            break

        if account_id not in result.session_expired_accounts:
            result.accounts_ok.append(account_id)

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
    """Strict delete-before-create. Never create if remove is not verified."""
    if not action.vehicle:
        raise FacebookAutomationError("Repost action missing vehicle payload")
    account_id = action.account_id or ""
    old_url = action.fb_listing_url
    if not old_url:
        row = store.get_fb_listing(action.autosell_id, account_id)
        old_url = row["fb_listing_url"] if row else None

    # Force hard-delete on the repost path (mark_sold alone often triggers
    # Facebook's "publicación duplicada" when re-creating the same vehicle).
    action_norm = (removal_action or "delete").strip().lower()
    if action_norm not in ("delete", "mark_sold"):
        action_norm = "delete"
    if action_norm != "delete":
        print(
            f"WARNING: {action.autosell_id}: repost coerced removal_action "
            f"{action_norm!r} → delete (avoid duplicate listing flag)"
        )
        action_norm = "delete"

    print(
        f"Repost {action.autosell_id}: remove-then-create "
        f"(action={action_norm}, old={old_url or 'none'})"
    )

    removed = False
    if not old_url:
        # Orphan / missing mapping — free the slot and proceed to create.
        print(
            f"WARNING: {action.autosell_id}: no fb_listing_url — treating as "
            f"already gone; clearing sync.db slot before create"
        )
        store.mark_fb_listing_removed(
            action.autosell_id,
            account_id,
            clear_url=True,
        )
        removed = True
    else:
        # --- Phase 1: remove (must succeed); do NOT create on failure ---
        try:
            ok = remove_vehicle_listing(
                page,
                old_url,
                autosell_id=action.autosell_id,
                removal_action=action_norm,
                log_dir=log_dir,
                require_verified=True,
                store=store,
                account_id=account_id,
            )
            if not ok:
                # Soft skip: controls not found / unverified — continue queue, no create.
                print(
                    f"WARNING: {action.autosell_id}: remove not confirmed — "
                    f"SKIP_CREATE (soft); continuing remaining queue "
                    f"(will not post a duplicate)"
                )
                return
            removed = True
            # Clear old URL so a mid-create crash is not treated as still live.
            store.mark_fb_listing_removed(
                action.autosell_id,
                account_id,
                clear_url=True,
            )
            print(
                f"  {action.autosell_id}: old listing cleared in sync.db "
                f"(status=removed, url=null)"
            )
            dismiss_overlays(page)
        except Exception as exc:
            if is_browser_dead(exc):
                raise
            # Explicit: never fall through to create
            raise FacebookPostingError(
                f"SKIP_CREATE: could not remove old listing for "
                f"{action.autosell_id} before repost: {exc}"
            ) from exc

    # Title/model match across all selling tabs — not only the old item id.
    if not ensure_no_matching_shelf_listings(
        page,
        action.vehicle,
        item_id=extract_item_id(old_url) if old_url else None,
        autosell_id=action.autosell_id,
    ):
        print(
            f"WARNING: {action.autosell_id}: matching listing still on "
            f"selling shelf — SKIP_CREATE (will not post a duplicate)"
        )
        return

    # Safety cooldown: let FB settle index after verified delete before create.
    cooldown = env_float("REPOST_DELETE_COOLDOWN_SEC", 7.0)
    if cooldown > 0:
        low = max(5.0, cooldown * 0.85)
        high = max(low, cooldown * 1.25)
        print(
            f"  {action.autosell_id}: delete→create cooldown "
            f"{low:.1f}–{high:.1f}s (REPOST_DELETE_COOLDOWN_SEC={cooldown})"
        )
        random_delay(low, high)

    # --- Phase 2: create only after verified remove ---
    try:
        new_url = create_vehicle_listing(
            page,
            action.vehicle,
            fb_config=fb_config,
            max_photos=max_photos,
            log_dir=log_dir,
        )
    except FacebookDeferredError:
        raise
    except Exception as exc:
        if removed and not is_browser_dead(exc):
            raise FacebookPostingError(
                f"NEEDS_RECREATE: removed/sold old listing but create failed "
                f"for {action.autosell_id}: {exc}"
            ) from exc
        raise

    store.record_repost(
        action.autosell_id,
        account_id,
        fb_listing_url=new_url,
        content_hash=action.vehicle.content_hash(),
    )
    result.reposts += 1
    print(f"Reposted {action.autosell_id} on {action.account_id}: {new_url}")
