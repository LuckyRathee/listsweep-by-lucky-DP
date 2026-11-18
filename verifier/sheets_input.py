"""Fetch a *publicly shared* Google Sheet as CSV without ever requesting a user-supplied URL.

Only the sheet ID and gid are taken from the pasted link (strict patterns); the export URL is built
here. Redirects are followed manually and only to Google hosts. Private sheets are not supported.
"""
import re
from urllib.parse import urlsplit

import httpx

MAX_SHEET_BYTES = 1_500_000
TIMEOUT_SECONDS = 10.0
MAX_REDIRECTS = 3
SHEET_URL = re.compile(r'^https://docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]{20,100})(?:[/?#].*)?$')
GID = re.compile(r'(?:[?&#]|^)gid=(\d{1,12})(?:&|#|$)')
NOT_PUBLIC = ('Could not read the sheet. Share it as "Anyone with the link can view", '
              'or download it as CSV/Excel and upload the file instead.')


def export_url(link: str) -> str:
    match = SHEET_URL.match(link.strip())
    if not match:
        raise ValueError('Paste a Google Sheets link that starts with https://docs.google.com/spreadsheets/d/')
    gid = GID.search(link)
    return (f'https://docs.google.com/spreadsheets/d/{match.group(1)}/export?format=csv'
            f'&gid={gid.group(1) if gid else 0}')


def _allowed_host(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    return parts.scheme == 'https' and (
        host == 'docs.google.com' or host.endswith('.googleusercontent.com'))


async def fetch_sheet_csv(link: str, client: httpx.AsyncClient | None = None) -> bytes:
    url = export_url(link)
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False)
    try:
        for _ in range(MAX_REDIRECTS + 1):
            async with client.stream('GET', url) as response:
                if response.is_redirect:
                    url = str(response.headers.get('location', ''))
                    if not _allowed_host(url):
                        raise ValueError(NOT_PUBLIC)  # typically a Google sign-in redirect
                    continue
                if response.status_code != 200:
                    raise ValueError(NOT_PUBLIC)
                if 'text/csv' not in response.headers.get('content-type', '').lower():
                    raise ValueError(NOT_PUBLIC)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_SHEET_BYTES:
                        raise ValueError('Sheet exceeds the 1.5 MB limit')
                return bytes(body)
        raise ValueError(NOT_PUBLIC)
    except httpx.HTTPError as error:
        raise ValueError('Could not reach Google Sheets; try again or upload a file') from error
    finally:
        if owns_client:
            await client.aclose()
