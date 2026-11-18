import dns.resolver

from verifier.dns_checks import MailDNS, resolve_mx


class FakeDNS:
    def __init__(self, mx_error=None):
        self.mx_error = mx_error
        self.queries = []

    async def resolve(self, domain, record):
        self.queries.append((domain, record))
        if record == 'MX':
            if self.mx_error:
                raise self.mx_error
            return [FakeMX(10, 'mx1.example.com'), FakeMX(5, 'mx0.example.com')]
        return ['192.0.2.1']


class FakeMX:
    def __init__(self, preference, exchange):
        self.preference = preference
        self.exchange = exchange


async def test_mx_records_are_priority_ordered_and_a_aaaa_is_only_a_fallback():
    dns = FakeDNS()
    result = await resolve_mx(dns, 'example.com')
    assert result.hosts == ['mx0.example.com', 'mx1.example.com']
    assert result.implicit_mx is False
    assert result.reason == 'mx_found'
    assert dns.queries == [('example.com', 'MX')]


async def test_nxdomain_and_null_mx_are_not_unknown():
    nxdomain = await resolve_mx(FakeDNS(dns.resolver.NXDOMAIN()), 'does-not-exist.test')
    async def null_mx(*args):
        return [FakeMX(0, '.')]
    class NullDNS:
        resolve = staticmethod(null_mx)
    null = await resolve_mx(NullDNS(), 'example.com')
    assert nxdomain.status == 'undeliverable' and nxdomain.reason == 'domain_not_found'
    assert null.status == 'undeliverable' and null.reason == 'null_mx'


async def test_temporary_dns_failure_is_never_a_invalid_address():
    result = await resolve_mx(FakeDNS(dns.resolver.Timeout()), 'example.com')
    assert result.status == 'unknown' and result.reason == 'dns_timeout'
