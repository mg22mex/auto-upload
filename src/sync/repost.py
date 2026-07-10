from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.models import SyncAction, Vehicle


def parse_older_than_days(raw: str) -> int:
    text = (raw or "").strip().lower()
    if text.endswith("d"):
        text = text[:-1]
    return max(0, int(text))


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
    older_than_days: int = 7,
    max_per_account: int = 10,
    is_on_hold,
    force: bool = False,
) -> tuple[list[SyncAction], list[str]]:
    """Plan repost actions. is_on_hold(autosell_id, account_id) -> bool."""
    if not explicit_ids and not all_eligible:
        raise ValueError("Specify explicit_ids or all_eligible=True")

    active_by_id = {vehicle.autosell_id: vehicle for vehicle in vehicles}
    now = datetime.now(timezone.utc)
    min_age = timedelta(days=older_than_days)

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
            skipped.append(f"{autosell_id} on {account_id}: repost hold active")
            return

        if all_eligible and older_than_days > 0:
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
            skipped.append(f"{autosell_id} on {account_id}: repost cap reached for account")
            return

        actions.append(
            SyncAction(
                action="repost",
                autosell_id=autosell_id,
                account_id=account_id,
                slug=vehicle.slug,
                reason="Manual repost" if explicit_ids else f"Eligible (>{older_than_days}d since post)",
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
            candidates: list[tuple[str, str | None]] = []
            for (aid, acct), row in live_by_key.items():
                if acct != account_id or aid not in active_by_id:
                    continue
                posted_at = row["posted_at"] if "posted_at" in row.keys() else None
                candidates.append((aid, posted_at))
            candidates.sort(key=lambda item: item[1] or "")
            for autosell_id, _ in candidates:
                if budget[account_id] <= 0:
                    break
                consider(autosell_id, account_id)

    return actions, skipped
