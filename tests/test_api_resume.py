import asyncio

from verifier.api import create_app
from verifier.csv_input import parse_csv
from verifier.store import Store


async def test_interrupted_job_is_resumed_on_startup(tmp_path):
    path = str(tmp_path / 'resume.sqlite3')
    rows, dupes, _ = parse_csv(b'email\na@example.test\nbad\n')
    job_id = Store(path).create_job(rows, dupes, {'allow_smtp': False, 'concurrency': 2})
    app = create_app(store_path=path, verifier=object(), api_key='k' * 32)
    async with app.router.lifespan_context(app):
        async with asyncio.timeout(5):
            while app.state.service.get_job(job_id)['status'] != 'completed':
                await asyncio.sleep(0.01)
    assert app.state.service.get_job(job_id)['processed'] == 2
