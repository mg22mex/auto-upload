from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import urlparse

from playwright.sync_api import Locator, Page

from src.facebook.errors import FacebookPostingError
from src.facebook.poster import _save_debug

RemovalAction = Literal["delete", "mark_sold"]

SELLING_URL = "https://www.facebook.com/marketplace/you/selling"
DASHBOARD_URL = "https://www.facebook.com/marketplace/you/dashboard"

_ALREADY_GONE_PHRASES = (
    "isn't available",
    "is not available",
    "isn't available right now",
    "content isn't available",
    "this content isn't available",
    "this content is no longer available",
    "no longer available",
    "no está disponible",
    "no esta disponible",
    "contenido no está disponible",
    "contenido no esta disponible",
    "este contenido no está disponible",
    "this listing is unavailable",
    "listing is unavailable",
    "listing unavailable",
    "esta publicación no está disponible",
    "publicación no disponible",
    "item not available",
    "not available right now",
    "has been deleted",
    "ha sido eliminada",
    "ha sido eliminado",
    "was deleted",
    "se eliminó",
    "se elimino",
    "page isn't available",
    "la página no está disponible",
    "sorry, this content isn't available",
    "lo sentimos, este contenido no está disponible",
)

# Sold / pending / out-of-stock counts as deactivated for repost (FB allows re-list).
_SOLD_OR_INACTIVE_PHRASES = (
    "marcado como vendido",
    "marked as sold",
    "you marked this as sold",
    "marcaste esto como vendido",
    "this item has been sold",
    "this listing has been sold",
    "this item is sold",
    "listing is sold",
    "item is sold",
    "está vendido",
    "esta vendido",
    "artículo vendido",
    "articulo vendido",
    "publicación vendida",
    "publicacion vendida",
    "sold out",
    "out of stock",
    "agotado",
    "sin existencias",
    "no disponible",
    "not available for sale",
    "marcar como disponible",  # mark-as-available control label in body scrape
    "mark as available",
    "marcado como pendiente",
    "marked as pending",
    "en pendiente",
    "pending sale",
)

# Standalone status chips on detail / shelf cards (ES/EN).
_SOLD_STATUS_TOKEN = re.compile(
    r"(?:^|[\s|•·/\-–—\[\('\"])"
    r"(?:vendido|sold|pending|pendiente|agotado)"
    r"(?:$|[\s|•·/\-–—\]\),'\"])",
    re.I | re.M,
)

# Active inventory only — sold items often remain linked under “Vendidos”.
_ACTIVE_SELLING_SHELVES = (
    SELLING_URL,
    f"{SELLING_URL}?status=ACTIVE",
    f"{SELLING_URL}?state=LIVE",
    f"{SELLING_URL}/?status=ACTIVE",
    "https://www.facebook.com/marketplace/you/selling/?status=IN_STOCK",
    "https://www.facebook.com/marketplace/you/selling?status=IN_STOCK",
)

_MARK_AVAILABLE = re.compile(
    r"mark as available|marcar como disponible",
    re.I,
)
_MARK_SOLD = re.compile(
    r"mark as sold|marcar como vendido|marcar vendido|mark sold|"
    r"^\s*sold\s*$",
    re.I,
)
_MARK_PENDING = re.compile(
    r"mark as pending|marcar como pendiente|mark pending|"
    r"^\s*pending\s*$",
    re.I,
)
_EDIT_LISTING = re.compile(
    r"^\s*edit\s*$|^\s*editar\s*$|edit listing|editar publicación|"
    r"edit item|editar anuncio",
    re.I,
)
_MANAGE_LISTING = re.compile(
    r"manage listing|administrar publicación|administrar anuncio|"
    r"^\s*manage\s*$|^\s*administrar\s*$",
    re.I,
)
_MESSAGE_SELLER = re.compile(
    r"send message|enviar mensaje|message seller|enviar un mensaje|"
    r"message the seller|mensajear al vendedor",
    re.I,
)
_DELETE_PATTERNS = (
    re.compile(
        r"delete listing|eliminar publicación|delete this listing|"
        r"eliminar esta publicación|eliminar anuncio|delete item|"
        r"eliminar artículo|remove listing|delete forever|"
        r"eliminar para siempre|borrar publicación|borrar anuncio|"
        r"delete post|delete this post",
        re.I,
    ),
    re.compile(r"^\s*delete\s*$|^\s*eliminar\s*$|^\s*borrar\s*$", re.I),
)
_MORE_MENU = re.compile(
    r"more options|more|más opciones|opciones|manage|administrar|"
    r"see more|ver más|actions|acciones|listing options|"
    r"opciones de la publicación|opciones del anuncio",
    re.I,
)
# Direct aria-labels (EN + ES) for owner listing controls.
_OWNER_ACTION_ARIA = (
    '[aria-label="Mark as sold"]',
    '[aria-label="Mark as pending"]',
    '[aria-label="Delete listing"]',
    '[aria-label="Delete"]',
    '[aria-label="Edit"]',
    '[aria-label="Edit listing"]',
    '[aria-label="Marcar como vendido"]',
    '[aria-label="Marcar como pendiente"]',
    '[aria-label="Eliminar publicación"]',
    '[aria-label="Eliminar"]',
    '[aria-label="Editar"]',
    '[aria-label="Editar publicación"]',
    '[aria-label*="Mark as sold" i]',
    '[aria-label*="Mark as pending" i]',
    '[aria-label*="Delete listing" i]',
    '[aria-label*="Marcar como vendido" i]',
    '[aria-label*="Marcar como pendiente" i]',
    '[aria-label*="Eliminar publicación" i]',
)
_MORE_ARIA_SELECTORS = (
    '[aria-label="Más opciones"]',
    '[aria-label="More options"]',
    '[aria-label="Más"]',
    '[aria-label="More"]',
    '[aria-label*="Más opciones" i]',
    '[aria-label*="More options" i]',
    '[aria-label*="Opciones" i]',
    '[aria-label*="Options" i]',
    '[aria-label*="Administrar" i]',
    '[aria-label*="Manage" i]',
    '[aria-label*="acciones" i]',
    '[aria-label*="Actions" i]',
)

# Owner selling dashboards (some accounts only list on one of these).
_SELLING_SHELVES = (
    SELLING_URL,
    f"{SELLING_URL}?status=ACTIVE",
    f"{SELLING_URL}?state=LIVE",
    DASHBOARD_URL,
    "https://www.facebook.com/marketplace/you/selling/?status=IN_STOCK",
)


def extract_item_id(listing_url: str) -> str | None:
    """Pull Marketplace item id from a listing URL."""
    if not listing_url:
        return None
    match = re.search(r"/item/(\d+)", listing_url)
    if match:
        return match.group(1)
    path = urlparse(listing_url).path.rstrip("/")
    tail = path.split("/")[-1] if path else ""
    return tail if tail.isdigit() else None


def remove_vehicle_listing(
    page: Page,
    listing_url: str,
    *,
    autosell_id: str,
    removal_action: str,
    log_dir: Path,
    require_verified: bool = False,
    store: object | None = None,
    account_id: str | None = None,
) -> bool:
    """Remove a Marketplace listing (delete or mark sold).

    Returns True when the listing is confirmed gone/sold/unavailable.

    When ``require_verified`` is True (repost path), a verified removal is
    required before create. Soft failures (controls not found) return False
    with a WARNING instead of raising, so the caller can continue the queue.

    Optional ``store`` + ``account_id``: call ``mark_fb_listing_removed`` when
    treating visitor-chrome orphans (not on selling shelf) as already deleted.

    Strategies (in order):
    1. Open ``listing_url`` — treat 404 / unavailable as already removed
    2. Delete / mark-sold via listing detail menu (ES/EN + three-dot aria-labels)
    3. Fallback: find item on ``/marketplace/you/selling`` and delete from there
    4. Optional mark-sold if primary action was delete but only sold controls appear
    """
    action = (removal_action or "mark_sold").strip().lower()
    if action not in ("delete", "mark_sold"):
        action = "mark_sold"

    item_id = extract_item_id(listing_url)
    print(
        f"  {autosell_id}: remove start action={action} "
        f"item_id={item_id or '?'} url={listing_url}"
    )

    def _purge_orphan_mapping(reason: str) -> bool:
        print(
            f"WARNING: {autosell_id}: {reason} — treating as already "
            f"deleted/unavailable; purging sync.db mapping"
        )
        if store is not None and account_id:
            try:
                store.mark_fb_listing_removed(  # type: ignore[attr-defined]
                    autosell_id,
                    account_id,
                    clear_url=True,
                )
                print(
                    f"  {autosell_id}: sync.db cleared "
                    f"(status=removed, url=null) account={account_id}"
                )
            except Exception as exc:
                print(f"WARNING: {autosell_id}: sync.db purge failed: {exc}")
        return True

    # --- Primary: listing detail URL ---
    try:
        page.goto(listing_url, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(2_500)
    except Exception as exc:
        print(f"  {autosell_id}: listing URL navigation failed ({exc}) — checking if gone")

    if _is_content_unavailable(page):
        return _purge_orphan_mapping(
            "listing content isn't available (already deleted/unavailable)"
        )

    if _listing_already_gone(page, listing_url=listing_url, item_id=item_id):
        print(f"  {autosell_id}: listing already sold/unavailable — treating as removed")
        if require_verified and not _is_content_unavailable(page):
            if not _verify_listing_removed(page, listing_url):
                # Unavailable/sold banners can flap; prefer soft-success if banner seen.
                if _is_content_unavailable(page) or _is_sold_or_deactivated(page):
                    return True
                _assert_removed_or_raise(page, listing_url, autosell_id, log_dir)
        return True

    visitor_detail = _is_visitor_listing_view(page)
    owner_detail = _is_owner_listing_view(page)
    saw_visitor_only = bool(visitor_detail and not owner_detail)
    if saw_visitor_only:
        print(
            f"  {autosell_id}: detail looks like buyer/visitor view "
            f"(Enviar mensaje / Message seller present; no Edit / Mark as sold) — "
            f"skipping detail menus, going to selling shelf"
        )
        last_error = FacebookPostingError(
            "Listing detail is visitor chrome (not owner). "
            "Usually wrong Facebook account owns this listing, or posting Page ≠ session."
        )
    else:
        last_error = None
        try:
            _perform_removal_on_current_page(page, action=action)
            page.wait_for_timeout(2_000)
        except Exception as exc:
            last_error = exc
            print(f"  {autosell_id}: detail-page remove failed: {exc}")
            # Owner page often has direct Marcar como vendido but no Eliminar in menu
            if action == "delete":
                try:
                    print(f"  {autosell_id}: detail fallback mark_sold (owner chrome)")
                    _perform_removal_on_current_page(page, action="mark_sold")
                    page.wait_for_timeout(2_000)
                    last_error = None
                except Exception as alt_exc:
                    last_error = alt_exc

    if _is_content_unavailable(page) or _listing_already_gone(
        page, listing_url=listing_url, item_id=item_id
    ):
        print(f"  {autosell_id}: listing gone after detail remove")
        if require_verified and not _is_content_unavailable(page):
            _assert_removed_or_raise(page, listing_url, autosell_id, log_dir)
        return True

    # --- Fallback: selling shelf (scroll-loaded inventory) ---
    shelf_removed = False
    if item_id:
        try:
            print(f"  {autosell_id}: trying selling-shelf removal for item {item_id}")
            if _remove_from_selling_shelf(page, item_id, action=action):
                shelf_removed = True
                page.wait_for_timeout(2_000)
            elif action == "delete":
                print(f"  {autosell_id}: selling-shelf retry with mark_sold")
                if _remove_from_selling_shelf(page, item_id, action="mark_sold"):
                    shelf_removed = True
                    page.wait_for_timeout(2_000)
        except Exception as exc:
            last_error = exc
            print(f"  {autosell_id}: selling-shelf remove failed: {exc}")

    if _is_content_unavailable(page) or _listing_already_gone(
        page, listing_url=listing_url, item_id=item_id
    ):
        print(f"  {autosell_id}: listing gone after selling-shelf remove")
        if require_verified and not _is_content_unavailable(page):
            _assert_removed_or_raise(page, listing_url, autosell_id, log_dir)
        return True

    # Visitor chrome + not on selling shelf → orphan / already-deleted URL.
    if saw_visitor_only and item_id and not shelf_removed:
        if not _item_on_any_selling_shelf(page, item_id):
            return _purge_orphan_mapping(
                "visitor/buyer detail chrome and item not found on selling shelf"
            )

    # Detail again after opening from selling
    try:
        page.goto(listing_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2_000)
        if _is_content_unavailable(page):
            return _purge_orphan_mapping(
                "content isn't available after shelf attempt"
            )
        if _listing_already_gone(page, listing_url=listing_url, item_id=item_id):
            if require_verified and not _is_content_unavailable(page):
                _assert_removed_or_raise(page, listing_url, autosell_id, log_dir)
            return True
        alt = "mark_sold" if action == "delete" else "delete"
        try:
            print(f"  {autosell_id}: fallback action={alt}")
            _perform_removal_on_current_page(page, action=alt)
            page.wait_for_timeout(2_000)
        except Exception as exc:
            last_error = exc
    except Exception as exc:
        last_error = exc

    if saw_visitor_only and item_id and not _item_on_any_selling_shelf(page, item_id):
        return _purge_orphan_mapping(
            "visitor/buyer detail chrome and item not found on selling shelf"
        )

    if require_verified:
        if _verify_listing_removed(page, listing_url):
            print(f"  {autosell_id}: removal verified (listing gone/sold)")
            return True
        if _is_content_unavailable(page):
            return _purge_orphan_mapping(
                "content isn't available on final check"
            )
        _save_debug(page, log_dir, autosell_id, "remove_failed")
        ownership_hint = ""
        try:
            if _is_visitor_listing_view(page) and not _is_owner_listing_view(page):
                ownership_hint = (
                    " Detail page is still visitor/buyer chrome — this Facebook "
                    "session likely does not own the listing (wrong account or Page). "
                    "Refresh the correct seller profile or remap sync.db account."
                )
        except Exception:
            pass
        detail = f": {last_error}" if last_error else ""
        print(
            f"WARNING: {autosell_id}: Delete/mark-sold controls not found "
            f"(EN: 'Mark as sold' / 'Delete listing'; "
            f"ES: 'Marcar como vendido' / 'Eliminar publicación') "
            f"and listing still active{detail}.{ownership_hint} "
            f"— skipping this item; continuing queue"
        )
        return False

    if _listing_already_gone(page, listing_url=listing_url, item_id=item_id):
        return True
    if _is_content_unavailable(page):
        return _purge_orphan_mapping("content isn't available")
    if last_error is not None:
        _save_debug(page, log_dir, autosell_id, "remove_failed")
        print(
            f"WARNING: {autosell_id}: remove failed ({last_error}) "
            f"— skipping this item; continuing queue"
        )
        return False
    if _listing_already_gone(page, listing_url=listing_url, item_id=item_id):
        return True
    print(
        f"WARNING: {autosell_id}: could not confirm removal "
        f"— skipping this item; continuing queue"
    )
    return False



def _item_on_any_selling_shelf(page: Page, item_id: str) -> bool:
    """True if item appears on any owner selling/dashboard shelf (scrolled)."""
    for shelf in _SELLING_SHELVES:
        try:
            page.goto(shelf, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(2_000)
        except Exception:
            continue
        if _find_item_link_scrolled(page, item_id) is not None:
            return True
    return False


def _is_content_unavailable(page: Page) -> bool:
    """True when FB shows the deleted/unavailable content banner (EN/ES)."""
    phrases = (
        "this content isn't available right now",
        "this content isn't available",
        "this content is no longer available",
        "sorry, this content isn't available",
        "content isn't available",
        "este contenido no está disponible",
        "este contenido no esta disponible",
        "lo sentimos, este contenido no está disponible",
        "contenido no está disponible",
        "contenido no esta disponible",
        "page isn't available",
        "la página no está disponible",
    )
    try:
        for phrase in phrases:
            loc = page.get_by_text(re.compile(re.escape(phrase), re.I))
            try:
                count = loc.count()
            except Exception:
                count = 0
            if not isinstance(count, int) or count <= 0:
                continue
            try:
                if loc.first.is_visible():
                    return True
            except Exception:
                continue
    except Exception:
        pass
    try:
        raw = page.locator("body").inner_text(timeout=4_000)
    except Exception:
        raw = ""
    if not isinstance(raw, str):
        return False
    body = raw.lower()
    if body and any(p in body for p in phrases):
        # Avoid matching generic marketplace chrome; require "content/contenido" cue.
        if "content" in body or "contenido" in body or "página" in body or "page isn't" in body:
            return True
    return False


def _perform_removal_on_current_page(page: Page, *, action: str) -> None:
    if action == "delete":
        _delete_listing(page)
    else:
        _mark_sold(page)


def _is_owner_listing_view(page: Page) -> bool:
    """Seller chrome: Edit / Mark as sold / Mark as pending (EN + ES)."""
    for pattern in (_MARK_SOLD, _MARK_PENDING, _EDIT_LISTING, _MANAGE_LISTING):
        try:
            for role in ("button", "link", "menuitem"):
                loc = page.get_by_role(role, name=pattern)
                try:
                    count = loc.count()
                except Exception:
                    count = 0
                if not isinstance(count, int) or count <= 0:
                    continue
                try:
                    if loc.first.is_visible():
                        return True
                except Exception:
                    continue
        except Exception:
            continue
    for selector in _OWNER_ACTION_ARIA:
        try:
            loc = page.locator(selector)
            try:
                count = loc.count()
            except Exception:
                count = 0
            if not isinstance(count, int) or count <= 0:
                continue
            try:
                if loc.first.is_visible():
                    return True
            except Exception:
                continue
        except Exception:
            continue
    return False


def _is_visitor_listing_view(page: Page) -> bool:
    """Buyer chrome: Enviar mensaje / message-seller composer, no owner actions."""
    if _is_owner_listing_view(page):
        return False
    try:
        msg = page.get_by_role("button", name=_MESSAGE_SELLER)
        if msg.count() and msg.first.is_visible():
            return True
    except Exception:
        pass
    try:
        body = page.locator("body").inner_text(timeout=3_000).lower()
    except Exception:
        body = ""
    if "envía un mensaje al vendedor" in body or "send the seller a message" in body:
        return True
    return False


def _click_role_name(page: Page, pattern: re.Pattern[str], *, roles: tuple[str, ...] = ("button", "menuitem", "link")) -> bool:
    for role in roles:
        try:
            loc = page.get_by_role(role, name=pattern)
            for i in range(min(loc.count() or 0, 6)):
                item = loc.nth(i)
                try:
                    if not item.is_visible():
                        continue
                    item.scroll_into_view_if_needed(timeout=2_000)
                    item.click(timeout=8_000)
                    page.wait_for_timeout(1_500)
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    # Text-filtered buttons (roles sometimes missing on FB)
    try:
        alt = page.locator('[role="button"], button, [role="menuitem"]').filter(has_text=pattern)
        for i in range(min(alt.count() or 0, 6)):
            item = alt.nth(i)
            try:
                if item.is_visible():
                    item.click(timeout=8_000)
                    page.wait_for_timeout(1_500)
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _remove_from_selling_shelf(page: Page, item_id: str, *, action: str) -> bool:
    """Locate the item card on selling dashboard and act via its ⋮ menu.

    Delete path: card "…" → "Delete listing" / "Eliminar publicación".
    Already-sold cards (Mark as available) count as handled for mark_sold;
    for delete we still try ⋮ → Delete to purge, else accept sold as done.
    """
    for shelf in _SELLING_SHELVES:
        try:
            page.goto(shelf, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(3_000)
        except Exception:
            continue

        link = _find_item_link_scrolled(page, item_id)
        if link is None:
            continue

        try:
            link.scroll_into_view_if_needed(timeout=5_000)
        except Exception:
            pass
        page.wait_for_timeout(500)

        card = _shelf_card_root(link)
        already_sold = _shelf_card_is_sold(card)

        if already_sold and action == "mark_sold":
            print(
                f"  shelf item {item_id}: already sold "
                f"(Mark as available) — treating as removed"
            )
            return True

        # Required path: open this card's "…" then Delete / Mark as sold.
        if _click_more_on_shelf_card(page, card, link):
            if action == "delete":
                if _click_menu_action(page, action="delete"):
                    _confirm_if_needed(page, prefer_delete=True)
                    page.wait_for_timeout(1_500)
                    return True
                # Menu open but no delete — try mark_sold then accept sold state
                if _click_menu_action(page, action="mark_sold"):
                    return _finish_mark_sold_flow(page) or already_sold
                if already_sold:
                    print(
                        f"  shelf item {item_id}: sold (Mark as available) and "
                        f"no Delete in menu — treating as handled"
                    )
                    return True
            else:
                if _click_menu_action(page, action="mark_sold"):
                    return _finish_mark_sold_flow(page)
                if _click_menu_action(page, action="delete"):
                    _confirm_if_needed(page, prefer_delete=True)
                    return True
                if already_sold:
                    return True

        # Direct Mark as available on card = already sold (no need to open detail).
        if already_sold:
            print(
                f"  shelf item {item_id}: Mark as available on card — "
                f"treating as already sold/handled"
            )
            return True

        # Last resort: open listing detail from the card (owner chrome).
        try:
            href = None
            try:
                href = link.get_attribute("href")
            except Exception:
                href = None
            if href and href.startswith("/"):
                page.goto(
                    f"https://www.facebook.com{href}",
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
            else:
                link.click(timeout=8_000)
            page.wait_for_timeout(2_500)
            if _is_sold_or_deactivated(page) or _is_content_unavailable(page):
                return True
            try:
                _perform_removal_on_current_page(page, action=action)
                return True
            except Exception:
                alt = "mark_sold" if action == "delete" else "delete"
                try:
                    _perform_removal_on_current_page(page, action=alt)
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


def _shelf_card_root(anchor: Locator) -> Locator:
    """Best-effort card/row container around a marketplace item link."""
    for xpath in (
        "xpath=ancestor::div[@role='article'][1]",
        "xpath=ancestor::div[.//a[contains(@href,'/marketplace/item/')]]"
        "[.//*[@aria-label='More options' or @aria-label='Más opciones' or "
        "@aria-label='More' or @aria-label='Más' or "
        "contains(@aria-label,'Mark as available') or "
        "contains(@aria-label,'Marcar como disponible')]][1]",
        "xpath=ancestor::div[8]",
        "xpath=ancestor::div[6]",
        "xpath=ancestor::div[4]",
    ):
        try:
            card = anchor.locator(xpath)
            if card.count() > 0:
                return card.first
        except Exception:
            continue
    return anchor


def _shelf_card_is_sold(card: Locator) -> bool:
    """True when this shelf card shows sold / Mark as available (EN+ES)."""
    try:
        for role in ("button", "menuitem", "link"):
            loc = card.get_by_role(role, name=_MARK_AVAILABLE)
            count = loc.count() if hasattr(loc, "count") else 0
            if isinstance(count, int) and count > 0:
                try:
                    if loc.first.is_visible():
                        return True
                except Exception:
                    return True
    except Exception:
        pass
    try:
        text = (card.inner_text(timeout=2_000) or "").lower()
    except Exception:
        text = ""
    if not text:
        return False
    if "mark as available" in text or "marcar como disponible" in text:
        return True
    # Status badge "Vendido"/"Sold" without the active "Mark as sold" CTA.
    if "vendido" in text and "marcar como vendido" not in text:
        return True
    if re.search(r"\bsold\b", text) and "mark as sold" not in text:
        return True
    return False


def _click_more_on_shelf_card(page: Page, card: Locator, anchor: Locator) -> bool:
    """Click the card-local ⋮ / More options control (required before Delete)."""
    selectors = list(_MORE_ARIA_SELECTORS) + [
        '[aria-label*="More" i]',
        '[aria-label*="Más" i]',
        'div[aria-haspopup="menu"]',
        '[role="button"][aria-haspopup="menu"]',
    ]
    for selector in selectors:
        try:
            btns = card.locator(selector)
            count = btns.count()
            if not isinstance(count, int) or count <= 0:
                continue
            for i in range(min(count, 6)):
                btn = btns.nth(i)
                try:
                    if not btn.is_visible():
                        continue
                    label = (btn.get_attribute("aria-label") or "").lower()
                    # Skip promote / boost / share lookalikes when possible.
                    if any(x in label for x in ("boost", "promote", "share", "compartir")):
                        continue
                    btn.scroll_into_view_if_needed(timeout=2_000)
                    btn.click(timeout=5_000)
                    page.wait_for_timeout(1_200)
                    if _menu_open(page):
                        return True
                except Exception:
                    continue
        except Exception:
            continue
    # Role/name fallback inside card
    try:
        named = card.get_by_role("button", name=_MORE_MENU)
        for i in range(min(named.count() or 0, 4)):
            try:
                b = named.nth(i)
                if b.is_visible():
                    b.click(timeout=5_000)
                    page.wait_for_timeout(1_200)
                    if _menu_open(page):
                        return True
            except Exception:
                continue
    except Exception:
        pass
    return _click_more_near_locator(page, anchor)


def _find_item_link_scrolled(page: Page, item_id: str) -> Locator | None:
    """Find item link; scroll selling inventory if needed (lazy lists)."""
    for _ in range(12):
        link = _find_item_link(page, item_id)
        if link is not None:
            return link
        try:
            page.mouse.wheel(0, 2800)
            page.wait_for_timeout(900)
        except Exception:
            try:
                page.evaluate("window.scrollBy(0, 2800)")
                page.wait_for_timeout(900)
            except Exception:
                break
    return _find_item_link(page, item_id)


def _find_item_link(page: Page, item_id: str) -> Locator | None:
    try:
        links = page.locator(f'a[href*="/marketplace/item/{item_id}"]')
        count = links.count()
        if count == 0:
            # Some cards use commerce redirect URLs containing the id as query
            links = page.locator(f'a[href*="{item_id}"]')
            count = links.count()
            if count == 0:
                return None
        for i in range(min(count, 16)):
            loc = links.nth(i)
            try:
                if loc.is_visible():
                    return loc
            except Exception:
                continue
        return links.first
    except Exception:
        return None


def _click_more_near_locator(page: Page, anchor: Locator) -> bool:
    """Open a ⋮ / Más opciones control in the same card as ``anchor``."""
    # Climb to a reasonable card ancestor and look for more-buttons inside
    for xpath in (
        "xpath=ancestor::div[.//*[@aria-label='Más opciones' or "
        "@aria-label='More options' or @aria-label='Más' or "
        "@aria-label='More']][1]",
        "xpath=ancestor::div[4]",
        "xpath=ancestor::div[6]",
        "xpath=ancestor::div[8]",
    ):
        try:
            card = anchor.locator(xpath)
            if card.count() == 0:
                continue
            root = card.first
            for selector in _MORE_ARIA_SELECTORS:
                btn = root.locator(selector)
                if btn.count() == 0:
                    continue
                try:
                    target = btn.first
                    if target.is_visible():
                        target.click(timeout=5_000)
                        page.wait_for_timeout(1_200)
                        if _menu_open(page):
                            return True
                except Exception:
                    continue
            # Role/name based inside card
            named = root.get_by_role("button", name=_MORE_MENU)
            for i in range(min(named.count() or 0, 4)):
                try:
                    b = named.nth(i)
                    if b.is_visible():
                        b.click(timeout=5_000)
                        page.wait_for_timeout(1_200)
                        if _menu_open(page):
                            return True
                except Exception:
                    continue
        except Exception:
            continue
    return _open_listing_menu(page)


def _menu_open(page: Page) -> bool:
    try:
        if page.get_by_role("menuitem").count() > 0:
            return True
        if page.get_by_role("menu").count() > 0:
            return True
    except Exception:
        pass
    return False


def _assert_removed_or_raise(
    page: Page,
    listing_url: str,
    autosell_id: str,
    log_dir: Path,
) -> None:
    if _is_content_unavailable(page):
        print(
            f"WARNING: {autosell_id}: content isn't available — removal verified"
        )
        return
    # Settlement buffer: FB CDN/detail cache often lags behind mark_sold UI.
    print(f"  {autosell_id}: waiting for Marketplace cache to settle…")
    page.wait_for_timeout(4_000)
    if _verify_listing_removed(page, listing_url):
        print(f"  {autosell_id}: removal verified (listing gone/sold)")
        return
    if _is_content_unavailable(page):
        print(
            f"WARNING: {autosell_id}: content isn't available after settle — "
            f"removal verified"
        )
        return
    _save_debug(page, log_dir, autosell_id, "remove_unverified")
    raise FacebookPostingError(
        f"Remove not verified for {autosell_id}: listing still appears active "
        f"at {listing_url}. Skipping create to avoid Facebook duplicate warning."
    )


def _verify_listing_removed(page: Page, listing_url: str) -> bool:
    """Reload listing URL / active shelf; True when deleted, sold, or inactive.

    Marked-sold / Vendido / pending / out-of-stock count as removed for repost
    (Marketplace allows a fresh create after deactivation; no hard 404 required).
    Retries with a multi-second settlement buffer because FB can show a stale
    active detail page briefly after mark-sold.
    """
    item_id = extract_item_id(listing_url)

    for attempt in range(4):
        # attempt 0: 3s, then 4s / 5s / 5s — give CDN time between reloads
        settle_ms = 3_000 if attempt == 0 else (4_000 if attempt == 1 else 5_000)
        page.wait_for_timeout(settle_ms)
        try:
            page.goto(listing_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2_500)
        except Exception:
            return True

        if _is_content_unavailable(page):
            print(
                f"  verify attempt {attempt + 1}: content isn't available — ok"
            )
            return True

        if _dialog_open(page):
            _finish_mark_sold_flow(page)
            page.wait_for_timeout(2_000)
            try:
                page.goto(listing_url, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(2_000)
            except Exception:
                return True
            if _is_content_unavailable(page):
                return True

        if _listing_already_gone(page, listing_url=listing_url, item_id=item_id):
            return True
        if _is_sold_or_deactivated(page):
            print(f"  verify attempt {attempt + 1}: sold/deactivated UI — ok")
            return True
        if not _looks_like_live_listing(page) and not _still_has_mark_sold_control(page):
            return True

        # Off active selling shelf is enough even if public URL still caches.
        if item_id and not _item_visible_on_active_selling(page, item_id):
            print(
                f"  verify attempt {attempt + 1}: not on active selling shelf — "
                "treating as deactivated"
            )
            try:
                page.goto(listing_url, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(2_000)
            except Exception:
                return True
            if _is_content_unavailable(page):
                return True
            if _listing_already_gone(page, listing_url=listing_url, item_id=item_id):
                return True
            if _is_sold_or_deactivated(page):
                return True
            if not _looks_like_live_listing(page) and not _still_has_mark_sold_control(page):
                return True
            if not _has_live_controls(page) and not _still_has_mark_sold_control(page):
                return True
            # Shelf gone + owner no longer has Mark as sold → accept despite URL cache
            if not _still_has_mark_sold_control(page):
                return True

        print(
            f"  verify attempt {attempt + 1}/4: still looks active "
            f"(cache lag?) — retrying"
        )

    return False


def _looks_like_live_listing(page: Page) -> bool:
    """True if listing still looks actively for sale (not sold/pending/deleted)."""
    if _is_sold_or_deactivated(page):
        return False
    if _listing_already_gone(page):
        return False
    try:
        if page.get_by_role("button", name=_MARK_AVAILABLE).count():
            return False
        if page.get_by_role("menuitem", name=_MARK_AVAILABLE).count():
            return False
    except Exception:
        pass
    try:
        if page.get_by_role("button", name=_MARK_SOLD).count():
            return True
        if page.get_by_role("menuitem", name=_MARK_SOLD).count():
            return True
    except Exception:
        pass
    # Owner edit without mark-sold still implies live inventory.
    try:
        if page.get_by_role("button", name=_EDIT_LISTING).count():
            return True
    except Exception:
        pass
    return _has_live_controls(page)


def _item_visible_on_selling(page: Page, item_id: str) -> bool:
    """Back-compat: True if item appears under active selling shelves only."""
    return _item_visible_on_active_selling(page, item_id)


def _item_visible_on_active_selling(page: Page, item_id: str) -> bool:
    """True if item id still links from *active* selling inventory (not sold tab)."""
    for shelf_url in _ACTIVE_SELLING_SHELVES:
        try:
            page.goto(shelf_url, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(2_500)
        except Exception:
            continue
        # Light scroll — FB lazy-loads the shelf.
        for _ in range(4):
            if _find_item_link(page, item_id) is not None:
                # Card may still show with a Vendido badge under a mixed feed.
                if _item_card_looks_sold(page, item_id):
                    return False
                return True
            try:
                page.mouse.wheel(0, 1400)
                page.wait_for_timeout(800)
            except Exception:
                break
    return False


def _item_card_looks_sold(page: Page, item_id: str) -> bool:
    """True when the shelf card for item_id carries sold/pending UI."""
    try:
        link = page.locator(f'a[href*="/marketplace/item/{item_id}"]').first
        if not link.count():
            return False
        # Walk up a couple of ancestors for nearby status text.
        for depth in range(1, 5):
            try:
                node = link
                for _ in range(depth):
                    node = node.locator("xpath=..")
                text = (node.inner_text(timeout=1_500) or "").lower()
            except Exception:
                continue
            if any(p in text for p in _SOLD_OR_INACTIVE_PHRASES):
                return True
            if _SOLD_STATUS_TOKEN.search(text or ""):
                return True
    except Exception:
        pass
    return False


def _is_sold_or_deactivated(page: Page) -> bool:
    """Detail or post-action page shows Vendido / sold / pending / out of stock."""
    try:
        available = page.get_by_role("button", name=_MARK_AVAILABLE)
        if available.count() and available.first.is_visible():
            return True
        if page.get_by_role("menuitem", name=_MARK_AVAILABLE).count():
            return True
    except Exception:
        pass

    try:
        body = page.locator("body").inner_text(timeout=5_000) or ""
    except Exception:
        body = ""
    body_l = body.lower()
    if body_l and any(p in body_l for p in _SOLD_OR_INACTIVE_PHRASES):
        return True

    # Status chips only — strip action labels that contain "sold"/"vendido" as verbs.
    cleaned = re.sub(
        r"mark(?:ing)? as sold|marcar como vendido|marcar vendido|mark sold|"
        r"mark as pending|marcar como pendiente|mark pending",
        " ",
        body,
        flags=re.I,
    )
    if cleaned and _SOLD_STATUS_TOKEN.search(cleaned):
        try:
            still_can_sell = bool(page.get_by_role("button", name=_MARK_SOLD).count())
            still_can_sell = still_can_sell or bool(
                page.get_by_role("menuitem", name=_MARK_SOLD).count()
            )
        except Exception:
            still_can_sell = False
        if not still_can_sell:
            return True
    return False


def _listing_already_gone(
    page: Page,
    *,
    listing_url: str | None = None,
    item_id: str | None = None,
) -> bool:
    """True when the FB listing is already sold, deleted, or unavailable."""
    # Hard veto: open confirm dialog or still-visible Mark as sold → not removed.
    if _dialog_open(page):
        return False
    if _still_has_mark_sold_control(page):
        return False

    if _is_sold_or_deactivated(page):
        return True

    try:
        available = page.get_by_role("button", name=_MARK_AVAILABLE)
        if available.count() and available.first.is_visible():
            return True
    except Exception:
        pass

    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""

    # On selling/dashboard shelves: never treat inventory chrome alone as "item gone".
    # (Shelf pages often contain words like "Vendido" in filters/tabs.)
    inventory_ui = any(
        marker in url
        for marker in (
            "/marketplace/you/",
            "/you/selling",
            "/you/dashboard",
            "/marketplace/create",
        )
    )
    if inventory_ui:
        return False

    # Redirected off the item detail (deleted / 404 shell) without login wall.
    if url and "login" not in url and "checkpoint" not in url:
        on_item = bool(re.search(r"/marketplace/item/\d+", url))
        if listing_url and item_id and not on_item:
            if "marketplace" in url or "facebook.com" in url:
                if not _has_live_controls(page):
                    return True
        if "unavailable" in url or "error" in url:
            return True

    try:
        body = page.locator("body").inner_text(timeout=5_000).lower()
    except Exception:
        body = ""

    if body and any(phrase in body for phrase in _ALREADY_GONE_PHRASES):
        return True

    # Empty-ish error pages often have very little text + no listing actions
    if body and len(body) < 400 and not _has_live_controls(page):
        if any(token in body for token in ("not found", "no encontrada", "404", "unavailable")):
            return True

    return False


def _still_has_mark_sold_control(page: Page) -> bool:
    """True if the seller can still 'Mark as sold' (listing still active for sale)."""
    try:
        for role in ("button", "menuitem"):
            loc = page.get_by_role(role, name=_MARK_SOLD)
            for i in range(min(loc.count() or 0, 4)):
                try:
                    if loc.nth(i).is_visible():
                        return True
                except Exception:
                    continue
    except Exception:
        pass
    return False


def _still_has_mark_pending_control(page: Page) -> bool:
    try:
        for role in ("button", "menuitem"):
            loc = page.get_by_role(role, name=_MARK_PENDING)
            for i in range(min(loc.count() or 0, 4)):
                try:
                    if loc.nth(i).is_visible():
                        return True
                except Exception:
                    continue
    except Exception:
        pass
    return False


def _dialog_open(page: Page) -> bool:
    try:
        dialogs = page.locator('[role="dialog"], [aria-modal="true"]')
        for i in range(min(dialogs.count() or 0, 5)):
            try:
                if dialogs.nth(i).is_visible():
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _removal_postcondition_ok(page: Page) -> bool:
    """True only when listing is deactivated.

    Hiding Marcar como vendido behind an open confirm dialog does NOT count.
    """
    if _dialog_open(page):
        return False
    if _is_sold_or_deactivated(page):
        return True
    if _still_has_mark_sold_control(page):
        return False
    if _still_has_mark_pending_control(page):
        return False
    # No for-sale buttons and no dialog → accept as deactivated for repost.
    return True


def _has_live_controls(page: Page) -> bool:
    """For-sale owner actions only (not Mark as available after sold)."""
    try:
        if page.get_by_role("button", name=_MARK_AVAILABLE).count():
            return False
        if page.get_by_role("menuitem", name=_MARK_AVAILABLE).count():
            return False
    except Exception:
        pass
    if _still_has_mark_sold_control(page):
        return True
    try:
        if page.get_by_role("button", name=_EDIT_LISTING).count():
            # Edit alone is weak — only count with other for-sale cues or pending.
            if page.get_by_role("button", name=_MARK_PENDING).count():
                return True
        for pattern in _DELETE_PATTERNS:
            if page.get_by_role("menuitem", name=pattern).count():
                return True
            if page.get_by_role("button", name=pattern).count():
                return True
    except Exception:
        pass
    return False


def _click_aria_action(page: Page, *, action: str) -> bool:
    """Click EN/ES aria-labelled owner controls (Mark as sold / Delete / …)."""
    if action == "mark_sold":
        selectors = [
            s
            for s in _OWNER_ACTION_ARIA
            if "sold" in s.lower() or "vendido" in s.lower()
        ]
    elif action == "delete":
        selectors = [
            s
            for s in _OWNER_ACTION_ARIA
            if "delete" in s.lower() or "eliminar" in s.lower()
        ]
    else:
        selectors = list(_OWNER_ACTION_ARIA)
    for selector in selectors:
        try:
            loc = page.locator(selector)
            for i in range(min(loc.count() or 0, 4)):
                item = loc.nth(i)
                try:
                    if not item.is_visible():
                        continue
                    item.scroll_into_view_if_needed(timeout=2_000)
                    item.click(timeout=8_000)
                    page.wait_for_timeout(1_500)
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


def _mark_sold(page: Page) -> None:
    """Mark listing sold; require post-condition (sold UI / no Mark as sold button)."""
    # Owner detail: blue "Mark as sold" / "Marcar como vendido" (role or aria-label)
    clicked = _click_role_name(page, _MARK_SOLD) or _click_aria_action(
        page, action="mark_sold"
    )
    if not clicked:
        _open_listing_menu(page)
        clicked = _click_menu_action(page, action="mark_sold")
    if not clicked:
        if _is_sold_or_deactivated(page) or not _still_has_mark_sold_control(page):
            if _is_sold_or_deactivated(page) or _listing_still_inactive_hint(page):
                return
        raise FacebookPostingError(
            "Mark-as-sold control not found "
            "(expected 'Mark as sold' / 'Marcar como vendido' as button or menu item)"
        )

    if not _finish_mark_sold_flow(page):
        raise FacebookPostingError(
            "Mark as sold clicked but listing still shows for-sale control "
            "(dialog not confirmed or Facebook rejected the status change)"
        )


def _listing_still_inactive_hint(page: Page) -> bool:
    """Weak inactive signal when Mark as sold is already gone."""
    return _is_sold_or_deactivated(page) or not _still_has_mark_sold_control(page)


def _finish_mark_sold_flow(page: Page) -> bool:
    """Confirm multi-step mark-sold dialogs until sold or for-sale control disappears."""
    # FB often opens a layered dialog: Confirm → sometimes Next/Done / buyer picker.
    confirm_patterns = (
        re.compile(
            r"mark as sold|marcar como vendido|marcar vendido",
            re.I,
        ),
        re.compile(r"confirm|confirmar|sí, marcar|si, marcar|yes, mark", re.I),
        re.compile(
            r"next|siguiente|continue|continuar|done|listo|guardar|save|"
            r"publish|publicar|finish|finalizar|aplicar|apply",
            re.I,
        ),
        re.compile(r"^\s*yes\s*$|^\s*sí\s*$|^\s*si\s*$|^\s*ok\s*$|^\s*aceptar\s*$", re.I),
    )
    dialog_sel = (
        '[role="dialog"], [aria-modal="true"], div[data-pagelet*="Dialog"], '
        'div[aria-label*="vendido" i], div[aria-label*="sold" i]'
    )

    def _click_in_scope(scope, pattern: re.Pattern[str]) -> bool:
        try:
            # Prefer primary filled buttons inside dialogs first.
            primaries = scope.locator(
                '[aria-label*="vendido" i], [aria-label*="sold" i], '
                '[aria-label*="confirmar" i], [aria-label*="confirm" i], '
                '[aria-label*="siguiente" i], [aria-label*="next" i], '
                '[aria-label*="listo" i], [aria-label*="done" i]'
            )
            for i in range(min(primaries.count() or 0, 6)):
                item = primaries.nth(i)
                try:
                    if item.is_visible():
                        item.click(timeout=5_000)
                        page.wait_for_timeout(1_500)
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        try:
            btn = scope.get_by_role("button", name=pattern)
            for i in range(min(btn.count() or 0, 4)):
                item = btn.nth(i)
                try:
                    if not item.is_visible():
                        continue
                    item.click(timeout=5_000)
                    page.wait_for_timeout(1_500)
                    return True
                except Exception:
                    continue
        except Exception:
            pass
        try:
            alt = scope.locator(
                '[role="button"], [role="menuitem"], button'
            ).filter(has_text=pattern)
            for i in range(min(alt.count() or 0, 4)):
                item = alt.nth(i)
                try:
                    if not item.is_visible():
                        continue
                    item.click(timeout=5_000)
                    page.wait_for_timeout(1_500)
                    return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    for _round in range(8):
        page.wait_for_timeout(1_000)
        if _removal_postcondition_ok(page):
            return True

        progressed = False
        # Dialog-scoped clicks first (avoids re-hitting page-level Mark as sold).
        try:
            dialogs = page.locator(dialog_sel)
            dcount = dialogs.count() or 0
        except Exception:
            dcount = 0
        if dcount:
            scope = dialogs.last
            for pattern in confirm_patterns:
                if _click_in_scope(scope, pattern):
                    progressed = True
                    break
            # Radio / list options inside sold dialog (e.g. sold outside Marketplace)
            if not progressed:
                try:
                    opts = scope.locator(
                        '[role="radio"], [role="option"], label, '
                        '[role="listitem"], div[role="button"]'
                    )
                    for i in range(min(opts.count() or 0, 8)):
                        item = opts.nth(i)
                        try:
                            txt = (item.inner_text(timeout=600) or "").lower()
                            if not item.is_visible():
                                continue
                            if any(
                                t in txt
                                for t in (
                                    "fuera",
                                    "otro",
                                    "other",
                                    "marketplace",
                                    "facebook",
                                    "no se",
                                    "prefer not",
                                    "prefiero no",
                                )
                            ):
                                item.click(timeout=4_000)
                                page.wait_for_timeout(1_000)
                                progressed = True
                                break
                        except Exception:
                            continue
                except Exception:
                    pass
        else:
            for pattern in confirm_patterns:
                # Avoid the still-visible page-level Mark as sold (would loop).
                if pattern.search("mark as sold") or pattern.search("marcar como vendido"):
                    continue
                if _click_in_scope(page, pattern):
                    progressed = True
                    break

        if not progressed:
            _confirm_if_needed(page)
            page.wait_for_timeout(1_200)
            if _removal_postcondition_ok(page):
                return True
            # Force-reclick mark sold once if no dialog appeared (flaky first click).
            if _round == 2 and _still_has_mark_sold_control(page):
                _click_role_name(page, _MARK_SOLD)
            if _round >= 4:
                break

    return _removal_postcondition_ok(page)


def _delete_listing(page: Page) -> None:
    # Rare: delete visible without ⋮ (EN + ES role / aria)
    for pattern in _DELETE_PATTERNS:
        if _click_role_name(page, pattern):
            _confirm_if_needed(page, prefer_delete=True)
            if not _still_has_mark_sold_control(page) or _listing_already_gone(page):
                return
            return
    if _click_aria_action(page, action="delete"):
        _confirm_if_needed(page, prefer_delete=True)
        return

    _open_listing_menu(page)
    if _click_menu_action(page, action="delete"):
        _confirm_if_needed(page, prefer_delete=True)
        return

    # Manage / Edit → nested delete
    if _click_role_name(page, _MANAGE_LISTING) or _click_role_name(page, _EDIT_LISTING):
        page.wait_for_timeout(1_500)
        _open_listing_menu(page)
        if _click_menu_action(page, action="delete"):
            _confirm_if_needed(page, prefer_delete=True)
            return
        for pattern in _DELETE_PATTERNS:
            if _click_role_name(page, pattern):
                _confirm_if_needed(page, prefer_delete=True)
                return

    if _listing_already_gone(page) or _is_content_unavailable(page):
        return
    raise FacebookPostingError(
        "Delete control not found "
        "(expected 'Delete listing' / 'Eliminar publicación')"
    )


def _click_menu_action(page: Page, *, action: str) -> bool:
    """Click delete or mark-sold from an open menu or page-level control."""
    patterns = _DELETE_PATTERNS if action == "delete" else (_MARK_SOLD,)
    getters: list[Callable[[], Locator]] = []
    for pattern in patterns:
        getters.extend(
            [
                lambda p=pattern: page.get_by_role("menuitem", name=p),
                lambda p=pattern: page.get_by_role("button", name=p),
                lambda p=pattern: page.get_by_role("link", name=p),
                lambda p=pattern: page.locator('[role="menuitem"]').filter(has_text=p),
                lambda p=pattern: page.locator('[role="button"]').filter(has_text=p),
                lambda p=pattern: page.get_by_text(p),
            ]
        )

    for getter in getters:
        try:
            target = getter()
            count = target.count()
            if count == 0:
                continue
            for i in range(min(count, 6)):
                item = target.nth(i)
                try:
                    if not item.is_visible():
                        continue
                    item.scroll_into_view_if_needed(timeout=2_000)
                    item.click(timeout=8_000)
                    page.wait_for_timeout(1_500)
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


def _open_listing_menu(page: Page) -> bool:
    """Open listing ⋮ / Más opciones menu. Returns True if a menu appears open."""
    # Exact aria-labels first (common on es-MX Marketplace)
    for selector in _MORE_ARIA_SELECTORS:
        try:
            buttons = page.locator(selector)
            for i in range(min(buttons.count() or 0, 8)):
                btn = buttons.nth(i)
                try:
                    if not btn.is_visible():
                        continue
                    btn.click(timeout=5_000)
                    page.wait_for_timeout(1_200)
                    if (
                        page.get_by_role("menuitem").count()
                        or page.locator('[role="menuitem"]').count()
                    ):
                        return True
                except Exception:
                    continue
        except Exception:
            continue

    candidates = [
        lambda: page.get_by_role("button", name=_MORE_MENU),
        lambda: page.get_by_label(_MORE_MENU),
        lambda: page.locator(
            '[aria-label*="More" i], [aria-label*="Más" i], '
            '[aria-label*="Options" i], [aria-label*="Opciones" i], '
            '[aria-label*="actions" i], [aria-label*="Acciones" i]'
        ),
    ]
    for getter in candidates:
        try:
            button = getter()
            if button.count() == 0:
                continue
            for i in range(min(button.count(), 10)):
                btn = button.nth(i)
                try:
                    if not btn.is_visible():
                        continue
                    btn.click(timeout=5_000)
                    page.wait_for_timeout(1_200)
                    if page.get_by_role("menuitem").count() or page.locator(
                        '[role="menuitem"]'
                    ).count():
                        return True
                except Exception:
                    continue
        except Exception:
            continue
    return _menu_open(page)


def _confirm_if_needed(page: Page, *, prefer_delete: bool = False) -> None:
    patterns = (
        [
            re.compile(
                r"delete listing|eliminar publicación|delete|eliminar|remove",
                re.I,
            ),
            re.compile(r"confirm|confirmar", re.I),
            re.compile(r"yes|sí|ok|aceptar|continuar", re.I),
        ]
        if prefer_delete
        else [
            re.compile(r"confirm|confirmar", re.I),
            re.compile(r"mark as sold|marcar como vendido", re.I),
            re.compile(r"delete listing|eliminar publicación|delete|eliminar", re.I),
            re.compile(r"yes|sí|ok|aceptar", re.I),
        ]
    )
    for pattern in patterns:
        try:
            button = page.get_by_role("button", name=pattern)
            if button.count() and button.first.is_visible():
                button.first.click(timeout=5_000)
                page.wait_for_timeout(2_000)
                return
        except Exception:
            continue
        try:
            alt = page.locator('[role="button"]').filter(has_text=pattern)
            if alt.count() and alt.first.is_visible():
                alt.first.click(timeout=5_000)
                page.wait_for_timeout(2_000)
                return
        except Exception:
            continue
