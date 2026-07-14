from pathlib import Path
from unittest import TestCase

from src.tbb_dashboard.download import parse_period, quarter_range
from src.tbb_dashboard.ingest import canonical_text, period_from_folder


class CoreTests(TestCase):
    def test_turkish_names_have_stable_keys(self) -> None:
        self.assertEqual(canonical_text("Mali Bünye"), "mali_bunye")
        self.assertEqual(canonical_text("İstikrarlı Fonlama Oranı"), "istikrarli_fonlama_orani")

    def test_period_folder_maps_to_quarter_end(self) -> None:
        self.assertEqual(
            period_from_folder(Path("2025-06")),
            ("2025-06-30", "Haziran 2025"),
        )

    def test_quarter_range_is_inclusive(self) -> None:
        self.assertEqual(
            quarter_range(parse_period("2025-09"), parse_period("2026-03")),
            [(2025, 9), (2025, 12), (2026, 3)],
        )
