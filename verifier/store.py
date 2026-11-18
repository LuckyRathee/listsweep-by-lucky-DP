"""SQLite jobs and resumable result persistence."""
import json
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = '''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 status TEXT NOT NULL, total INTEGER NOT NULL, processed INTEGER NOT NULL DEFAULT 0,
 duplicate_count INTEGER NOT NULL DEFAULT 0, error TEXT,
 options TEXT NOT NULL DEFAULT '{}' );
CREATE TABLE IF NOT EXISTS results (
 job_id TEXT NOT NULL, position INTEGER NOT NULL, email TEXT NOT NULL,
 original_email TEXT NOT NULL, normalized_email TEXT, domain TEXT,
 status TEXT NOT NULL, reason TEXT NOT NULL, flags TEXT NOT NULL DEFAULT '[]',
 disposable INTEGER NOT NULL DEFAULT 0, role_based INTEGER NOT NULL DEFAULT 0,
 catch_all INTEGER NOT NULL DEFAULT 0, smtp_code INTEGER, mx_host TEXT, checked_at TEXT,
 metadata TEXT NOT NULL DEFAULT '{}', PRIMARY KEY (job_id, position)
);
CREATE INDEX IF NOT EXISTS results_status_idx ON results(job_id, status);
CREATE TABLE IF NOT EXISTS api_keys (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE, prefix TEXT NOT NULL,
 created_at TEXT NOT NULL, last_used_at TEXT
);
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
 role TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', must_change INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, csrf TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id);
CREATE TABLE IF NOT EXISTS activity (
 id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL, actor_type TEXT NOT NULL,
 action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS suppressions (
 email TEXT PRIMARY KEY, reason TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL
);
'''
RESULT_FIELDS = 'email, status, reason, send_eligible, deliverable_candidate, disposable, role_based, catch_all, smtp_code, checked_at, mx_host'


class Store:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def now():
        return datetime.now(UTC).isoformat()

    def create_job(self, rows, duplicate_count=0, options=None):
        job_id = str(uuid.uuid4())
        now = self.now()
        with self._connect() as db:
            db.execute('INSERT INTO jobs(id, created_at, updated_at, status, total, duplicate_count, options) VALUES (?,?,?,?,?,?,?)',
                (job_id, now, now, 'queued', len(rows), duplicate_count, json.dumps(options or {})))
            db.executemany('INSERT INTO results(job_id, position, email, original_email, metadata, status, reason) VALUES (?,?,?,?,?,?,?)',
                [(job_id, r.position, r.email, r.original, json.dumps(r.metadata), 'unknown', 'queued') for r in rows])
        return job_id

    def set_job_status(self, job_id, status, processed=None, error=None):
        fields, values = ['status=?', 'updated_at=?'], [status, self.now()]
        if processed is not None:
            fields.append('processed=?')
            values.append(processed)
        if error is not None:
            fields.append('error=?')
            values.append(error)
        values.append(job_id)
        with self._connect() as db:
            db.execute(f'UPDATE jobs SET {", ".join(fields)} WHERE id=?', values)

    def save_result(self, job_id, position, result):
        now = self.now()
        with self._connect() as db:
            db.execute('''UPDATE results SET normalized_email=?, domain=?, status=?, reason=?, flags=?,
                disposable=?, role_based=?, catch_all=?, smtp_code=?, mx_host=?, checked_at=?
                WHERE job_id=? AND position=?''', (
                result.get('normalized_email'), result.get('domain'), result['status'], result['reason'],
                json.dumps(result.get('flags', [])), int(result.get('disposable', False)),
                int(result.get('role_based', False)), int(result.get('catch_all', False)),
                result.get('smtp_code'), result.get('mx_host'), now, job_id, position))
            db.execute('''UPDATE jobs SET processed=(SELECT COUNT(*) FROM results WHERE job_id=? AND reason != 'queued'), updated_at=? WHERE id=?''', (job_id, now, job_id))

    def pending(self, job_id):
        with self._connect() as db:
            return db.execute('SELECT position, email FROM results WHERE job_id=? AND reason=? ORDER BY position', (job_id, 'queued')).fetchall()

    def job(self, job_id):
        with self._connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not row:
                return None
            item = self._job_dict(db, row)
            return item

    def _job_dict(self, db, row):
        item = dict(row)
        item['options'] = json.loads(item['options'])
        item['counts'] = dict(db.execute(
            'SELECT status, COUNT(*) FROM results WHERE job_id=? GROUP BY status',
            (item['id'],),
        ).fetchall())
        item['reason_counts'] = dict(db.execute(
            'SELECT reason, COUNT(*) FROM results WHERE job_id=? GROUP BY reason',
            (item['id'],),
        ).fetchall())
        item['eligible_count'] = sum(
            1 for r in self.results(item['id']) if r['send_eligible']
        )
        return item

    def list_jobs(self, limit=50):
        """Newest jobs first, without per-address rows."""
        with self._connect() as db:
            rows = db.execute('SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?', (limit,)).fetchall()
            jobs = []
            for row in rows:
                item = dict(row)
                item['options'] = json.loads(item['options'])
                item['counts'] = dict(db.execute(
                    'SELECT status, COUNT(*) FROM results WHERE job_id=? GROUP BY status',
                    (item['id'],)).fetchall())
                item['reason_counts'] = dict(db.execute(
                    'SELECT reason, COUNT(*) FROM results WHERE job_id=? GROUP BY reason',
                    (item['id'],)).fetchall())
                jobs.append(item)
            return jobs

    def incomplete(self):
        """Return interrupted and not-yet-finished jobs with their saved options."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM jobs WHERE status IN ('queued', 'running') ORDER BY created_at"
            ).fetchall()
            return [self._job_dict(db, row) for row in rows]

    def results(self, job_id):
        with self._connect() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM results WHERE job_id=? ORDER BY position', (job_id,)).fetchall()]
        for row in rows:
            row['flags'] = json.loads(row.pop('flags'))
            row['metadata'] = json.loads(row.pop('metadata'))
            row['deliverable_candidate'] = row['status'] == 'deliverable_candidate'
            row['send_eligible'] = (
                row['status'] == 'deliverable_candidate'
                and row['reason'] == 'smtp_rcpt_accepted'
                and not row['catch_all']
                and not row['role_based']
                and not row['disposable']
            )
        return rows

    # --- API keys: only a SHA-256 hash and a short display prefix are stored, never the key ---
    def add_key(self, key_id, name, key_hash, prefix):
        with self._connect() as db:
            db.execute('INSERT INTO api_keys(id, name, key_hash, prefix, created_at) VALUES (?,?,?,?,?)',
                       (key_id, name, key_hash, prefix, self.now()))
            return dict(db.execute('SELECT id, name, prefix, created_at, last_used_at FROM api_keys WHERE id=?',
                                   (key_id,)).fetchone())

    def list_keys(self):
        with self._connect() as db:
            return [dict(r) for r in db.execute(
                'SELECT id, name, prefix, created_at, last_used_at FROM api_keys ORDER BY created_at DESC').fetchall()]

    def key_hashes(self):
        with self._connect() as db:
            return {row['key_hash']: row['id'] for row in db.execute('SELECT id, key_hash FROM api_keys').fetchall()}

    def count_keys(self):
        with self._connect() as db:
            return db.execute('SELECT COUNT(*) FROM api_keys').fetchone()[0]

    def delete_key(self, key_id):
        with self._connect() as db:
            return db.execute('DELETE FROM api_keys WHERE id=?', (key_id,)).rowcount > 0

    def touch_key(self, key_id):
        with self._connect() as db:
            db.execute('UPDATE api_keys SET last_used_at=? WHERE id=?', (self.now(), key_id))

    # --- accounts: password hashes and session-token hashes only ---
    PUBLIC_USER = 'id, email, role, status, must_change, created_at, last_login_at'

    def add_user(self, user_id, email, password_hash, role, must_change=False):
        with self._connect() as db:
            db.execute('INSERT INTO users(id, email, password_hash, role, status, must_change, created_at) VALUES (?,?,?,?,?,?,?)',
                       (user_id, email, password_hash, role, 'active', int(must_change), self.now()))
        return self.user(user_id)

    def user(self, user_id):
        with self._connect() as db:
            row = db.execute(f'SELECT {self.PUBLIC_USER} FROM users WHERE id=?', (user_id,)).fetchone()
            return dict(row) if row else None

    def user_by_email(self, email):
        with self._connect() as db:
            row = db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
            return dict(row) if row else None

    def list_users(self):
        with self._connect() as db:
            return [dict(r) for r in db.execute(f'SELECT {self.PUBLIC_USER} FROM users ORDER BY created_at').fetchall()]

    def count_users(self):
        with self._connect() as db:
            return db.execute('SELECT COUNT(*) FROM users').fetchone()[0]

    def count_active_admins(self):
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND status='active'").fetchone()[0]

    def update_user(self, user_id, role=None, status=None):
        with self._connect() as db:
            if role is not None:
                db.execute('UPDATE users SET role=? WHERE id=?', (role, user_id))
            if status is not None:
                db.execute('UPDATE users SET status=? WHERE id=?', (status, user_id))

    def set_password(self, user_id, password_hash, must_change):
        with self._connect() as db:
            db.execute('UPDATE users SET password_hash=?, must_change=? WHERE id=?', (password_hash, int(must_change), user_id))

    def touch_login(self, user_id):
        with self._connect() as db:
            db.execute('UPDATE users SET last_login_at=? WHERE id=?', (self.now(), user_id))

    def delete_user(self, user_id):
        with self._connect() as db:
            db.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))
            return db.execute('DELETE FROM users WHERE id=?', (user_id,)).rowcount > 0

    def create_session(self, token_hash, user_id, csrf, expires_at):
        with self._connect() as db:
            db.execute('DELETE FROM sessions WHERE expires_at < ?', (time.time(),))
            db.execute('INSERT INTO sessions(token_hash, user_id, csrf, expires_at) VALUES (?,?,?,?)',
                       (token_hash, user_id, csrf, expires_at))

    def session(self, token_hash):
        """The signed-in user for a live session, or None (expired, signed out, or user disabled)."""
        with self._connect() as db:
            row = db.execute('''SELECT s.csrf, s.user_id, u.email, u.role, u.must_change FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash=? AND s.expires_at > ? AND u.status='active' ''', (token_hash, time.time())).fetchone()
            return dict(row) if row else None

    def delete_session(self, token_hash):
        with self._connect() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (token_hash,))

    def delete_user_sessions(self, user_id, keep_token_hash=None):
        with self._connect() as db:
            db.execute('DELETE FROM sessions WHERE user_id=? AND token_hash != ?', (user_id, keep_token_hash or ''))

    # --- activity log: who did what; never contains passwords or keys ---
    MAX_ACTIVITY = 5000

    def log_activity(self, actor, actor_type, action, target='', detail='', address=''):
        with self._connect() as db:
            db.execute('INSERT INTO activity(at, actor, actor_type, action, target, detail, address) VALUES (?,?,?,?,?,?,?)',
                       (self.now(), actor[:120], actor_type, action, target[:200], detail[:300], address[:64]))
            db.execute('DELETE FROM activity WHERE id <= (SELECT id FROM activity ORDER BY id DESC LIMIT 1 OFFSET ?)',
                       (self.MAX_ACTIVITY,))

    def list_activity(self, prefixes=None, limit=50, offset=0):
        where, params = '', []
        if prefixes:
            where = 'WHERE ' + ' OR '.join('action LIKE ?' for _ in prefixes)
            params = [f'{prefix}%' for prefix in prefixes]
        with self._connect() as db:
            total = db.execute(f'SELECT COUNT(*) FROM activity {where}', params).fetchone()[0]
            rows = db.execute(f'SELECT * FROM activity {where} ORDER BY id DESC LIMIT ? OFFSET ?',
                              [*params, limit, offset]).fetchall()
            return [dict(r) for r in rows], total

    def key_name(self, key_id):
        with self._connect() as db:
            row = db.execute('SELECT name FROM api_keys WHERE id=?', (key_id,)).fetchone()
            return row['name'] if row else None

    def suppressed(self, email):
        with self._connect() as db:
            row = db.execute('SELECT * FROM suppressions WHERE email=?', (email.lower(),)).fetchone()
            return dict(row) if row else None

    def suppress(self, email, reason, source):
        with self._connect() as db:
            db.execute('INSERT OR REPLACE INTO suppressions(email, reason, source, created_at) VALUES (?,?,?,?)', (email.lower(), reason, source, self.now()))
