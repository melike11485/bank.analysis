from pathlib import Path
from unittest import TestCase

from src.tbb_dashboard.download import DEFAULT_START, parse_period, quarter_range
from src.tbb_dashboard.ingest import canonical_text, classify_entity, period_from_folder


class CoreTests(TestCase):
    def test_turkish_names_have_stable_keys(self) -> None:
        self.assertEqual(canonical_text("Mali Bünye"), "mali_bunye")
        self.assertEqual(canonical_text("İstikrarlı Fonlama Oranı"), "istikrarli_fonlama_orani")

    def test_period_folder_maps_to_quarter_end(self) -> None:
        self.assertEqual(
            period_from_folder(Path("2025-06")),
            ("2025-06-30", "Haziran 2025"),
        )

    def test_entity_classification_uses_names_not_indentation(self) -> None:
        self.assertEqual(classify_entity("  Mevduat Bankaları"), "group")
        self.assertEqual(classify_entity("  Türkiye Vakıflar Bankası T.A.O."), "bank")
        self.assertEqual(classify_entity("Sektör Toplamı"), "sector")

    def test_quarter_range_is_inclusive(self) -> None:
        self.assertEqual(
            quarter_range(parse_period("2025-09"), parse_period("2026-03")),
            [(2025, 9), (2025, 12), (2026, 3)],
        )

    def test_default_download_starts_in_march_2020(self) -> None:
        self.assertEqual(DEFAULT_START, (2020, 3))
