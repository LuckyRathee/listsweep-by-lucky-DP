"""Authentication, throttling and error shapes for the HTTP API."""
from httpx import ASGITransport, AsyncClient

from verifier.api import create_app

KEY = 'k' * 32


def client_for(tmp_path, **kwargs):
    app = create_app(store_path=str(tmp_path / 's.sqlite3'), verifier=object(), **kwargs)
    return AsyncClient(transport=ASGITransport(app=app), base_url='http://test')


async def test_x_api_key_and_bearer_are_both_accepted(tmp_path):
    async with client_for(tmp_path, api_key=KEY) as client:
        assert (await client.get('/v1/jobs/none', headers={'X-API-Key': KEY})).status_code == 404
        bearer = {'Authorization': f'Bearer {KEY}'}
        assert (await client.get('/v1/jobs/none', headers=bearer)).status_code == 404


async def test_wrong_missing_and_non_ascii_keys_are_401_not_500(tmp_path):
    async with client_for(tmp_path, api_key=KEY) as client:
        assert (await client.get('/v1/jobs/none')).status_code == 401
        assert (await client.get('/v1/jobs/none', headers={'X-API-Key': 'wrong'})).status_code == 401
        odd = {'X-API-Key': 'café'.encode('latin-1')}
        assert (await client.get('/v1/jobs/none', headers=odd)).status_code == 401
        assert (await client.get('/v1/jobs/none',
                                 headers={'Authorization': 'Basic abc'})).status_code == 401


async def test_per_key_rate_limit_returns_429_but_health_is_exempt(tmp_path):
    async with client_for(tmp_path, api_key=KEY, rate_limit=3) as client:
        headers = {'X-API-Key': KEY}
        codes = [(await client.get('/v1/jobs/none', headers=headers)).status_code for _ in range(5)]
        assert codes == [404, 404, 404, 429, 429]
        assert (await client.get('/healthz')).status_code == 200


async def test_smtp_is_refused_with_403_when_not_enabled(tmp_path):
    async with client_for(tmp_path, api_key=KEY) as client:
        response = await client.post(
            '/v1/verify/csv', headers={'X-API-Key': KEY}, data={'allow_smtp': 'true'},
            files={'file': ('a.csv', 'email\na@example.test\n', 'text/csv')})
        assert response.status_code == 403


async def test_offline_job_does_no_network_and_reports_unknown(tmp_path):
    import asyncio
    async with client_for(tmp_path, api_key=KEY) as client:
        headers = {'X-API-Key': KEY}
        created = await client.post(
            '/v1/verify/csv', headers=headers,
            files={'file': ('a.csv', 'name,email\nA,a@example.test\n', 'text/csv')})
        assert created.status_code == 202
        job_id = created.json()['job_id']
        for _ in range(50):
            job = (await client.get(f'/v1/jobs/{job_id}', headers=headers)).json()
            if job['status'] == 'completed':
                break
            await asyncio.sleep(0.02)
        rows = (await client.get(f'/v1/jobs/{job_id}/results', headers=headers)).json()
        assert rows[0]['reason'] == 'verification_disabled' and not rows[0]['send_eligible']
