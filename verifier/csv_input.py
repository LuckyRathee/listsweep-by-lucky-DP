"""CSV intake with strict limits and duplicate removal."""
import csv
from dataclasses import dataclass
from io import StringIO

MAX_EMAILS = 2000


@dataclass(frozen=True)
class InputRow:
    position: int
    email: str
    original: str
    metadata: dict[str, str]


def parse_csv(content: bytes) -> tuple[list[InputRow], int, str | None]:
    try:
        text = content.decode('utf-8-sig')
    except UnicodeDecodeError:
        return [], 0, 'CSV must be UTF-8 (or UTF-8 with BOM)'
    try:
        reader = csv.DictReader(StringIO(text))
        if reader.fieldnames is None:
            return [], 0, 'CSV has no header'
        aliases = {'email', 'emails', 'email address', 'e-mail', 'e mail'}
        selected = next(
            (name for name in reader.fieldnames if name.strip().lower() in aliases), None
        )
        if selected is None:
            return [], 0, 'CSV must have an email column (header: email, emails, or email address)'
        rows, seen, duplicates = [], set(), 0
        for position, row in enumerate(reader, start=1):
            original = (row.get(selected) or '').strip()
            key = original.lower()
            if not original:
                continue
            if key in seen:
                duplicates += 1
                continue
            if len(rows) >= MAX_EMAILS:
                return [], duplicates, f'CSV exceeds the {MAX_EMAILS} unique-email limit'
            from .syntax import normalize
            normalized = normalize(original)
            key = (normalized[:normalized.rfind("@")] + "@" + normalized.rsplit("@", 1)[1].lower()) if normalized else original
            seen.add(key)
            rows.append(InputRow(position, normalized or original, original, {
                k: (v or '').strip() for k, v in row.items()
                if k != selected and k is not None
            }))
        if not rows:
            return [], duplicates, 'CSV contains no email addresses'
    except csv.Error:
        return [], 0, 'CSV could not be parsed'
    return rows, duplicates, None


def _formula_safe(value):
    # Spreadsheet apps execute cells starting with these characters as formulas.
    if isinstance(value, str) and value[:1] in ('=', '+', '-', '@', '\t', '\r'):
        return "'" + value
    return value


def export_all_csv(results: list[dict]) -> str:
    fields = [
        'email', 'status', 'reason', 'send_eligible', 'deliverable_candidate',
        'disposable', 'role_based', 'catch_all', 'smtp_code', 'checked_at', 'mx_host',
    ]
    lead_columns = []
    for row in results:
        for key in row.get('metadata') or {}:
            if key not in lead_columns:
                lead_columns.append(key)
    stream = StringIO(newline='')
    writer = csv.DictWriter(
        stream, fieldnames=fields + [f'lead_{key}' for key in lead_columns], extrasaction='ignore'
    )
    writer.writeheader()
    for row in results:
        out = {key: row.get(key) for key in fields}  # verifier-generated; leave the address intact
        out.update({f'lead_{k}': _formula_safe(v) for k, v in (row.get('metadata') or {}).items()})
        writer.writerow(out)
    return stream.getvalue()


def export_send_csv(results: list[dict]) -> str:
    return export_all_csv([r for r in results if r['send_eligible']])
