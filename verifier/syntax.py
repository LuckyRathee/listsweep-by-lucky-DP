"""Syntax-only parsing; the SMTP layer must not accept Unicode SMTPUTF8 addresses."""
from email_validator import validate_email, EmailNotValidError


def normalize(raw: str) -> str | None:
    text = raw.strip()
    if not text.isascii() or not text or '\r' in text or '\n' in text:
        return None
    try:
        parsed = validate_email(
            text, check_deliverability=False, test_environment=True, allow_quoted_local=True
        )
    except EmailNotValidError:
        return None
    normalized = text[:text.rfind('@') + 1] + parsed.domain
    local, domain = normalized.rsplit('@', 1)
    # Keep the local part byte-for-byte; only an IDN/domain case is canonicalized.
    if not normalized.isascii():
        return None
    if len(local.encode('ascii')) > 64 or len(normalized) > 254:
        return None
    if '[' in domain or ']' in domain:
        return None
    return normalized
