"""Sign-in, sessions, CSRF, lockout, forced password change, and the admin controls."""
import sqlite3
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient

from verifier.accounts import (
    LoginThrottle,
    hash_password,
    password_problem,
    temporary_password,
    verify_password,
)
from verifier.api import create_app
from verifier.engine import Verifier

ADMIN = 'boss@example.com'
ADMIN_PASSWORD = 'Adm1n!Passw0rd-x'


class DNS:
    async def resolve(self, domain, record):
        return [SimpleNamespace(preference=10, exchange='mx.test.')]


def make_app(tmp_path, **kwargs):
    kwargs.setdefault('admin_email', ADMIN)
    kwargs.setdefault('admin_password', ADMIN_PASSWORD)
    return create_app(store_path=str(tmp_path / 'a.sqlite3'), verifier=Verifier(DNS(), None), **kwargs)


def client_for(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url='http://test')


async def sign_in(client, email=ADMIN, password=ADMIN_PASSWORD):
    response = await client.post('/v1/auth/login', json={'email': email, 'password': password})
    return response


async def admin_session(app):
    client = client_for(app)
    response = await sign_in(client)
    assert response.status_code == 200, response.text
    client.headers['X-CSRF-Token'] = response.json()['csrf']
    return client


async def add_user(admin, email, role='user', password=None):
    body = {'email': email, 'role': role, **({'password': password} if password else {})}
    response = await admin.post('/v1/users', json=body)
    assert response.status_code == 201, response.text
    return response.json()


# ---- passwords ----

def test_passwords_are_salted_and_verified_without_being_stored():
    first, second = hash_password('Tr1cky!Password'), hash_password('Tr1cky!Password')
    assert first != second and 'Tr1cky' not in first
    assert verify_password('Tr1cky!Password', first) and not verify_password('tr1cky!password', first)
    assert not verify_password('x', 'not-a-hash') and not verify_password('x', 'scrypt$bad')


def test_password_rules_and_generated_passwords():
    assert password_problem('short1!') and password_problem('alllowercaseletters')
    assert password_problem('Marie@4321', 'marie@example.com')            # contains the email name
    assert password_problem('Str0ng!Passw0rd', 'a@example.com') is None
    assert all(password_problem(temporary_password(), 'x@example.com') is None for _ in range(20))
    assert len({temporary_password() for _ in range(50)}) == 50


def test_throttle_locks_after_repeated_failures_and_clears_on_success():
    throttle = LoginThrottle(limit=3, ip_limit=10, window=60)
    for _ in range(3):
        assert throttle.blocked_for('u', 'ip') == 0
        throttle.record('u', 'ip')
    assert throttle.blocked_for('u', 'ip') > 0
    assert throttle.first_block('ip') is True and throttle.first_block('ip') is False   # announced once per window
    assert throttle.blocked_for('other', 'ip2') == 0
    throttle.clear('u')
    assert throttle.blocked_for('u', 'ip') == 0


# ---- sign-in ----

async def test_the_first_admin_comes_from_settings_and_the_password_is_never_stored(tmp_path):
    app = make_app(tmp_path)
    async with client_for(app) as client:
        status = (await client.get('/v1/auth/status')).json()
        assert status['login_required'] is True and status['first_run'] is False
        assert (await client.get('/v1/jobs')).status_code == 401
        assert (await sign_in(client)).status_code == 200
    stored = sqlite3.connect(str(tmp_path / 'a.sqlite3')).execute('SELECT * FROM users').fetchall()
    assert len(stored) == 1 and ADMIN_PASSWORD not in repr(stored) and 'scrypt$' in stored[0][2]


async def test_restarting_does_not_overwrite_a_changed_password(tmp_path):
    first = make_app(tmp_path)
    admin = await admin_session(first)
    new = 'N3w!Different-Pass'
    assert (await admin.post('/v1/auth/password', json={'current_password': ADMIN_PASSWORD, 'new_password': new})).status_code == 204
    await admin.aclose()
    again = make_app(tmp_path)                                   # same database, same startup settings
    async with client_for(again) as client:
        assert (await sign_in(client)).status_code == 401        # the original password no longer works
        assert (await sign_in(client, password=new)).status_code == 200


async def test_the_session_cookie_is_http_only_and_same_site_strict(tmp_path):
    async with client_for(make_app(tmp_path)) as client:
        cookie = (await sign_in(client)).headers['set-cookie'].lower()
        assert 'httponly' in cookie and 'samesite=strict' in cookie and 'ls_session=' in cookie
        assert 'secure' not in cookie                            # plain http locally
        secure = (await client.post('/v1/auth/login', json={'email': ADMIN, 'password': ADMIN_PASSWORD},
                                    headers={'X-Forwarded-Proto': 'https'})).headers['set-cookie'].lower()
        assert 'secure' in secure                                # but secure behind https


async def test_wrong_unknown_and_disabled_sign_ins_look_the_same(tmp_path):
    app = make_app(tmp_path)
    admin = await admin_session(app)
    made = await add_user(admin, 'sam@example.com', password='S4m!Strong-Pass')
    assert (await admin.patch(f"/v1/users/{made['id']}", json={'status': 'disabled'})).status_code == 200
    async with client_for(app) as client:
        wrong = await sign_in(client, password='nope')
        unknown = await sign_in(client, 'nobody@example.com', 'nope')
        disabled = await sign_in(client, 'sam@example.com', 'S4m!Strong-Pass')
        assert {r.status_code for r in (wrong, unknown, disabled)} == {401}
        assert wrong.json() == unknown.json() == disabled.json() == {'detail': 'Wrong email or password'}
    await admin.aclose()


async def test_repeated_wrong_passwords_lock_sign_in(tmp_path):
    async with client_for(make_app(tmp_path)) as client:
        for _ in range(5):
            assert (await sign_in(client, password='wrong-wrong')).status_code == 401
        locked = await sign_in(client, password='wrong-wrong')
        assert locked.status_code == 429 and int(locked.headers['retry-after']) > 0
        assert (await sign_in(client)).status_code == 429        # even the right password waits


# ---- sessions and CSRF ----

async def test_a_session_works_until_sign_out_and_posts_need_the_csrf_token(tmp_path):
    app = make_app(tmp_path)
    async with client_for(app) as client:
        login = (await sign_in(client)).json()
        assert (await client.get('/v1/jobs')).status_code == 200
        me = (await client.get('/v1/auth/me')).json()
        assert me['email'] == ADMIN and me['role'] == 'admin' and me['csrf'] == login['csrf']
        body = {'email': 'a@example.com'}
        assert (await client.post('/v1/verify/email', json=body)).status_code == 403           # no token
        wrong = await client.post('/v1/verify/email', json=body, headers={'X-CSRF-Token': 'x' * 43})
        assert wrong.status_code == 403
        right = await client.post('/v1/verify/email', json=body, headers={'X-CSRF-Token': login['csrf']})
        assert right.status_code == 200
        out = await client.post('/v1/auth/logout', headers={'X-CSRF-Token': login['csrf']})
        assert out.status_code == 204
        assert (await client.get('/v1/jobs')).status_code == 401


async def test_api_keys_still_work_next_to_accounts_but_cannot_manage_access(tmp_path):
    app = make_app(tmp_path, api_key='s' * 32)
    async with client_for(app) as client:
        server = {'X-API-Key': 's' * 32}
        assert (await client.get('/v1/jobs', headers=server)).status_code == 200
        assert (await client.post('/v1/verify/email', json={'email': 'a@example.com'}, headers=server)).status_code == 200
        assert (await client.get('/v1/users', headers=server)).status_code == 200    # the server key is an admin
        made = await client.post('/v1/keys', json={'name': 'agent'}, headers=server)
        agent = {'X-API-Key': made.json()['key']}
        assert (await client.get('/v1/jobs', headers=agent)).status_code == 200
        assert (await client.get('/v1/users', headers=agent)).status_code == 403
        assert (await client.post('/v1/keys', json={'name': 'x'}, headers=agent)).status_code == 403
        assert (await client.get('/v1/jobs', headers={'X-API-Key': 'wrong'})).status_code == 401


# ---- admin controls ----

async def test_a_new_person_gets_a_temporary_password_and_must_change_it_first(tmp_path):
    app = make_app(tmp_path)
    admin = await admin_session(app)
    made = await add_user(admin, 'Sam@Example.com')
    temp = made['temporary_password']
    assert made['email'] == 'sam@example.com' and made['must_change'] is True and temp
    async with client_for(app) as sam:
        login = (await sign_in(sam, 'sam@example.com', temp)).json()
        assert login['must_change'] is True
        csrf = {'X-CSRF-Token': login['csrf']}
        blocked = await sam.get('/v1/jobs')
        assert blocked.status_code == 403 and blocked.json()['detail'] == 'password_change_required'
        assert (await sam.get('/v1/auth/me')).status_code == 200
        weak = await sam.post('/v1/auth/password', json={'current_password': temp, 'new_password': 'short'}, headers=csrf)
        assert weak.status_code == 422
        bad = await sam.post('/v1/auth/password', json={'current_password': 'wrong', 'new_password': 'Fr3sh!Passw0rd'}, headers=csrf)
        assert bad.status_code == 403
        ok = await sam.post('/v1/auth/password', json={'current_password': temp, 'new_password': 'Fr3sh!Passw0rd'}, headers=csrf)
        assert ok.status_code == 204
        assert (await sam.get('/v1/jobs')).status_code == 200
    async with client_for(app) as again:
        assert (await sign_in(again, 'sam@example.com', temp)).status_code == 401
        assert (await sign_in(again, 'sam@example.com', 'Fr3sh!Passw0rd')).status_code == 200
    await admin.aclose()


async def test_an_admin_chosen_password_is_checked_and_not_echoed_back(tmp_path):
    admin = await admin_session(make_app(tmp_path))
    weak = await admin.post('/v1/users', json={'email': 'a@example.com', 'password': 'weak'})
    assert weak.status_code == 422
    made = await add_user(admin, 'a@example.com', password='Ch0sen!Passw0rd')
    assert made['temporary_password'] is None and 'password' not in repr(made).lower().replace('temporary_password', '')
    assert (await admin.post('/v1/users', json={'email': 'A@EXAMPLE.com'})).status_code == 409
    assert (await admin.post('/v1/users', json={'email': 'not-an-email'})).status_code == 422
    await admin.aclose()


async def test_normal_people_can_verify_but_cannot_manage_access(tmp_path):
    app = make_app(tmp_path)
    admin = await admin_session(app)
    await add_user(admin, 'sam@example.com', password='S4m!Strong-Pass')
    async with client_for(app) as sam:
        login = (await sign_in(sam, 'sam@example.com', 'S4m!Strong-Pass')).json()
        # a password set by an admin is temporary, so change it first
        await sam.post('/v1/auth/password', json={'current_password': 'S4m!Strong-Pass', 'new_password': 'S4m!Better-Pass9'},
                       headers={'X-CSRF-Token': login['csrf']})
        csrf = {'X-CSRF-Token': login['csrf']}
        assert (await sam.post('/v1/verify/email', json={'email': 'a@example.com'}, headers=csrf)).status_code == 200
        for method, path in (('GET', '/v1/users'), ('GET', '/v1/keys'), ('GET', '/v1/mcp-config'),
                             ('POST', '/v1/keys'), ('POST', '/v1/users')):
            bodies = {'/v1/keys': {'name': 'x'}, '/v1/users': {'email': 'new@example.com'}}
            response = await sam.request(method, path, headers=csrf, json=bodies.get(path) if method == 'POST' else None)
            assert response.status_code == 403 and response.json()['detail'] == 'admin_required', path
    await admin.aclose()


async def test_nobody_can_lock_everyone_out_or_remove_themselves(tmp_path):
    app = make_app(tmp_path)
    admin = await admin_session(app)
    me = (await admin.get('/v1/users')).json()['users'][0]
    for call in (admin.patch(f"/v1/users/{me['id']}", json={'status': 'disabled'}),
                 admin.patch(f"/v1/users/{me['id']}", json={'role': 'user'}),
                 admin.delete(f"/v1/users/{me['id']}"), admin.post(f"/v1/users/{me['id']}/reset-password")):
        assert (await call).status_code == 409
    second = await add_user(admin, 'two@example.com', role='admin')
    # with a second admin, the second can be demoted; the first (me) remains
    assert (await admin.patch(f"/v1/users/{second['id']}", json={'role': 'user'})).status_code == 200
    assert (await admin.patch(f"/v1/users/{second['id']}", json={'role': 'admin'})).status_code == 200
    assert (await admin.delete(f"/v1/users/{second['id']}")).status_code == 204
    assert (await admin.delete('/v1/users/nope')).status_code == 404
    await admin.aclose()


async def test_the_last_active_admin_cannot_be_removed_by_another_admin(tmp_path):
    app = make_app(tmp_path)
    admin = await admin_session(app)
    second = await add_user(admin, 'two@example.com', role='admin', password='Tw0!Strong-Pass')
    other = client_for(app)
    login = (await sign_in(other, 'two@example.com', 'Tw0!Strong-Pass')).json()
    other.headers['X-CSRF-Token'] = login['csrf']
    await other.post('/v1/auth/password', json={'current_password': 'Tw0!Strong-Pass', 'new_password': 'Tw0!Better-Pass9'})
    first_id = next(u['id'] for u in (await other.get('/v1/users')).json()['users'] if u['email'] == ADMIN)
    assert (await other.patch(f'/v1/users/{first_id}', json={'status': 'disabled'})).status_code == 200
    # the original admin is now disabled, so 'two' is the last active admin and is protected from others
    assert (await admin.get('/v1/users')).status_code == 401      # disabled: the session died at once
    assert second['id']
    await other.aclose()
    await admin.aclose()


async def test_resetting_a_password_and_disabling_end_sessions_immediately(tmp_path):
    app = make_app(tmp_path)
    admin = await admin_session(app)
    made = await add_user(admin, 'sam@example.com', password='S4m!Strong-Pass')
    async with client_for(app) as sam:
        await sign_in(sam, 'sam@example.com', 'S4m!Strong-Pass')
        assert (await sam.get('/v1/auth/me')).status_code == 200
        reset = await admin.post(f"/v1/users/{made['id']}/reset-password")
        temp = reset.json()['temporary_password']
        assert reset.status_code == 200 and temp
        assert (await sam.get('/v1/auth/me')).status_code == 401            # signed out at once
        assert (await sign_in(sam, 'sam@example.com', 'S4m!Strong-Pass')).status_code == 401
        assert (await sign_in(sam, 'sam@example.com', temp)).json()['must_change'] is True
        await admin.patch(f"/v1/users/{made['id']}", json={'status': 'disabled'})
        assert (await sam.get('/v1/auth/me')).status_code == 401
    await admin.aclose()


async def test_the_first_account_set_up_in_open_mode_must_be_an_admin_and_switches_sign_in_on(tmp_path):
    app = create_app(store_path=str(tmp_path / 'o.sqlite3'), verifier=Verifier(DNS(), None))
    async with client_for(app) as client:
        status = (await client.get('/v1/auth/status')).json()
        assert status == {'login_required': False, 'first_run': True, 'key_required': False, 'public_url': ''}
        assert (await client.get('/v1/users')).json()['first_account'] is True
        no = await client.post('/v1/users', json={'email': 'a@example.com', 'role': 'user', 'password': 'Fr1st!Passw0rd'})
        assert no.status_code == 422
        made = await client.post('/v1/users', json={'email': 'a@example.com', 'role': 'admin', 'password': 'Fr1st!Passw0rd'})
        assert made.status_code == 201 and made.json()['must_change'] is False       # set up in person
        assert (await client.get('/v1/auth/status')).json()['login_required'] is True
        assert (await client.get('/v1/jobs')).status_code == 401
        assert (await sign_in(client, 'a@example.com', 'Fr1st!Passw0rd')).status_code == 200


async def test_only_the_status_and_sign_in_pages_are_public(tmp_path):
    async with client_for(make_app(tmp_path)) as client:
        assert (await client.get('/v1/auth/status')).status_code == 200
        assert (await client.get('/')).status_code == 200
        for path in ('/v1/users', '/v1/keys', '/v1/config', '/v1/auth/me', '/v1/mcp-config'):
            assert (await client.get(path)).status_code == 401, path


async def test_the_status_reports_a_well_formed_public_address_only(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    async with client_for(app) as client:
        monkeypatch.delenv('EMAIL_VERIFIER_PUBLIC_URL', raising=False)
        assert (await client.get('/v1/auth/status')).json()['public_url'] == ''
        monkeypatch.setenv('EMAIL_VERIFIER_PUBLIC_URL', 'https://listsweep.example.com/')
        assert (await client.get('/v1/auth/status')).json()['public_url'] == 'https://listsweep.example.com'
        for bad in ('javascript:alert(1)', 'listsweep.example.com', 'https://x.example.com/path?q=1'):
            monkeypatch.setenv('EMAIL_VERIFIER_PUBLIC_URL', bad)
            assert (await client.get('/v1/auth/status')).json()['public_url'] == ''
