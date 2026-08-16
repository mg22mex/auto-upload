from __future__ import annotations

from src.models import SyncAction, Vehicle
from src.sync.allocator import Allocation


def plan_sync_actions(
    vehicles: list[Vehicle],
    account_ids: list[str],
    live_listings: list,
    *,
    max_creates_per_account: int,
    allocation: Allocation | None = None,
    enforce_overflow_removals: bool = False,
) -> list[SyncAction]:
    active_by_id = {vehicle.autosell_id: vehicle for vehicle in vehicles}
    active_ids = set(active_by_id)

    live_by_key: dict[tuple[str, str], object] = {}
    for row in live_listings:
        live_by_key[(row["autosell_id"], row["account_id"])] = row

    assigned_keys = allocation.assigned_keys() if allocation else None
    create_pairs = set(allocation.creates) if allocation else None
    overflow_keys = set(allocation.overflow) if allocation else set()

    actions: list[SyncAction] = []

    seen_removals: set[tuple[str, str]] = set()
    for (autosell_id, account_id), row in live_by_key.items():
        if account_id not in account_ids:
            continue
        drop = autosell_id not in active_ids
        if (
            not drop
            and enforce_overflow_removals
            and (autosell_id, account_id) in overflow_keys
        ):
            drop = True
            reason = "Over slot cap / duplicate account occupancy"
        else:
            reason = "Vehicle no longer on public autosell.mx catalog"
        if not drop:
            continue
        key = (autosell_id, account_id)
        if key in seen_removals:
            continue
        seen_removals.add(key)
        actions.append(
            SyncAction(
                action="remove",
                autosell_id=autosell_id,
                account_id=account_id,
                slug="",
                reason=reason,
            )
        )

    create_budget = {account_id: max_creates_per_account for account_id in account_ids}

    for vehicle in vehicles:
        for account_id in account_ids:
            key = (vehicle.autosell_id, account_id)
            existing = live_by_key.get(key)

            if assigned_keys is not None:
                if existing is None:
                    if key not in (create_pairs or set()):
                        continue
                elif key not in assigned_keys:
                    continue

            if existing is None:
                if create_budget[account_id] <= 0:
                    actions.append(
                        SyncAction(
                            action="create",
                            autosell_id=vehicle.autosell_id,
                            account_id=account_id,
                            slug=vehicle.slug,
                            reason="Deferred: daily create cap reached for account",
                            vehicle=vehicle,
                        )
                    )
                    continue
                reason = (
                    "Assigned slot — missing on Facebook"
                    if create_pairs is not None
                    else "New public catalog vehicle missing on Facebook"
                )
                actions.append(
                    SyncAction(
                        action="create",
                        autosell_id=vehicle.autosell_id,
                        account_id=account_id,
                        slug=vehicle.slug,
                        reason=reason,
                        vehicle=vehicle,
                    )
                )
                create_budget[account_id] -= 1
                continue

            if existing["content_hash"] != vehicle.content_hash():
                actions.append(
                    SyncAction(
                        action="update",
                        autosell_id=vehicle.autosell_id,
                        account_id=account_id,
                        slug=vehicle.slug,
                        reason="Price, photos, or details changed on website",
                        vehicle=vehicle,
                    )
                )

    return actions


def split_executable_actions(actions: list[SyncAction]) -> tuple[list[SyncAction], list[SyncAction]]:
    deferred: list[SyncAction] = []
    executable: list[SyncAction] = []
    for action in actions:
        if action.action == "create" and action.reason.startswith("Deferred:"):
            deferred.append(action)
        else:
            executable.append(action)
    return executable, deferred
