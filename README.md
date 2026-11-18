<p align="left"><img src="verifier/ui/digital-patron-logo.png" alt="DigitalPatron" height="56"></p>

# ListSweep

**by [DigitalPatron](https://digitalpatron.in/)**

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
| `undeliverable` | Definite failure: bad syntax, null MX, domain doesn't exist or has no mail server, a 550-class rejection, or a prior hard bounce. |
| `unknown` | Not proven either way: timeouts, greylisting (4xx), catch-all domains, or checks switched off. |

`send_eligible` is stricter still. It is true only for a `deliverable_candidate` that is not catch-all, not disposable and not a role address (`info@`, `sales@` and so on).

## Safe by default

- **SMTP is off.** Turning it on needs `VERIFIER_SMTP_ENABLED=true` plus a real sender identity (`VERIFIER_HELO_DOMAIN` and `VERIFIER_MAIL_FROM`).
- **No private targets.** The SMTP probe refuses non-public IPs and connects to the DNS-checked address, which blocks DNS-rebinding tricks.
- **Polite probing.** Per-host concurrency and spacing limits, hard timeouts, and a newline check that stops SMTP command injection.
- **Locked-down API.** API key required in production (32+ characters), constant-time comparison, per-key rate limit.
- **Bounded jobs.** Up to 2,000 unique addresses and 1.5 MB per upload, with duplicates removed.

## Web interface

Open `http://127.0.0.1:8080/` (enter your API key if the server needs one). The page offers:

- **Check one address** from the box at the top. Press Verify to see the verdict, a one-line reason, the domain's mail servers, and an honest account of what happened at each step (for example "Refused by the recipient's server" when a mail server blocks the check).
- **Check a whole list**: upload a `.csv` or `.xlsx` file, or paste a link to a Google Sheet shared as "Anyone with the link can view".
- **One "how thorough" control** in both places: syntax only, plus domain records (DNS), or plus a mailbox check (SMTP). It starts on the most thorough check the server allows.
- **Live progress with Cancel.** Addresses already checked are kept.
- **A plain-language summary of each run** ("4 not checked: the recipient's mail server refused this network...") with a "Show" link that filters the table to those rows.
- **A sortable, searchable results table** that includes your own columns (name, company, and so on). Click a row for the full explanation, server reply code and mail server.
- **Re-check unknowns** (or all addresses) as a new job, and download all results or only send-eligible rows. Downloads are named after your file.
- **Jobs tab** with history, what each job checked, a download per job, and a tag on jobs run by an older version of the checker.
- **API tab** with copy-ready `curl` and Python examples.

The URL records where you are (`#/jobs`, `#/api`, `#/job/<id>`), so refresh and Back keep your place.

The page itself carries no data and is the only thing served without a key. The key is held in the browser tab's session storage, sent only as a header, and never put in a URL; "Forget key" clears it. Results are rendered as plain text, and the page runs under a strict Content-Security-Policy with no external assets.

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

### Verify a file

The file needs an `email` column (also `emails`, `email address`, `e-mail`). CSV must be UTF-8. Excel means `.xlsx` (the first sheet; legacy `.xls` is not supported). Other columns are kept and come back as `lead_*` columns in exports.

Add `-F check_dns=true` to look up each domain's mail records. Without it, a job is syntax-only.

```sh
curl -H "X-API-Key: $KEY" -F file=@leads.csv -F check_dns=true http://127.0.0.1:8080/v1/verify/csv
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
| `GET /` and `/ui/*` | The web interface, no key needed |
| `GET /v1/config` | Limits, and whether DNS and SMTP checks are available |
| `POST /v1/verify/email` | One address, answered immediately and not stored (JSON `email`, optional `check_dns`, `allow_smtp`) |
| `POST /v1/verify/csv` | Upload a CSV (`file`, optional `check_dns`, `allow_smtp`, `concurrency` 1-25); returns 202 and a `job_id` |
| `POST /v1/verify/xlsx` | Same, for an Excel `.xlsx` file |
| `POST /v1/verify/sheet` | JSON `{"url": "...", "check_dns": true}` for a publicly shared Google Sheet |
| `GET /v1/jobs` | Recent jobs, newest first |
| `GET /v1/jobs/{id}` | Status, progress, counts and a count per reason |
| `POST /v1/jobs/{id}/cancel` | Stop a queued or running job; finished rows are kept |
| `POST /v1/jobs/{id}/recheck` | JSON `{"scope": "unknown"}` or `"all"`; start a new job from a finished one |
| `GET /v1/jobs/{id}/results` | Results as JSON (`send_only`, `offset`, `limit`) |
| `GET /v1/jobs/{id}/results.csv` | Results as CSV (`send_only`) |

Jobs are stored in SQLite and resume after a restart.

## Use it from Claude (MCP)

The MCP server is separate from the web page. A local stdio MCP server exposes the same engine as tools: `verify_emails`, `verify_csv`, `get_verification_job`, `get_verification_results`, `export_verification_csv`, `record_hard_bounce` and `get_suppression`.

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
- **With SMTP off, no address can be confirmed.** Valid-looking addresses come back `unknown`. Turn on `check_dns` to also catch null-MX and non-existent domains; DNS alone never makes an address `send_eligible`.
- **Google Sheets must be publicly shared** ("Anyone with the link"). The server needs outbound HTTPS to `docs.google.com`. Private sheets are not supported; export them to CSV or Excel.
- **Excel:** first sheet only, up to 5 MB and 20,000 rows scanned.
- **Rate limiting is in-process**, so run one worker.
- **Not a licence to send.** `send_eligible` is a filter, not consent. You are responsible for lawful outreach.

## Roadmap

- [x] Opt-in DNS-only checks (MX, null-MX, bad domain) without SMTP
- [x] Web UI: single-address check, upload, live progress, results table, downloads, job history, API tab
- [x] Excel (`.xlsx`) and Google Sheets (public link) import
- [ ] Private Google Sheets via Google sign-in
- [ ] Multiple API keys, and webhooks when a job finishes
- [ ] PostgreSQL storage
