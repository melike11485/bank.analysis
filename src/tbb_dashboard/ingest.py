from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import unicodedata
from datetime import date
from pathlib import Path
from typing import Iterator

import xlrd


MONTH_NAMES = {3: "Mart", 6: "Haziran", 9: "Eylül", 12: "Aralık"}
SOURCE_GROUPS = {
    "1": "mali_bunye",
    "2": "aktifler",
    "3": "pasifler",
    "4": "gelir_gider",
    "5": "nazim",
}
GROUP_ENTITY_KEYS = {
    "kalkinma_ve_yatirim_bankalari",
    "kamu_sermayeli_bankalar",
    "mevduat_bankalari",
    "tas_mevd_sig_fonuna_devr_bankalar",
    "yabanci_sermayeli_bankalar",
    "ozel_sermayeli_bankalar",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    observation_id TEXT PRIMARY KEY,
    period_end TEXT NOT NULL,
    period_label TEXT NOT NULL,
    source_group TEXT NOT NULL,
    source_file TEXT NOT NULL,
    sheet_name TEXT NOT NULL,
    sheet_key TEXT NOT NULL,
    report_title TEXT NOT NULL,
    entity_name TEXT NOT NULL,
    entity_name_raw TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    metric_path TEXT NOT NULL,
    metric_key TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    source_col INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS load_audit (
    period_end TEXT NOT NULL,
    source_group TEXT NOT NULL,
    source_file TEXT NOT NULL,
    sheet_name TEXT NOT NULL,
    sheet_key TEXT NOT NULL,
    source_rows INTEGER NOT NULL,
    source_columns INTEGER NOT NULL,
    entities_loaded INTEGER NOT NULL,
    values_loaded INTEGER NOT NULL,
    PRIMARY KEY (period_end, source_group, sheet_key)
);
CREATE TABLE IF NOT EXISTS schema_audit (
    period_end TEXT NOT NULL,
    source_group TEXT NOT NULL,
    sheet_name TEXT NOT NULL,
    sheet_key TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (period_end, source_group, sheet_key)
);
CREATE INDEX IF NOT EXISTS idx_obs_period ON observations(period_end);
CREATE INDEX IF NOT EXISTS idx_obs_entity ON observations(entity_key);
CREATE INDEX IF NOT EXISTS idx_obs_metric ON observations(metric_key);
CREATE INDEX IF NOT EXISTS idx_obs_sheet ON observations(source_group, sheet_key);
"""


def normalize_space(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def canonical_text(value: object) -> str:
    text = normalize_space(value).casefold().replace("ı", "i")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def stable_id(*parts: object) -> str:
    value = "|".join(normalize_space(part).casefold() for part in parts)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def source_group(path: Path) -> str | None:
    match = re.match(r"\s*([1-5])", path.name)
    return SOURCE_GROUPS.get(match.group(1)) if match else None


def period_from_folder(path: Path) -> tuple[str, str]:
    match = re.fullmatch(r"(20\d{2})-(03|06|09|12)", path.name)
    if not match:
        raise ValueError(f"Dönem klasörü YYYY-MM biçiminde değil: {path}")
    year, month = int(match.group(1)), int(match.group(2))
    day = 31 if month in (3, 12) else 30
    return date(year, month, day).isoformat(), f"{MONTH_NAMES[month]} {year}"


def report_title(sheet: xlrd.sheet.Sheet) -> str:
    for row in range(min(3, sheet.nrows)):
        candidate = normalize_space(sheet.cell_value(row, 0)) if sheet.ncols else ""
        if candidate and not re.search(r"20\d{2}", candidate):
            return candidate
    return ""


def expanded_header_matrix(sheet: xlrd.sheet.Sheet) -> list[list[str]]:
    rows = range(3, min(6, sheet.nrows))
    matrix = [
        [normalize_space(sheet.cell_value(row, col)) for col in range(sheet.ncols)]
        for row in rows
    ]
    row_index = {row: index for index, row in enumerate(rows)}
    for row_start, row_end, col_start, col_end in sheet.merged_cells:
        if row_start not in row_index:
            continue
        value = normalize_space(sheet.cell_value(row_start, col_start))
        for row in range(row_start, min(row_end, rows.stop)):
            if row not in row_index:
                continue
            for col in range(col_start, col_end):
                matrix[row_index[row]][col] = value
    return matrix


def metric_headers(sheet: xlrd.sheet.Sheet) -> list[str]:
    matrix = expanded_header_matrix(sheet)
    headers = ["entity"]
    duplicates: dict[str, int] = {}
    for col in range(1, sheet.ncols):
        parts: list[str] = []
        for row in matrix:
            value = row[col]
            if value and value.casefold() != "banka" and (not parts or parts[-1] != value):
                parts.append(value)
        base = " > ".join(parts) or f"metric_col_{col + 1:03d}"
        duplicates[base] = duplicates.get(base, 0) + 1
        suffix = f" [{duplicates[base]}]" if duplicates[base] > 1 else ""
        headers.append(base + suffix)
    return headers


def infer_unit(title: str, metric: str) -> str:
    metric_lower = metric.casefold()
    title_lower = title.casefold()
    if "%" in metric_lower or "(%)" in metric_lower:
        return "%"
    if "milyon tl" in metric_lower or "milyon tl" in title_lower:
        return "Milyon TL"
    if "oran" in metric_lower or "%" in title_lower:
        return "%"
    return "Bilinmiyor"


def classify_entity(raw: str) -> str:
    clean = canonical_text(raw)
    if clean.startswith("sektor"):
        return "sector"
    if clean in GROUP_ENTITY_KEYS:
        return "group"
    return "bank"


def iter_sheet_observations(
    period_end: str,
    period_label: str,
    group: str,
    source_file: str,
    sheet: xlrd.sheet.Sheet,
) -> tuple[list[tuple], tuple]:
    title = report_title(sheet)
    sheet_key = canonical_text(sheet.name)
    headers = metric_headers(sheet)
    observations: list[tuple] = []
    entities_loaded = 0
    values_loaded = 0
    for row in range(7, sheet.nrows):
        raw_entity = str(sheet.cell_value(row, 0) or "")
        entity_name = normalize_space(raw_entity)
        if not entity_name:
            continue
        values = [sheet.cell_value(row, col) for col in range(1, sheet.ncols)]
        if not any(isinstance(value, (int, float)) for value in values):
            continue
        entities_loaded += 1
        entity_key = canonical_text(entity_name)
        for col, value in enumerate(values, start=1):
            if not isinstance(value, (int, float)):
                continue
            metric_path = headers[col]
            metric_key = f"{group}.{sheet_key}.{canonical_text(metric_path)}"
            observation_id = stable_id(
                period_end, group, sheet_key, entity_key, metric_key
            )
            observations.append(
                (
                    observation_id,
                    period_end,
                    period_label,
                    group,
                    source_file,
                    sheet.name,
                    sheet_key,
                    title,
                    entity_name,
                    raw_entity,
                    entity_key,
                    classify_entity(raw_entity),
                    metric_path,
                    metric_key,
                    float(value),
                    infer_unit(title, metric_path),
                    row + 1,
                    col + 1,
                )
            )
            values_loaded += 1
    audit = (
        period_end,
        group,
        source_file,
        sheet.name,
        sheet_key,
        sheet.nrows,
        sheet.ncols,
        entities_loaded,
        values_loaded,
    )
    return observations, audit


def load_period(folder: Path) -> tuple[list[tuple], list[tuple]]:
    period_end, period_label = period_from_folder(folder)
    observations: list[tuple] = []
    audits: list[tuple] = []
    for path in sorted(folder.glob("*.xls")):
        group = source_group(path)
        if not group:
            continue
        book = xlrd.open_workbook(path, on_demand=True, formatting_info=False)
        try:
            for sheet in book.sheets():
                if sheet.ncols < 2 or not report_title(sheet):
                    continue
                sheet_observations, audit = iter_sheet_observations(
                    period_end, period_label, group, path.name, sheet
                )
                observations.extend(sheet_observations)
                audits.append(audit)
        finally:
            book.release_resources()
    unique: dict[str, tuple] = {}
    for observation in observations:
        observation_id = observation[0]
        previous = unique.get(observation_id)
        if previous and previous[14] != observation[14]:
            raise ValueError(
                "Aynı dönem/banka/metrik için çelişen iki değer bulundu: "
                f"{folder.name} {observation[8]} {observation[12]}"
            )
        unique[observation_id] = observation
    return list(unique.values()), audits


def chunked(rows: list[tuple], size: int = 10_000) -> Iterator[list[tuple]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def rebuild_schema_audit(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM schema_audit")
    periods = [
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT period_end FROM load_audit ORDER BY period_end"
        )
    ]
    sheets = list(
        connection.execute(
            "SELECT source_group, sheet_key, MAX(sheet_name) "
            "FROM load_audit GROUP BY source_group, sheet_key"
        )
    )
    present = {
        (period, group, sheet_key)
        for period, group, sheet_key in connection.execute(
            "SELECT period_end, source_group, sheet_key FROM load_audit"
        )
    }
    published_months: dict[tuple[str, str], set[str]] = {}
    first_published: dict[tuple[str, str], str] = {}
    for period, group, sheet_key in present:
        published_months.setdefault((group, sheet_key), set()).add(period[5:7])
        key = (group, sheet_key)
        first_published[key] = min(period, first_published.get(key, period))
    rows = [
        (
            period,
            group,
            sheet_name,
            sheet_key,
            (
                "present"
                if (period, group, sheet_key) in present
                else "not_yet_published"
                if period < first_published[(group, sheet_key)]
                else "missing"
                if period[5:7] in published_months[(group, sheet_key)]
                else "not_published_for_quarter"
            ),
        )
        for period in periods
        for group, sheet_key, sheet_name in sheets
    ]
    connection.executemany(
        "INSERT INTO schema_audit VALUES (?, ?, ?, ?, ?)", rows
    )


def ingest(raw_dir: Path, database: Path) -> None:
    period_folders = sorted(
        path
        for path in raw_dir.iterdir()
        if path.is_dir() and re.fullmatch(r"20\d{2}-(03|06|09|12)", path.name)
    )
    if not period_folders:
        raise ValueError(f"Dönem klasörü bulunamadı: {raw_dir}")
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.executescript(SCHEMA)
        for folder in period_folders:
            period_end, _ = period_from_folder(folder)
            observations, audits = load_period(folder)
            if not observations:
                print(f"{folder.name}: sayısal veri bulunamadı, atlandı")
                continue
            connection.execute(
                "DELETE FROM observations WHERE period_end = ?", (period_end,)
            )
            connection.execute(
                "DELETE FROM load_audit WHERE period_end = ?", (period_end,)
            )
            for rows in chunked(observations):
                connection.executemany(
                    "INSERT OR REPLACE INTO observations VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
            connection.executemany(
                "INSERT OR REPLACE INTO load_audit VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                audits,
            )
            print(
                f"{folder.name}: {len(observations):,} gözlem, "
                f"{len(audits)} sayfa yüklendi"
            )
        rebuild_schema_audit(connection)
        connection.execute("ANALYZE")
    with sqlite3.connect(database) as connection:
        observations = connection.execute(
            "SELECT COUNT(*) FROM observations"
        ).fetchone()[0]
        periods = connection.execute(
            "SELECT COUNT(DISTINCT period_end) FROM observations"
        ).fetchone()[0]
        missing = connection.execute(
            "SELECT COUNT(*) FROM schema_audit WHERE status='missing'"
        ).fetchone()[0]
        not_published = connection.execute(
            "SELECT COUNT(*) FROM schema_audit "
            "WHERE status='not_published_for_quarter'"
        ).fetchone()[0]
        not_yet_published = connection.execute(
            "SELECT COUNT(*) FROM schema_audit "
            "WHERE status='not_yet_published'"
        ).fetchone()[0]
    print(
        f"Tamamlandı: {observations:,} gözlem, {periods} dönem, "
        f"{missing} gerçek eksik, {not_published} dönemsel yayımlanmayan, "
        f"{not_yet_published} henüz yürürlükte olmayan sayfa kaydı -> {database}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="İndirilen TBB XLS dönemlerini SQLite veri ambarına yükler"
    )
    parser.add_argument("--raw", type=Path, default=Path("data/raw"))
    parser.add_argument(
        "--database", type=Path, default=Path("data/processed/tbb.db")
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    ingest(args.raw, args.database)
