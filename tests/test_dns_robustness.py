"""DNS edge cases, lookup caching, entity-expansion safety, and the single-address check."""
import zipfile
from io import BytesIO
from types import SimpleNamespace

import dns.resolver
import openpyxl
import pytest
from httpx import ASGITransport, AsyncClient

from verifier.api import create_app
from verifier.dns_checks import resolve_mx
from verifier.engine import Verifier
from verifier.xlsx_input import xlsx_to_csv

KEY = 'k' * 32


class ScriptedDNS:
    """Answers per (record) with a value or an exception to raise."""

    def __init__(self, **answers):
        self.answers, self.calls = answers, []

    async def resolve(self, domain, record):
        self.calls.append((domain, record))
        answer = self.answers[record]
        if isinstance(answer, Exception):
            raise answer
        return answer


async def test_domain_with_no_mx_a_or_aaaa_cannot_receive_mail():
    dns_ = ScriptedDNS(MX=dns.resolver.NoAnswer(), A=dns.resolver.NoAnswer(), AAAA=dns.resolver.NoAnswer())
    result = await resolve_mx(dns_, 'empty.test')
    assert (result.status, result.reason) == ('undeliverable', 'no_mail_host')


async def test_ipv6_only_domain_uses_the_aaaa_fallback():
    dns_ = ScriptedDNS(MX=dns.resolver.NoAnswer(), A=dns.resolver.NoAnswer(), AAAA=['2001:db8::1'])
    result = await resolve_mx(dns_, 'v6.test')
    assert (result.status, result.reason, result.hosts) == ('deliverable_candidate', 'a_aaaa_fallback', ['v6.test'])


@pytest.mark.parametrize(('error', 'status', 'reason'), [
    (dns.resolver.Timeout(), 'unknown', 'dns_timeout'),
    (dns.resolver.NXDOMAIN(), 'undeliverable', 'domain_not_found'),
    (dns.resolver.NoNameservers(), 'unknown', 'dns_no_nameservers'),
])
async def test_errors_during_the_a_fallback_are_classified_not_crashes(error, status, reason):
    dns_ = ScriptedDNS(MX=dns.resolver.NoAnswer(), A=error, AAAA=dns.resolver.NoAnswer())
    result = await resolve_mx(dns_, 'x.test')
    assert (result.status, result.reason) == (status, reason)


async def test_transient_dns_failures_are_not_cached_but_good_answers_are():
    class Flaky:
        def __init__(self):
            self.calls = 0

        async def resolve(self, domain, record):
            self.calls += 1
            if self.calls == 1:
                raise dns.resolver.Timeout()
            return [SimpleNamespace(preference=10, exchange='mx.test.')]

    flaky = Flaky()
    verifier = Verifier(flaky, None)
    first = await verifier.verify('a@flaky.test')
    second = await verifier.verify('a@flaky.test')
    third = await verifier.verify('b@flaky.test')
    assert first['reason'] == 'dns_timeout'
    assert second['reason'] == 'mailbox_check_disabled'   # retried after the timeout
    assert third['reason'] == 'mailbox_check_disabled' and flaky.calls == 2  # cached after success


def test_xml_entity_expansion_in_a_workbook_is_refused():
    book = openpyxl.Workbook()
    book.active.append(['Email'])
    book.active.append(['a@ok.test'])
    raw = BytesIO()
    book.save(raw)
    hostile = BytesIO()
    with zipfile.ZipFile(BytesIO(raw.getvalue())) as source, zipfile.ZipFile(hostile, 'w') as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == 'xl/worksheets/sheet1.xml':
                data = (b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
                        + data.split(b'?>', 1)[-1])
            target.writestr(item, data)
    with pytest.raises(ValueError):
        xlsx_to_csv(hostile.getvalue())


# ---- single address ----

class DNS:
    async def resolve(self, domain, record):
        if domain == 'null.test':
            return [SimpleNamespace(preference=0, exchange='.')]
        if domain == 'gone.test':
            raise dns.resolver.NXDOMAIN()
        return [SimpleNamespace(preference=10, exchange='mx.test.')]


def single_client(tmp_path):
    app = create_app(store_path=str(tmp_path / 's.sqlite3'), api_key=KEY, verifier=Verifier(DNS(), None))
    return AsyncClient(transport=ASGITransport(app=app), base_url='http://test')


async def post_email(client, **body):
    return await client.post('/v1/verify/email', headers={'X-API-Key': KEY}, json=body)


async def test_single_email_with_dns_reports_domain_evidence(tmp_path):
    async with single_client(tmp_path) as client:
        ok = (await post_email(client, email='  Ann@OK.test ', check_dns=True)).json()
        assert ok['email'] == 'Ann@ok.test' and ok['syntax_valid'] and ok['dns_checked']
        assert (ok['status'], ok['reason']) == ('unknown', 'mailbox_check_disabled')
        assert ok['mx_hosts'] == ['mx.test'] and ok['send_eligible'] is False
        assert (await post_email(client, email='x@null.test', check_dns=True)).json()['reason'] == 'null_mx'
        assert (await post_email(client, email='x@gone.test', check_dns=True)).json()['reason'] == 'domain_not_found'
        role = (await post_email(client, email='info@ok.test', check_dns=True)).json()
        assert role['role_based'] is True and 'role_based' in role['flags']


async def test_single_email_defaults_to_no_network_and_rejects_bad_input(tmp_path):
    async with single_client(tmp_path) as client:
        offline = (await post_email(client, email='a@ok.test')).json()
        assert offline['reason'] == 'verification_disabled' and offline['dns_checked'] is False
        invalid = (await post_email(client, email='not-an-email')).json()
        assert (invalid['status'], invalid['reason'], invalid['syntax_valid']) == ('undeliverable', 'invalid_syntax', False)
        assert (await post_email(client, email='')).status_code == 422
        assert (await post_email(client, email='a' * 400 + '@x.test')).status_code == 422
        assert (await post_email(client, email='a@ok.test', allow_smtp=True)).status_code == 403
        assert (await client.post('/v1/verify/email', json={'email': 'a@ok.test'})).status_code == 401


# ---- SMTP on by default when the server has it ----

class FakeProbe:
    def __init__(self):
        self.checked = []

    async def check(self, email, domain, hosts):
        from verifier.policy import ProbeVerdict
        self.checked.append(email)
        return ProbeVerdict('deliverable_candidate', 'smtp_rcpt_accepted', 250, host='mx.test')


async def test_when_smtp_is_enabled_every_check_uses_it_unless_turned_off(tmp_path):
    probe = FakeProbe()
    app = create_app(store_path=str(tmp_path / 's.sqlite3'), api_key=KEY, smtp_enabled=True,
                     verifier=Verifier(DNS(), probe))
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        headers = {'X-API-Key': KEY}
        config = (await client.get('/v1/config', headers=headers)).json()
        assert config['smtp_available'] and config['smtp_default']
        one = (await client.post('/v1/verify/email', headers=headers, json={'email': 'ann@ok.test'})).json()
        assert one['smtp_checked'] is True and one['reason'] == 'smtp_rcpt_accepted' and probe.checked == ['ann@ok.test']
        off = (await client.post('/v1/verify/email', headers=headers,
                                 json={'email': 'bob@ok.test', 'allow_smtp': False})).json()
        assert off['smtp_checked'] is False and probe.checked == ['ann@ok.test']
        job = (await client.post('/v1/verify/csv', headers=headers,
                                 files={'file': ('a.csv', 'email\ncy@ok.test\n', 'text/csv')})).json()
        assert job['options']['allow_smtp'] is True


def test_server_refuses_to_start_with_smtp_enabled_but_no_sender_identity(monkeypatch, tmp_path):
    monkeypatch.setenv('VERIFIER_SMTP_ENABLED', 'true')
    monkeypatch.delenv('VERIFIER_HELO_DOMAIN', raising=False)
    monkeypatch.delenv('VERIFIER_MAIL_FROM', raising=False)
    with pytest.raises(RuntimeError, match='sender identity'):
        create_app(store_path=str(tmp_path / 's.sqlite3'), api_key=KEY)
