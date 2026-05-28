from __future__ import annotations

from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from config import OUTPUT_COLUMNS


INPUT_COLUMNS = ["Company Name", "City", "State", "Known Website", "Notes"]
URL_COLUMNS = {"Known Website", "Official Website", "Careers Page URL", "Jobs RSS Feed URL"}


def read_companies(path: Path) -> list[dict[str, Any]]:
    workbook = load_workbook(path)
    sheet = workbook.active
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        return []

    headers = [str(value).strip() if value is not None else "" for value in rows[0]]
    header_indexes = {header: index for index, header in enumerate(headers)}
    if "Company Name" not in header_indexes:
        raise ValueError("Input workbook must include a 'Company Name' column.")

    companies: list[dict[str, Any]] = []
    for row in rows[1:]:
        record = {column: "" for column in INPUT_COLUMNS}
        for column in INPUT_COLUMNS:
            if column in header_indexes:
                value = row[header_indexes[column]]
                record[column] = str(value).strip() if value is not None else ""
        if record["Company Name"]:
            companies.append(record)
    return companies


def write_results(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Enriched Companies"
    sheet.append(OUTPUT_COLUMNS)

    for result in rows:
        sheet.append([result.get(column, "") for column in OUTPUT_COLUMNS])

    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions

    for cell in sheet[1]:
        cell.font = Font(bold=True)

    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            header = sheet.cell(row=1, column=cell.column).value
            if header in URL_COLUMNS and cell.value:
                cell.hyperlink = str(cell.value)
                cell.style = "Hyperlink"

    for column_cells in sheet.columns:
        max_length = 0
        column_letter = get_column_letter(column_cells[0].column)
        for cell in column_cells:
            value = "" if cell.value is None else str(cell.value)
            max_length = max(max_length, len(value))
        sheet.column_dimensions[column_letter].width = min(max(max_length + 2, 12), 70)

    workbook.save(path)


def create_sample_workbook(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Companies"
    sheet.append(INPUT_COLUMNS)
    sheet.append(["BECU", "Tukwila", "WA", "https://www.becu.org", ""])
    sheet.append(["WECU", "Bellingham", "WA", "https://www.wecu.com", ""])
    sheet.append(["Canvas Credit Union", "Lone Tree", "CO", "", ""])
    sheet.append(["Ent Credit Union", "Colorado Springs", "CO", "", ""])
    sheet.append(["FirstBank", "Lakewood", "CO", "", ""])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for column_cells in sheet.columns:
        column_letter = get_column_letter(column_cells[0].column)
        max_length = max(len(str(cell.value or "")) for cell in column_cells)
        sheet.column_dimensions[column_letter].width = max(max_length + 2, 14)
    workbook.save(path)
