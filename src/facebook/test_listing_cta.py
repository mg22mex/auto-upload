"""Unit tests — Marketplace WhatsApp CTA links in listing descriptions."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlparse, parse_qs

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.facebook.listing_cta import (
    DEFAULT_WA_PERIFERICO,
    DEFAULT_WA_SAN_FELIPE,
    build_whatsapp_link,
    infer_vehicle_branch,
    whatsapp_cta_for_vehicle,
)
from src.facebook.util import vehicle_description
from src.models import Vehicle


def _sample_vehicle(**overrides) -> Vehicle:
    base = dict(
        autosell_id="obj9999",
        slug="mazda-cx5-2020",
        title="CX-5",
        brand="Mazda",
        year="2020",
        price="$300,000",
        mileage="45,000 kms",
        version="Grand Touring",
        url="https://www.autosell.mx/catalogo/mazda-cx5-2020",
        image_urls=[],
        specs={"Año": "2020", "Precio": "$300,000", "Kilometraje": "45,000 kms"},
    )
    base.update(overrides)
    return Vehicle(**base)


class TestWhatsAppLinkBuilder(unittest.TestCase):
    def test_periferico_link_encoding(self):
        link = build_whatsapp_link(
            phone=DEFAULT_WA_PERIFERICO,
            year="2020",
            make="Mazda",
            model="CX-5",
        )
        parsed = urlparse(link)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "wa.me")
        self.assertEqual(parsed.path, f"/{DEFAULT_WA_PERIFERICO}")
        text = parse_qs(parsed.query)["text"][0]
        self.assertEqual(
            unquote(text),
            "Hola, me interesa información sobre el 2020 Mazda CX-5",
        )

    def test_san_felipe_phone(self):
        link = build_whatsapp_link(
            phone=DEFAULT_WA_SAN_FELIPE,
            year="2021",
            make="Toyota",
            model="Hilux",
        )
        self.assertIn(f"wa.me/{DEFAULT_WA_SAN_FELIPE}", link)
        text = parse_qs(urlparse(link).query)["text"][0]
        self.assertIn("2021 Toyota Hilux", unquote(text))


class TestBranchInference(unittest.TestCase):
    def test_defaults_to_periferico(self):
        vehicle = _sample_vehicle()
        self.assertEqual(infer_vehicle_branch(vehicle), "periferico")

    def test_sucursal_san_felipe(self):
        vehicle = _sample_vehicle(
            specs={
                "Año": "2020",
                "Precio": "$300,000",
                "Sucursal": "San Felipe",
            }
        )
        self.assertEqual(infer_vehicle_branch(vehicle), "san_felipe")

    def test_ubicacion_periferico(self):
        vehicle = _sample_vehicle(
            specs={
                "Ubicación": "Periférico Sur",
            }
        )
        self.assertEqual(infer_vehicle_branch(vehicle), "periferico")


class TestVehicleDescriptionCTA(unittest.TestCase):
    def test_description_includes_periferico_cta(self):
        vehicle = _sample_vehicle()
        text = vehicle_description(vehicle)
        self.assertIn("Más información: https://www.autosell.mx/catalogo/mazda-cx5-2020", text)
        self.assertIn("📲 **¡Contáctanos por WhatsApp!**", text)
        self.assertIn("asesor de Periférico", text)
        self.assertIn(f"wa.me/{DEFAULT_WA_PERIFERICO}", text)
        self.assertIn("2020%20Mazda%20CX-5", text)

    def test_description_san_felipe_branch(self):
        vehicle = _sample_vehicle(
            specs={
                "Año": "2021",
                "Precio": "$500,000",
                "Sucursal": "San Felipe",
            },
            title="Hilux",
            brand="Toyota",
            year="2021",
            slug="toyota-hilux-2021",
        )
        text = vehicle_description(vehicle)
        self.assertIn("asesor de San Felipe", text)
        self.assertIn(f"wa.me/{DEFAULT_WA_SAN_FELIPE}", text)
        self.assertIn("2021%20Toyota%20Hilux", text)

    def test_explicit_branch_override(self):
        vehicle = _sample_vehicle()
        text = vehicle_description(vehicle, branch="san_felipe")
        self.assertIn(f"wa.me/{DEFAULT_WA_SAN_FELIPE}", text)
        self.assertIn("asesor de San Felipe", text)

    def test_env_phone_override(self):
        vehicle = _sample_vehicle()
        with patch.dict(
            "os.environ",
            {"MARKETPLACE_WA_PERIFERICO": "5215551234567"},
            clear=False,
        ):
            text = vehicle_description(vehicle)
        self.assertIn("wa.me/5215551234567", text)

    def test_whatsapp_cta_block_format(self):
        block = whatsapp_cta_for_vehicle(_sample_vehicle())
        self.assertTrue(block.startswith("\n\n📲"))
        self.assertIn("Haz clic en el enlace", block)


if __name__ == "__main__":
    unittest.main()
