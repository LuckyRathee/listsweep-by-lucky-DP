import asyncio

import pytest

from verifier.job_service import JobService


async def wait_for_job(service, job_id):
    async with asyncio.timeout(2):
        while service.get_job(job_id)["status"] not in {"completed", "failed"}:
            await asyncio.sleep(0.005)
    return service.get_job(job_id)


async def test_offline_verification_is_syntax_only_and_never_uses_network(tmp_path):
    class NetworkMustNotRun:
        def initial(self, email):
            raise AssertionError("the network verifier was called")

        async def verify(self, email, allow_smtp=False):
            raise AssertionError("DNS or SMTP was called")

    service = JobService(tmp_path / "jobs.sqlite3", verifier=NetworkMustNotRun())
    job_id = service.submit_emails(["Jane@Example.test", "not-an-email"])
    try:
        status = await wait_for_job(service, job_id)
        assert status["status"] == "completed"
        assert status["processed"] == status["total"] == 2
        results = service.get_results(job_id)
        assert results[0]["status"] == "unknown"
        assert results[0]["reason"] == "verification_disabled"
        assert results[0]["send_eligible"] is False
        assert results[1]["status"] == "undeliverable"
        assert results[1]["reason"] == "invalid_syntax"
    finally:
        await service.close()

async def test_a_failed_item_does_not_fail_the_whole_job(tmp_path):
    from verifier.job_service import JobService
    from verifier.engine import Verifier
    class DNS:
        async def resolve(self, domain, kind):
            from types import SimpleNamespace
            return [SimpleNamespace(preference=10, exchange='mx.test.')]
    class SMTP:
        async def check(self, email, domain, hosts):
            from verifier.policy import ProbeVerdict
            if email.startswith('bad@'):
                raise TimeoutError()
            return ProbeVerdict('deliverable_candidate', 'smtp_rcpt_accepted', 250)
    service = JobService(tmp_path / 'errors.sqlite3', verifier=Verifier(DNS(), SMTP()),
                         smtp_enabled=True)
    job_id = service.submit_emails(['jane@example.test', 'bad@example.test'], allow_smtp=True)
    try:
        status = await wait_for_job(service, job_id)
        assert status['status'] == 'completed'
        rows = service.get_results(job_id)
        assert rows[0]['send_eligible'] is True
        assert rows[1]['status'] == 'unknown' and not rows[1]['send_eligible']
    finally:
        await service.close()
