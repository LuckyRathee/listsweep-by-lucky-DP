"""The admin activity log: what is recorded, who is named, and what is never recorded."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient

from verifier.api import create_app
from verifier.engine import Verifier

ADMIN = 'boss@example.com'
ADMIN_PASSWORD = 'Adm1n!Passw0rd-x'
SERVER_KEY = 's' * 32


class DNS:
    async def resolve(self, domain, record):
        return [SimpleNamespace(preference=10, exchange='mx.test.')]


def make_app(tmp_path, **kwargs):
    kwargs.setdefault('admin_email', ADMIN)
    kwargs.setdefault('admin_password', ADMIN_PASSWORD)
    return create_app(store_path=str(tmp_path / 'a.sqlite3'), verifier=Verifier(DNS(), None), **kwargs)


def client_for(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url='http://test')


async def admin_session(app, email=ADMIN, password=ADMIN_PASSWORD):
    client = client_for(app)
    response = await client.post('/v1/auth/login', json={'email': email, 'password': password})
    assert response.status_code == 200, response.text
    client.headers['X-CSRF-Token'] = response.json()['csrf']
    return client


async def events(admin, **params):
    response = await admin.get('/v1/activity', params=params)
    assert response.status_code == 200, response.text
    return response.json()


def actions(payload):
    return [item['action'] for item in payload['items']]


async def test_sign_in_events_are_recorded_and_never_contain_passwords(tmp_path):
    app = make_app(tmp_path)
    async with client_for(app) as stranger:
        await stranger.post('/v1/auth/login', json={'email': ADMIN, 'password': 'Wr0ng!Guess-123'})
        await stranger.post('/v1/auth/login', json={'email': 'nobody@example.com', 'password': 'Wr0ng!Guess-123'})
        # someone pasted a password into the email box: it must not be stored
        await stranger.post('/v1/auth/login', json={'email': 'My$ecretPassw0rd!', 'password': 'x'})
    admin = await admin_session(app)
    await admin.post('/v1/auth/password', json={'current_password': ADMIN_PASSWORD, 'new_password': 'N3w!Different-Pass'})
    await admin.post('/v1/auth/logout')
    data = await events(await admin_session(app, password='N3w!Different-Pass'))
    rows = data['items']
    assert actions(data)[:2] == ['auth.login', 'auth.logout']      # newest first
    assert {'auth.login_failed', 'auth.password_changed'} <= set(actions(data))
    failed = [r for r in rows if r['action'] == 'auth.login_failed']
    assert {r['actor'] for r in failed} == {ADMIN, 'nobody@example.com', '(not an email address)'}
    assert all(r['detail'] == 'wrong email or password' for r in failed)     # same wording for every cause
    dump = json.dumps(rows)
    for secret in (ADMIN_PASSWORD, 'Wr0ng!Guess-123', 'N3w!Different-Pass', 'My$ecretPassw0rd!'):
        assert secret not in dump
    raw = sqlite3.connect(str(tmp_path / 'a.sqlite3')).execute('SELECT * FROM activity').fetchall()
    assert 'Passw0rd' not in repr(raw) and 'Guess' not in repr(raw)


async def test_lockout_is_recorded(tmp_path):
    app = make_app(tmp_path)
    async with client_for(app) as stranger:
        for _ in range(6):
            await stranger.post('/v1/auth/login', json={'email': ADMIN, 'password': 'Wr0ng!Guess-123'})
    stored = sqlite3.connect(str(tmp_path / 'a.sqlite3')).execute(
        "SELECT action FROM activity WHERE action='auth.login_blocked'").fetchall()
    assert stored


async def test_people_and_key_changes_name_who_did_them(tmp_path):
    app = make_app(tmp_path, api_key=SERVER_KEY)
    admin = await admin_session(app)
    made = (await admin.post('/v1/users', json={'email': 'Sam@Example.com', 'role': 'user'})).json()
    await admin.patch(f"/v1/users/{made['id']}", json={'role': 'admin'})
    await admin.patch(f"/v1/users/{made['id']}", json={'status': 'disabled'})
    await admin.post(f"/v1/users/{made['id']}/reset-password")
    await admin.delete(f"/v1/users/{made['id']}")
    key = (await admin.post('/v1/keys', json={'name': 'Sales agent'})).json()
    await admin.delete(f"/v1/keys/{key['id']}")
    people = await events(admin, category='people')
    texts = [(r['action'], r['target'], r['detail'], r['actor']) for r in reversed(people['items'])]
    assert texts[0][0] == 'system.admin_created' and texts[0][3] == 'system'
    assert ('user.created', 'sam@example.com', 'role user, temporary password', ADMIN) in texts
    assert ('user.updated', 'sam@example.com', 'role user to admin', ADMIN) in texts
    assert ('user.updated', 'sam@example.com', 'disabled', ADMIN) in texts
    assert ('user.password_reset', 'sam@example.com', 'signed out everywhere', ADMIN) in texts
    assert ('user.deleted', 'sam@example.com', '', ADMIN) in texts
    keys = await events(admin, category='keys')
    kinds = [(r['action'], r['target'], r['detail'].startswith('key ls_')) for r in reversed(keys['items'])]
    assert kinds == [('key.created', 'Sales agent', True), ('key.deleted', 'Sales agent', True)]
    assert key['key'] not in json.dumps(keys)                      # only the short prefix is ever logged
    async with client_for(app) as server:
        await server.post('/v1/keys', json={'name': 'By server'}, headers={'X-API-Key': SERVER_KEY})
    again = await events(admin, category='keys')
    assert again['items'][0]['actor'] == 'server key' and again['items'][0]['actor_type'] == 'server'
    await admin.aclose()


async def test_jobs_are_logged_with_who_started_cancelled_and_rechecked_them(tmp_path):
    app = make_app(tmp_path, api_key=SERVER_KEY)
    admin = await admin_session(app)
    key = (await admin.post('/v1/keys', json={'name': 'Research bot'})).json()
    async with client_for(app) as agent:
        headers = {'X-API-Key': key['key']}
        csv = 'email\n' + '\n'.join(f'p{i}@ok.test' for i in range(3)) + '\n'
        job = (await agent.post('/v1/verify/csv', headers=headers, data={'check_dns': 'true'},
                                files={'file': ('Leads.csv', csv, 'text/csv')})).json()
        for _ in range(100):
            if (await agent.get(f"/v1/jobs/{job['job_id']}", headers=headers)).json()['status'] == 'completed':
                break
            await asyncio.sleep(0.02)
        await agent.post(f"/v1/jobs/{job['job_id']}/recheck", headers=headers, json={'scope': 'all'})
    data = await events(admin, category='jobs')
    rows = {r['action']: r for r in data['items']}
    assert rows['job.started']['actor'] == 'Research bot' and rows['job.started']['actor_type'] == 'key'
    assert rows['job.started']['target'] == 'Leads.csv' and rows['job.started']['detail'] == '3 addresses, domain checks'
    assert rows['job.rechecked']['target'] == 'Leads.csv (re-check)' and rows['job.rechecked']['detail'] == '3 addresses, all'
    await admin.post('/v1/keys', json={'name': 'x'})
    await admin.delete(f"/v1/keys/{key['id']}")
    after = await events(admin, category='jobs')
    assert after['items'][-1]['actor'] == 'Research bot'            # the name survives only as text in the log
    await admin.aclose()


async def test_only_admins_can_read_the_log_and_open_mode_counts_as_admin(tmp_path):
    app = make_app(tmp_path)
    admin = await admin_session(app)
    await admin.post('/v1/users', json={'email': 'sam@example.com', 'password': 'S4m!Strong-Pass'})
    async with client_for(app) as sam:
        login = (await sam.post('/v1/auth/login', json={'email': 'sam@example.com', 'password': 'S4m!Strong-Pass'})).json()
        csrf = {'X-CSRF-Token': login['csrf']}
        await sam.post('/v1/auth/password', headers=csrf, json={'current_password': 'S4m!Strong-Pass', 'new_password': 'S4m!Better-Pass9'})
        assert (await sam.get('/v1/activity')).status_code == 403
    async with client_for(app) as nobody:
        assert (await nobody.get('/v1/activity')).status_code == 401
    open_app = create_app(store_path=str(tmp_path / 'o.sqlite3'), verifier=Verifier(DNS(), None))
    async with client_for(open_app) as client:
        assert (await client.get('/v1/activity')).status_code == 200
    await admin.aclose()


async def test_filters_paging_and_limits(tmp_path):
    app = make_app(tmp_path)
    admin = await admin_session(app)
    for index in range(7):
        await admin.post('/v1/keys', json={'name': f'k{index}'})
    everything = await events(admin)
    assert everything['total'] >= 9 and everything['items'][0]['id'] > everything['items'][-1]['id']
    page = await events(admin, category='keys', limit=3, offset=0)
    nxt = await events(admin, category='keys', limit=3, offset=3)
    assert page['total'] == 7 and len(page['items']) == 3 and len(nxt['items']) == 3
    assert not {r['id'] for r in page['items']} & {r['id'] for r in nxt['items']}
    assert (await admin.get('/v1/activity', params={'category': 'bogus'})).status_code == 422
    assert len((await events(admin, limit=100000))['items']) <= 200
    assert (await events(admin, category='jobs'))['total'] == 0
    await admin.aclose()


async def test_the_log_keeps_only_the_newest_entries(tmp_path, monkeypatch):
    from verifier.store import Store
    monkeypatch.setattr(Store, 'MAX_ACTIVITY', 5)
    app = make_app(tmp_path)
    admin = await admin_session(app)
    for index in range(10):
        await admin.post('/v1/keys', json={'name': f'k{index}'})
    data = await events(admin)
    assert data['total'] == 5 and data['items'][0]['target'] == 'k9'      # only the newest 5 are kept
    assert all(r['action'] != 'system.admin_created' for r in data['items'])
    await admin.aclose()


async def test_a_logging_failure_never_breaks_the_action(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    admin = await admin_session(app)

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError('disk full')

    monkeypatch.setattr(app.state.store, 'log_activity', broken)
    made = await admin.post('/v1/keys', json={'name': 'still works'})
    assert made.status_code == 201
    await admin.aclose()
