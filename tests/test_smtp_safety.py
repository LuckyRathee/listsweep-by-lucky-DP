import asyncio

import pytest

from verifier.smtp import AsyncSMTPProbe
from verifier.dns_checks import MailDNS


class DummyDNS:
    async def resolve_addresses(self, host):
        return ['93.184.216.34']


class DummyProbe(AsyncSMTPProbe):
    def __init__(self):
        super().__init__(dns=DummyDNS(), timeout=0.05, allow_private=True)


@pytest.mark.asyncio
async def test_non_global_addresses_are_blocked():
    class PrivateDNS:
        async def resolve_addresses(self, host):
            return ['127.0.0.1']
    probe = AsyncSMTPProbe(PrivateDNS(), timeout=0.1)
    result = await probe.check('user@example.com', 'example.com', ['127.0.0.1'])
    assert result.reason == 'smtp_non_public_target'


@pytest.mark.asyncio
async def test_command_rejects_newlines_to_prevent_smtp_injection():
    probe = DummyProbe()
    with pytest.raises(ValueError):
        await probe._command(None, None, 'RCPT TO:<a@example.com>\r\nDATA')
