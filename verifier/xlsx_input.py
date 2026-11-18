"""Convert an uploaded .xlsx workbook to CSV bytes so it shares the CSV limits and parser."""
import csv
import zipfile
from io import BytesIO, StringIO

import openpyxl

MAX_XLSX_BYTES = 5_000_000
MAX_UNCOMPRESSED_BYTES = 50_000_000   # zip-bomb guard: a small file can expand hugely
MAX_ZIP_MEMBERS = 200
MAX_ROWS_READ = 20_000
MAX_COLUMNS = 100


def xlsx_to_csv(content: bytes) -> bytes:
    if len(content) > MAX_XLSX_BYTES:
        raise ValueError('Excel file exceeds the 5 MB limit')
    if not content.startswith(b'PK\x03\x04'):
        raise ValueError('Not an .xlsx workbook (legacy .xls files are not supported; save as .xlsx or CSV)')
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ZIP_MEMBERS or sum(m.file_size for m in members) > MAX_UNCOMPRESSED_BYTES:
                raise ValueError('Excel file is too large when unpacked')
    except zipfile.BadZipFile as error:
        raise ValueError('Excel file is corrupt') from error
    try:
        workbook = openpyxl.load_workbook(BytesIO(content), read_only=True, data_only=True)
    except Exception as error:
        raise ValueError('Excel file could not be read') from error
    try:
        sheet = workbook.worksheets[0] if workbook.worksheets else None
        if sheet is None:
            raise ValueError('Workbook has no sheets')
        stream = StringIO(newline='')
        writer = csv.writer(stream)
        wrote = 0
        for index, row in enumerate(sheet.iter_rows(values_only=True, max_col=MAX_COLUMNS), start=1):
            if index > MAX_ROWS_READ:
                raise ValueError(f'Sheet has more than {MAX_ROWS_READ} rows; split it into smaller files')
            cells = ['' if value is None else str(value) for value in row]
            while cells and not cells[-1].strip():
                cells.pop()  # read-only sheets pad every row out to max_col
            if not cells:
                continue
            writer.writerow(cells)
            wrote += 1
        if not wrote:
            raise ValueError('Sheet is empty')
        return stream.getvalue().encode('utf-8')
    finally:
        workbook.close()
