import dns.resolver
import pytest

from verifier.dns_checks import resolve_mx
from verifier.policy import ProbeVerdict, detect_catch_all, send_eligible
from verifier.smtp import AsyncSMTPProbe


async def test_catch_all_candidate_and_control_are_unknown_not_valid():
    class DNS:
        async def resolve(self, domain, record):
            if record == 'MX':
                return [type('MX', (), {'preference': 10, 'exchange': 'mx.example.net.'})()]
        async def resolve_addresses(self, host):
            return ['127.0.0.1']
    class SMTP:
        async def _transact(self, host, addresses):
            return [ProbeVerdict('deliverable_candidate', 'smtp_rcpt_accepted', 250) for _ in addresses]
    class Probe(SMTP, AsyncSMTPProbe):
        def __init__(self):
            super().__init__(DNS(), min_host_interval=0, allow_private=True)
    result = await Probe().check('jane@example.com', 'example.com', ['mx.example.net'])
    assert result.status == 'unknown' and result.reason == 'catch_all_domain'
    assert not send_eligible(result.status, result.reason, result.flags)


@pytest.mark.parametrize(('candidate_code', 'control_code', 'status', 'reason'), [
    (250, 550, 'deliverable_candidate', 'smtp_rcpt_accepted'),
    (251, 550, 'unknown', 'smtp_control_inconclusive'),
    (550, 250, 'undeliverable', 'smtp_mailbox_rejected'),
    (451, 550, 'unknown', 'smtp_control_inconclusive'),
])
def test_double_probe_matrix(candidate_code, control_code, status, reason):
    accepted = __import__('verifier.policy', fromlist=['interpret_rcpt']).interpret_rcpt
    result = detect_catch_all(accepted(candidate_code), accepted(control_code))
    assert (result.status, result.reason) == (status, reason)


async def test_private_target_can_only_be_probed_in_the_fixture_mode():
    class DNS:
        async def resolve_addresses(self, host):
            return ['127.0.0.1']
    probe = AsyncSMTPProbe(DNS())
    result = await probe.check('jane@example.com', 'example.com', ['127.0.0.1'])
    assert result.reason == 'smtp_non_public_target'
