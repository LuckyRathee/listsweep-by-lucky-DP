"""DNS resolution with consistent conservative outcomes."""
from dataclasses import dataclass

import dns.exception
import dns.resolver
import dns.asyncresolver


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
        addresses = []
        for kind in ('AAAA', 'A'):
            try:
                addresses.extend(r.address for r in await self.resolve(host, kind))
            except (dns.resolver.NXDOMAIN, dns.resolver.NODATA, dns.resolver.NoAnswer):
                continue
        return addresses


async def resolve_mx(resolver, domain: str) -> MailDNS:
    try:
        answers = await resolver.resolve(domain, 'MX')
        records = sorted(answers, key=lambda r: r.preference)
        if len(records) == 1 and str(records[0].exchange) == '.':
            return MailDNS('undeliverable', 'null_mx', [])
        hosts = [str(r.exchange).rstrip('.') for r in records]
        return MailDNS('deliverable_candidate', 'mx_found', hosts)
    except (dns.resolver.NoAnswer, getattr(dns.resolver, "NODATA", dns.resolver.NoAnswer)):
        try:
            await resolver.resolve(domain, 'A')
        except (dns.resolver.NoAnswer, getattr(dns.resolver, "NODATA", dns.resolver.NoAnswer)):
            await resolver.resolve(domain, 'AAAA')
        return MailDNS('deliverable_candidate', 'a_aaaa_fallback', [domain], True)
    except dns.resolver.NXDOMAIN:
        return MailDNS('undeliverable', 'domain_not_found', [])
    except dns.resolver.NoNameservers:
        return MailDNS('unknown', 'dns_no_nameservers', [])
    except dns.resolver.Timeout:
        return MailDNS('unknown', 'dns_timeout', [])
    except dns.exception.DNSException:
        return MailDNS('unknown', 'dns_failure', [])
