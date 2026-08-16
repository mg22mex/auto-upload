"""Partition catalog vehicles across Facebook accounts with a hard slot cap."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from src.models import Vehicle

DEFAULT_MAX_LISTINGS_PER_ACCOUNT = 15


def slot_allocator_config(config: dict[str, Any] | None) -> dict[str, Any]:
    sync = (config or {}).get("sync") or {}
    raw = sync.get("slot_allocator") or {}
    max_n = sync.get("max_listings_per_account")
    if max_n is None:
        max_n = raw.get("max_listings_per_account", DEFAULT_MAX_LISTINGS_PER_ACCOUNT)
    enabled = raw.get("enabled")
    if enabled is None:
        enabled = True
    return {
        "enabled": bool(enabled),
        "max_listings_per_account": max(1, int(max_n)),
        "enforce_overflow_removals": bool(raw.get("enforce_overflow_removals", False)),
    }


def allocate_from_config(
    config: dict[str, Any] | None,
    vehicles: Sequence[Vehicle],
    account_ids: Sequence[str],
    live_listings: Iterable[Any],
) -> Allocation | None:
    cfg = slot_allocator_config(config)
    if not cfg["enabled"]:
        return None
    return allocate_slots(
        vehicles,
        account_ids,
        live_listings,
        max_per_account=cfg["max_listings_per_account"],
    )


def _posted_key(posted_at: str | None) -> str:
    return posted_at or ""


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        if key in row.keys():
            return row[key]
    except Exception:
        pass
    if isinstance(row, dict):
        return row.get(key, default)
    return default


@dataclass
class Allocation:
    """Desired live occupancy after slot assignment."""

    by_account: dict[str, list[str]]
    waitlist: list[str]
    overflow: list[tuple[str, str]]  # (autosell_id, account_id) live extras
    creates: list[tuple[str, str]]  # assigned but not currently live
    max_per_account: int
    account_ids: list[str]

    def assigned_ids(self, account_id: str) -> set[str]:
        return set(self.by_account.get(account_id) or [])

    def assigned_keys(self) -> set[tuple[str, str]]:
        keys: set[tuple[str, str]] = set()
        for account_id, ids in self.by_account.items():
            for autosell_id in ids:
                keys.add((autosell_id, account_id))
        return keys

    def occupancy(self, account_id: str) -> int:
        return len(self.by_account.get(account_id) or [])

    def free_slots(self, account_id: str) -> int:
        return max(0, self.max_per_account - self.occupancy(account_id))

    def total_capacity(self) -> int:
        return self.max_per_account * len(self.account_ids)

    def format_table(self) -> str:
        lines = [
            f"{'Account':<14} {'Assigned':>8} {'Capacity':>8} {'Free':>6}  Vehicle ids (first 15)"
        ]
        for account_id in self.account_ids:
            ids = self.by_account.get(account_id) or []
            preview = ", ".join(ids[:8])
            if len(ids) > 8:
                preview += f" … +{len(ids) - 8}"
            lines.append(
                f"{account_id:<14} {len(ids):>8} {self.max_per_account:>8} "
                f"{self.free_slots(account_id):>6}  {preview}"
            )
        lines.append(
            f"Waitlist: {len(self.waitlist)}  |  Overflow live (not in partition): "
            f"{len(self.overflow)}  |  New slot fills: {len(self.creates)}"
        )
        return "\n".join(lines)


def allocate_slots(
    vehicles: Sequence[Vehicle],
    account_ids: Sequence[str],
    live_listings: Iterable[Any],
    *,
    max_per_account: int = DEFAULT_MAX_LISTINGS_PER_ACCOUNT,
) -> Allocation:
    """Sticky partition: keep existing live pairs, cap at max_per_account, fill empties.

    Duplicate live rows (same vehicle on two accounts) keep the oldest ``posted_at``.
    Over-capacity on one account: keep the 15 oldest ``posted_at`` (FIFO bump queue).
    Vacancies fill from unassigned catalog, catalog order (scrape/newest-first).
    Sold/removed rows are absent from live_listings so their slots free automatically.
    """
    accounts = [a for a in account_ids if a]
    max_n = max(1, int(max_per_account))
    catalog_ids = [v.autosell_id for v in vehicles]
    catalog_set = set(catalog_ids)

    live_rows: list[tuple[str, str, str | None]] = []
    for row in live_listings:
        status = _row_get(row, "status", "live")
        if status and str(status) != "live":
            continue
        aid = _row_get(row, "autosell_id")
        acct = _row_get(row, "account_id")
        if not aid or not acct or acct not in accounts:
            continue
        if aid not in catalog_set:
            continue
        live_rows.append((str(aid), str(acct), _row_get(row, "posted_at")))

    # One vehicle → one account (oldest listing wins).
    by_vehicle: dict[str, list[tuple[str, str | None]]] = {}
    for aid, acct, posted in live_rows:
        by_vehicle.setdefault(aid, []).append((acct, posted))

    sticky: dict[str, str] = {}
    overflow: list[tuple[str, str]] = []
    for aid, pairs in by_vehicle.items():
        pairs.sort(key=lambda item: _posted_key(item[1]))
        sticky[aid] = pairs[0][0]
        for acct, _posted in pairs[1:]:
            overflow.append((aid, acct))

    by_account: dict[str, list[tuple[str, str | None]]] = {a: [] for a in accounts}
    posted_lookup = {(aid, acct): posted for aid, acct, posted in live_rows}
    for aid, acct in sticky.items():
        by_account[acct].append((aid, posted_lookup.get((aid, acct))))

    assigned: dict[str, list[str]] = {a: [] for a in accounts}
    for acct in accounts:
        holders = sorted(by_account[acct], key=lambda item: _posted_key(item[1]))
        keep = holders[:max_n]
        drop = holders[max_n:]
        assigned[acct] = [aid for aid, _ in keep]
        for aid, _posted in drop:
            overflow.append((aid, acct))

    occupying = {aid for ids in assigned.values() for aid in ids}
    waitlist = [aid for aid in catalog_ids if aid not in occupying]
    creates: list[tuple[str, str]] = []

    def emptiest() -> str | None:
        under = [a for a in accounts if len(assigned[a]) < max_n]
        if not under:
            return None
        return min(under, key=lambda a: (len(assigned[a]), accounts.index(a)))

    remaining: list[str] = []
    for aid in waitlist:
        acct = emptiest()
        if acct is None:
            remaining.append(aid)
            continue
        assigned[acct].append(aid)
        creates.append((aid, acct))
    waitlist = remaining

    return Allocation(
        by_account=assigned,
        waitlist=waitlist,
        overflow=overflow,
        creates=creates,
        max_per_account=max_n,
        account_ids=list(accounts),
    )
