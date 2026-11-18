import asyncio
import socket

import dns.asyncresolver
import dns.resolver as dns_resolver_module
import dns.resolver
import pytest

from verifier.dns_checks import DNSResolver, resolve_mx
from verifier.policy import ProbeVerdict
from verifier.smtp import AsyncSMTPProbe


class FailingDNS:
    async def resolve_addresses(self, host):
        raise dns.resolver.NXDOMAIN()


async def test_unresolved_mx_result_is_unknown_not_a_crash():
    probe = AsyncSMTPProbe(FailingDNS(), min_host_interval=0)
    result = await probe.check('person@example.com', 'example.com', ['invalid.test'])
    assert result.status == 'unknown' and result.reason == 'smtp_unresolved'


class FakeRecords:
    def __init__(self, *addresses):
        self.addresses = addresses


async def test_probe_binds_to_the_dns_checked_ip_to_prevent_rebinding():
    class LoopbackDNS:
        def __init__(self):
            self.queries = []


        async def resolve_addresses(self, host):
            self.queries.append(host)
            return ['127.0.0.1']
    class TestProbe(AsyncSMTPProbe):
        async def _transact(self, host, addresses):
            assert host == '127.0.0.1'
            return [ProbeVerdict('undeliverable', 'smtp_mailbox_rejected', 550), ProbeVerdict('unknown', 'x')]
    dns = LoopbackDNS()
    probe = TestProbe(dns, min_host_interval=0, allow_private=False)
    result = await probe.check('person@example.com', 'example.com', ['127.0.0.1'])
    assert result.reason == 'smtp_non_public_target'


async def test_fallback_can_reach_another_mx_after_temporary_failure():
    class DNS:
        async def resolve_addresses(self, host):
            return ['127.0.0.1']
    class Probe(AsyncSMTPProbe):
        def __init__(self):
            super().__init__(DNS(), min_host_interval=0, allow_private=True)
            self.calls = []

        async def _transact(self, host, addresses):
            self.calls.append(host)
            if len(self.calls) == 1:
                raise TimeoutError()
            return [ProbeVerdict('deliverable_candidate', 'smtp_rcpt_accepted', 250),
                    ProbeVerdict('undeliverable', 'smtp_mailbox_rejected', 550)]
    probe = Probe()
    result = await probe.check('person@example.test', 'example.test', ['mx0.test', 'mx1.test'])
    assert probe.calls == ['127.0.0.1', '127.0.0.1']
    assert result.status == 'deliverable_candidate'


async def test_unknown_never_becomes_deliverable_when_both_probes_time_out():
    class DNS:
        async def resolve_addresses(self, host):
            return ['127.0.0.1']


    class Probe(AsyncSMTPProbe):
        async def _transact(self, host, addresses):
            await asyncio.sleep(5)
    probe = Probe(DNS(), timeout=0.01, allow_private=True)
    result = await probe.check('person@example.com', 'example.com', ['mx.test'])
    assert result.status == 'unknown'


class AAAA:
    address = '2606:2800:21f:cb07:6820:80da:af6b:8b2c'


class NoAddress:
    pass


async def test_real_resolver_handles_fallback_aaaa_without_a_record():
    class Resolver:
        async def resolve(self, domain, kind):
            if kind == 'MX':
                raise dns.resolver.NoAnswer()
            if kind == 'A':
                raise dns.resolver.NoAnswer()
            if kind == 'AAAA':
                return [AAAA()]
    resolver = DNSResolver()
    resolver.resolver = Resolver()
    records = await resolve_mx(resolver, 'example.test')
    assert records.hosts == ['example.test'] and records.implicit_mx is True
