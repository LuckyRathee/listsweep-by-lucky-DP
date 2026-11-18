# Lead Email Verifier

**Conservative bulk email verification that tells you what it doesn't know.**

Most verifiers hand you a green tick and hope. This one doesn't. It sorts every address into `deliverable_candidate`, `undeliverable` or `unknown`, and it **never promotes an unknown to valid**. A timeout, a greylist, a catch-all server or a blocked port stays `unknown`, because that is the honest answer.

> It never sends email. There is no `DATA` command, no campaign feature and no zero-bounce guarantee.

---

## What it does

```
 CSV / emails
      │
      ▼
 ┌──────────┐   ┌───────────────┐   ┌──────────────┐   ┌────────────────┐
 │  Syntax  │──▶│ Suppression   │──▶│  DNS (MX /   │──▶│ SMTP RCPT +    │
 │  check   │   │ list (bounces)│   │  null-MX)    │   │ control probe  │
 └──────────┘   └───────────────┘   └──────────────┘   └────────────────┘
   always on        always on         SMTP path only      off by default
```

| Result | Meaning |
| --- | --- |
| `deliverable_candidate` | The server accepted this exact address and rejected a random control address, so it isn't a catch-all. Evidence, not proof. |
| `undeliverable` | Definite failure: bad syntax, null MX, domain doesn't exist, disposable domain, a 550-class rejection, or a prior hard bounce. |
| `unknown` | Not proven either way: timeouts, greylisting (4xx), catch-all domains, or checks switched off. |

`send_eligible` is stricter still. It is true only for a `deliverable_candidate` that is not catch-all, not disposable and not a role address (`info@`, `sales@` and so on).

## Safe by default

- **SMTP is off.** Turning it on needs `VERIFIER_SMTP_ENABLED=true` plus a real sender identity (`VERIFIER_HELO_DOMAIN` and `VERIFIER_MAIL_FROM`).
- **No private targets.** The SMTP probe refuses non-public IPs and connects to the DNS-checked address, which blocks DNS-rebinding tricks.
- **Polite probing.** Per-host concurrency and spacing limits, hard timeouts, and a newline check that stops SMTP command injection.
- **Locked-down API.** API key required in production (32+ characters), constant-time comparison, per-key rate limit.
- **Bounded jobs.** Up to 2,000 unique addresses and 1.5 MB per upload, with duplicates removed.

## Quick start

Requires Python 3.11+.

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn verifier.api:app --port 8080
```

With no key configured it runs open on localhost, which is fine for trying it out. Check it's alive:

```sh
curl http://127.0.0.1:8080/healthz
```

### Verify a CSV

The file needs an `email` column (also `emails`, `email address`, `e-mail`). UTF-8 only. Other columns are kept and come back as `lead_*` columns in exports.

```sh
curl -H "X-API-Key: $KEY" -F file=@leads.csv http://127.0.0.1:8080/v1/verify/csv
# -> 202 {"job_id": "...", "status": "queued", ...}

curl -H "X-API-Key: $KEY" http://127.0.0.1:8080/v1/jobs/$JOB_ID
curl -H "X-API-Key: $KEY" "http://127.0.0.1:8080/v1/jobs/$JOB_ID/results?send_only=true"
curl -H "X-API-Key: $KEY" "http://127.0.0.1:8080/v1/jobs/$JOB_ID/results.csv" -o results.csv
```

Authenticate with `X-API-Key: <key>` **or** `Authorization: Bearer <key>`.

### API

| Route | What it does |
| --- | --- |
| `GET /healthz` | Liveness, no auth |
| `POST /v1/verify/csv` | Upload a CSV (`file`, optional `allow_smtp`, `concurrency` 1-25); returns 202 and a `job_id` |
| `GET /v1/jobs/{id}` | Status, progress and counts |
| `GET /v1/jobs/{id}/results` | Results as JSON (`send_only`, `offset`, `limit`) |
| `GET /v1/jobs/{id}/results.csv` | Results as CSV (`send_only`) |

Jobs are stored in SQLite and resume after a restart.

## Use it from Claude (MCP)

A local stdio MCP server exposes the same engine as tools: `verify_emails`, `verify_csv`, `get_verification_job`, `get_verification_results`, `export_verification_csv`, `record_hard_bounce` and `get_suppression`.

```sh
python -m verifier.mcp_connector
```

Hard-bounce feedback goes into a local suppression list that is checked before any DNS or SMTP work.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `EMAIL_VERIFIER_API_KEY` | none | API key, 32+ characters |
| `EMAIL_VERIFIER_ENV` | none | `production` makes the key mandatory |
| `EMAIL_VERIFIER_DB` | `data/jobs.sqlite3` | SQLite path |
| `EMAIL_VERIFIER_RATE_LIMIT_PER_MINUTE` | `120` | Per-key cap, `0` disables |
| `VERIFIER_SMTP_ENABLED` | `false` | Allow live SMTP probing |
| `VERIFIER_HELO_DOMAIN` / `VERIFIER_MAIL_FROM` | `localhost` | Sender identity for SMTP |
| `VERIFIER_EXPORT_DIR` / `VERIFIER_DB` | project paths | MCP export and database locations |

## Deploy

`Dockerfile` and `docker-compose.yml` run a single worker with a SQLite volume, bound to `127.0.0.1:8080` for access over an SSH tunnel.

```sh
cp .env.example .env   # set EMAIL_VERIFIER_API_KEY
docker compose up --build -d
```

The full EC2 plan for Mumbai (`ap-south-1`), covering security groups, SSH access, SMTP port-25 rules and a release checklist, is in [docs/aws-deployment-guide.md](docs/aws-deployment-guide.md).

## Test

```sh
pip install pytest pytest-asyncio aiosmtpd ruff
pytest -q
```

The suite uses fake DNS and a local SMTP fixture, so no real addresses are probed.

## Status and limits

- **Tested locally; not yet deployed.** The Docker files haven't been built or run.
- **SQLite only.** PostgreSQL is not implemented.
- **With SMTP off, jobs are syntax-only.** Every valid address comes back `unknown`. An opt-in DNS-only mode is planned.
- **CSV only for now.**
- **Rate limiting is in-process**, so run one worker.
- **Not a licence to send.** `send_eligible` is a filter, not consent. You are responsible for lawful outreach.

## Roadmap

- [ ] Opt-in DNS-only checks (MX, null-MX, bad domain) without SMTP
- [ ] Web UI: drag-and-drop upload, live progress, results table, downloads
- [ ] Excel (`.xlsx`) and Google Sheets (link) import
- [ ] Job history and an API tab with ready-to-copy examples
