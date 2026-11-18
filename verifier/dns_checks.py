"""DNS resolution with consistent conservative outcomes."""
from dataclasses import dataclass

import dns.asyncresolver
import dns.exception
import dns.resolver


@dataclass(frozen=True)
class MailDNS:
    status: str
    reason: str
    hosts: list[str]
    implicit_mx: bool = False


class DNSResolver:
    def __init__(self, lifetime: float = 4.0):
        self.resolver = dns.asyncresolver.Resolver()
        self.resolver.lifetime = lifetime
        self.resolver.timeout = lifetime

    async def resolve(self, domain: str, record: str):
        return await self.resolver.resolve(domain, record)

    async def resolve_addresses(self, host: str) -> list[str]:
        """IPv6 then IPv4 addresses for a mail host. A missing record type is normal (most hosts
        have no AAAA); only if neither type yields an address AND a lookup truly failed do we raise."""
        addresses, failure = [], None
        for kind in ('AAAA', 'A'):
            try:
                addresses.extend(r.address for r in await self.resolve(host, kind))
            except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
                continue
            except dns.exception.DNSException as error:  # timeout, SERVFAIL, no nameservers
                failure = error
        if not addresses and failure is not None:
            raise failure
        return addresses


def _failure(error) -> MailDNS | None:
    """Map a resolver error to a conservative outcome; None means 'no data of this type'."""
    if isinstance(error, dns.resolver.NXDOMAIN):
        return MailDNS('undeliverable', 'domain_not_found', [])
    if isinstance(error, dns.resolver.NoNameservers):
        return MailDNS('unknown', 'dns_no_nameservers', [])
    if isinstance(error, dns.exception.Timeout):
        return MailDNS('unknown', 'dns_timeout', [])
    if isinstance(error, (dns.resolver.NoAnswer,)):
        return None
    return MailDNS('unknown', 'dns_failure', [])


async def _implicit_mx(resolver, domain: str) -> MailDNS:
    """RFC 5321 fallback: with no MX, the domain's own A/AAAA record is the mail host."""
    for record in ('A', 'AAAA'):
        try:
            await resolver.resolve(domain, record)
            return MailDNS('deliverable_candidate', 'a_aaaa_fallback', [domain], True)
        except dns.exception.DNSException as error:
            outcome = _failure(error)
            if outcome is not None:
                return outcome
    return MailDNS('undeliverable', 'no_mail_host', [])


async def resolve_mx(resolver, domain: str) -> MailDNS:
    try:
        answers = await resolver.resolve(domain, 'MX')
    except dns.resolver.NoAnswer:
        return await _implicit_mx(resolver, domain)
    except dns.exception.DNSException as error:
        return _failure(error) or MailDNS('unknown', 'dns_failure', [])
    records = sorted(answers, key=lambda r: r.preference)
    if len(records) == 1 and str(records[0].exchange) == '.':
        return MailDNS('undeliverable', 'null_mx', [])
    hosts = [str(r.exchange).rstrip('.') for r in records]
    return MailDNS('deliverable_candidate', 'mx_found', hosts)
