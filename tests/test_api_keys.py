"""Managed API keys, the MCP connection info, and the aligned MCP tools."""
import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from verifier.api import MAX_KEYS, create_app
from verifier.connect_info import MCP_TOOLS
from verifier.engine import Verifier
from verifier.job_service import JobService
from verifier.mcp_connector import create_mcp
from verifier.policy import ProbeVerdict

SERVER_KEY = 's' * 32


class DNS:
    async def resolve(self, domain, record):
        return [SimpleNamespace(preference=10, exchange='mx.test.')]


class Probe:
    def __init__(self):
        self.calls = []

    async def check(self, email, domain, hosts):
        self.calls.append(email)
        return ProbeVerdict('deliverable_candidate', 'smtp_rcpt_accepted', 250, host='mx.test')


def make_app(tmp_path, api_key=None, **kwargs):
    return create_app(store_path=str(tmp_path / 's.sqlite3'), api_key=api_key,
                      verifier=Verifier(DNS(), Probe()), **kwargs)


def client_for(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url='http://test')


async def make_key(client, name='Sales agent', headers=None):
    response = await client.post('/v1/keys', json={'name': name}, headers=headers or {})
    assert response.status_code == 201, response.text
    return response.json()


async def test_a_created_key_is_unique_shown_once_and_stored_only_as_a_hash(tmp_path):
    async with client_for(make_app(tmp_path)) as client:
        first = await make_key(client, 'First')
        headers = {'X-API-Key': first['key']}
        second = await make_key(client, 'Second', headers)
        assert first['key'] != second['key'] and first['key'].startswith('ls_') and len(first['key']) >= 40
        assert first['prefix'] == first['key'][:8]
        listing = (await client.get('/v1/keys', headers=headers)).json()
        assert [k['name'] for k in listing['keys']] == ['Second', 'First'] and listing['server_key'] is False
        assert all(set(k) == {'id', 'name', 'prefix', 'created_at', 'last_used_at'} for k in listing['keys'])
        assert first['key'] not in (await client.get('/v1/keys', headers=headers)).text
    stored = sqlite3.connect(str(tmp_path / 's.sqlite3')).execute('SELECT * FROM api_keys').fetchall()
    assert len(stored) == 2 and first['key'] not in repr(stored) and second['key'] not in repr(stored)


async def test_auth_turns_on_with_the_first_key_and_each_key_can_be_revoked(tmp_path):
    async with client_for(make_app(tmp_path)) as client:
        assert (await client.get('/v1/config')).json()['auth_required'] is False
        assert (await client.get('/v1/jobs')).status_code == 200            # open until a key exists
        one = await make_key(client, 'One')
        assert (await client.get('/v1/config')).status_code == 401           # now a key is required
        two = await make_key(client, 'Two', {'X-API-Key': one['key']})
        for secret in (one['key'], two['key']):
            assert (await client.get('/v1/jobs', headers={'X-API-Key': secret})).status_code == 200
            assert (await client.get('/v1/jobs', headers={'Authorization': f'Bearer {secret}'})).status_code == 200
        assert (await client.get('/v1/jobs', headers={'X-API-Key': 'ls_' + 'x' * 43})).status_code == 401
        revoked = await client.delete(f"/v1/keys/{one['id']}", headers={'X-API-Key': two['key']})
        assert revoked.status_code == 204
        assert (await client.get('/v1/jobs', headers={'X-API-Key': one['key']})).status_code == 401
        assert (await client.get('/v1/jobs', headers={'X-API-Key': two['key']})).status_code == 200
        assert (await client.delete(f"/v1/keys/{one['id']}", headers={'X-API-Key': two['key']})).status_code == 404


async def test_the_server_key_still_works_alongside_managed_keys_and_cannot_be_deleted(tmp_path):
    async with client_for(make_app(tmp_path, api_key=SERVER_KEY)) as client:
        server = {'X-API-Key': SERVER_KEY}
        assert (await client.post('/v1/keys', json={'name': 'x'})).status_code == 401
        key = await make_key(client, 'App', server)
        assert (await client.get('/v1/keys', headers=server)).json()['server_key'] is True
        assert (await client.get('/v1/jobs', headers={'X-API-Key': key['key']})).status_code == 200
        await client.delete(f"/v1/keys/{key['id']}", headers=server)
        assert (await client.get('/v1/jobs', headers={'X-API-Key': key['key']})).status_code == 401
        assert (await client.get('/v1/jobs', headers=server)).status_code == 200      # server key unaffected
        assert (await client.get('/v1/config', headers=server)).json()['auth_required'] is True


async def test_key_names_are_validated_and_the_number_of_keys_is_capped(tmp_path):
    async with client_for(make_app(tmp_path, api_key=SERVER_KEY)) as client:
        server = {'X-API-Key': SERVER_KEY}
        for bad in ('', '   ', 'x' * 61):
            assert (await client.post('/v1/keys', json={'name': bad}, headers=server)).status_code == 422
        for index in range(MAX_KEYS):
            await make_key(client, f'k{index}', server)
        full = await client.post('/v1/keys', json={'name': 'one more'}, headers=server)
        assert full.status_code == 409


async def test_using_a_managed_key_records_when_it_was_last_used(tmp_path):
    async with client_for(make_app(tmp_path)) as client:
        key = await make_key(client)
        headers = {'X-API-Key': key['key']}
        assert (await client.get('/v1/keys', headers=headers)).json()['keys'][0]['last_used_at'] is not None
        assert key['last_used_at'] is None


async def test_a_managed_key_is_rate_limited_separately_from_the_server_key(tmp_path):
    app = make_app(tmp_path, api_key=SERVER_KEY, rate_limit=2)
    async with client_for(app) as client:
        server = {'X-API-Key': SERVER_KEY}
        key = await make_key(client, 'A', server)           # request 1 on the server identity
        mine = {'X-API-Key': key['key']}
        codes = [(await client.get('/v1/jobs', headers=mine)).status_code for _ in range(3)]
        assert codes == [200, 200, 429]
        assert (await client.get('/v1/jobs', headers=server)).status_code == 200


# ---- MCP connection info ----

async def test_mcp_config_lists_the_real_command_environment_and_every_tool(tmp_path):
    app = make_app(tmp_path, api_key=SERVER_KEY)
    async with client_for(app) as client:
        assert (await client.get('/v1/mcp-config')).status_code == 401
        config = (await client.get('/v1/mcp-config', headers={'X-API-Key': SERVER_KEY})).json()
        assert config['args'] == ['-m', 'verifier.mcp_connector'] and config['command']
        assert config['env']['VERIFIER_DB'].endswith('s.sqlite3') and 'PYTHONPATH' in config['env']
        assert 'VERIFIER_SMTP_ENABLED' not in config['env']      # SMTP is off on this server
    service = JobService(tmp_path / 'm.sqlite3', verifier=Verifier(DNS(), Probe()))
    advertised = {tool.name for tool in await create_mcp(service).list_tools()}
    assert advertised == {name for name, _ in MCP_TOOLS}         # the UI list cannot drift from the server


async def test_mcp_config_includes_the_sender_settings_only_when_smtp_is_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv('VERIFIER_HELO_DOMAIN', 'probe.example.test')
    monkeypatch.setenv('VERIFIER_MAIL_FROM', 'probe@probe.example.test')
    async with client_for(make_app(tmp_path, smtp_enabled=True)) as client:
        env = (await client.get('/v1/mcp-config')).json()['env']
        assert env['VERIFIER_SMTP_ENABLED'] == 'true' and env['VERIFIER_HELO_DOMAIN'] == 'probe.example.test'


# ---- the MCP tools now match the web app ----

async def wait_done(service, job_id):
    async with asyncio.timeout(5):
        while service.get_job(job_id)['status'] not in {'completed', 'failed', 'cancelled'}:
            await asyncio.sleep(0.01)


async def test_mcp_checks_mailboxes_by_default_when_smtp_is_enabled_and_supports_check_dns(tmp_path):
    probe = Probe()
    service = JobService(tmp_path / 'm.sqlite3', verifier=Verifier(DNS(), probe), smtp_enabled=True)
    mcp = create_mcp(service)
    try:
        _, first = await mcp.call_tool('verify_emails', {'emails': ['a@ok.test']})
        await wait_done(service, first['job_id'])
        assert first['options']['allow_smtp'] is True and probe.calls == ['a@ok.test']
        _, offline = await mcp.call_tool('verify_emails', {'emails': ['b@ok.test'], 'allow_smtp': False})
        assert offline['options']['allow_smtp'] is False and offline['options']['check_dns'] is False
        _, domain_only = await mcp.call_tool('verify_emails', {'emails': ['c@ok.test'], 'allow_smtp': False,
                                                                'check_dns': True})
        await wait_done(service, domain_only['job_id'])
        assert service.get_results(domain_only['job_id'])[0]['reason'] == 'mailbox_check_disabled'
    finally:
        await service.close()


async def test_mcp_can_recheck_and_cancel_jobs(tmp_path):
    class Unsure(Probe):
        async def check(self, email, domain, hosts):
            self.calls.append(email)
            if email.startswith('maybe'):
                return ProbeVerdict('unknown', 'smtp_temporary_failure', 451)
            return ProbeVerdict('deliverable_candidate', 'smtp_rcpt_accepted', 250)

    service = JobService(tmp_path / 'm.sqlite3', verifier=Verifier(DNS(), Unsure()), smtp_enabled=True)
    mcp = create_mcp(service)
    try:
        _, job = await mcp.call_tool('verify_emails', {'emails': ['a@ok.test', 'maybe@ok.test']})
        await wait_done(service, job['job_id'])
        _, again = await mcp.call_tool('recheck_verification_job', {'job_id': job['job_id']})
        assert again['total'] == 1 and again['options']['rechecked_from'] == job['job_id']
        await wait_done(service, again['job_id'])
        with pytest.raises(Exception, match='queued or running'):
            await mcp.call_tool('cancel_verification_job', {'job_id': job['job_id']})
    finally:
        await service.close()
