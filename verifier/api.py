"""HTTP API for bounded, persistent CSV verification jobs.

Bind it to loopback or a private network and reach it over an SSH tunnel. It never sends
mail; SMTP probing stays off unless VERIFIER_SMTP_ENABLED=true and a sender identity is set.
"""
import asyncio
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .accounts import (
    DUMMY_HASH,
    LoginThrottle,
    hash_password,
    hash_token,
    new_token,
    normalize_email,
    password_problem,
    temporary_password,
    verify_password,
)
from .connect_info import MCP_TOOLS
from .csv_input import MAX_EMAILS, export_all_csv, export_send_csv
from .job_service import RESULT_VERSION, JobService
from .sheets_input import fetch_sheet_csv
from .xlsx_input import MAX_XLSX_BYTES, xlsx_to_csv

MINIMUM_API_KEY_LENGTH = 32
MAX_UPLOAD_BYTES = 1_500_000
UI_DIR = Path(__file__).resolve().parent / 'ui'
SECURITY_HEADERS = {
    'Content-Security-Policy': (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
    'Cache-Control': 'no-store',
}


class EmailRequest(BaseModel):
    email: str = Field(max_length=320)
    allow_smtp: bool | None = None  # None: use SMTP whenever the server has it enabled
    check_dns: bool = False


MAX_KEYS = 50
MAX_USERS = 200
SESSION_COOKIE = 'ls_session'
SESSION_SECONDS = 12 * 3600
SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}
ACTIVITY_CATEGORIES = {'signins': ('auth.',), 'people': ('user.', 'system.'), 'keys': ('key.',), 'jobs': ('job.',)}
PASSWORD_CHANGE_PATHS = {'/v1/auth/me', '/v1/auth/password', '/v1/auth/logout'}


class LoginRequest(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=300)


class PasswordRequest(BaseModel):
    current_password: str = Field(max_length=300)
    new_password: str = Field(max_length=300)


class UserCreate(BaseModel):
    email: str = Field(max_length=320)
    role: Literal['admin', 'user'] = 'user'
    password: str | None = Field(default=None, max_length=300)   # omit to generate a temporary one


class UserUpdate(BaseModel):
    role: Literal['admin', 'user'] | None = None
    status: Literal['active', 'disabled'] | None = None


class KeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class RecheckRequest(BaseModel):
    scope: str = 'unknown'
    check_dns: bool | None = None     # None: keep the original job's setting
    allow_smtp: bool | None = None


class SheetRequest(BaseModel):
    url: str = Field(max_length=2000)
    allow_smtp: bool | None = None
    check_dns: bool = False
    concurrency: int = 4


def is_public_path(path: str) -> bool:
    """The static page shell carries no data and is the only thing served without the key."""
    return path in ('/healthz', '/', '/v1/auth/login', '/v1/auth/status') or path.startswith('/ui/')


def _supplied_key(request: Request) -> bytes:
    """Accept `X-API-Key: <key>` or `Authorization: Bearer <key>`."""
    supplied = request.headers.get('X-API-Key')
    if supplied is None:
        header = request.headers.get('Authorization', '')
        scheme, _, token = header.partition(' ')
        supplied = token.strip() if scheme.lower() == 'bearer' else ''
    return supplied.encode('utf-8', errors='replace')


class RateLimiter:
    """Fixed-window, in-process limiter. Correct only for a single worker process."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._window = 0
        self._counts: dict[str, int] = {}

    def allow(self, identity: str) -> bool:
        if self.per_minute <= 0:
            return True
        window = int(time.monotonic() // 60)
        if window != self._window:
            self._window, self._counts = window, {}
        self._counts[identity] = self._counts.get(identity, 0) + 1
        return self._counts[identity] <= self.per_minute


def create_app(store_path=None, verifier=None, api_key=None, smtp_enabled=None, rate_limit=None,
               sheet_fetcher=None, admin_email=None, admin_password=None):
    """Create an isolated application; dependencies are injectable for local testing."""
    configured_key = api_key if api_key is not None else (os.getenv('EMAIL_VERIFIER_API_KEY') or None)
    if configured_key is not None and len(configured_key) < MINIMUM_API_KEY_LENGTH:
        raise ValueError('API key must contain at least 32 characters')
    if rate_limit is None:
        rate_limit = int(os.getenv('EMAIL_VERIFIER_RATE_LIMIT_PER_MINUTE', '120'))
    limiter = RateLimiter(rate_limit)
    expected = configured_key.encode('utf-8') if configured_key else None
    fetch_sheet = sheet_fetcher or fetch_sheet_csv

    service = JobService(
        store_path or os.getenv('EMAIL_VERIFIER_DB', 'data/jobs.sqlite3'),
        verifier=verifier, smtp_enabled=smtp_enabled,
    )

    admin_email = admin_email or os.getenv('EMAIL_VERIFIER_ADMIN_EMAIL')
    admin_password = admin_password or os.getenv('EMAIL_VERIFIER_ADMIN_PASSWORD')
    if (os.getenv('EMAIL_VERIFIER_ENV', '').lower() == 'production' and not configured_key
            and not (admin_email and admin_password) and service.store.count_users() == 0):
        raise RuntimeError('Set EMAIL_VERIFIER_API_KEY or EMAIL_VERIFIER_ADMIN_EMAIL and '
                           'EMAIL_VERIFIER_ADMIN_PASSWORD in production')
    if admin_email and admin_password:
        # First-run admin. It is created only if missing, so a password changed later is never overwritten.
        try:
            bootstrap_email = normalize_email(admin_email)
        except ValueError as error:
            raise ValueError('EMAIL_VERIFIER_ADMIN_EMAIL is not a valid email address') from error
        if len(admin_password) > 200:
            raise ValueError('EMAIL_VERIFIER_ADMIN_PASSWORD is too long')
        if service.store.user_by_email(bootstrap_email) is None:
            service.store.add_user(str(uuid.uuid4()), bootstrap_email, hash_password(admin_password), 'admin')
            service.store.log_activity('system', 'system', 'system.admin_created', bootstrap_email,
                                       'first admin from the server settings')
    throttle = LoginThrottle()

    @asynccontextmanager
    async def lifespan(_):
        # Resume jobs interrupted by a restart; results already saved are kept.
        for job in service.store.incomplete():
            try:
                service.schedule(job['id'])
            except PermissionError as error:
                service.store.set_job_status(job['id'], 'failed', error=str(error))
        try:
            yield
        finally:
            await service.close()

    application = FastAPI(title='ListSweep by DigitalPatron', version='0.1.0', lifespan=lifespan)
    application.state.service = service
    application.state.store = service.store
    application.state.verifier = service.verifier

    @application.middleware('http')
    async def guard(request: Request, call_next):
        response = await _authorize(request, call_next)
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        return response

    last_touched: dict[str, float] = {}

    def users_exist() -> bool:
        return service.store.count_users() > 0

    def auth_required() -> bool:
        return expected is not None or service.store.count_keys() > 0 or users_exist()

    def identify(supplied: bytes) -> str | None:
        """Who is this key? 'server' for the environment key, a key id for a managed key, else None."""
        if expected is not None and hmac.compare_digest(supplied, expected):
            return 'server'
        digest = hashlib.sha256(supplied).hexdigest().encode()
        for stored, key_id in service.store.key_hashes().items():
            if hmac.compare_digest(digest, stored.encode()):
                return key_id
        return None

    def is_admin(principal: dict) -> bool:
        kind = principal['type']
        if kind in ('open', 'server'):
            return True
        if kind == 'user':
            return principal['role'] == 'admin'
        return not users_exist()           # a managed key manages access only where there are no accounts

    def require_admin(request: Request) -> dict:
        principal = request.state.principal
        if principal is None or not is_admin(principal):
            raise HTTPException(403, 'admin_required')
        return principal

    def address_of(request: Request) -> str:
        return request.client.host if request.client else ''

    def actor_of(request: Request) -> tuple[str, str]:
        principal = getattr(request.state, 'principal', None)
        if principal is None:
            return 'anonymous', 'none'
        if principal['type'] == 'user':
            return principal['email'], 'user'
        if principal['type'] == 'server':
            return 'server key', 'server'
        if principal['type'] == 'key':
            return service.store.key_name(principal['id']) or 'deleted key', 'key'
        return 'open access', 'open'

    def record(request: Request, action: str, target: str = '', detail: str = '') -> None:
        """Add to the activity log. A logging problem must never break the action itself."""
        try:
            actor, kind = actor_of(request)
            service.store.log_activity(actor, kind, action, target, detail, address_of(request))
        except sqlite3.Error:
            pass

    def record_as(email: str, kind: str, action: str, request: Request, detail: str = '') -> None:
        try:
            service.store.log_activity(email, kind, action, '', detail, address_of(request))
        except sqlite3.Error:
            pass

    async def _authorize(request: Request, call_next):
        path = request.url.path
        request.state.principal = None
        if is_public_path(path):
            return await call_next(request)
        principal = {'type': 'open'}
        if auth_required():
            principal = None
            bearer = request.headers.get('Authorization', '').lower().startswith('bearer ')
            if request.headers.get('X-API-Key') is not None or bearer:
                who = identify(_supplied_key(request))
                if who is None:
                    return JSONResponse(status_code=401, content={'detail': 'invalid_api_key'})
                principal = {'type': 'server' if who == 'server' else 'key', 'id': who}
                now = time.monotonic()
                if who != 'server' and now - last_touched.get(who, -120.0) > 60:   # at most once a minute
                    last_touched[who] = now
                    service.store.touch_key(who)
            else:
                token = request.cookies.get(SESSION_COOKIE)
                session = service.store.session(hash_token(token)) if token else None
                if session is None:
                    return JSONResponse(status_code=401, content={
                        'detail': 'login_required' if users_exist() else 'invalid_api_key'})
                principal = {'type': 'user', 'id': session['user_id'], 'email': session['email'],
                             'role': session['role'], 'must_change': bool(session['must_change']),
                             'csrf': session['csrf'], 'token_hash': hash_token(token)}
                if request.method not in SAFE_METHODS and not hmac.compare_digest(
                        request.headers.get('X-CSRF-Token', '').encode(), session['csrf'].encode()):
                    return JSONResponse(status_code=403, content={'detail': 'csrf_failed'})
                if principal['must_change'] and path not in PASSWORD_CHANGE_PATHS:
                    return JSONResponse(status_code=403, content={'detail': 'password_change_required'})
        request.state.principal = principal
        if not limiter.allow(principal.get('id', 'anonymous')):
            return JSONResponse(status_code=429, content={'detail': 'rate_limited'},
                                headers={'Retry-After': '60'})
        return await call_next(request)

    @application.get('/healthz')
    async def health():
        return {'ok': True, 'service': 'local-email-verifier'}

    def smtp_default(allow_smtp):
        """An unspecified allow_smtp means: check mailboxes whenever this server can."""
        return service.smtp_enabled if allow_smtp is None else allow_smtp

    def submit(request, content, allow_smtp, concurrency, check_dns, filename):
        allow_smtp = smtp_default(allow_smtp)
        try:
            job_id = service.submit_csv(
                content, allow_smtp=allow_smtp, concurrency=concurrency,
                filename=filename, check_dns=check_dns,
            )
        except PermissionError as error:
            raise HTTPException(403, str(error)) from error
        except (ValueError, TypeError) as error:
            raise HTTPException(422, str(error)) from error
        job = service.get_job(job_id)
        depth = 'mailbox checks' if allow_smtp else ('domain checks' if check_dns else 'syntax only')
        record(request, 'job.started', filename, f"{job['total']} addresses, {depth}")
        return {**job, 'job_id': job_id, 'max_emails': MAX_EMAILS}

    @application.post('/v1/verify/csv', status_code=202)
    async def verify_csv(request: Request, file: Annotated[UploadFile, File()],
                         allow_smtp: Annotated[bool | None, Form()] = None,
                         concurrency: Annotated[int, Form()] = 4,
                         check_dns: Annotated[bool, Form()] = False):
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        return submit(request, content, allow_smtp, concurrency, check_dns, file.filename or 'upload.csv')

    @application.post('/v1/verify/email')
    async def verify_email(body: EmailRequest):
        """Verify one address immediately; nothing is stored."""
        try:
            return await service.verify_one(body.email, smtp_default(body.allow_smtp), body.check_dns)
        except PermissionError as error:
            raise HTTPException(403, str(error)) from error
        except (ValueError, TypeError) as error:
            raise HTTPException(422, str(error)) from error

    @application.post('/v1/verify/xlsx', status_code=202)
    async def verify_xlsx(request: Request, file: Annotated[UploadFile, File()],
                          allow_smtp: Annotated[bool | None, Form()] = None,
                          concurrency: Annotated[int, Form()] = 4,
                          check_dns: Annotated[bool, Form()] = False):
        raw = await file.read(MAX_XLSX_BYTES + 1)
        try:
            content = xlsx_to_csv(raw)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return submit(request, content, allow_smtp, concurrency, check_dns, file.filename or 'upload.xlsx')

    @application.post('/v1/verify/sheet', status_code=202)
    async def verify_sheet(body: SheetRequest, request: Request):
        try:
            content = await fetch_sheet(body.url)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return submit(request, content, body.allow_smtp, body.concurrency, body.check_dns, 'google-sheet.csv')

    @application.get('/v1/config')
    async def config():
        return {
            'smtp_available': service.smtp_enabled,
            'smtp_default': service.smtp_enabled,
            'engine': RESULT_VERSION,
            'dns_check_available': True,
            'max_emails': MAX_EMAILS,
            'max_csv_bytes': MAX_UPLOAD_BYTES,
            'max_xlsx_bytes': MAX_XLSX_BYTES,
            'concurrency_range': [1, 25],
            'auth_required': auth_required(),
        }

    @application.get('/v1/jobs')
    async def list_jobs(limit: int = 50):
        return service.store.list_jobs(max(1, min(limit, 200)))

    def job_or_404(job_id):
        job = service.get_job(job_id)
        if job is None:
            raise HTTPException(404, 'job_not_found')
        return job

    @application.get('/v1/jobs/{job_id}')
    async def get_job(job_id: str):
        return {**job_or_404(job_id), 'job_id': job_id}

    def page(job_id, send_only, offset, limit):
        job_or_404(job_id)
        try:
            return service.get_results(job_id, send_only, offset, limit)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @application.get('/v1/jobs/{job_id}/results')
    async def results(job_id: str, send_only: bool = False, offset: int = 0, limit: int = 1000):
        return page(job_id, send_only, offset, limit)

    @application.get('/v1/jobs/{job_id}/results.csv')
    async def results_csv(job_id: str, send_only: bool = False):
        job = job_or_404(job_id)
        rows = [r for r in service.store.results(job_id) if r['send_eligible'] or not send_only]
        text = export_send_csv(rows) if send_only else export_all_csv(rows)
        stem = re.sub(r'[^A-Za-z0-9._-]+', '-', Path(job['options'].get('filename') or 'results').stem)
        name = f"{stem.strip('-')[:60] or 'results'}-{'send-eligible' if send_only else 'all'}.csv"
        return PlainTextResponse(text, media_type='text/csv',
                                 headers={'Content-Disposition': f'attachment; filename="{name}"'})

    @application.post('/v1/jobs/{job_id}/cancel')
    async def cancel_job(job_id: str, request: Request):
        job = job_or_404(job_id)
        try:
            service.cancel(job_id)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        await asyncio.sleep(0)  # let a cancelled task record its status
        record(request, 'job.cancelled', job['options'].get('filename') or job_id)
        return {**service.get_job(job_id), 'job_id': job_id}

    @application.post('/v1/jobs/{job_id}/recheck', status_code=202)
    async def recheck_job(job_id: str, request: Request, body: RecheckRequest | None = None):
        job_or_404(job_id)
        try:
            new_id = service.recheck(
                job_id, body.scope if body else 'unknown',
                check_dns=body.check_dns if body else None,
                allow_smtp=body.allow_smtp if body else None)
        except PermissionError as error:
            raise HTTPException(403, str(error)) from error
        except ValueError as error:
            raise HTTPException(409 if 'Wait' in str(error) else 422, str(error)) from error
        new_job = service.get_job(new_id)
        record(request, 'job.rechecked', new_job['options'].get('filename') or new_id,
               f"{new_job['total']} addresses, {(body.scope if body else 'unknown')}")
        return {**new_job, 'job_id': new_id}

    # ---------- sign-in ----------
    def public_user(user: dict) -> dict:
        return {key: user[key] for key in ('id', 'email', 'role', 'status', 'created_at', 'last_login_at')} | {
            'must_change': bool(user['must_change'])}

    def attempted_label(email: str) -> str:
        try:
            return normalize_email(email)
        except ValueError:
            return '(not an email address)'

    @application.get('/v1/auth/status')
    async def auth_status():
        users = users_exist()
        return {'login_required': users, 'first_run': not auth_required(),
                'key_required': (expected is not None or service.store.count_keys() > 0) and not users}

    @application.post('/v1/auth/login')
    async def login(body: LoginRequest, request: Request, response: Response):
        address = request.client.host if request.client else 'unknown'
        email = body.email.strip().lower()
        user_key, ip_key = f'{address}|{email}', f'ip|{address}'
        wait = throttle.blocked_for(user_key, ip_key)
        if wait:
            if throttle.first_block(ip_key):
                record_as(attempted_label(email), 'none', 'auth.login_blocked', request, 'too many failed attempts')
            raise HTTPException(429, f'Too many sign-in attempts. Try again in {max(1, wait // 60)} minutes.',
                                headers={'Retry-After': str(wait)})
        user = service.store.user_by_email(email)
        matched = verify_password(body.password, user['password_hash'] if user else DUMMY_HASH)
        if not (user and matched and user['status'] == 'active'):
            throttle.record(user_key, ip_key)
            record_as(attempted_label(email), 'none', 'auth.login_failed', request, 'wrong email or password')
            raise HTTPException(401, 'Wrong email or password')
        throttle.clear(user_key)
        token, csrf = new_token(), new_token()
        service.store.create_session(hash_token(token), user['id'], csrf, time.time() + SESSION_SECONDS)
        service.store.touch_login(user['id'])
        record_as(user['email'], 'user', 'auth.login', request)
        secure = request.url.scheme == 'https' or request.headers.get('X-Forwarded-Proto') == 'https'
        response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                            samesite='strict', secure=secure, path='/')
        return {'email': user['email'], 'role': user['role'], 'must_change': bool(user['must_change']),
                'csrf': csrf}

    @application.get('/v1/auth/me')
    async def me(request: Request):
        principal = request.state.principal
        if principal['type'] == 'user':
            return {'email': principal['email'], 'role': principal['role'], 'method': 'user',
                    'must_change': principal['must_change'], 'csrf': principal['csrf']}
        return {'email': None, 'role': 'admin' if is_admin(principal) else 'user',
                'method': principal['type'], 'must_change': False, 'csrf': None}

    @application.post('/v1/auth/logout', status_code=204)
    async def logout(request: Request, response: Response):
        principal = request.state.principal
        if principal and principal['type'] == 'user':
            record(request, 'auth.logout')
            service.store.delete_session(principal['token_hash'])
        response.delete_cookie(SESSION_COOKIE, path='/')

    @application.post('/v1/auth/password', status_code=204)
    async def change_password(body: PasswordRequest, request: Request):
        principal = request.state.principal
        if principal['type'] != 'user':
            raise HTTPException(400, 'Sign in with an account to change a password')
        address = request.client.host if request.client else 'unknown'
        user_key = f"pw|{principal['id']}"
        wait = throttle.blocked_for(user_key, f'ip|{address}')
        if wait:
            raise HTTPException(429, 'Too many attempts. Try again later.', headers={'Retry-After': str(wait)})
        row = service.store.user_by_email(principal['email'])
        if not verify_password(body.current_password, row['password_hash']):
            throttle.record(user_key, f'ip|{address}')
            raise HTTPException(403, 'Your current password is not right')
        if body.new_password == body.current_password:
            raise HTTPException(422, 'Choose a password different from the current one')
        problem = password_problem(body.new_password, principal['email'])
        if problem:
            raise HTTPException(422, problem)
        service.store.set_password(principal['id'], hash_password(body.new_password), False)
        service.store.delete_user_sessions(principal['id'], keep_token_hash=principal['token_hash'])
        throttle.clear(user_key)
        record(request, 'auth.password_changed')

    # ---------- people (admin) ----------
    @application.get('/v1/users')
    async def list_users(request: Request):
        require_admin(request)
        return {'users': [public_user(u) for u in service.store.list_users()], 'max_users': MAX_USERS,
                'first_account': not users_exist()}

    @application.post('/v1/users', status_code=201)
    async def create_user(body: UserCreate, request: Request):
        require_admin(request)
        try:
            email = normalize_email(body.email)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        first = not users_exist()
        if first and body.role != 'admin':
            raise HTTPException(422, 'The first account must be an admin')
        if service.store.count_users() >= MAX_USERS:
            raise HTTPException(409, f'At most {MAX_USERS} accounts are allowed')
        if service.store.user_by_email(email):
            raise HTTPException(409, 'There is already an account with that email')
        chosen = body.password
        if chosen is not None:
            problem = password_problem(chosen, email)
            if problem:
                raise HTTPException(422, problem)
        password = chosen if chosen is not None else temporary_password()
        # Someone else's password is always temporary, except the very first admin set up in person.
        user = service.store.add_user(str(uuid.uuid4()), email, hash_password(password), body.role,
                                      must_change=not first)
        record(request, 'user.created', email, f'role {body.role}' + (', temporary password' if chosen is None else ''))
        return {**public_user({**user, 'must_change': user['must_change']}),
                'temporary_password': password if chosen is None else None}

    def target_user(user_id: str) -> dict:
        user = service.store.user(user_id)
        if user is None:
            raise HTTPException(404, 'user_not_found')
        return user

    def would_remove_last_admin(user: dict, role: str | None, status: str | None) -> bool:
        losing = user['role'] == 'admin' and user['status'] == 'active' and (
            (role is not None and role != 'admin') or (status is not None and status != 'active'))
        return losing and service.store.count_active_admins() <= 1

    @application.patch('/v1/users/{user_id}')
    async def update_user(user_id: str, body: UserUpdate, request: Request):
        principal = require_admin(request)
        user = target_user(user_id)
        if principal.get('id') == user_id:
            raise HTTPException(409, "You can't change your own role or disable your own account")
        if would_remove_last_admin(user, body.role, body.status):
            raise HTTPException(409, 'That would leave no active admin')
        service.store.update_user(user_id, role=body.role, status=body.status)
        if body.status == 'disabled':
            service.store.delete_user_sessions(user_id)
        changes = ([f'role {user["role"]} to {body.role}'] if body.role and body.role != user['role'] else []) + (
            [body.status] if body.status and body.status != user['status'] else [])
        record(request, 'user.updated', user['email'], ', '.join(changes))
        return public_user(service.store.user(user_id))

    @application.post('/v1/users/{user_id}/reset-password')
    async def reset_password(user_id: str, request: Request):
        principal = require_admin(request)
        target_user(user_id)
        if principal.get('id') == user_id:
            raise HTTPException(409, 'Use the Account page to change your own password')
        password = temporary_password()
        service.store.set_password(user_id, hash_password(password), True)
        service.store.delete_user_sessions(user_id)
        record(request, 'user.password_reset', service.store.user(user_id)['email'], 'signed out everywhere')
        return {'temporary_password': password}

    @application.delete('/v1/users/{user_id}', status_code=204)
    async def delete_user(user_id: str, request: Request):
        principal = require_admin(request)
        user = target_user(user_id)
        if principal.get('id') == user_id:
            raise HTTPException(409, "You can't delete your own account")
        if would_remove_last_admin(user, 'user', None):
            raise HTTPException(409, 'That would leave no active admin')
        service.store.delete_user(user_id)
        record(request, 'user.deleted', user['email'])

    def log_key(request: Request, action: str, name: str, prefix: str) -> None:
        record(request, action, name, f'key {prefix}...' if prefix else '')

    @application.get('/v1/keys')
    async def list_keys(request: Request):
        require_admin(request)
        return {'keys': service.store.list_keys(), 'server_key': expected is not None,
                'max_keys': MAX_KEYS}

    @application.post('/v1/keys', status_code=201)
    async def create_key(body: KeyRequest, request: Request):
        require_admin(request)
        name = body.name.strip()
        if not name:
            raise HTTPException(422, 'Give the key a name')
        if service.store.count_keys() >= MAX_KEYS:
            raise HTTPException(409, f'You can have at most {MAX_KEYS} keys; delete one first')
        secret = 'ls_' + secrets.token_urlsafe(32)
        record = service.store.add_key(str(uuid.uuid4()), name, hashlib.sha256(secret.encode()).hexdigest(),
                                       secret[:8])
        log_key(request, 'key.created', name, secret[:8])
        return {**record, 'key': secret}   # the only time the key itself is ever returned

    @application.delete('/v1/keys/{key_id}', status_code=204)
    async def delete_key(key_id: str, request: Request):
        require_admin(request)
        doomed = next((k for k in service.store.list_keys() if k['id'] == key_id), None)
        if not service.store.delete_key(key_id):
            raise HTTPException(404, 'key_not_found')
        log_key(request, 'key.deleted', doomed['name'] if doomed else key_id, doomed['prefix'] if doomed else '')

    @application.get('/v1/activity')
    async def activity(request: Request, category: str = 'all', limit: int = 50, offset: int = 0):
        require_admin(request)
        if category != 'all' and category not in ACTIVITY_CATEGORIES:
            raise HTTPException(422, 'category must be all, signins, people, keys or jobs')
        rows, total = service.store.list_activity(
            None if category == 'all' else ACTIVITY_CATEGORIES[category],
            max(1, min(limit, 200)), max(0, offset))
        return {'items': rows, 'total': total, 'offset': max(0, offset)}

    @application.get('/v1/mcp-config')
    async def mcp_config(request: Request):
        require_admin(request)
        """Settings for connecting an MCP client, filled in from how this server is running."""
        env = {'PYTHONPATH': str(Path(__file__).resolve().parent.parent),
               'VERIFIER_DB': str(Path(service.store.path).resolve())}
        if service.smtp_enabled:
            env['VERIFIER_SMTP_ENABLED'] = 'true'
            for name in ('VERIFIER_HELO_DOMAIN', 'VERIFIER_MAIL_FROM'):
                if os.getenv(name):
                    env[name] = os.environ[name]
        return {'command': sys.executable, 'args': ['-m', 'verifier.mcp_connector'], 'env': env,
                'tools': [{'name': n, 'description': d} for n, d in MCP_TOOLS]}

    @application.get('/', include_in_schema=False)
    async def index():
        return FileResponse(UI_DIR / 'index.html')

    application.mount('/ui', StaticFiles(directory=UI_DIR), name='ui')

    return application


app = create_app()
