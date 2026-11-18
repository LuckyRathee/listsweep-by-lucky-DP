"""Bounded asynchronous verification jobs used by the MCP connector."""
import asyncio
import os

from .csv_input import InputRow, parse_csv
from .dns_checks import DNSResolver
from .engine import Verifier
from .smtp import AsyncSMTPProbe
from .smtp_config import SMTPSettings
from .store import Store
from .syntax import normalize


def domain_key(email: str) -> str:
    """Treat domain names case-insensitively without collapsing local-part casing."""
    normalized = normalize(email)
    if not normalized:
        return email.strip()
    return normalized[:normalized.rfind('@')] + '@' + normalized.rsplit('@', 1)[1].lower()


class JobService:
    def __init__(self, store_path, verifier=None, smtp_enabled=None):
        self.store = Store(str(store_path))
        if verifier is None:
            dns = DNSResolver()
            settings = SMTPSettings.from_environment()
            enabled = os.getenv('VERIFIER_SMTP_ENABLED', '').lower() == 'true'
            smtp = AsyncSMTPProbe(
                dns, helo_domain=settings.helo_domain, mail_from=settings.mail_from,
            ) if enabled and settings.ready else None
            verifier = Verifier(dns, smtp)
        self.verifier = verifier
        if smtp_enabled is None:
            smtp_enabled = os.getenv('VERIFIER_SMTP_ENABLED', '').lower() == 'true'
        self.smtp_enabled = bool(smtp_enabled)
        self._tasks = {}

    @staticmethod
    def dedupe(emails):
        if not isinstance(emails, list) or not emails or len(emails) > 4000:
            raise ValueError('emails must contain 1 to 4,000 address strings')
        rows, seen = [], set()
        duplicates = 0
        for position, value in enumerate(emails, 1):
            if not isinstance(value, str) or len(value) > 320:
                raise ValueError('Each address must be a string of at most 320 characters')
            original = value.strip()
            if not original:
                continue
            normalized = normalize(original)
            key = domain_key(original)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            rows.append(InputRow(position, normalized or original, original, {}))
            if len(rows) > 2000:
                raise ValueError('Maximum 2,000 unique addresses per job')
        if not rows:
            raise ValueError('No email addresses supplied')
        return rows, duplicates

    def _check_concurrency(self, concurrency):
        if type(concurrency) is not int or not 1 <= concurrency <= 25:
            raise ValueError('concurrency must be an integer from 1 through 25')

    def require_smtp_if_requested(self, allow_smtp):
        if type(allow_smtp) is not bool:
            raise TypeError('allow_smtp must be true or false')
        if allow_smtp and not self.smtp_enabled:
            raise PermissionError('SMTP is disabled; set VERIFIER_SMTP_ENABLED=true and configure VERIFIER_HELO_DOMAIN and VERIFIER_MAIL_FROM')

    def submit_emails(self, emails, allow_smtp=False, concurrency=4):
        rows, duplicates = self.dedupe(emails)
        self._check_concurrency(concurrency)
        self.require_smtp_if_requested(allow_smtp)
        job_id = self.store.create_job(rows, duplicates, {
            'allow_smtp': allow_smtp, 'concurrency': concurrency,
        })
        self.schedule(job_id)
        return job_id

    def submit_csv(self, content, allow_smtp=False, concurrency=4, filename='agent.csv'):
        if not isinstance(content, bytes) or not content:
            raise ValueError('CSV input is required')
        if len(content) > 1_500_000:
            raise ValueError('CSV exceeds the 1.5 MB limit')
        rows, duplicates, error = parse_csv(content)
        if error:
            raise ValueError(error)
        self._check_concurrency(concurrency)
        self.require_smtp_if_requested(allow_smtp)
        job_id = self.store.create_job(rows, duplicates, {
            'allow_smtp': allow_smtp, 'concurrency': concurrency, 'filename': filename,
        })
        self.schedule(job_id)
        return job_id

    def schedule(self, job_id):
        job = self.store.job(job_id)
        if not job:
            raise KeyError('job_not_found')
        options = job['options']
        self.require_smtp_if_requested(options['allow_smtp'])
        loop = asyncio.get_running_loop()
        tasks = self._tasks.setdefault(loop, set())
        if any(task.get_name() == f'verify-{job_id}' and not task.done() for task in tasks):
            return
        task = loop.create_task(self._run_job(job_id, options), name=f'verify-{job_id}')
        tasks.add(task)
        task.add_done_callback(lambda completed: (tasks.discard(completed), completed.exception()))

    async def _run_job(self, job_id, options):
        self.store.set_job_status(job_id, 'running')
        semaphore = asyncio.Semaphore(options['concurrency'])

        async def worker(row):
            async with semaphore:
                email = row['email']
                normalized = normalize(email)
                try:
                    if not normalized:
                        result = {'normalized_email': None, 'status': 'undeliverable', 'reason': 'invalid_syntax'}
                    elif self.store.suppressed(normalized):
                        result = {
                            'normalized_email': normalized, 'domain': normalized.rsplit('@', 1)[1],
                            'status': 'undeliverable', 'reason': 'suppressed_prior_bounce',
                        }
                    elif not options['allow_smtp']:
                        result = {
                            'normalized_email': normalized, 'domain': normalized.rsplit('@', 1)[1],
                            'status': 'unknown', 'reason': 'verification_disabled',
                        }
                    else:
                        result = await self.verifier.verify(normalized, allow_smtp=True)
                except Exception:
                    result = {
                        'normalized_email': normalized, 'status': 'unknown',
                        'reason': 'verification_error',
                    }
                self.store.save_result(job_id, row['position'], result)

        try:
            await asyncio.gather(*(worker(row) for row in self.store.pending(job_id)))
        except asyncio.CancelledError:
            self.store.set_job_status(job_id, 'queued')
            raise
        except Exception as error:
            self.store.set_job_status(job_id, 'failed', error=str(error))
            raise
        self.store.set_job_status(job_id, 'completed', processed=self.store.job(job_id)['processed'])

    def get_job(self, job_id):
        return self.store.job(job_id)

    def get_results(self, job_id, send_only=False, offset=0, limit=1000):
        if not self.store.job(job_id):
            raise KeyError('job_not_found')
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('offset must be non-negative and limit must be 1 through 1,000')
        rows = self.store.results(job_id)
        if send_only:
            rows = [row for row in rows if row['send_eligible']]
        return rows[offset:offset + limit]

    def record_bounce(self, email):
        normalized = normalize(email)
        if not normalized:
            raise ValueError('A valid address is required for hard-bounce feedback')
        self.store.suppress(normalized, 'hard_bounce', 'agent_feedback')

    async def close(self):
        loop = asyncio.get_running_loop()
        tasks = self._tasks.pop(loop, set())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
