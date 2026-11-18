"""Cancel, re-check, run summary, engine version, download names, honest SMTP outcome."""
import asyncio
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from verifier.api import create_app
from verifier.engine import Verifier
from verifier.job_service import RESULT_VERSION, smtp_outcome_for
from verifier.policy import ProbeVerdict

KEY = 'k' * 32
H = {'X-API-Key': KEY}


class DNS:
    async def resolve(self, domain, record):
        return [SimpleNamespace(preference=10, exchange='mx.test.')]


class Probe:
    """Behaviour chosen by the local part; `delay` makes jobs slow enough to cancel."""

    def __init__(self, delay=0.0):
        self.delay, self.calls = delay, []

    async def check(self, email, domain, hosts):
        self.calls.append(email)
        await asyncio.sleep(self.delay)
        local = email.split('@')[0]
        if local.startswith('temp'):
            return ProbeVerdict('unknown', 'smtp_temporary_failure', 451)
        if local.startswith('blocked'):
            return ProbeVerdict('unknown', 'smtp_blocked_by_policy', 550)
        return ProbeVerdict('deliverable_candidate', 'smtp_rcpt_accepted', 250, host='mx.test')


def client_for(tmp_path, probe=None):
    app = create_app(store_path=str(tmp_path / 's.sqlite3'), api_key=KEY, smtp_enabled=True,
                     verifier=Verifier(DNS(), probe or Probe()))
    return AsyncClient(transport=ASGITransport(app=app), base_url='http://test')


async def submit(client, emails, **data):
    csv = 'name,email\n' + '\n'.join(f'N{i},{e}' for i, e in enumerate(emails)) + '\n'
    response = await client.post('/v1/verify/csv', headers=H, data=data,
                                 files={'file': ('Leads List.csv', csv, 'text/csv')})
    assert response.status_code == 202, response.text
    return response.json()['job_id']


async def wait_for(client, job_id, *statuses):
    for _ in range(200):
        job = (await client.get(f'/v1/jobs/{job_id}', headers=H)).json()
        if job['status'] in statuses:
            return job
        await asyncio.sleep(0.02)
    raise AssertionError(f'job stuck in {job["status"]}')


async def test_a_running_job_can_be_cancelled_and_keeps_what_it_finished(tmp_path):
    emails = [f'p{i}@ok.test' for i in range(12)]
    async with client_for(tmp_path, Probe(delay=0.15)) as client:
        job_id = await submit(client, emails, concurrency='1')
        await asyncio.sleep(0.4)
        cancelled = await client.post(f'/v1/jobs/{job_id}/cancel', headers=H)
        assert cancelled.status_code == 200
        job = await wait_for(client, job_id, 'cancelled')
        assert 0 < job['processed'] < 12
        rows = (await client.get(f'/v1/jobs/{job_id}/results', headers=H)).json()
        assert sum(r['reason'] == 'queued' for r in rows) == 12 - job['processed']
        assert (await client.post(f'/v1/jobs/{job_id}/cancel', headers=H)).status_code == 409
        # a cancelled job can be re-checked: the unchecked rows are picked up
        again = await client.post(f'/v1/jobs/{job_id}/recheck', headers=H, json={'scope': 'unknown'})
        assert again.status_code == 202 and again.json()['total'] == 12 - job['processed']


async def test_cancel_and_recheck_reject_unknown_ids_running_jobs_and_bad_scope(tmp_path):
    async with client_for(tmp_path, Probe(delay=0.2)) as client:
        assert (await client.post('/v1/jobs/nope/cancel', headers=H)).status_code == 404
        assert (await client.post('/v1/jobs/nope/recheck', headers=H)).status_code == 404
        job_id = await submit(client, [f'p{i}@ok.test' for i in range(5)], concurrency='1')
        assert (await client.post(f'/v1/jobs/{job_id}/recheck', headers=H)).status_code == 409
        await client.post(f'/v1/jobs/{job_id}/cancel', headers=H)
        await wait_for(client, job_id, 'cancelled')
    async with client_for(tmp_path) as client:
        done = await submit(client, ['a@ok.test'])
        await wait_for(client, done, 'completed')
        assert (await client.post(f'/v1/jobs/{done}/recheck', headers=H, json={'scope': 'x'})).status_code == 422
        assert (await client.post(f'/v1/jobs/{done}/recheck', headers=H)).status_code == 422  # nothing unknown


async def test_recheck_unknowns_reruns_only_those_and_keeps_lead_columns(tmp_path):
    probe = Probe()
    async with client_for(tmp_path, probe) as client:
        job_id = await submit(client, ['a@ok.test', 'temp1@ok.test', 'temp2@ok.test', 'b@ok.test'])
        await wait_for(client, job_id, 'completed')
        before = len(probe.calls)
        response = await client.post(f'/v1/jobs/{job_id}/recheck', headers=H)
        new = response.json()
        assert response.status_code == 202 and new['total'] == 2
        assert new['options']['rechecked_from'] == job_id and '(re-check)' in new['options']['filename']
        await wait_for(client, new['job_id'], 'completed')
        assert sorted(probe.calls[before:]) == ['temp1@ok.test', 'temp2@ok.test']
        rows = (await client.get(f"/v1/jobs/{new['job_id']}/results", headers=H)).json()
        assert [r['metadata']['name'] for r in rows] == ['N1', 'N2']
        everything = (await client.post(f'/v1/jobs/{job_id}/recheck', headers=H, json={'scope': 'all'})).json()
        assert everything['total'] == 4


async def test_run_summary_counts_reasons_and_jobs_record_the_engine_version(tmp_path):
    async with client_for(tmp_path) as client:
        job_id = await submit(client, ['a@ok.test', 'blocked1@ok.test', 'blocked2@ok.test', 'temp@ok.test'])
        job = await wait_for(client, job_id, 'completed')
        assert job['reason_counts'] == {'smtp_rcpt_accepted': 1, 'smtp_blocked_by_policy': 2,
                                        'smtp_temporary_failure': 1}
        assert job['options']['engine'] == RESULT_VERSION
        listed = (await client.get('/v1/jobs', headers=H)).json()[0]
        assert listed['reason_counts'] == job['reason_counts']
        assert (await client.get('/v1/config', headers=H)).json()['engine'] == RESULT_VERSION


async def test_download_is_named_after_the_uploaded_file(tmp_path):
    async with client_for(tmp_path) as client:
        job_id = await submit(client, ['a@ok.test'])
        await wait_for(client, job_id, 'completed')
        full = await client.get(f'/v1/jobs/{job_id}/results.csv', headers=H)
        assert full.headers['content-disposition'] == 'attachment; filename="Leads-List-all.csv"'
        sent = await client.get(f'/v1/jobs/{job_id}/results.csv?send_only=true', headers=H)
        assert 'Leads-List-send-eligible.csv' in sent.headers['content-disposition']


@pytest.mark.parametrize(('allow', 'reason', 'outcome'), [
    (False, 'verification_disabled', 'not_run'),
    (True, 'smtp_rcpt_accepted', 'answered'), (True, 'smtp_mailbox_rejected', 'answered'),
    (True, 'catch_all_domain', 'answered'),
    (True, 'smtp_blocked_by_policy', 'refused'),
    (True, 'smtp_unreachable', 'unreachable'), (True, 'smtp_unresolved', 'unreachable'),
    (True, 'invalid_syntax', 'skipped'), (True, 'null_mx', 'skipped'), (True, 'domain_not_found', 'skipped'),
])
def test_smtp_outcome_is_derived_from_the_result_not_the_request(allow, reason, outcome):
    assert smtp_outcome_for(allow, reason) == outcome


async def test_single_check_reports_what_actually_happened_at_the_mailbox_step(tmp_path):
    async with client_for(tmp_path) as client:
        async def one(email, **extra):
            return (await client.post('/v1/verify/email', headers=H, json={'email': email, **extra})).json()
        ok = await one('a@ok.test')
        assert (ok['smtp_outcome'], ok['smtp_checked']) == ('answered', True)
        blocked = await one('blocked@ok.test')
        assert (blocked['smtp_outcome'], blocked['smtp_checked']) == ('refused', False)
        bad = await one('not-an-email')
        assert (bad['smtp_outcome'], bad['dns_checked']) == ('skipped', False)
        off = await one('a@ok.test', allow_smtp=False)
        assert (off['smtp_outcome'], off['smtp_checked']) == ('not_run', False)
