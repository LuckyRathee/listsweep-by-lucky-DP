"""Local MCP connector: job-based verification, not email sending.

Use stdio locally. Do not expose stdio to a network. No DATA command or outbound
message is issued. A candidate result is inconclusive evidence, not deliverability proof.
"""
import asyncio
import csv
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .csv_input import export_all_csv, export_send_csv
from .job_service import JobService

NOTE = 'send_only is a conservative candidate filter, not consent, permission to send cold email, or a zero-bounce guarantee. Unknown/catch-all/temporary failures are never promoted to valid.'
PROJECT = Path(__file__).resolve().parent.parent
DISCLAIMER = {
    'status_semantics': 'deliverable_candidate = specific SMTP RCPT 250 with non-catch-all control; unknown = not proven either way; undeliverable = evidence of definite failure',
    'zero_bounce_guarantee': False,
}


def make_service():
    home = Path(os.getenv('HERMES_HOME', str(Path.home() / '.hermes')))
    db = Path(os.getenv('VERIFIER_DB', str(home / 'email-verifier' / 'jobs.sqlite3')))
    db.parent.mkdir(parents=True, exist_ok=True)
    return JobService(db)


async def submit_csv(service, content, allow_smtp=False, concurrency=4):
    if isinstance(content, str):
        content = content.encode('utf-8')
    return service.submit_csv(content, allow_smtp=allow_smtp, concurrency=concurrency)


def create_mcp(service):
    @asynccontextmanager
    async def lifecycle(_):
        try:
            yield
        finally:
            await service.close()

    mcp = FastMCP('email_verifier', instructions=NOTE, lifespan=lifecycle)

    def summary(job_id):
        job = service.get_job(job_id)
        return {**job, 'job_id': job_id, **DISCLAIMER}

    @mcp.tool(description='Start one verification job for up to 2,000 unique email strings. Leave allow_smtp false for offline syntax-only checks. DNS and SMTP only occur when explicitly configured. ' + NOTE)
    async def verify_emails(emails: list[str], allow_smtp: bool = False, concurrency: int = 4) -> dict[str, Any]:
        job_id = service.submit_emails(emails, allow_smtp, concurrency)
        return summary(job_id)

    @mcp.tool(description='Verify a local CSV or supplied UTF-8 CSV text containing an email column. Maximum 2,000 unique addresses. Other business columns are retained for review/export. ' + NOTE)
    async def verify_csv(csv_path: str | None = None, csv_text: str | None = None,
                         allow_smtp: bool = False, concurrency: int = 4) -> dict[str, Any]:
        if bool(csv_path) == bool(csv_text):
            raise ValueError('Supply exactly one of csv_path or csv_text')
        if csv_path:
            allowed = [PROJECT, Path.home() / 'Downloads', Path.home() / 'Desktop',
                       Path(os.getenv('VERIFIER_INPUT_DIR', str(PROJECT / 'data')))]
            path = Path(csv_path).expanduser().resolve(strict=False)
            if not any(path.is_relative_to(parent.resolve()) for parent in allowed):
                raise PermissionError('CSV path must be within the verifier project or your Downloads/Desktop folders')
            if path.suffix.lower() != '.csv':
                raise ValueError('Only .csv files are accepted')
            content = path.read_bytes()
        else:
            content = csv_text.encode('utf-8')
        return summary(service.submit_csv(content, allow_smtp, concurrency))

    @mcp.tool(description='Read verification job status and aggregate counts. Read-only. ' + NOTE)
    async def get_verification_job(job_id: str) -> dict[str, Any]:
        job = service.get_job(job_id)
        if not job:
            raise KeyError('job_not_found')
        return summary(job_id)

    @mcp.tool(description='Read a page of persisted results. send_only=true includes only conservative candidates, not permission to send or guaranteed live mailboxes. ' + NOTE)
    async def get_verification_results(job_id: str, send_only: bool = False,
                                      offset: int = 0, limit: int = 1000) -> dict[str, Any]:
        return {'job_id': job_id, 'send_only': send_only, 'offset': offset,
                'results': service.get_results(job_id, send_only, offset, limit), **DISCLAIMER}

    @mcp.tool(description='Write a UTF-8 CSV report to a new file. send_only filters candidates; it does not prove delivery or establish sending consent. ' + NOTE)
    async def export_verification_csv(job_id: str, send_only: bool = False) -> dict[str, Any]:
        job = service.get_job(job_id)
        if job is None:
            raise KeyError('job_not_found')
        if job['status'] != 'completed':
            raise ValueError('Wait until the job is completed before export')
        rows = [r for r in service.store.results(job_id) if r['send_eligible'] or not send_only]
        directory = Path(os.getenv('VERIFIER_EXPORT_DIR', str(PROJECT / 'exports'))).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        output = directory / f'{job_id}-{"candidates" if send_only else "all"}.csv'
        if output.exists():
            raise FileExistsError('Export already exists; choose a new job instead of overwriting')
        text = export_send_csv(rows) if send_only else export_all_csv(rows)
        with output.open('x', encoding='utf-8', newline='') as stream:
            stream.write(text)
        return {'path': str(output), 'count': len(rows), **DISCLAIMER}

    @mcp.tool(description='Record observed HARD bounce feedback into the local suppression list. Expected hard bounces, never arbitrary flags or guessed addresses. Contact owner is responsible for lawful compliance.')
    async def record_hard_bounce(email: str) -> dict[str, Any]:
        service.record_bounce(email)
        return {'email': email, 'recorded': True, 'suppression': service.store.suppressed(email)}

    @mcp.tool(description='Read whether an address is in the local bounce suppression list. Read-only.')
    async def get_suppression(email: str) -> dict[str, Any]:
        return {'email': email, 'suppression': service.store.suppressed(email)}

    return mcp


def main():
    create_mcp(make_service()).run(transport='stdio')


if __name__ == '__main__':
    main()
