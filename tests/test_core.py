from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from src.tbb_dashboard.download import DEFAULT_START, parse_period, quarter_range
from src.tbb_dashboard.labels import metric_display_label
from src.tbb_dashboard.ingest import (
    availability_status,
    canonical_text,
    classify_entity,
    deduplicate_observations,
    ensure_database,
    first_data_row,
    metric_headers,
    period_from_folder,
)


class FakeSheet:
    def __init__(self, rows: list[list[object]], merged_cells: list[tuple]) -> None:
        self.name = "Varlıklar"
        self.nrows = len(rows)
        self.ncols = max(len(row) for row in rows)
        self.rows = [row + [""] * (self.ncols - len(row)) for row in rows]
        self.merged_cells = merged_cells

    def cell_value(self, row: int, col: int) -> object:
        return self.rows[row][col]


class CoreTests(TestCase):
    @staticmethod
    def observation(value: float) -> tuple:
        row = [None] * 18
        row[0] = "same-id"
        row[8] = "Örnek Banka A.Ş."
        row[10] = "ornek_banka_a_s"
        row[12] = "Örnek Metrik"
        row[14] = value
        return tuple(row)

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

    def test_currency_abbreviations_are_expanded_outside_mali_bunye(self) -> None:
        self.assertEqual(
            metric_display_label("aktifler.test.metric", "Krediler - TP / YP"),
            "Krediler - Türk Parası / Yabancı Para",
        )
        self.assertEqual(
            metric_display_label("mali_bunye.test.metric", "Krediler - TP / YP"),
            "Krediler - TP / YP",
        )

    def test_summary_fallback_is_not_reported_as_missing(self) -> None:
        period = "2024-12-31"
        group = "pasifler"
        sheet = "ser_benz"
        self.assertEqual(
            availability_status(
                period,
                group,
                sheet,
                present=set(),
                published_months={(group, sheet): {"12"}},
                first_published={(group, sheet): "2020-12-31"},
                summary_available={(period, group, sheet)},
            ),
            "summary_available",
        )

    def test_header_hierarchy_follows_excel_boundaries_when_rows_shift(self) -> None:
        sheet = FakeSheet(
            [
                [""],
                ["Varlıklar, Milyon TL"],
                ["(Mart 2020)"],
                [""],
                ["", "Finansal Varlıklar (net)", "", "", "", "İtfa Edilmiş Maliyeti ile Ölçülen FV (Net)"],
                ["", "Likit Aktifler", "", "", "", "Krediler"],
                ["Banka", "Nakit Değerler ve TCMB", "Bankalar", "Para Piyasalarından Alacaklar", "Toplam", "Krediler"],
                [""],
                ["Sektör Toplamı", 10.0, 20.0, 30.0, 60.0, 40.0],
            ],
            [
                (4, 5, 1, 5),
                (5, 6, 1, 5),
                (4, 5, 5, 6),
                (5, 7, 5, 6),
            ],
        )

        self.assertEqual(first_data_row(sheet), 8)
        self.assertEqual(
            metric_headers(sheet),
            [
                "entity",
                "Finansal Varlıklar (net) > Likit Aktifler > Nakit Değerler ve TCMB",
                "Finansal Varlıklar (net) > Likit Aktifler > Bankalar",
                "Finansal Varlıklar (net) > Likit Aktifler > Para Piyasalarından Alacaklar",
                "Finansal Varlıklar (net) > Likit Aktifler > Toplam",
                "İtfa Edilmiş Maliyeti ile Ölçülen FV (Net) > Krediler",
            ],
        )

    def test_identical_source_rows_are_counted_once(self) -> None:
        observation = self.observation(10.0)
        self.assertEqual(
            deduplicate_observations([observation, observation], "test"),
            [observation],
        )

    def test_conflicting_duplicate_rows_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            deduplicate_observations(
                [self.observation(10.0), self.observation(11.0)], "test"
            )

    def test_missing_database_is_built_once_and_published_atomically(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            raw_dir = root / "raw"
            database = root / "processed" / "tbb.db"
            raw_dir.mkdir()

            def fake_ingest(source: Path, target: Path) -> None:
                self.assertEqual(source, raw_dir)
                self.assertNotEqual(target, database)
                target.write_bytes(b"complete database")

            with patch(
                "src.tbb_dashboard.ingest.ingest", side_effect=fake_ingest
            ) as mocked_ingest:
                self.assertTrue(ensure_database(raw_dir, database))
                self.assertEqual(database.read_bytes(), b"complete database")
                self.assertFalse(ensure_database(raw_dir, database))
                mocked_ingest.assert_called_once()
