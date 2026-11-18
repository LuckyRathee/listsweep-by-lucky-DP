import csv
import io
import socket
from types import SimpleNamespace

from contextlib import asynccontextmanager

import dns.asyncresolver
import dns.resolver
import pytest

from verifier.store import Store
from aiosmtpd.controller import Controller
from aiosmtpd.handlers import Message
from httpx import ASGITransport, AsyncClient

from verifier.api import create_app
from verifier.engine import Verifier
from verifier.smtp import AsyncSMTPProbe


class DNS:
    async def resolve(self, domain, record):
        if record == 'MX':
            if domain == 'null.test':
                return [SimpleNamespace(preference=0, exchange='.')]
            if domain == 'dns.test':
                raise dns.resolver.Timeout()
            return [SimpleNamespace(preference=10, exchange='mx.test.')]
        raise dns.resolver.NoAnswer()

    async def resolve_addresses(self, host):
        return ['127.0.0.1']


class SMTPHandler(Message):
    async def handle_message(self, message):
        return None

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        address = address.lower()
        if address.startswith('notfound@'):
            return '550 5.1.1 mailbox unavailable'
        if address.startswith('timeout@'):
            return '451 4.7.1 greylisted'
        if address.startswith('jane@') or address.startswith('info@'):
            if address.startswith('evr-'):
                return '550 5.1.1 unknown'
            envelope.rcpt_tos.append(address)
            return '250 OK'
        if address.endswith('@catch.test'):
            envelope.rcpt_tos.append(address)
            return '250 OK'
        return '550 5.1.1 unknown'


@pytest.fixture
async def smtp_server():
    with socket.socket() as probe_socket:
        probe_socket.bind(('127.0.0.1', 0))
        port = probe_socket.getsockname()[1]
    controller = Controller(SMTPHandler(), hostname='127.0.0.1', port=port)
    controller.start()
    try:
        yield port
    finally:
        controller.stop()


@pytest.fixture
async def client(tmp_path, smtp_server):
    async with make_client(tmp_path, smtp_server) as session:
        yield session


@asynccontextmanager
async def make_client(tmp_path, smtp_server):
    dns = DNS()
    probe = AsyncSMTPProbe(dns, timeout=2, min_host_interval=0,
                           port=smtp_server, allow_private=True)
    verifier = Verifier(dns, probe, disposable={'trash.test'}, concurrency=5)
    app = create_app(store_path=str(tmp_path / 'test.sqlite3'), verifier=verifier,
                     smtp_enabled=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as session:
        yield session


class TestAPI:
    def test_store_persists_options_and_lists_resumable_jobs(self, tmp_path):
        from verifier.csv_input import parse_csv

        rows, duplicates, error = parse_csv(b'email\na@example.test\nb@example.test\n')
        assert error is None
        store = Store(str(tmp_path / 'resume.sqlite3'))
        job_id = store.create_job(rows, duplicates, {'allow_smtp': True, 'concurrency': 4})
        store.set_job_status(job_id, 'running')
        store.save_result(job_id, rows[0].position, {
            'status': 'unknown', 'reason': 'dns_timeout', 'normalized_email': 'a@example.test'
        })
        completed_id = store.create_job(rows[:1], 0, {'allow_smtp': False, 'concurrency': 2})
        store.set_job_status(completed_id, 'completed')

        incomplete = store.incomplete()
        assert [job['id'] for job in incomplete] == [job_id]
        assert incomplete[0]['options'] == {'allow_smtp': True, 'concurrency': 4}
        assert [row['position'] for row in store.pending(job_id)] == [rows[1].position]

    async def test_health_does_not_require_authentication(self, tmp_path):
        app = create_app(store_path=str(tmp_path / 'test.sqlite3'), verifier=object(),
                         api_key='x' * 32)
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/healthz')
            assert response.status_code == 200
            assert response.json() == {'ok': True, 'service': 'local-email-verifier'}


async def test_csv_job_to_send_eligible_export(client):
    data = 'name,email\nA,Jane@Example.test\nB,catchall@catch.test\nC,notfound@example.test\n'
    data += 'D,timeout@example.test\nE,not-an-email\nF,info@example.test\n'
    data += 'G,foo@trash.test\nH,x@null.test\nI,x@dns.test\n'
    response = await client.post('/v1/verify/csv', files={'file': ('leads.csv', data, 'text/csv')},
                                 data={'allow_smtp': 'true', 'concurrency': '4'})
    assert response.status_code == 202, response.text
    job_id = response.json()['job_id']
    for _ in range(20):
        status = (await client.get(f'/v1/jobs/{job_id}')).json()
        if status['status'] == 'completed':
            break
        await __import__('asyncio').sleep(0.05)
    assert status['processed'] == status['total'] == 9
    results = (await client.get(f'/v1/jobs/{job_id}/results')).json()
    reasons = {r['original_email']: r['reason'] for r in results}
    assert reasons == {
        'Jane@Example.test': 'smtp_rcpt_accepted',
        'catchall@catch.test': 'catch_all_domain',
        'notfound@example.test': 'smtp_mailbox_rejected',
        'timeout@example.test': 'smtp_temporary_failure',
        'not-an-email': 'invalid_syntax',
        'info@example.test': 'smtp_rcpt_accepted',
        'foo@trash.test': 'disposable_domain',
        'x@null.test': 'null_mx', 'x@dns.test': 'dns_timeout',
    }
    assert [(r['email'], r['send_eligible']) for r in (await client.get(
        f'/v1/jobs/{job_id}/results?send_only=true')).json()] == [
            ('Jane@example.test', True)]
    export = await client.get(f'/v1/jobs/{job_id}/results.csv?send_only=true')
    assert list(csv.DictReader(io.StringIO(export.text)))[0]['email'] == 'Jane@example.test'
    assert 'not-an-email' in (await client.get(f'/v1/jobs/{job_id}/results.csv')).text


async def test_upload_rejects_bad_column_and_enforces_api_key(tmp_path, smtp_server):
    dns = DNS()
    verifier = Verifier(dns, AsyncSMTPProbe(dns, allow_private=True, port=smtp_server))
    app = create_app(store_path=str(tmp_path / 'test.sqlite3'), verifier=verifier, api_key='k' * 32)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.get('/healthz')).status_code == 200
        assert (await client.get('/v1/jobs/unknown')).status_code == 401
        response = await client.post('/v1/verify/csv', headers={'X-API-Key': 'k' * 32},
                                    files={'file': ('a.csv', 'name,website\nA,example.com', 'text/csv')})
        assert response.status_code == 422
        assert 'email column' in response.json()['detail']
