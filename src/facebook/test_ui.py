from __future__ import annotations

import unittest

from src.facebook.poster import PHOTO_ADD_RE
from src.facebook.ui import DRAFT_SAVED_RE


class TestPhotoAddLabels(unittest.TestCase):
    def test_agregar_fotos(self):
        self.assertTrue(PHOTO_ADD_RE.search("Agregar fotos"))
        self.assertTrue(PHOTO_ADD_RE.search("Add Photos"))
        self.assertTrue(PHOTO_ADD_RE.search("Añadir fotos"))


class TestDraftSaved(unittest.TestCase):
    def test_es_en(self):
        self.assertTrue(DRAFT_SAVED_RE.search("Borrador guardado"))
        self.assertTrue(DRAFT_SAVED_RE.search("Draft saved"))
        self.assertFalse(DRAFT_SAVED_RE.search("Publicar"))


if __name__ == "__main__":
    unittest.main()
