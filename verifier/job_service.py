"""Bounded asynchronous verification jobs used by the MCP connector."""
import asyncio
import os

from .csv_input import InputRow, parse_csv
from .dns_checks import DNSResolver
from .engine import ROLE_LOCALS, Verifier
from .smtp import AsyncSMTPProbe
from .smtp_config import SMTPSettings
from .store import Store
from .syntax import normalize

# Bump when result semantics change, so older jobs can be flagged and re-checked.
# 1 = before the SMTP/DNS classification fixes; 2 = current.
RESULT_VERSION = 2


def domain_key(email: str) -> str:
    """Treat domain names case-insensitively without collapsing local-part casing."""
    normalized = normalize(email)
    if not normalized:
        return email.strip()
    return normalized[:normalized.rfind('@')] + '@' + normalized.rsplit('@', 1)[1].lower()


SMTP_ANSWERED = {
    'smtp_rcpt_accepted', 'smtp_mailbox_rejected', 'catch_all_domain', 'smtp_control_inconclusive',
    'smtp_ambiguous_response', 'smtp_connection_dropped', 'smtp_temporary_failure',
}
SMTP_UNREACHABLE = {'smtp_unreachable', 'smtp_unresolved', 'smtp_non_public_target'}


def smtp_outcome_for(allow_smtp: bool, reason: str) -> str:
    """What really happened at the mailbox step, from the result and not from the request."""
    if not allow_smtp:
        return 'not_run'
    if reason in SMTP_ANSWERED:
        return 'answered'
    if reason == 'smtp_blocked_by_policy':
        return 'refused'
    if reason in SMTP_UNREACHABLE:
        return 'unreachable'
    return 'skipped'  # stopped earlier (syntax, DNS, suppression) so SMTP was never attempted


class JobService:
    def __init__(self, store_path, verifier=None, smtp_enabled=None):
        self.store = Store(str(store_path))
        if verifier is None:
            dns = DNSResolver()
            settings = SMTPSettings.from_environment()
            enabled = os.getenv('VERIFIER_SMTP_ENABLED', '').lower() == 'true'
            if enabled and not settings.ready:
                raise RuntimeError(
                    'VERIFIER_SMTP_ENABLED=true needs a real sender identity '
                    f'(VERIFIER_HELO_DOMAIN and VERIFIER_MAIL_FROM): {settings.reason}')
            smtp = AsyncSMTPProbe(
                dns, helo_domain=settings.helo_domain, mail_from=settings.mail_from,
            ) if enabled and settings.ready else None
            verifier = Verifier(dns, smtp)
        self.verifier = verifier
        if smtp_enabled is None:
            smtp_enabled = os.getenv('VERIFIER_SMTP_ENABLED', '').lower() == 'true'
        self.smtp_enabled = bool(smtp_enabled)
        self._tasks = {}
        self._cancelled = set()

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

    @staticmethod
    def _check_dns_flag(check_dns):
        if type(check_dns) is not bool:
            raise TypeError('check_dns must be true or false')

    def submit_emails(self, emails, allow_smtp=False, concurrency=4, check_dns=False):
        rows, duplicates = self.dedupe(emails)
        self._check_concurrency(concurrency)
        self._check_dns_flag(check_dns)
        self.require_smtp_if_requested(allow_smtp)
        job_id = self.store.create_job(rows, duplicates, {
            'allow_smtp': allow_smtp, 'concurrency': concurrency, 'check_dns': check_dns,
            'engine': RESULT_VERSION,
        })
        self.schedule(job_id)
        return job_id

    def submit_csv(self, content, allow_smtp=False, concurrency=4, filename='agent.csv',
                   check_dns=False):
        if not isinstance(content, bytes) or not content:
            raise ValueError('CSV input is required')
        if len(content) > 1_500_000:
            raise ValueError('CSV exceeds the 1.5 MB limit')
        rows, duplicates, error = parse_csv(content)
        if error:
            raise ValueError(error)
        self._check_concurrency(concurrency)
        self._check_dns_flag(check_dns)
        self.require_smtp_if_requested(allow_smtp)
        job_id = self.store.create_job(rows, duplicates, {
            'allow_smtp': allow_smtp, 'concurrency': concurrency, 'filename': filename,
            'check_dns': check_dns, 'engine': RESULT_VERSION,
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
        task.add_done_callback(
            lambda completed: (tasks.discard(completed), completed.cancelled() or completed.exception()))

    async def check_address(self, email, allow_smtp=False, check_dns=False):
        """Check one address with the same rules a job uses. Returns (normalized, result)."""
        normalized = normalize(email)
        try:
            if not normalized:
                result = {'normalized_email': None, 'status': 'undeliverable', 'reason': 'invalid_syntax'}
            elif self.store.suppressed(normalized):
                result = {
                    'normalized_email': normalized, 'domain': normalized.rsplit('@', 1)[1],
                    'status': 'undeliverable', 'reason': 'suppressed_prior_bounce',
                }
            elif not allow_smtp and check_dns:
                # DNS evidence only (MX, null MX, NXDOMAIN); never a mailbox claim.
                result = await self.verifier.verify(normalized, allow_smtp=False)
            elif not allow_smtp:
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
        return normalized, result

    async def verify_one(self, email, allow_smtp=False, check_dns=False):
        """Verify a single address immediately (no job) and describe it for display."""
        if not isinstance(email, str) or not email.strip() or len(email) > 320:
            raise ValueError('Enter one email address of at most 320 characters')
        self._check_dns_flag(check_dns)
        self.require_smtp_if_requested(allow_smtp)
        original = email.strip()
        normalized, result = await self.check_address(original, allow_smtp, check_dns)
        local, _, domain = (normalized or '').rpartition('@')
        flags = list(result.get('flags', []))
        role_based = bool(normalized) and local.lower().split('+')[0] in ROLE_LOCALS
        if role_based and 'role_based' not in flags:
            flags.append('role_based')
        disposable = bool(result.get('disposable'))
        catch_all = bool(result.get('catch_all'))
        status, reason = result['status'], result['reason']
        smtp_outcome = smtp_outcome_for(allow_smtp, reason)
        dns_checked = bool(normalized) and (allow_smtp or check_dns) and reason not in (
            'suppressed_prior_bounce', 'verification_error')
        return {
            'email': normalized or original, 'original_email': original,
            'normalized_email': normalized, 'domain': domain or None,
            'syntax_valid': bool(normalized), 'status': status, 'reason': reason,
            'flags': flags, 'role_based': role_based, 'disposable': disposable,
            'catch_all': catch_all, 'smtp_code': result.get('smtp_code'),
            'mx_host': result.get('mx_host'), 'mx_hosts': result.get('mx_hosts', []),
            'smtp_checked': smtp_outcome == 'answered', 'smtp_outcome': smtp_outcome,
            'dns_checked': dns_checked,
            'deliverable_candidate': status == 'deliverable_candidate',
            'send_eligible': status == 'deliverable_candidate' and reason == 'smtp_rcpt_accepted'
            and not (role_based or disposable or catch_all),
        }

    async def _run_job(self, job_id, options):
        self.store.set_job_status(job_id, 'running')
        semaphore = asyncio.Semaphore(options['concurrency'])

        async def worker(row):
            async with semaphore:
                _, result = await self.check_address(
                    row['email'], options['allow_smtp'], bool(options.get('check_dns')))
                self.store.save_result(job_id, row['position'], result)

        try:
            await asyncio.gather(*(worker(row) for row in self.store.pending(job_id)))
        except asyncio.CancelledError:
            if job_id in self._cancelled:
                self._cancelled.discard(job_id)
                self.store.set_job_status(job_id, 'cancelled')
            else:
                self.store.set_job_status(job_id, 'queued')  # shutdown: resume on next start
            raise
        except Exception as error:
            self.store.set_job_status(job_id, 'failed', error=str(error))
            raise
        self.store.set_job_status(job_id, 'completed', processed=self.store.job(job_id)['processed'])

    def cancel(self, job_id):
        """Stop a queued or running job. Results already saved are kept."""
        job = self.store.job(job_id)
        if job is None:
            raise KeyError('job_not_found')
        if job['status'] not in ('queued', 'running'):
            raise ValueError('Only a queued or running job can be cancelled')
        running = [task for tasks in self._tasks.values() for task in tasks
                   if task.get_name() == f'verify-{job_id}' and not task.done()]
        if running:
            self._cancelled.add(job_id)
            for task in running:
                task.cancel()
        else:
            self.store.set_job_status(job_id, 'cancelled')
        return job_id

    def recheck(self, job_id, scope='unknown', check_dns=None, allow_smtp=None):
        """Start a new job from a finished job's addresses: the unresolved ones, or all."""
        if scope not in ('unknown', 'all'):
            raise ValueError("scope must be 'unknown' or 'all'")
        job = self.store.job(job_id)
        if job is None:
            raise KeyError('job_not_found')
        if job['status'] in ('queued', 'running'):
            raise ValueError('Wait for the job to finish (or cancel it) before re-checking')
        picked = [r for r in self.store.results(job_id)
                  if scope == 'all' or r['status'] == 'unknown' or r['reason'] == 'queued']
        if not picked:
            raise ValueError('Nothing to re-check')
        rows = [InputRow(index, r['email'], r['original_email'], r['metadata'])
                for index, r in enumerate(picked, start=1)]
        options = dict(job['options'])
        # A re-check may go deeper than the original run (e.g. add the mailbox check).
        if allow_smtp is not None:
            self.require_smtp_if_requested(allow_smtp)
            options['allow_smtp'] = allow_smtp
        if check_dns is not None:
            self._check_dns_flag(check_dns)
            options['check_dns'] = check_dns
        self.require_smtp_if_requested(bool(options.get('allow_smtp')))
        base = options.get('filename') or 'job'
        options.update(engine=RESULT_VERSION, rechecked_from=job_id,
                       filename=f'{base} (re-check)' if '(re-check)' not in base else base)
        new_id = self.store.create_job(rows, 0, options)
        self.schedule(new_id)
        return new_id

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
