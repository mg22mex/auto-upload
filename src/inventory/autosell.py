from __future__ import annotations

import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from src.models import Vehicle

SLUG_PATTERN = re.compile(r"^/catalogo/([a-z0-9][a-z0-9-]*)$", re.I)
OBJ_ID_PATTERN = re.compile(r"/public/uploads/catalogo/(obj\d+)/", re.I)
IMAGE_PATTERN = re.compile(
    r"https?://[^\"'\s]+/public/uploads/catalogo/[^\"'\s]+\.(?:jpe?g|png|webp)",
    re.I,
)


class AutosellCatalogError(Exception):
    pass


class AutosellScraper:
    def __init__(
        self,
        base_url: str = "https://www.autosell.mx",
        catalog_path: str = "/catalogo",
        timeout_sec: int = 30,
        delay_between_pages_sec: float = 0.5,
        delay_between_details_sec: float = 0.3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.catalog_path = catalog_path
        self.timeout_sec = timeout_sec
        self.delay_between_pages_sec = delay_between_pages_sec
        self.delay_between_details_sec = delay_between_details_sec
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (compatible; AutosellSync/1.0; +https://www.autosell.mx)"
                ),
                "Accept-Language": "es-MX,es;q=0.9",
            }
        )

    def fetch_all_public_vehicles(self) -> list[Vehicle]:
        slugs = self._fetch_all_slugs()
        vehicles: list[Vehicle] = []
        for index, slug in enumerate(sorted(slugs), start=1):
            vehicle = self._fetch_vehicle_detail(slug)
            if vehicle is None:
                continue
            vehicles.append(vehicle)
            if index < len(slugs):
                time.sleep(self.delay_between_details_sec)
        return vehicles

    def _fetch_all_slugs(self) -> set[str]:
        slugs: set[str] = set()
        page = 1
        max_page = 1

        while page <= max_page:
            if page == 1:
                url = f"{self.base_url}{self.catalog_path}"
            else:
                url = f"{self.base_url}{self.catalog_path}/?action=list&p={page}"

            html = self._get(url)
            soup = BeautifulSoup(html, "html.parser")
            slugs.update(self._extract_slugs(soup))
            max_page = max(max_page, self._extract_max_page(soup))
            page += 1
            if page <= max_page:
                time.sleep(self.delay_between_pages_sec)

        if not slugs:
            raise AutosellCatalogError("No vehicle slugs found on public catalog.")
        return slugs

    def _extract_slugs(self, soup: BeautifulSoup) -> set[str]:
        slugs: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if href.startswith("http"):
                path = href.replace(self.base_url, "")
            else:
                path = href
            match = SLUG_PATTERN.match(path.split("?", 1)[0])
            if match:
                slugs.add(match.group(1).lower())
        return slugs

    def _extract_max_page(self, soup: BeautifulSoup) -> int:
        pages = []
        for anchor in soup.find_all("a", href=True):
            match = re.search(r"action=list&p=(\d+)", anchor["href"])
            if match:
                pages.append(int(match.group(1)))
        for node in soup.select(".btn-page"):
            text = node.get_text(strip=True)
            if text.isdigit():
                pages.append(int(text))
        return max(pages) if pages else 1

    def _fetch_vehicle_detail(self, slug: str) -> Vehicle | None:
        url = f"{self.base_url}/catalogo/{slug}"
        html = self._get(url)
        soup = BeautifulSoup(html, "html.parser")

        autosell_id = self._extract_autosell_id(html)
        if not autosell_id:
            return None

        title = self._text(soup.select_one(".vehiculo-titulo")) or slug.replace("-", " ")
        brand_raw = self._text(soup.select_one(".vehiculo-marca")) or ""
        brand = re.sub(r"^marca:\s*", "", brand_raw, flags=re.I).strip()

        specs = self._extract_specs(soup)
        image_urls = self._extract_images(html, autosell_id)

        return Vehicle(
            autosell_id=autosell_id,
            slug=slug,
            title=title,
            brand=brand,
            year=specs.get("Año", ""),
            price=specs.get("Precio", ""),
            mileage=specs.get("Kilometraje", ""),
            version=specs.get("Versión", ""),
            url=url,
            image_urls=image_urls,
            specs=specs,
        )

    def _extract_autosell_id(self, html: str) -> str | None:
        match = OBJ_ID_PATTERN.search(html)
        return match.group(1) if match else None

    def _extract_specs(self, soup: BeautifulSoup) -> dict[str, str]:
        specs: dict[str, str] = {}
        for label_node in soup.select(".label"):
            label = label_node.get_text(strip=True)
            value_node = label_node.find_next_sibling(class_="value")
            if not label or value_node is None:
                continue
            value = value_node.get_text(strip=True)
            if value:
                specs[label] = value
        return specs

    def _extract_images(self, html: str, autosell_id: str) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        needle = f"/{autosell_id}/"
        for url in IMAGE_PATTERN.findall(html):
            if needle not in url:
                continue
            normalized = url.split("?", 1)[0]
            if normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
        return ordered

    def _text(self, node) -> str:
        if node is None:
            return ""
        return node.get_text(strip=True)

    def _get(self, url: str) -> str:
        response = self.session.get(url, timeout=self.timeout_sec)
        response.raise_for_status()
        return response.text


def fetch_catalog(config: dict) -> list[Vehicle]:
    autosell_cfg = config.get("autosell", {})
    scraper = AutosellScraper(
        base_url=autosell_cfg.get("base_url", "https://www.autosell.mx"),
        catalog_path=autosell_cfg.get("catalog_path", "/catalogo"),
        timeout_sec=int(autosell_cfg.get("request_timeout_sec", 30)),
        delay_between_pages_sec=float(autosell_cfg.get("delay_between_pages_sec", 0.5)),
        delay_between_details_sec=float(autosell_cfg.get("delay_between_details_sec", 0.3)),
    )
    return scraper.fetch_all_public_vehicles()
