"""Bugs found by comparing real runs against another verifier."""
import dns.resolver
import pytest

from verifier.dns_checks import DNSResolver
from verifier.policy import interpret_rcpt, refused_by_policy
from verifier.smtp import AsyncSMTPProbe

MS_BLOCK = ('5.7.1 Service unavailable, Client host [223.181.103.155] blocked using Spamhaus. '
            'To request removal from this list see https://www.spamhaus.org/query/ip/223.181.103.155')


class ScriptedResolver(DNSResolver):
    def __init__(self, **answers):
        super().__init__()
        self.answers = answers

    async def resolve(self, domain, record):
        answer = self.answers[record]
        if isinstance(answer, Exception):
            raise answer
        return [type('R', (), {'address': a})() for a in answer]


async def test_ipv4_only_mail_host_resolves_instead_of_crashing():
    # Most mail hosts have no AAAA record. This used to raise AttributeError (dns.resolver.NODATA).
    resolver = ScriptedResolver(AAAA=dns.resolver.NoAnswer(), A=['72.18.130.36'])
    assert await resolver.resolve_addresses('mx1.example.test') == ['72.18.130.36']


async def test_one_failing_record_type_does_not_hide_the_other():
    resolver = ScriptedResolver(AAAA=dns.resolver.Timeout(), A=['192.0.2.7'])
    assert await resolver.resolve_addresses('mx.example.test') == ['192.0.2.7']


async def test_a_real_lookup_failure_with_no_addresses_is_raised():
    resolver = ScriptedResolver(AAAA=dns.resolver.Timeout(), A=dns.resolver.Timeout())
    with pytest.raises(dns.exception.Timeout):
        await resolver.resolve_addresses('mx.example.test')
    assert await ScriptedResolver(AAAA=dns.resolver.NXDOMAIN(), A=dns.resolver.NoAnswer()
                                  ).resolve_addresses('gone.test') == []


async def test_unresolved_is_no_longer_reported_for_ipv4_only_hosts():
    class Probe(AsyncSMTPProbe):
        async def _transact(self, host, addresses):
            from verifier.policy import ProbeVerdict
            assert host == '93.184.216.34'
            return [ProbeVerdict('deliverable_candidate', 'smtp_rcpt_accepted', 250),
                    ProbeVerdict('undeliverable', 'smtp_mailbox_rejected', 550)]

    resolver = ScriptedResolver(AAAA=dns.resolver.NoAnswer(), A=['93.184.216.34'])
    result = await Probe(resolver, min_host_interval=0).check('a@example.test', 'example.test', ['mx.example.test'])
    assert result.reason == 'smtp_rcpt_accepted'


@pytest.mark.parametrize('text', [
    MS_BLOCK,
    '554 5.7.1 Service unavailable; Client host [1.2.3.4] blocked by zen.spamhaus.org',
    '550 5.7.606 Access denied, sending IP banned',
    '550 Your IP has a poor reputation',
    '554 5.7.1 <x@y.test>: Relay access denied',
    '550 dynamic/residential IP not allowed, no PTR record',
])
def test_a_refusal_of_our_ip_is_unknown_never_a_mailbox_verdict(text):
    verdict = interpret_rcpt(550, text)
    assert (verdict.status, verdict.reason) == ('unknown', 'smtp_blocked_by_policy')
    assert refused_by_policy(text)


@pytest.mark.parametrize('text', [
    '5.1.1 <x@y.test>: Recipient address rejected: User unknown in virtual mailbox table',
    '5.1.10 RESOLVER.ADR.RecipientNotFound; Recipient not found by SMTP address lookup',
    '5.4.1 Recipient address rejected: Access denied. AS(201806281)',
    'User unknown',
    'No such user here',
    '',
])
def test_a_genuine_unknown_recipient_is_still_undeliverable(text):
    verdict = interpret_rcpt(550, text)
    assert (verdict.status, verdict.reason) == ('undeliverable', 'smtp_mailbox_rejected')


def test_temporary_and_accepted_replies_are_unchanged():
    assert interpret_rcpt(250, 'OK').status == 'deliverable_candidate'
    assert interpret_rcpt(451, 'greylisted').reason == 'smtp_temporary_failure'


async def test_a_blocked_probe_is_unknown_even_when_both_addresses_are_refused():
    class Probe(AsyncSMTPProbe):
        async def _transact(self, host, addresses):
            return [interpret_rcpt(550, MS_BLOCK) for _ in addresses]

    resolver = ScriptedResolver(AAAA=dns.resolver.NoAnswer(), A=['93.184.216.34'])
    result = await Probe(resolver, min_host_interval=0).check('a@example.test', 'example.test', ['mx.example.test'])
    assert (result.status, result.reason) == ('unknown', 'smtp_blocked_by_policy')


def test_a_bare_550_with_no_wording_about_the_mailbox_is_ambiguous_not_undeliverable():
    verdict = interpret_rcpt(550, 'send email')
    assert (verdict.status, verdict.reason) == ('unknown', 'smtp_ambiguous_response')
    assert interpret_rcpt(550, '5.2.2 mailbox full').status == 'unknown'


async def test_server_hanging_up_after_the_candidate_keeps_the_candidate_answer():
    import asyncio

    async def serve(reader, writer):
        async def send(line):
            writer.write((line + '\r\n').encode()); await writer.drain()
        await send('220 test ESMTP')
        replies = {'EHLO': '250 hello', 'MAIL': '250 OK', 'RCPT': None}
        rcpt = 0
        while True:
            line = (await reader.readline()).decode().strip()
            if not line:
                break
            verb = line.split()[0].upper()
            if verb == 'RCPT':
                rcpt += 1
                if rcpt == 1:
                    await send('550 5.1.1 user unknown')
                else:
                    writer.close()
                    return
            else:
                await send(replies.get(verb, '250 OK'))

    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        probe = AsyncSMTPProbe(DNSResolver(), timeout=3, port=port, allow_private=True, min_host_interval=0)
        verdicts = await probe._transact('127.0.0.1', ['a@x.test', 'evr-1@x.test'])
    assert verdicts[0].reason == 'smtp_mailbox_rejected'
    assert verdicts[1].reason == 'smtp_connection_dropped'


async def test_any_permanent_refusal_of_the_random_control_address_clears_catch_all():
    class Probe(AsyncSMTPProbe):
        async def _transact(self, host, addresses):
            return [interpret_rcpt(250, 'OK'), interpret_rcpt(550, 'send email')]

    resolver = ScriptedResolver(AAAA=dns.resolver.NoAnswer(), A=['93.184.216.34'])
    result = await Probe(resolver, min_host_interval=0).check('a@example.test', 'example.test', ['mx.example.test'])
    assert (result.status, result.reason) == ('deliverable_candidate', 'smtp_rcpt_accepted')


async def test_a_policy_block_on_the_control_does_not_count_as_a_rejection():
    class Probe(AsyncSMTPProbe):
        async def _transact(self, host, addresses):
            return [interpret_rcpt(250, 'OK'), interpret_rcpt(550, MS_BLOCK)]

    resolver = ScriptedResolver(AAAA=dns.resolver.NoAnswer(), A=['93.184.216.34'])
    result = await Probe(resolver, min_host_interval=0).check('a@example.test', 'example.test', ['mx.example.test'])
    assert result.status == 'unknown'
