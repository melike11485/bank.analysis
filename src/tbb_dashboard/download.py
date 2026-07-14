from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


BASE_URL = "https://www.tbb.org.tr"
CATEGORY_URL = f"{BASE_URL}/istatistiki-raporlar/11236"
REPORT_CATEGORY_ID = "11244"
QUARTER_MONTHS = (3, 6, 9, 12)
DEFAULT_START = (2020, 3)
OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
USER_AGENT = "TBB-dashboard-research/1.0 (+personal analytical use)"


@dataclass(frozen=True)
class DownloadRecord:
    period: str
    report_url: str
    source_url: str
    local_path: str
    sha256: str
    size_bytes: int
    status: str


def fetch(url: str, timeout: int = 45) -> tuple[bytes, str]:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urlopen(request, timeout=timeout) as response:
        return response.read(), response.headers.get("Content-Type", "")


def fetch_text(url: str) -> str:
    payload, _ = fetch(url)
    return payload.decode("utf-8", errors="replace")


def discover_year_ids() -> dict[int, str]:
    page = fetch_text(CATEGORY_URL)
    pattern = re.compile(
        rf'value="/istatistiki-raporlar-liste/{REPORT_CATEGORY_ID}\?rapor_donemi=(\d+)"'
        r"[^>]*>\s*(20\d{2})\s*</option>",
        re.IGNORECASE,
    )
    result = {int(year): term_id for term_id, year in pattern.findall(page)}
    if not result:
        raise RuntimeError("TBB yıl seçenekleri bulunamadı; sayfa yapısı değişmiş olabilir")
    return result


def discover_report_url(year_id: str, year: int, month: int) -> str | None:
    list_url = (
        f"{BASE_URL}/istatistiki-raporlar-liste/{REPORT_CATEGORY_ID}"
        f"?rapor_donemi={year_id}&ay={month}"
    )
    try:
        page = fetch_text(list_url)
    except HTTPError as error:
        if error.code == 404:
            return None
        raise
    month_names = {3: "mart", 6: "haziran", 9: "eylul", 12: "aralik"}
    expected = f"/istatistiki-raporlar/{year}-{month_names[month]}-"
    for href in re.findall(r'href="([^"]+)"', page, flags=re.IGNORECASE):
        clean = html.unescape(href)
        if clean.startswith(expected) and "konsolide-olmayansolo-banka" in clean:
            return urljoin(BASE_URL, clean)
    return None


def discover_xls_links(report_url: str) -> list[tuple[str, str]]:
    page = fetch_text(report_url)
    pattern = re.compile(
        r'<a\s+href="([^"]*/download/node/[^"]+)"[^>]*'
        r'title="([^"]+\.xls)"[^>]*>',
        re.IGNORECASE,
    )
    links = [
        (html.unescape(filename), urljoin(BASE_URL, html.unescape(href)))
        for href, filename in pattern.findall(page)
    ]
    if len(links) < 5:
        raise RuntimeError(
            f"Beklenen TBB XLS bağlantıları bulunamadı ({len(links)} adet): {report_url}"
        )
    return links


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def validate_xls(payload: bytes, content_type: str, url: str) -> None:
    if not payload.startswith(OLE_SIGNATURE):
        preview = payload[:80].decode("utf-8", errors="replace")
        raise ValueError(
            f"İçerik eski Excel/OLE biçiminde değil: {url}; "
            f"content-type={content_type}; başlangıç={preview!r}"
        )


def parse_period(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(20\d{2})-(03|06|09|12)", value)
    if not match:
        raise argparse.ArgumentTypeError("Dönem YYYY-MM olmalı; ay 03/06/09/12 olmalıdır")
    return int(match.group(1)), int(match.group(2))


def current_quarter() -> tuple[int, int]:
    today = date.today()
    month = max(month for month in QUARTER_MONTHS if month <= today.month)
    return today.year, month


def quarter_range(start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]]:
    periods = []
    year, month = start
    while (year, month) <= end:
        periods.append((year, month))
        index = QUARTER_MONTHS.index(month)
        year, month = (
            (year + 1, 3)
            if index == len(QUARTER_MONTHS) - 1
            else (year, QUARTER_MONTHS[index + 1])
        )
    return periods


def sync(
    start: tuple[int, int],
    end: tuple[int, int],
    output_dir: Path,
    delay_seconds: float = 0.4,
) -> list[DownloadRecord]:
    years = discover_year_ids()
    records: list[DownloadRecord] = []
    for year, month in quarter_range(start, end):
        period = f"{year}-{month:02d}"
        if year not in years:
            print(f"{period}: TBB yıl seçeneği henüz yok, atlandı")
            continue
        report_url = discover_report_url(years[year], year, month)
        if not report_url:
            print(f"{period}: rapor henüz yayımlanmamış, atlandı")
            continue
        target_dir = output_dir / period
        target_dir.mkdir(parents=True, exist_ok=True)
        links = discover_xls_links(report_url)
        print(f"{period}: {len(links)} dosya bulundu")
        for filename, source_url in links:
            target = target_dir / filename
            if target.exists():
                payload = target.read_bytes()
                validate_xls(payload, "local-file", str(target))
                status = "existing"
                print(f"  mevcut: {filename}")
            else:
                payload, content_type = fetch(source_url)
                validate_xls(payload, content_type, source_url)
                target.write_bytes(payload)
                status = "downloaded"
                print(f"  indirildi: {filename} ({len(payload):,} bayt)")
                time.sleep(delay_seconds)
            records.append(
                DownloadRecord(
                    period=period,
                    report_url=report_url,
                    source_url=source_url,
                    local_path=str(target),
                    sha256=digest(payload),
                    size_bytes=len(payload),
                    status=status,
                )
            )
        time.sleep(delay_seconds)

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = output_dir / "manifest.json"
    manifest.write_text(
        json.dumps([asdict(record) for record in records], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Manifest: {manifest}")
    return records


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="TBB solo banka seçilmiş tablolarını çeyrek bazında indirir"
    )
    parser.add_argument("--start", type=parse_period, default=DEFAULT_START)
    parser.add_argument("--end", type=parse_period, default=current_quarter())
    parser.add_argument("--output", type=Path, default=Path("data/raw"))
    parser.add_argument("--delay", type=float, default=0.4)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        sync(args.start, args.end, args.output, args.delay)
    except (HTTPError, URLError) as error:
        raise SystemExit(f"TBB bağlantı hatası: {error}") from error
