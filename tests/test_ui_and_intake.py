"""Web UI shell, DNS-only mode, job listing, Excel and Google Sheets intake."""
import asyncio
import zipfile
from io import BytesIO
from types import SimpleNamespace

import dns.resolver
import httpx
import openpyxl
import pytest
from httpx import ASGITransport, AsyncClient

from verifier.api import create_app
from verifier.engine import Verifier
from verifier.sheets_input import NOT_PUBLIC, export_url, fetch_sheet_csv
from verifier.xlsx_input import xlsx_to_csv

KEY = 'k' * 32
SHEET_ID = 'A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0'


class DNS:
    async def resolve(self, domain, record):
        if domain == 'null.test':
            return [SimpleNamespace(preference=0, exchange='.')]
        if domain == 'gone.test':
            raise dns.resolver.NXDOMAIN()
        if domain == 'slow.test':
            raise dns.resolver.Timeout()
        return [SimpleNamespace(preference=10, exchange='mx.test.')]


def make_app(tmp_path, **kwargs):
    return create_app(store_path=str(tmp_path / 's.sqlite3'), api_key=KEY,
                      verifier=Verifier(DNS(), None), **kwargs)


def client_for(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url='http://test')


async def wait_done(client, job_id):
    for _ in range(100):
        job = (await client.get(f'/v1/jobs/{job_id}', headers={'X-API-Key': KEY})).json()
        if job['status'] == 'completed':
            return job
        await asyncio.sleep(0.02)
    raise AssertionError('job did not finish')


def xlsx_bytes(rows):
    book = openpyxl.Workbook()
    for row in rows:
        book.active.append(row)
    stream = BytesIO()
    book.save(stream)
    return stream.getvalue()


# ---- UI shell and headers ----

async def test_page_shell_is_public_but_data_routes_need_the_key(tmp_path):
    async with client_for(make_app(tmp_path)) as client:
        page = await client.get('/')
        assert page.status_code == 200 and 'Lead Email Verifier' in page.text
        assert (await client.get('/ui/app.js')).status_code == 200
        assert (await client.get('/ui/style.css')).status_code == 200
        for path in ('/v1/config', '/v1/jobs', '/v1/jobs/x'):
            assert (await client.get(path)).status_code == 401
        assert (await client.get('/docs')).status_code == 401  # API docs are not public


async def test_security_headers_and_no_inline_script_in_the_shell(tmp_path):
    async with client_for(make_app(tmp_path)) as client:
        response = await client.get('/')
        csp = response.headers['content-security-policy']
        assert "script-src 'self'" in csp and "default-src 'none'" in csp and 'unsafe-inline' not in csp
        assert response.headers['x-content-type-options'] == 'nosniff'
        assert response.headers['cache-control'] == 'no-store'
        html = response.text
        assert ' onclick=' not in html and ' style=' not in html
        assert '<script>' not in html
        # every asset is same-origin: no external src/href (the sheet-link placeholder is just text)
        assert 'src="http' not in html and 'href="http' not in html and 'src="//' not in html


def test_ui_javascript_never_writes_untrusted_text_as_html():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / 'verifier' / 'ui' / 'app.js').read_text()
    for banned in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'eval('):
        assert banned not in source


async def test_config_reports_limits_and_that_smtp_is_off(tmp_path):
    async with client_for(make_app(tmp_path)) as client:
        config = (await client.get('/v1/config', headers={'X-API-Key': KEY})).json()
        assert config['smtp_available'] is False and config['max_emails'] == 2000
        assert config['auth_required'] is True


# ---- DNS-only mode ----

async def test_dns_mode_reports_domain_evidence_and_never_makes_anything_send_eligible(tmp_path):
    csv = 'email\na@ok.test\nb@null.test\nc@gone.test\nd@slow.test\nnot-an-email\n'
    async with client_for(make_app(tmp_path)) as client:
        headers = {'X-API-Key': KEY}
        created = await client.post('/v1/verify/csv', headers=headers, data={'check_dns': 'true'},
                                    files={'file': ('a.csv', csv, 'text/csv')})
        assert created.status_code == 202
        job_id = created.json()['job_id']
        await wait_done(client, job_id)
        rows = (await client.get(f'/v1/jobs/{job_id}/results', headers=headers)).json()
        reasons = {r['email']: (r['status'], r['reason']) for r in rows}
        assert reasons == {
            'a@ok.test': ('unknown', 'mailbox_check_disabled'),
            'b@null.test': ('undeliverable', 'null_mx'),
            'c@gone.test': ('undeliverable', 'domain_not_found'),
            'd@slow.test': ('unknown', 'dns_timeout'),
            'not-an-email': ('undeliverable', 'invalid_syntax'),
        }
        assert not any(r['send_eligible'] for r in rows)


async def test_default_job_still_does_no_dns(tmp_path):
    calls = []

    class Spy(DNS):
        async def resolve(self, domain, record):
            calls.append(domain)
            return await super().resolve(domain, record)

    app = create_app(store_path=str(tmp_path / 's.sqlite3'), api_key=KEY,
                     verifier=Verifier(Spy(), None))
    async with client_for(app) as client:
        created = await client.post('/v1/verify/csv', headers={'X-API-Key': KEY},
                                    files={'file': ('a.csv', 'email\na@ok.test\n', 'text/csv')})
        await wait_done(client, created.json()['job_id'])
    assert calls == []


# ---- job listing ----

async def test_jobs_are_listed_newest_first_with_counts(tmp_path):
    async with client_for(make_app(tmp_path)) as client:
        headers = {'X-API-Key': KEY}
        ids = []
        for name in ('one.csv', 'two.csv'):
            response = await client.post('/v1/verify/csv', headers=headers,
                                         files={'file': (name, 'email\na@ok.test\n', 'text/csv')})
            ids.append(response.json()['job_id'])
            await wait_done(client, ids[-1])
            await asyncio.sleep(0.01)
        jobs = (await client.get('/v1/jobs', headers=headers)).json()
        assert [j['id'] for j in jobs] == ids[::-1]
        assert jobs[0]['options']['filename'] == 'two.csv' and jobs[0]['counts'] == {'unknown': 1}


# ---- Excel ----

def test_xlsx_is_converted_to_csv_with_other_columns_kept():
    out = xlsx_to_csv(xlsx_bytes([['Name', 'Email'], ['Ann', 'ann@ok.test'], [None, None], ['Bob', 'bob@ok.test']]))
    assert out.decode().splitlines() == ['Name,Email', 'Ann,ann@ok.test', 'Bob,bob@ok.test']


@pytest.mark.parametrize('payload', [b'', b'not a workbook', b'\xd0\xcf\x11\xe0legacy-xls', b'PK\x03\x04junk'])
def test_bad_or_legacy_excel_is_rejected_cleanly(payload):
    with pytest.raises(ValueError):
        xlsx_to_csv(payload)


def test_zip_that_unpacks_hugely_is_rejected():
    stream = BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('xl/big.xml', b'0' * 60_000_000)
    assert len(stream.getvalue()) < 100_000
    with pytest.raises(ValueError, match='too large'):
        xlsx_to_csv(stream.getvalue())


async def test_xlsx_upload_runs_a_job_and_bad_files_get_422(tmp_path):
    async with client_for(make_app(tmp_path)) as client:
        headers = {'X-API-Key': KEY}
        data = xlsx_bytes([['Company', 'Email'], ['Acme', 'ann@ok.test'], ['Beta', 'bad']])
        created = await client.post('/v1/verify/xlsx', headers=headers, data={'check_dns': 'true'},
                                    files={'file': ('leads.xlsx', data, 'application/octet-stream')})
        assert created.status_code == 202
        await wait_done(client, created.json()['job_id'])
        rows = (await client.get(f"/v1/jobs/{created.json()['job_id']}/results", headers=headers)).json()
        assert [r['email'] for r in rows] == ['ann@ok.test', 'bad'] and rows[0]['metadata'] == {'Company': 'Acme'}
        bad = await client.post('/v1/verify/xlsx', headers=headers,
                                files={'file': ('x.xlsx', b'nope', 'application/octet-stream')})
        assert bad.status_code == 422


# ---- Google Sheets ----

def test_sheet_links_are_parsed_strictly():
    base = f'https://docs.google.com/spreadsheets/d/{SHEET_ID}'
    assert export_url(f'{base}/edit#gid=123') == f'{base}/export?format=csv&gid=123'
    assert export_url(f'{base}/edit?usp=sharing') == f'{base}/export?format=csv&gid=0'
    for bad in ('http://docs.google.com/spreadsheets/d/' + SHEET_ID, 'https://evil.test/spreadsheets/d/' + SHEET_ID,
                'https://docs.google.com.evil.test/spreadsheets/d/' + SHEET_ID,
                'https://docs.google.com/spreadsheets/d/short', 'file:///etc/passwd', 'http://127.0.0.1:8080/',
                'https://docs.google.com/spreadsheets/d/' + SHEET_ID + '@evil.test', ''):
        with pytest.raises(ValueError):
            export_url(bad)


def mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


async def test_public_sheet_csv_is_fetched_from_the_built_export_url():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, headers={'content-type': 'text/csv; charset=utf-8'}, content=b'email\na@ok.test\n')

    link = f'https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit?x=1#gid=7'
    async with mock_client(handler) as client:
        assert await fetch_sheet_csv(link, client) == b'email\na@ok.test\n'
    assert seen == [f'https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=7']


async def test_redirect_to_a_google_sign_in_or_other_host_is_refused_and_never_followed():
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(302, headers={'location': 'https://accounts.google.com/ServiceLogin?x=1'})

    link = f'https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit'
    async with mock_client(handler) as client:
        with pytest.raises(ValueError, match='Anyone with the link'):
            await fetch_sheet_csv(link, client)
    assert seen == ['docs.google.com']

    def internal(request):
        return httpx.Response(302, headers={'location': 'http://169.254.169.254/latest/meta-data'})

    async with mock_client(internal) as client:
        with pytest.raises(ValueError):
            await fetch_sheet_csv(link, client)


@pytest.mark.parametrize('response', [
    httpx.Response(404, headers={'content-type': 'text/html'}, content=b'<html>'),
    httpx.Response(200, headers={'content-type': 'text/html'}, content=b'<html>login</html>'),
    httpx.Response(403, headers={'content-type': 'text/csv'}, content=b'x'),
])
async def test_non_csv_and_error_responses_get_the_share_hint(response):
    async with mock_client(lambda request: response) as client:
        with pytest.raises(ValueError) as error:
            await fetch_sheet_csv(f'https://docs.google.com/spreadsheets/d/{SHEET_ID}', client)
    assert str(error.value) == NOT_PUBLIC


async def test_oversized_sheet_is_cut_off():
    big = httpx.Response(200, headers={'content-type': 'text/csv'}, content=b'a' * 2_000_000)
    async with mock_client(lambda request: big) as client:
        with pytest.raises(ValueError, match='1.5 MB'):
            await fetch_sheet_csv(f'https://docs.google.com/spreadsheets/d/{SHEET_ID}', client)


async def test_sheet_route_creates_a_job_and_surfaces_fetch_errors(tmp_path):
    async def good(url):
        return b'email\nann@ok.test\n'

    async def bad(url):
        raise ValueError(NOT_PUBLIC)

    link = f'https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit'
    async with client_for(make_app(tmp_path, sheet_fetcher=good)) as client:
        created = await client.post('/v1/verify/sheet', headers={'X-API-Key': KEY},
                                    json={'url': link, 'check_dns': True})
        assert created.status_code == 202
        job = await wait_done(client, created.json()['job_id'])
        assert job['options']['filename'] == 'google-sheet.csv'
    async with client_for(make_app(tmp_path, sheet_fetcher=bad)) as client:
        failed = await client.post('/v1/verify/sheet', headers={'X-API-Key': KEY}, json={'url': link})
        assert failed.status_code == 422 and failed.json()['detail'] == NOT_PUBLIC
