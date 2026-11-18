"""Pipeline: cheap checks -> MX -> controlled SMTP -> per-send eligibility."""
import asyncio

from .dns_checks import resolve_mx
from .policy import send_eligible
from .syntax import normalize


ROLE_LOCALS = {'admin', 'contact', 'hello', 'hr', 'info', 'jobs', 'marketing', 'office', 'sales', 'support'}


MAX_CACHED_DOMAINS = 10_000


class Verifier:
    def __init__(self, dns, smtp_probe, disposable=frozenset(), concurrency=10):
        self.dns = dns
        self.smtp = smtp_probe
        self.disposable = disposable
        self.concurrency = concurrency
        self._mx_cache = {}
        self._mx_lock = asyncio.Lock()
        self._disposable_cache = {}

    async def _mx(self, domain):
        async with self._mx_lock:
            task = self._mx_cache.get(domain)
            if task is None:
                if len(self._mx_cache) >= MAX_CACHED_DOMAINS:
                    self._mx_cache.clear()
                task = asyncio.create_task(resolve_mx(self.dns, domain))
                self._mx_cache[domain] = task
        try:
            outcome = await asyncio.shield(task)
        except Exception:
            await self._forget(domain, task)
            raise
        if outcome.status == 'unknown':
            await self._forget(domain, task)  # a transient failure must not stick until restart
        return outcome

    async def _forget(self, domain, task):
        async with self._mx_lock:
            if self._mx_cache.get(domain) is task:
                del self._mx_cache[domain]

    def initial(self, email):
        normalized = normalize(email)
        result = {'original': email, 'email': normalized or email, 'normalized_email': normalized, 'domain': None,
                       'status': 'undeliverable', 'reason': 'invalid_syntax', 'flags': [], 'disposable': False,
                      'role_based': False, 'catch_all': False, 'smtp_code': None, 'mx_host': None}
        if not normalized:
            return None, result
        local, domain = normalized.rsplit('@', 1)
        result['domain'] = domain
        result['role_based'] = local.lower().split('+')[0] in ROLE_LOCALS
        if result['role_based']:
            result['flags'].append('role_based')
        return domain, result

    async def verify(self, email, allow_smtp=False):
        domain, result = self.initial(email)
        if domain is None:
            return result
        if domain in self.disposable:
            result.update(status='undeliverable', reason='disposable_domain', disposable=True)
            result['flags'].append('disposable')
            return result
        mx = await self._mx(domain)
        result['mx_hosts'] = list(mx.hosts)
        if mx.status == 'undeliverable':
            result.update(status='undeliverable', reason=mx.reason)
        elif mx.status == 'unknown':
            result.update(status='unknown', reason=mx.reason)
        elif not allow_smtp:
            result.update(status='unknown', reason='mailbox_check_disabled')
        else:
            normalized = result['normalized_email']
            probe = await self.smtp.check(normalized, domain, mx.hosts)
            result.update(status=probe.status, reason=probe.reason,
                          smtp_code=probe.smtp_code, mx_host=probe.host)
        result['catch_all'] = result['reason'] == 'catch_all_domain'
        if result['catch_all']:
            result['flags'].append('catch_all')
        result['send_eligible'] = send_eligible(result['status'], result['reason'], result['flags'])
        return result


async def bounded_gather(items, worker, concurrency):
    sem = asyncio.Semaphore(concurrency)
    async def run(item):
        async with sem:
            return await worker(item)
    return await asyncio.gather(*(run(item) for item in items))
