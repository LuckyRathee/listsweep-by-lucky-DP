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
- **Locked-down access.** Sign-in with salted password hashes and lockout, or API keys for agents (32+ characters, constant-time comparison, per-key rate limit). Production refuses to start with neither configured.
- **Bounded jobs.** Up to 2,000 unique addresses and 1.5 MB per upload, with duplicates removed.

## Web interface

Open `http://127.0.0.1:8080/` and sign in. It is laid out like a desktop app: a fixed sidebar (Verify, Jobs, API, Admin for admins, who you are signed in as, connection status) and a workspace that scrolls on its own. In a narrow window the sidebar becomes a top bar.

### Signing in and accounts
- **First admin:** set `EMAIL_VERIFIER_ADMIN_EMAIL` and `EMAIL_VERIFIER_ADMIN_PASSWORD` in `.env` and start the server. The account is created once, only if it doesn't exist, so a password changed later is never overwritten. Only a salted hash of the password is stored. After the first start you can delete the password line from `.env`.
- **Sign-in page:** email and password. Five wrong attempts lock that account and address out for 15 minutes, and a wrong email looks exactly like a wrong password.
- **Account page:** change your password. A password an admin gives you is temporary, and you must replace it the first time you sign in. Passwords need 10+ characters mixing at least three of lowercase, uppercase, numbers and symbols, and can't contain your email name.
- **Admin page (admins only):**
  - **People:** add people with a generated or chosen password, make someone an admin or a normal user, disable or enable them, reset their password, or delete them. Disabling or resetting signs them out immediately. You can't remove yourself or the last active admin.
  - **API keys:** create and delete named keys for agents and apps.
  - **Activity:** who signed in (and failed to), who changed people and keys, and which jobs were started, cancelled or re-checked, with filters. Passwords and keys are never recorded, and the newest 5,000 events are kept.
- **Normal users** can verify and see jobs but can't manage people, keys or the activity log. Everyone shares the same jobs and results.
- **Server locked to keys only:** if API keys exist but no accounts do, the page shows a message telling the owner to set the two settings above. API keys for agents and scripts keep working either way.

- **One shared "How thorough" setting** at the top: syntax only, plus domain records (DNS), or plus a mailbox check (SMTP). It starts on the most thorough check the server allows and applies to everything below, including re-checks.
- **One address**: type it and press Verify. You get the verdict, a one-line reason, a three-step strip (Syntax, Domain, Mailbox) showing what really happened at each step ("Refused by the server" when a mail server blocks the check), and only the details worth reading.
- **A list**: upload a `.csv` or `.xlsx` file, or paste a link to a Google Sheet shared as "Anyone with the link can view". Progress is live and can be cancelled; addresses already checked are kept.
- **Results**: a headline ("10 of 15 are deliverable candidates") with a proportion bar, a plain-language summary of the run with "Show" filters, and a sortable, searchable table that includes your own columns. Each row shows the result and the reason together plus a quiet send-eligible mark. Click a row for the full explanation.
- **Re-check unknowns** (or all addresses) as a new job, and download all results or only send-eligible rows from one Download menu. Files are named after your upload.
- **Jobs** keeps history with what each job checked, a download per job, and a tag on jobs run by an older version of the checker. **API** has copy-ready `curl` and Python examples.
- **Keyboard**: `Ctrl/Cmd+1`, `+2`, `+3` switch sections, `Ctrl/Cmd+K` jumps to the address box, and arrow keys move through tabs and the thoroughness options.
- **Look**: black and white with the logo's orange as the one accent. The background is a still dot grid with a very slow light; a thin orange sweep line runs across the window only while a check is in progress.

The URL records where you are (`#/jobs`, `#/api`, `#/job/<id>`), so refresh and Back keep your place.

The page itself carries no data and is the only thing served before sign-in. The session is an HttpOnly, SameSite=Strict cookie that scripts can't read, requests that change anything also need a CSRF token, and sessions end after 12 hours or when you sign out. Results are rendered as plain text, and the page runs under a strict Content-Security-Policy with no external assets.

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
| `GET /v1/keys`, `POST /v1/keys`, `DELETE /v1/keys/{id}` | List, create (key returned once) and delete API keys |
| `GET /v1/mcp-config` | MCP settings filled in for this server |
| `GET /v1/jobs/{id}` | Status, progress, counts and a count per reason |
| `POST /v1/jobs/{id}/cancel` | Stop a queued or running job; finished rows are kept |
| `POST /v1/jobs/{id}/recheck` | JSON `{"scope": "unknown"}` or `"all"`; start a new job from a finished one |
| `GET /v1/jobs/{id}/results` | Results as JSON (`send_only`, `offset`, `limit`) |
| `GET /v1/jobs/{id}/results.csv` | Results as CSV (`send_only`) |

Jobs are stored in SQLite and resume after a restart.

## Connect an agent

The **API** tab in the app walks through both options with your server's real paths filled in. In short:

### Option A: HTTP API with a key
1. An admin opens the **Admin** page and creates a key (name it after the agent). Keys look like `ls_...`, are unique, and are shown once; only a fingerprint is stored. Delete a key there and it stops working immediately.
2. Point the agent at the server's address and send the key on every request, as `X-API-Key: <key>` or `Authorization: Bearer <key>`.
3. The tab includes ready-to-paste agent instructions and `curl` and Python examples.

Keys are for agents and scripts, not for the browser. Setting `EMAIL_VERIFIER_API_KEY` adds a server key that always works and cannot be deleted from the page. Only an admin can see the MCP settings.

### Option B: MCP (local, no key)
The MCP server is a local process the agent starts itself, so nothing goes over a network and there is no key. Add this to the agent's MCP settings (the API tab shows it with your paths):

```json
{
  "mcpServers": {
    "listsweep": {
      "command": "/path/to/venv/bin/python",
      "args": ["-m", "verifier.mcp_connector"],
      "env": { "PYTHONPATH": "/path/to/lead-email-verifier", "VERIFIER_DB": "/path/to/lead-email-verifier/data/jobs.sqlite3" }
    }
  }
}
```

Add `VERIFIER_SMTP_ENABLED`, `VERIFIER_HELO_DOMAIN` and `VERIFIER_MAIL_FROM` to `env` to allow mailbox checks. Setting `VERIFIER_DB` to the app's database makes jobs your agent runs show up in the Jobs tab.

Tools: `verify_emails`, `verify_csv`, `get_verification_job`, `get_verification_results`, `export_verification_csv`, `cancel_verification_job`, `recheck_verification_job`, `record_hard_bounce` and `get_suppression`. Like the web app, the check tools take `check_dns` and `allow_smtp`; `allow_smtp` defaults to on when the server has SMTP enabled. Hard-bounce feedback goes into a local suppression list that is checked before any DNS or SMTP work.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `EMAIL_VERIFIER_ADMIN_EMAIL` / `EMAIL_VERIFIER_ADMIN_PASSWORD` | none | First admin account, created on startup if missing |
| `EMAIL_VERIFIER_API_KEY` | none | Optional server-wide API key, 32+ characters |
| `EMAIL_VERIFIER_ENV` | none | `production` makes the key mandatory |
| `EMAIL_VERIFIER_DB` | `data/jobs.sqlite3` | SQLite path |
| `EMAIL_VERIFIER_RATE_LIMIT_PER_MINUTE` | `120` | Per-key cap, `0` disables |
| `VERIFIER_SMTP_ENABLED` | `false` | Allow live SMTP probing |
| `VERIFIER_HELO_DOMAIN` / `VERIFIER_MAIL_FROM` | `localhost` | Sender identity for SMTP |
| `VERIFIER_EXPORT_DIR` / `VERIFIER_DB` | project paths | MCP export and database locations |

## Deploy

`Dockerfile` and `docker-compose.yml` run a single worker with a SQLite volume, bound to `127.0.0.1:8080` for access over an SSH tunnel.

```sh
cp .env.example .env   # set EMAIL_VERIFIER_ADMIN_EMAIL and EMAIL_VERIFIER_ADMIN_PASSWORD
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
- [x] Sign-in, an Account page, and an Admin page for people, API keys and activity
- [ ] Webhooks when a job finishes, per-person job privacy, and single sign-on
- [ ] PostgreSQL storage
