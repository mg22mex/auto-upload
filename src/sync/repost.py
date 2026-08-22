from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from src.models import SyncAction, Vehicle
from src.sync.weekly_bump import DEFAULT_MAX_PER_ACCOUNT_PER_RUN, DEFAULT_MIN_AGE_DAYS

# Live listings at least this old are eligible for relist/repost (and renew).
DEFAULT_REPOST_MIN_AGE_DAYS = DEFAULT_MIN_AGE_DAYS
DEFAULT_MAX_PER_ACCOUNT = DEFAULT_MAX_PER_ACCOUNT_PER_RUN
# Explicit --unlimited only (not --force). Daily runs must keep the batch cap.
UNLIMITED_PER_ACCOUNT = 10_000


def parse_older_than_days(raw: str) -> float:
    """Parse age floor as days (supports ``1.5``, ``2``, ``2d``)."""
    text = (raw or "").strip().lower()
    if text.endswith("d"):
        text = text[:-1]
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError(f"invalid older-than days: {raw!r}") from exc
    return max(0.0, value)


def resolve_min_age_days(
    cli_raw: str | None,
    *,
    env_names: tuple[str, ...] = ("REPOST_MIN_AGE_DAYS",),
    config_default: float = DEFAULT_REPOST_MIN_AGE_DAYS,
    force: bool = False,
) -> float:
    """CLI ``0`` and ``--force`` both disable the posted-at age floor."""
    if force:
        return 0.0
    if cli_raw is not None and str(cli_raw).strip() != "":
        return parse_older_than_days(str(cli_raw))
    for name in env_names:
        raw = os.getenv(name)
        if raw is not None and str(raw).strip() != "":
            return parse_older_than_days(raw)
    return max(0.0, float(config_default))


def resolve_max_per_account(
    cli_max: int | None,
    *,
    older_than_days: float,
    force: bool,
    env_name: str,
    config_default: int,
    unlimited: bool = False,
) -> int:
    """Daily batch cap. ``--force`` does not lift the cap (anti-ban).

    Pass ``unlimited=True`` or ``cli_max`` >= UNLIMITED_PER_ACCOUNT for a
    full-shelf run. ``older_than_days`` is unused for the cap (kept for callers).
    """
    del older_than_days, force  # age/holds are separate; cap always applies
    if unlimited:
        return UNLIMITED_PER_ACCOUNT
    if cli_max is not None:
        return max(0, int(cli_max))
    raw = os.getenv(env_name)
    if raw is not None and str(raw).strip() != "":
        return max(0, int(raw))
    return max(0, int(config_default))


def _posted_age_days(posted_at: str | None, now: datetime) -> float | None:
    if not posted_at:
        return None
    try:
        parsed = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed).total_seconds() / 86400


def plan_repost_actions(
    vehicles: list[Vehicle],
    account_ids: list[str],
    live_listings: list,
    *,
    explicit_ids: set[str] | None = None,
    all_eligible: bool = False,
    older_than_days: float = DEFAULT_REPOST_MIN_AGE_DAYS,
    max_per_account: int = 10,
    is_on_hold,
    force: bool = False,
    action_name: str = "repost",
    assigned_by_account: dict[str, set[str]] | None = None,
) -> tuple[list[SyncAction], list[str]]:
    """Plan repost/renew actions for live listings.

    With ``all_eligible=True``, only rows with
    ``now - posted_at >= older_than_days`` (default 2) are planned unless
    ``force=True`` or ``older_than_days == 0`` (all live dates).
    ``action_name='repost'`` means full delete+recreate (relist);
    ``'renew'`` is FB native Renovar (same URL). Prefer repost for momentum.

    ``is_on_hold(autosell_id, account_id) -> bool``.
    """
    if not explicit_ids and not all_eligible:
        raise ValueError("Specify explicit_ids or all_eligible=True")
    if action_name not in ("repost", "renew"):
        raise ValueError(f"Unsupported action_name: {action_name}")

    active_by_id = {vehicle.autosell_id: vehicle for vehicle in vehicles}
    now = datetime.now(timezone.utc)
    min_age = timedelta(days=float(older_than_days))
    hold_label = "repost hold" if action_name == "repost" else "renew hold"
    cap_label = "repost" if action_name == "repost" else "renew"

    live_by_key: dict[tuple[str, str], object] = {}
    for row in live_listings:
        if row["status"] != "live":
            continue
        live_by_key[(row["autosell_id"], row["account_id"])] = row

    actions: list[SyncAction] = []
    skipped: list[str] = []
    budget = {account_id: max_per_account for account_id in account_ids}

    def consider(autosell_id: str, account_id: str) -> None:
        vehicle = active_by_id.get(autosell_id)
        if vehicle is None:
            skipped.append(f"{autosell_id} on {account_id}: not in public catalog")
            return

        row = live_by_key.get((autosell_id, account_id))
        if row is None or not row["fb_listing_url"]:
            skipped.append(f"{autosell_id} on {account_id}: no live listing in sync.db")
            return

        if not force and is_on_hold(autosell_id, account_id):
            skipped.append(f"{autosell_id} on {account_id}: {hold_label} active")
            return

        apply_age = all_eligible and older_than_days > 0 and not force
        if apply_age:
            posted_at = row["posted_at"] if "posted_at" in row.keys() else None
            if posted_at:
                try:
                    posted = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
                    if posted.tzinfo is None:
                        posted = posted.replace(tzinfo=timezone.utc)
                    if now - posted < min_age:
                        age = _posted_age_days(posted_at, now)
                        skipped.append(
                            f"{autosell_id} on {account_id}: posted {age:.1f}d ago "
                            f"(min {older_than_days}d)"
                        )
                        return
                except ValueError:
                    pass

        if budget[account_id] <= 0:
            skipped.append(f"{autosell_id} on {account_id}: {cap_label} cap reached for account")
            return

        age_label = (
            str(int(older_than_days))
            if float(older_than_days).is_integer()
            else str(older_than_days)
        )
        if explicit_ids:
            reason = f"Manual {action_name}"
        elif force or older_than_days <= 0:
            reason = f"Eligible (force/min-age {age_label}d)"
        else:
            reason = f"Eligible (>{age_label}d since post)"
        actions.append(
            SyncAction(
                action=action_name,
                autosell_id=autosell_id,
                account_id=account_id,
                slug=vehicle.slug,
                reason=reason,
                vehicle=vehicle,
                fb_listing_url=row["fb_listing_url"],
            )
        )
        budget[account_id] -= 1

    if explicit_ids:
        for account_id in account_ids:
            for autosell_id in sorted(explicit_ids):
                consider(autosell_id, account_id)
    else:
        for account_id in account_ids:
            assigned = None
            if assigned_by_account is not None:
                assigned = assigned_by_account.get(account_id)
            candidates: list[tuple[str, str | None]] = []
            for (aid, acct), row in live_by_key.items():
                if acct != account_id or aid not in active_by_id:
                    continue
                if assigned is not None and aid not in assigned:
                    continue
                posted_at = row["posted_at"] if "posted_at" in row.keys() else None
                candidates.append((aid, posted_at))
            candidates.sort(key=lambda item: item[1] or "")  # FIFO: oldest posted_at first
            for autosell_id, _ in candidates:
                if budget[account_id] <= 0:
                    break
                consider(autosell_id, account_id)

    return actions, skipped
