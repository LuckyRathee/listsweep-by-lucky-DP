"""HTTP API for bounded, persistent CSV verification jobs.

Bind it to loopback or a private network and reach it over an SSH tunnel. It never sends
mail; SMTP probing stays off unless VERIFIER_SMTP_ENABLED=true and a sender identity is set.
"""
import hashlib
import hmac
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .csv_input import MAX_EMAILS, export_all_csv, export_send_csv
from .job_service import JobService
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
    allow_smtp: bool = False
    check_dns: bool = False


class SheetRequest(BaseModel):
    url: str = Field(max_length=2000)
    allow_smtp: bool = False
    check_dns: bool = False
    concurrency: int = 4


def is_public_path(path: str) -> bool:
    """The static page shell carries no data and is the only thing served without the key."""
    return path in ('/healthz', '/') or path.startswith('/ui/')


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
               sheet_fetcher=None):
    """Create an isolated application; dependencies are injectable for local testing."""
    configured_key = api_key if api_key is not None else os.getenv('EMAIL_VERIFIER_API_KEY')
    if configured_key is not None and len(configured_key) < MINIMUM_API_KEY_LENGTH:
        raise ValueError('API key must contain at least 32 characters')
    if os.getenv('EMAIL_VERIFIER_ENV', '').lower() == 'production' and not configured_key:
        raise RuntimeError('EMAIL_VERIFIER_API_KEY is required in production')
    if rate_limit is None:
        rate_limit = int(os.getenv('EMAIL_VERIFIER_RATE_LIMIT_PER_MINUTE', '120'))
    limiter = RateLimiter(rate_limit)
    expected = configured_key.encode('utf-8') if configured_key else None
    fetch_sheet = sheet_fetcher or fetch_sheet_csv

    service = JobService(
        store_path or os.getenv('EMAIL_VERIFIER_DB', 'data/jobs.sqlite3'),
        verifier=verifier, smtp_enabled=smtp_enabled,
    )

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

    application = FastAPI(title='Email Verifier', version='0.1.0', lifespan=lifespan)
    application.state.service = service
    application.state.store = service.store
    application.state.verifier = service.verifier

    @application.middleware('http')
    async def guard(request: Request, call_next):
        response = await _authorize(request, call_next)
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        return response

    async def _authorize(request: Request, call_next):
        if is_public_path(request.url.path):
            return await call_next(request)
        identity = 'anonymous'
        if expected is not None:
            supplied = _supplied_key(request)
            if not hmac.compare_digest(supplied, expected):
                return JSONResponse(status_code=401, content={'detail': 'invalid_api_key'})
            identity = hashlib.sha256(supplied).hexdigest()
        if not limiter.allow(identity):
            return JSONResponse(status_code=429, content={'detail': 'rate_limited'},
                                headers={'Retry-After': '60'})
        return await call_next(request)

    @application.get('/healthz')
    async def health():
        return {'ok': True, 'service': 'local-email-verifier'}

    def submit(content, allow_smtp, concurrency, check_dns, filename):
        try:
            job_id = service.submit_csv(
                content, allow_smtp=allow_smtp, concurrency=concurrency,
                filename=filename, check_dns=check_dns,
            )
        except PermissionError as error:
            raise HTTPException(403, str(error)) from error
        except (ValueError, TypeError) as error:
            raise HTTPException(422, str(error)) from error
        return {**service.get_job(job_id), 'job_id': job_id, 'max_emails': MAX_EMAILS}

    @application.post('/v1/verify/csv', status_code=202)
    async def verify_csv(file: Annotated[UploadFile, File()],
                         allow_smtp: Annotated[bool, Form()] = False,
                         concurrency: Annotated[int, Form()] = 4,
                         check_dns: Annotated[bool, Form()] = False):
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        return submit(content, allow_smtp, concurrency, check_dns, file.filename or 'upload.csv')

    @application.post('/v1/verify/email')
    async def verify_email(body: EmailRequest):
        """Verify one address immediately; nothing is stored."""
        try:
            return await service.verify_one(body.email, body.allow_smtp, body.check_dns)
        except PermissionError as error:
            raise HTTPException(403, str(error)) from error
        except (ValueError, TypeError) as error:
            raise HTTPException(422, str(error)) from error

    @application.post('/v1/verify/xlsx', status_code=202)
    async def verify_xlsx(file: Annotated[UploadFile, File()],
                          allow_smtp: Annotated[bool, Form()] = False,
                          concurrency: Annotated[int, Form()] = 4,
                          check_dns: Annotated[bool, Form()] = False):
        raw = await file.read(MAX_XLSX_BYTES + 1)
        try:
            content = xlsx_to_csv(raw)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return submit(content, allow_smtp, concurrency, check_dns, file.filename or 'upload.xlsx')

    @application.post('/v1/verify/sheet', status_code=202)
    async def verify_sheet(body: SheetRequest):
        try:
            content = await fetch_sheet(body.url)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return submit(content, body.allow_smtp, body.concurrency, body.check_dns, 'google-sheet.csv')

    @application.get('/v1/config')
    async def config():
        return {
            'smtp_available': service.smtp_enabled,
            'dns_check_available': True,
            'max_emails': MAX_EMAILS,
            'max_csv_bytes': MAX_UPLOAD_BYTES,
            'max_xlsx_bytes': MAX_XLSX_BYTES,
            'concurrency_range': [1, 25],
            'auth_required': expected is not None,
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
        job_or_404(job_id)
        rows = [r for r in service.store.results(job_id) if r['send_eligible'] or not send_only]
        text = export_send_csv(rows) if send_only else export_all_csv(rows)
        return PlainTextResponse(text, media_type='text/csv')

    @application.get('/', include_in_schema=False)
    async def index():
        return FileResponse(UI_DIR / 'index.html')

    application.mount('/ui', StaticFiles(directory=UI_DIR), name='ui')

    return application


app = create_app()
