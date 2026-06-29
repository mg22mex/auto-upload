from __future__ import annotations

import re
from dataclasses import dataclass

from src.models import Vehicle

# Exact values from FB Marketplace vehicle composer (English UI)
FB_BODY_STYLES = (
    "Coupe", "Truck", "Sedan", "Hatchback", "SUV", "Convertible",
    "Wagon", "Minivan", "Small Car", "Other",
)
FB_EXTERIOR_COLORS = (
    "Black", "Blue", "Brown", "Gold", "Green", "Gray", "Pink", "Purple", "Red",
    "Silver", "Orange", "White", "Yellow", "Charcoal", "Off white", "Tan",
    "Beige", "Burgundy", "Turquoise", "Other",
)
FB_INTERIOR_COLORS = ("Black", "Blue", "Brown", "Gray", "Green", "Red", "White", "Beige", "Other")
FB_FUEL_TYPES = ("Gasoline", "Diesel", "Electric", "Flex", "Hybrid", "Plug-in hybrid", "Other")
FB_CONDITIONS = ("Excellent", "Good", "Fair", "Poor")
FB_TRANSMISSIONS = ("Automatic transmission", "Manual transmission")

# Autosell marca -> Facebook Make dropdown label
MAKE_ALIASES: dict[str, str] = {
    "mercedes benz": "Mercedes-Benz",
    "mercedes-benz": "Mercedes-Benz",
    "can-am": "Can-Am",
    "can am": "Can-Am",
    "land rover": "Land Rover",
    "mini": "MINI",
    "kia": "Kia",
    "infiniti": "INFINITI",
    "bmw": "BMW",
    "gmc": "GMC",
    "ram": "Ram",
    "volkswagen": "Volkswagen",
}

DEFAULT_EXTERIOR = "Silver"
DEFAULT_INTERIOR = "Black"
DEFAULT_FUEL = "Gasoline"
DEFAULT_TRANSMISSION = "Automatic transmission"
DEFAULT_CONDITION = "Excellent"
DEFAULT_VEHICLE_TYPE = "Car/Truck"
DEFAULT_BODY_STYLE = "Sedan"

DIESEL_MARKERS = (
    "diesel", "tdi", "duramax", "powerstroke", "cummins", "ecoblue", "bluetec",
)
ELECTRIC_MARKERS = ("electric", "eléctrico", "electrico", " ev ", "bolt", "leaf", " id.4")
HYBRID_MARKERS = ("hybrid", "híbrido", "hibrido", "prius")
MANUAL_MARKERS = ("manual", "estándar", "estandar", "stick", " mt ")

TRUCK_BODY_MARKERS = (
    "ram", "f-150", "f150", "silverado", "tundra", "tacoma", "ranger", "frontier",
    "np300", "titan", "1500", "2500", "3500", "cheyenne", "l200", "amarok", "pickup",
)
SUV_BODY_MARKERS = (
    "suv", "cx-", "cx50", "cx-5", "cx-9", "x-trail", "xtrail", "cr-v", "rav4",
    "tucson", "explorer", "expedition", "equinox", "traverse", "santa fe", "tahoe",
    "suburban", "q3", "q5", "q7", "q8", "x3", "x5", "glc", "gle", "macan", "cayenne",
    "escape", "edge", "bronco", "pathfinder", "murano", "rogue", "kicks", "seltos",
    "sportage", "outlander", "pilot", "highlander", "4runner",
)
VAN_BODY_MARKERS = (
    "sedona", "caravan", "pacifica", "odyssey", "sienna", "transit", "sprinter",
    "promaster", "nv200", "nv350", "urvan", "minivan",
)
COUPE_BODY_MARKERS = ("coupe", "coupé", "mustang", "camaro", "corvette")
HATCH_BODY_MARKERS = ("hatch", "golf", "fit", "march", "spark", "i10", "i20", "versa")
WAGON_BODY_MARKERS = ("wagon", "station wagon", "baúl", "baul")
CONVERTIBLE_BODY_MARKERS = ("convertible", "convertible", "cabrio")


@dataclass(frozen=True)
class ListingAttributes:
    make: str
    model: str
    mileage_km: str
    body_style: str
    exterior_color: str
    interior_color: str
    fuel_type: str
    transmission: str
    condition: str
    vehicle_type: str

    def summary(self) -> str:
        return (
            f"make={self.make}, model={self.model}, km={self.mileage_km}, "
            f"body={self.body_style}, ext={self.exterior_color}, "
            f"fuel={self.fuel_type}, trans={self.transmission}, cond={self.condition}"
        )


def categorize_vehicle(vehicle: Vehicle) -> ListingAttributes:
    haystack = _vehicle_haystack(vehicle)
    specs = {k.lower(): v for k, v in vehicle.specs.items()}

    return ListingAttributes(
        make=fb_make_name(vehicle.brand),
        model=fb_model_name(vehicle.title),
        mileage_km=parse_mileage_km(vehicle.mileage),
        body_style=_infer_body_style(vehicle, haystack, specs),
        exterior_color=_infer_exterior_color(specs),
        interior_color=_infer_interior_color(specs),
        fuel_type=_infer_fuel_type(haystack, specs),
        transmission=_infer_transmission(haystack, specs),
        condition=_infer_condition(specs),
        vehicle_type=_infer_vehicle_type(haystack),
    )


def parse_mileage_km(mileage: str) -> str:
    match = re.search(r"[\d,]+", mileage or "")
    if not match:
        return "12345"
    digits = match.group(0).replace(",", "")
    return digits or "12345"


def fb_make_name(brand: str) -> str:
    cleaned = (brand or "").strip()
    if not cleaned:
        return cleaned
    alias = MAKE_ALIASES.get(cleaned.lower())
    return alias or cleaned


def fb_model_name(title: str) -> str:
    """Normalize autosell model titles for FB (e.g. 'A 3' -> 'A3')."""
    text = (title or "").strip()
    if not text:
        return text
    compact = re.sub(r"(?<=[A-Za-z])\s+(?=[A-Za-z0-9])", "", text)
    return re.sub(r"\s{2,}", " ", compact).strip()


def _vehicle_haystack(vehicle: Vehicle) -> str:
    return " ".join(
        part
        for part in (
            vehicle.brand,
            vehicle.title,
            vehicle.version,
            vehicle.slug,
            " ".join(vehicle.specs.values()),
        )
        if part
    ).lower()


def _infer_body_style(vehicle: Vehicle, haystack: str, specs: dict[str, str]) -> str:
    for key in ("body style", "estilo", "carrocería", "carroceria", "tipo"):
        value = specs.get(key)
        if value:
            return _normalize_body_style(value)
    if any(marker in haystack for marker in TRUCK_BODY_MARKERS):
        return "Truck"
    if any(marker in haystack for marker in VAN_BODY_MARKERS):
        return "Minivan"
    if any(marker in haystack for marker in SUV_BODY_MARKERS):
        return "SUV"
    if any(marker in haystack for marker in CONVERTIBLE_BODY_MARKERS):
        return "Convertible"
    if any(marker in haystack for marker in COUPE_BODY_MARKERS):
        return "Coupe"
    if any(marker in haystack for marker in WAGON_BODY_MARKERS):
        return "Wagon"
    if any(marker in haystack for marker in HATCH_BODY_MARKERS):
        return "Hatchback"
    return DEFAULT_BODY_STYLE


def _normalize_body_style(value: str) -> str:
    mapping = {
        "sedan": "Sedan", "sedán": "Sedan", "saloon": "Sedan",
        "suv": "SUV", "crossover": "SUV",
        "truck": "Truck", "pickup": "Truck", "camioneta": "Truck",
        "hatchback": "Hatchback", "compact": "Small Car", "small car": "Small Car",
        "minivan": "Minivan", "van": "Minivan",
        "coupe": "Coupe", "coupé": "Coupe",
        "wagon": "Wagon", "convertible": "Convertible",
    }
    cleaned = value.strip()
    normalized = mapping.get(cleaned.lower(), cleaned)
    if normalized in FB_BODY_STYLES:
        return normalized
    return DEFAULT_BODY_STYLE


def _infer_exterior_color(specs: dict[str, str]) -> str:
    for key in ("exterior color", "color exterior", "color", "exterior"):
        value = specs.get(key)
        if value:
            return _normalize_exterior_color(value)
    return DEFAULT_EXTERIOR


def _infer_interior_color(specs: dict[str, str]) -> str:
    for key in ("interior color", "color interior", "interior"):
        value = specs.get(key)
        if value:
            return _normalize_interior_color(value)
    return DEFAULT_INTERIOR


def _normalize_exterior_color(value: str) -> str:
    mapping = {
        "gris": "Gray", "grey": "Gray", "gray": "Gray",
        "plata": "Silver", "silver": "Silver",
        "negro": "Black", "black": "Black",
        "blanco": "White", "white": "White",
        "rojo": "Red", "red": "Red",
        "azul": "Blue", "blue": "Blue",
        "verde": "Green", "green": "Green",
        "dorado": "Gold", "gold": "Gold",
        "cafe": "Brown", "brown": "Brown", "café": "Brown",
        "naranja": "Orange", "orange": "Orange",
        "amarillo": "Yellow", "yellow": "Yellow",
        "beige": "Beige", "carbon": "Charcoal", "charcoal": "Charcoal",
    }
    normalized = mapping.get(value.strip().lower(), value.strip())
    if normalized in FB_EXTERIOR_COLORS:
        return normalized
    return DEFAULT_EXTERIOR


def _normalize_interior_color(value: str) -> str:
    mapping = {
        "negro": "Black", "black": "Black",
        "gris": "Gray", "grey": "Gray", "gray": "Gray",
        "beige": "Beige", "blanco": "White", "white": "White",
    }
    normalized = mapping.get(value.strip().lower(), value.strip())
    if normalized in FB_INTERIOR_COLORS:
        return normalized
    return DEFAULT_INTERIOR


def _infer_fuel_type(haystack: str, specs: dict[str, str]) -> str:
    for key in ("fuel", "combustible", "fuel type", "tipo de combustible"):
        value = specs.get(key)
        if value:
            return _normalize_fuel_type(value)
    if any(marker in haystack for marker in ELECTRIC_MARKERS):
        return "Electric"
    if any(marker in haystack for marker in HYBRID_MARKERS):
        return "Hybrid"
    if any(marker in haystack for marker in DIESEL_MARKERS):
        return "Diesel"
    return DEFAULT_FUEL


def _normalize_fuel_type(value: str) -> str:
    mapping = {
        "gasolina": "Gasoline", "gasoline": "Gasoline", "petrol": "Gasoline",
        "diesel": "Diesel", "diésel": "Diesel",
        "electric": "Electric", "eléctrico": "Electric", "electrico": "Electric",
        "hybrid": "Hybrid", "híbrido": "Hybrid", "hibrido": "Hybrid",
        "flex": "Flex",
    }
    normalized = mapping.get(value.strip().lower(), value.strip())
    if normalized in FB_FUEL_TYPES:
        return normalized
    return DEFAULT_FUEL


def _infer_transmission(haystack: str, specs: dict[str, str]) -> str:
    for key in ("transmission", "transmisión", "transmision"):
        value = specs.get(key)
        if value:
            return _normalize_transmission(value)
    if any(marker in haystack for marker in MANUAL_MARKERS):
        return "Manual transmission"
    return DEFAULT_TRANSMISSION


def _normalize_transmission(value: str) -> str:
    lowered = value.strip().lower()
    if "manual" in lowered or "estándar" in lowered or "estandar" in lowered:
        return "Manual transmission"
    return DEFAULT_TRANSMISSION


def _infer_condition(specs: dict[str, str]) -> str:
    for key in ("condition", "condición", "condicion", "estado"):
        value = specs.get(key)
        if value:
            normalized = value.strip().title()
            if normalized in FB_CONDITIONS:
                return normalized
    return DEFAULT_CONDITION


def _infer_vehicle_type(haystack: str) -> str:
    if any(marker in haystack for marker in ("can-am", "can am", "atv", "cuatrimoto")):
        return "Powersport"
    return DEFAULT_VEHICLE_TYPE
