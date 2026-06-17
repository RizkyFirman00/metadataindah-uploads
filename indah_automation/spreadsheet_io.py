from __future__ import annotations

import csv
import datetime as dt
import posixpath
import re
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .shared import IndahError, clean


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
DATE_NUM_FMT_IDS = {
    14,
    15,
    16,
    17,
    18,
    19,
    20,
    21,
    22,
    27,
    28,
    29,
    30,
    31,
    32,
    33,
    34,
    35,
    36,
    45,
    46,
    47,
    50,
    51,
    52,
    53,
    54,
    55,
    56,
    57,
    58,
}


@dataclass
class TableData:
    headers: List[str]
    rows: List[Dict[str, str]]


def safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", name or "upload").strip(" .")
    return (cleaned or "upload")[:140]


def normalize_slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", (value or "").lower()).strip("_")
    return re.sub(r"_+", "_", normalized)


def name_matches_slug(name: str, slug: str) -> bool:
    normalized_name = normalize_slug(name)
    normalized_slug = normalize_slug(slug)
    excel_truncated_slug = normalized_slug[:31]
    return (
        normalized_name == normalized_slug
        or normalized_name.endswith(normalized_slug)
        or normalized_slug in normalized_name.split("_sheet_")
        or (
            len(normalized_slug) > 31
            and (
                normalized_name == excel_truncated_slug
                or normalized_name.endswith(excel_truncated_slug)
            )
        )
    )


def download_spreadsheet_url(url: str, output_dir: Path) -> Path:
    normalized_url, extension = normalize_download_url(url)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"spreadsheet_from_url{extension}"
    request = urllib.request.Request(
        normalized_url,
        headers={"User-Agent": "Mozilla/5.0 INDAH metadata automation"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            path.write_bytes(response.read())
    except Exception as exc:
        raise IndahError(f"Gagal download spreadsheet: {exc}") from exc
    return path


def normalize_download_url(url: str) -> Tuple[str, str]:
    parsed = urllib.parse.urlparse(url.strip())
    if "docs.google.com" in parsed.netloc and "/spreadsheets/d/" in parsed.path:
        match = re.search(r"/spreadsheets/d/([^/]+)", parsed.path)
        if not match:
            raise IndahError("URL Google Sheets tidak valid.")
        sheet_id = match.group(1)
        query = urllib.parse.parse_qs(parsed.query)
        gid = query.get("gid", [None])[0]
        if not gid and parsed.fragment:
            gid = urllib.parse.parse_qs(parsed.fragment).get("gid", [None])[0]
        if gid:
            return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}", ".csv"
        return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx", ".xlsx"
    suffix = Path(parsed.path).suffix.lower()
    if suffix in {".csv", ".tsv", ".xlsx", ".xlsm"}:
        return url, suffix
    return url, ".xlsx"


def read_spreadsheet_tables(path: Path) -> Dict[str, TableData]:
    suffix = path.suffix.lower()
    if suffix in {".csv", ".tsv"}:
        return {path.stem: read_delimited_table(path)}
    if suffix in {".xlsx", ".xlsm"}:
        return read_xlsx_tables(path)
    if suffix == ".xls":
        raise IndahError("File .xls lama belum didukung. Simpan ulang sebagai .xlsx atau .csv.")
    raise IndahError(f"Format file belum didukung: {path.name}")


def read_delimited_table(path: Path) -> TableData:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    sample = text[:4096]
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        delimiter = dialect.delimiter
    except csv.Error:
        pass
    reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
    headers = [clean(header) or f"column_{index + 1}" for index, header in enumerate(reader.fieldnames or [])]
    rows: List[Dict[str, str]] = []
    for raw_row in reader:
        row = {
            (clean(key) or f"column_{index + 1}"): clean(value) or ""
            for index, (key, value) in enumerate(raw_row.items())
            if key is not None
        }
        if any(clean(value) for value in row.values()):
            rows.append(row)
    return TableData(headers=headers, rows=rows)


def read_xlsx_tables(path: Path) -> Dict[str, TableData]:
    try:
        with zipfile.ZipFile(path) as archive:
            shared_strings = read_shared_strings(archive)
            date_style_ids = read_date_style_ids(archive)
            sheet_targets = read_workbook_sheet_targets(archive)
            tables: Dict[str, TableData] = {}
            for sheet_name, sheet_path in sheet_targets:
                matrix = read_sheet_matrix(archive, sheet_path, shared_strings, date_style_ids)
                table = matrix_to_table(matrix)
                tables[sheet_name] = table
            return tables
    except zipfile.BadZipFile as exc:
        raise IndahError(f"File Excel rusak/tidak valid: {path.name}") from exc


def read_shared_strings(archive: zipfile.ZipFile) -> List[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    values: List[str] = []
    for item in root.findall(f"{{{MAIN_NS}}}si"):
        text = "".join(node.text or "" for node in item.iter(f"{{{MAIN_NS}}}t"))
        values.append(text)
    return values


def read_date_style_ids(archive: zipfile.ZipFile) -> set:
    try:
        root = ET.fromstring(archive.read("xl/styles.xml"))
    except KeyError:
        return set()
    custom_date_ids = set()
    num_fmts = root.find(f"{{{MAIN_NS}}}numFmts")
    if num_fmts is not None:
        for fmt in num_fmts.findall(f"{{{MAIN_NS}}}numFmt"):
            fmt_id = int(fmt.attrib.get("numFmtId", "0"))
            code = fmt.attrib.get("formatCode", "")
            if looks_like_date_format(code):
                custom_date_ids.add(fmt_id)
    style_ids = set()
    cell_xfs = root.find(f"{{{MAIN_NS}}}cellXfs")
    if cell_xfs is not None:
        for index, xf in enumerate(cell_xfs.findall(f"{{{MAIN_NS}}}xf")):
            num_fmt_id = int(xf.attrib.get("numFmtId", "0"))
            if num_fmt_id in DATE_NUM_FMT_IDS or num_fmt_id in custom_date_ids:
                style_ids.add(index)
    return style_ids


def looks_like_date_format(code: str) -> bool:
    stripped = re.sub(r'"[^"]*"', "", code.lower())
    stripped = re.sub(r"\[[^\]]+\]", "", stripped)
    stripped = stripped.replace("\\", "")
    return bool(re.search(r"(^|[^a-z])([dmyhs]){1,4}([^a-z]|$)", stripped))


def read_workbook_sheet_targets(archive: zipfile.ZipFile) -> List[Tuple[str, str]]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    rel_targets = {}
    for rel in rels.findall(f"{{{PACKAGE_REL_NS}}}Relationship"):
        target = rel.attrib.get("Target", "")
        if target.startswith("/"):
            target_path = target.lstrip("/")
        else:
            target_path = posixpath.normpath(posixpath.join("xl", target))
        rel_targets[rel.attrib.get("Id", "")] = target_path

    sheets: List[Tuple[str, str]] = []
    sheets_root = workbook.find(f"{{{MAIN_NS}}}sheets")
    if sheets_root is None:
        return sheets
    for sheet in sheets_root.findall(f"{{{MAIN_NS}}}sheet"):
        rel_id = sheet.attrib.get(f"{{{DOC_REL_NS}}}id")
        target = rel_targets.get(rel_id or "")
        if target:
            sheets.append((sheet.attrib.get("name", "Sheet"), target))
    return sheets


def read_sheet_matrix(
    archive: zipfile.ZipFile,
    sheet_path: str,
    shared_strings: Sequence[str],
    date_style_ids: set,
) -> List[List[str]]:
    root = ET.fromstring(archive.read(sheet_path))
    cells: Dict[Tuple[int, int], str] = {}
    max_row = -1
    max_col = -1
    for row in root.iter(f"{{{MAIN_NS}}}row"):
        fallback_col = 0
        row_index = int(row.attrib.get("r", "1")) - 1
        for cell in row.findall(f"{{{MAIN_NS}}}c"):
            ref = cell.attrib.get("r", "")
            if ref:
                row_index = cell_row_index(ref, row_index)
                col_index = cell_column_index(ref)
            else:
                col_index = fallback_col
            fallback_col = col_index + 1
            value = read_cell_value(cell, shared_strings, date_style_ids)
            if value:
                cells[(row_index, col_index)] = value
                max_row = max(max_row, row_index)
                max_col = max(max_col, col_index)
    if max_row < 0 or max_col < 0:
        return []
    matrix = [["" for _ in range(max_col + 1)] for _ in range(max_row + 1)]
    for (row_index, col_index), value in cells.items():
        matrix[row_index][col_index] = value
    return matrix


def cell_column_index(ref: str) -> int:
    match = re.match(r"([A-Z]+)", ref.upper())
    if not match:
        return 0
    total = 0
    for char in match.group(1):
        total = total * 26 + (ord(char) - ord("A") + 1)
    return total - 1


def cell_row_index(ref: str, default: int) -> int:
    match = re.search(r"(\d+)", ref)
    return int(match.group(1)) - 1 if match else default


def read_cell_value(cell: ET.Element, shared_strings: Sequence[str], date_style_ids: set) -> str:
    cell_type = cell.attrib.get("t")
    value_node = cell.find(f"{{{MAIN_NS}}}v")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{{{MAIN_NS}}}t")).strip()
    if value_node is None or value_node.text is None:
        return ""
    raw_value = value_node.text
    if cell_type == "s":
        index = int(float(raw_value))
        return shared_strings[index].strip() if 0 <= index < len(shared_strings) else ""
    if cell_type == "b":
        return "TRUE" if raw_value == "1" else "FALSE"
    style_id = int(cell.attrib.get("s", "-1"))
    if style_id in date_style_ids:
        converted = excel_serial_to_text(raw_value)
        if converted is not None:
            return converted
    return format_number_text(raw_value)


def excel_serial_to_text(value: str) -> Optional[str]:
    try:
        serial = float(value)
    except ValueError:
        return None
    if serial <= 0:
        return None
    base = dt.datetime(1899, 12, 30)
    converted = base + dt.timedelta(days=serial)
    if converted.time() == dt.time(0, 0):
        return converted.strftime("%Y-%m-%d")
    return converted.strftime("%Y-%m-%d %H:%M:%S")


def format_number_text(value: str) -> str:
    try:
        number = float(value)
    except ValueError:
        return value.strip()
    if number.is_integer() and re.fullmatch(r"-?\d+(\.0+)?", value):
        return str(int(number))
    return value.strip()


def matrix_to_table(matrix: Sequence[Sequence[str]]) -> TableData:
    if not matrix:
        return TableData(headers=[], rows=[])
    header_index = 0
    for index, row in enumerate(matrix):
        if any(clean(value) for value in row):
            header_index = index
            break
    width = max(len(row) for row in matrix[header_index:])
    raw_headers = list(matrix[header_index]) + [""] * (width - len(matrix[header_index]))
    headers = unique_headers(raw_headers[:width])
    rows: List[Dict[str, str]] = []
    for raw_row in matrix[header_index + 1 :]:
        values = list(raw_row) + [""] * (width - len(raw_row))
        if not any(clean(value) for value in values):
            continue
        rows.append({headers[index]: clean(values[index]) or "" for index in range(width)})
    return TableData(headers=headers, rows=rows)


def unique_headers(headers: Sequence[str]) -> List[str]:
    result: List[str] = []
    counts: Dict[str, int] = {}
    for index, header in enumerate(headers):
        name = clean(header) or f"column_{index + 1}"
        count = counts.get(name, 0) + 1
        counts[name] = count
        result.append(name if count == 1 else f"{name}_{count}")
    return result


def choose_table(tables: Dict[str, TableData], slug: str, *, allow_single_sheet: bool = False) -> Optional[Tuple[str, TableData]]:
    matches = [(name, table) for name, table in tables.items() if name_matches_slug(name, slug)]
    if matches:
        return matches[0]
    if allow_single_sheet and len(tables) == 1:
        return next(iter(tables.items()))
    return None


def write_table_csv(path: Path, table: TableData) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = table.headers[:]
    if not headers:
        for row in table.rows:
            for key in row:
                if key not in headers:
                    headers.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for row in table.rows:
            writer.writerow({header: row.get(header, "") for header in headers})


def prepare_phase_sources(
    *,
    required_slugs: Sequence[str],
    optional_slugs: Sequence[str],
    specific_files: Dict[str, Path],
    workbook_files: Sequence[Path],
    output_dir: Path,
) -> Dict[str, Dict[str, str]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    all_slugs = list(required_slugs) + list(optional_slugs)
    prepared: Dict[str, Dict[str, str]] = {}

    for slug, source_path in specific_files.items():
        tables = read_spreadsheet_tables(source_path)
        chosen = choose_table(tables, slug, allow_single_sheet=True)
        if chosen is None:
            raise IndahError(f"Tidak menemukan sheet untuk {slug} di {source_path.name}.")
        sheet_name, table = chosen
        output_path = output_dir / f"{slug}.csv"
        write_table_csv(output_path, table)
        prepared[slug] = {
            "source": source_path.name,
            "sheet": sheet_name,
            "csv": str(output_path),
            "rows": str(len(table.rows)),
        }

    for source_path in workbook_files:
        tables = read_spreadsheet_tables(source_path)
        for slug in all_slugs:
            if slug in prepared:
                continue
            chosen = choose_table(tables, slug, allow_single_sheet=False)
            if chosen is None:
                continue
            sheet_name, table = chosen
            output_path = output_dir / f"{slug}.csv"
            write_table_csv(output_path, table)
            prepared[slug] = {
                "source": source_path.name,
                "sheet": sheet_name,
                "csv": str(output_path),
                "rows": str(len(table.rows)),
            }

    missing = [slug for slug in required_slugs if slug not in prepared]
    if missing:
        joined = ", ".join(missing)
        raise IndahError(f"Input wajib belum ada: {joined}")
    return prepared
