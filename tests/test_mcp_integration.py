"""MCP-facing contract tests; no external addresses are checked."""
import asyncio
import csv
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from verifier.engine import Verifier
from verifier.job_service import JobService
from verifier.mcp_connector import create_mcp, submit_csv
from verifier.policy import ProbeVerdict

PROJECT = Path(__file__).resolve().parents[1]


class DNS:
    def __init__(self):
        self.calls = []

    async def resolve(self, domain, record):
        self.calls.append((domain, record))
        if domain == 'no-mail.test':
            return [SimpleNamespace(preference=0, exchange='.')]
        if domain == 'temp.test':
            import dns.resolver
            raise dns.resolver.Timeout()
        return [SimpleNamespace(preference=10, exchange='mx.test.')]

    async def resolve_addresses(self, host):
        return ['127.0.0.1']


class SMTP:
    def __init__(self):
        self.calls = []

    async def check(self, email, domain, mx_hosts):
        self.calls.append(email)
        if email.startswith('bad@'):
            raise TimeoutError()
        if email.startswith('catchall@'):
            return ProbeVerdict('unknown', 'catch_all_domain', 250, ['catch_all'])
        return ProbeVerdict('deliverable_candidate', 'smtp_rcpt_accepted', 250, host='fixture.mx')


def service(tmp_path, **kwargs):
    return JobService(tmp_path / 'mcp.sqlite3', verifier=Verifier(DNS(), SMTP()), **kwargs)


async def wait_for_job(service, job_id):
    async with asyncio.timeout(5):
        while service.get_job(job_id)['status'] not in {'completed', 'failed'}:
            await asyncio.sleep(0.005)
    return service.get_job(job_id)


def parsed(result):
    if isinstance(result, tuple):  # structured tools return (content blocks, structured dict)
        result = result[0]
    assert result and result[0].type == 'text'
    return json.loads(result[0].text)


async def test_offline_default_cannot_use_the_network(tmp_path):
    worker = service(tmp_path)
    job = await wait_for_job(worker, worker.submit_emails(['Jane@Example.test', 'bad']))
    try:
        assert job['processed'] == 2
        rows = worker.get_results(job['id'])
        assert rows[0]['status'] == 'unknown' and rows[0]['reason'] == 'verification_disabled'
        assert not rows[0]['send_eligible']
        assert rows[1]['reason'] == 'invalid_syntax'
        assert not worker.verifier.dns.calls and not worker.verifier.smtp.calls
    finally:
        await worker.close()


async def test_one_error_does_not_fail_the_job_or_promote_unknown(tmp_path):
    worker = service(tmp_path, smtp_enabled=True)
    job = await wait_for_job(worker, worker.submit_emails(
        ['jane@example.test', 'bad@example.test', 'a@no-mail.test', 'a@temp.test'],
        allow_smtp=True,
    ))
    try:
        assert job['status'] == 'completed'
        rows = worker.get_results(job['id'])
        assert rows[0]['send_eligible']
        assert rows[1]['status'] == 'unknown' and rows[1]['reason'] == 'verification_error'
        assert rows[2]['reason'] == 'null_mx'
        assert rows[3]['reason'] == 'dns_timeout'
        assert not any(row['send_eligible'] for row in rows[1:])
    finally:
        await worker.close()


async def test_smtp_is_rejected_by_default_without_network(tmp_path):
    worker = service(tmp_path)
    try:
        with pytest.raises(PermissionError):
            worker.submit_emails(['jane@example.test'], allow_smtp=True)
        assert not worker.verifier.dns.calls
    finally:
        await worker.close()


async def test_csv_job_and_export_preserve_original_business_data(tmp_path):
    worker = service(tmp_path, smtp_enabled=True)
    mcp = create_mcp(worker)
    job = await submit_csv(worker, 'business_name,email\nA Co,jane@example.test\nB Co,bad@example.test\n', allow_smtp=True)
    await wait_for_job(worker, job)
    try:
        result = parsed(await mcp.call_tool('get_verification_results', {
            'job_id': job, 'send_only': True,
        }))
        assert [row['email'] for row in result['results']] == ['jane@example.test']
        export = parsed(await mcp.call_tool('export_verification_csv', {
            'job_id': job, 'send_only': True,
        }))
        assert Path(export['path']).is_file()
        with Path(export['path']).open(newline='') as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == 1
        assert rows[0]['lead_business_name'] == 'A Co'
        assert rows[0]['send_eligible'] == 'True'
        assert export['count'] == 1
        assert export['zero_bounce_guarantee'] is False
    finally:
        await worker.close()


async def test_bounce_is_suppressed_before_dns_or_smtp(tmp_path):
    worker = service(tmp_path, smtp_enabled=True)
    worker.record_bounce('jane@example.test')
    job = await wait_for_job(worker, worker.submit_emails(['jane@example.test'], allow_smtp=True))
    try:
        result = worker.get_results(job['id'])[0]
        assert result['reason'] == 'suppressed_prior_bounce' and not result['send_eligible']
        assert not worker.verifier.dns.calls and not worker.verifier.smtp.calls
    finally:
        await worker.close()


async def test_real_stdio_client_discovers_tools_and_rejects_live_smtp(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable, args=['-m', 'verifier.mcp_connector'],
        env={'PYTHONPATH': str(PROJECT), 'VERIFIER_DB': str(tmp_path / 'stdio.sqlite3'),
             'VERIFIER_EXPORT_DIR': str(tmp_path / 'exports'),
             'VERIFIER_SMTP_ENABLED': 'false'},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == {
                'verify_csv', 'verify_emails', 'get_verification_job',
                'get_verification_results', 'export_verification_csv',
                'cancel_verification_job', 'recheck_verification_job',
                'record_hard_bounce', 'get_suppression',
            }
            response = await client.call_tool('verify_emails', {
                'emails': ['bad', 'jane@example.test'], 'allow_smtp': False,
            })
            assert not response.isError, response
            assert response.structuredContent['total'] == 2
            job_id = response.structuredContent['job_id']
            async with asyncio.timeout(5):
                while True:
                    status = await client.call_tool('get_verification_job', {'job_id': job_id})
                    if status.structuredContent['status'] == 'completed':
                        break
                    await asyncio.sleep(0.01)
            results = await client.call_tool('get_verification_results', {
                'job_id': job_id, 'send_only': True,
            })
            assert results.structuredContent['results'] == []
            denied = await client.call_tool('verify_emails', {
                'emails': ['jane@example.test'], 'allow_smtp': True,
            })
            assert denied.isError
