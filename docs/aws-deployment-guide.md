# AWS EC2 supplement: conservative email-verification API

**Status: nothing is deployed to AWS.** This guide describes how to deploy the code in this repository to a single EC2 instance; no instance, security group, or AWS integration exists yet. The application code and its 87 tests pass locally (`pytest`), but the `Dockerfile` and `docker-compose.yml` have **not been built or run** (Docker was not available when they were written), so treat the first `docker compose up --build` as the first real test.

**Storage is SQLite on a Docker named volume.** PostgreSQL is *not* implemented: `verifier/store.py` uses `sqlite3` only, and there is no `DATABASE_URL` setting. Moving to PostgreSQL is a separate work item (store rewrite, migrations, backup/restore), not a configuration change. Names, ports, and environment variables below match the code as of this revision.

## Recommended Mumbai-region layout

- Create a VPC in **Asia Pacific (Mumbai), `ap-south-1`**. Put the EC2 instance on a private subnet. The database is a SQLite file on the instance's Docker volume, so there is no separate database tier today; back up the volume (see below). Do not expose the API to the public internet.
- Prefer **EC2 Instance Connect Endpoint** for private-instance SSH. Alternatively, use one patched bastion that is reachable only from a known administrator CIDR (for example, an office `/32`) and permits forwarding only to the verifier's private address on TCP/22. Never use `0.0.0.0/0` for SSH.
- Use separate security groups: the administration path permits TCP/22 only from the bastion/EIC endpoint; the application security group has **no inbound public HTTP/HTTPS rule** (add a database security group permitting TCP/5432 from the application only if PostgreSQL is later implemented). A private instance needs a controlled outbound path (normally NAT) for image pulls, OS updates, and DNS. Do not open inbound TCP/25—the verifier does not operate a mail server.
- Grant no broad AWS permissions to the application. A local-first Compose deployment needs no SES/SNS/SQS credentials until those integrations are deliberately added.

## SSH and Compose launch

Generate or import an SSH key in the console; keep the private key only on the administrator workstation and run `chmod 600 ~/.ssh/verifier_ed25519`. Use the actual Ubuntu user and private DNS/IP:

```sshconfig
# ~/.ssh/config
Host verifier
    HostName 10.0.1.25                 # example private IP; replace it
    User ubuntu
    IdentityFile ~/.ssh/verifier_ed25519
    IdentitiesOnly yes
    ProxyJump admin-bastion           # omit when using an EIC endpoint command
```

```sh
ssh verifier                         # first test the private administration path
ssh -N -L 127.0.0.1:8080:127.0.0.1:8080 verifier
```

On the instance, install Docker's maintained packages and copy the repository (at least `Dockerfile`, `docker-compose.yml`, `requirements.txt`, `verifier/`) to `/opt/email-verifier`. Create the secrets file there:

```sh
cd /opt/email-verifier
cp .env.example .env && chmod 600 .env
# Put a random key of at least 32 characters in EMAIL_VERIFIER_API_KEY:
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

The repository's `docker-compose.yml` runs one service, `api`, with these settings:

| Variable | Value / purpose |
| --- | --- |
| `EMAIL_VERIFIER_ENV=production` | The app refuses to start without an API key. |
| `EMAIL_VERIFIER_API_KEY` | Required, 32+ characters, from `.env`. Never reuse an SSH secret; never commit `.env`. |
| `EMAIL_VERIFIER_DB=/data/jobs.sqlite3` | SQLite file on the `verifier_data` named volume. |
| `EMAIL_VERIFIER_RATE_LIMIT_PER_MINUTE` | Per-key request cap, default 120 (`0` disables). Fixed window, in-process, so it is only correct with the single uvicorn worker the image uses. |
| `VERIFIER_SMTP_ENABLED=false` | No SMTP probing. Leave it `false` (see below). |

The port is published on `127.0.0.1:8080` only. Send the key as `X-API-Key: <key>` **or** `Authorization: Bearer <key>`; both are accepted and compared in constant time. `/healthz` is the only unauthenticated path. Reaching the API over the SSH tunnel is not a substitute for the key, and TLS in front of it would not be either; keep the key and any IP restrictions.

API routes: `POST /v1/verify/email` (one address, answered immediately, nothing stored), `POST /v1/verify/csv`, `POST /v1/verify/xlsx` and `POST /v1/verify/sheet` (all return 202 with `job_id`; options `check_dns`, `allow_smtp`, `concurrency`), `GET /v1/config`, `GET /v1/jobs`, `GET /v1/jobs/{id}`, `GET /v1/jobs/{id}/results` and `GET /v1/jobs/{id}/results.csv` (both accept `send_only=true`). Limits: 2,000 unique addresses, 1.5 MB per CSV, 5 MB per `.xlsx`. Jobs interrupted by a restart are resumed on startup.

**Web interface.** The same service serves a browser page at `/` (static files under `verifier/ui/`, copied into the image with the rest of `verifier/`). The page shell is the only unauthenticated content; every data route still needs the key, and `/docs` stays behind it. Open it through the same SSH tunnel at `http://127.0.0.1:8080/`. It sends the key as a header only and holds it in session storage; responses carry a strict Content-Security-Policy.

```sh
cd /opt/email-verifier
docker compose config --quiet       # validates interpolation, not application readiness
docker compose up --build -d
docker compose ps                   # the image has a /healthz HEALTHCHECK
curl -fsS http://127.0.0.1:8080/healthz
# Over the tunnel: a wrong key must be rejected, the right one accepted.
curl -s -o /dev/null -w '%{http_code}\n' -H 'X-API-Key: wrong' http://127.0.0.1:8080/v1/jobs/x   # 401
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $KEY" http://127.0.0.1:8080/v1/jobs/x  # 404
```

The schema is created idempotently on start (`CREATE TABLE IF NOT EXISTS`); there is no migration tool, so a schema change needs its own reviewed release step. Back up the volume while the container is stopped or with SQLite's online backup (`sqlite3 /data/jobs.sqlite3 ".backup /backup/jobs.sqlite3"`), encrypt the copy, and rehearse a restore into an isolated instance before holding production data. The WAL-mode database must be copied together with its `-wal`/`-shm` files if you copy files directly.

## Verification semantics and SMTP safeguards

What the code does today: with SMTP off (the default and the only supported production mode here) a job performs **syntax normalisation only, with no DNS lookup**, and every syntactically valid address is stored as `unknown` / `verification_disabled`; invalid syntax is `undeliverable` / `invalid_syntax`. An explicit **`check_dns=true`** option (a checkbox in the web interface, on by default there) adds MX, null-MX and NXDOMAIN evidence: valid-looking addresses on a mail-capable domain become `unknown` / `mailbox_check_disabled`, null-MX and missing domains become `undeliverable`, and lookup failures stay `unknown` / `dns_timeout`. The API default is still no network access. Nothing in either mode can make an address `send_eligible`. Separately, the principle below holds for any future DNS evidence: it is **not mailbox proof**. An MX or A/AAAA lookup identifies a possible receiving host; that alone does not prove an individual mailbox exists. A domain with exactly one DNS MX record `0 .` explicitly accepts no mail and must not trigger connections to fallback addresses—return the application's non-deliverable/null-MX outcome.[2]

With `VERIFIER_SMTP_ENABLED=false`, a request with `allow_smtp=true` is refused with HTTP 403. When SMTP is on, timeouts or blocked SMTP must remain reported as **unknown/unavailable**, not converted to "valid" or "invalid." **Policy conflict to resolve before ever enabling SMTP:** `verifier/smtp.py` already sends a second `RCPT TO` for a random control address at the same domain to detect catch-all servers, which is the "random RCPT" probing this paragraph discourages. It is unreachable while SMTP is off. Decide explicitly whether that control probe is acceptable before setting `VERIFIER_SMTP_ENABLED=true`. Do not add `VRFY`, further catch-all, or bulk probing: RCPT responses are server-policy-dependent, `VRFY` may be restricted because it discloses information, and none justifies a deliverability guarantee.[1] The service must not send messages, enumerate addresses, retry aggressively, or provide campaigns, list upload for outreach, or bulk-send features. Initially permit only DNS egress and explicitly required update/image endpoints; keep outbound TCP/25 blocked at both security-group/network-firewall and application layers.

**Egress for the Google Sheets import.** `POST /v1/verify/sheet` makes an outbound HTTPS request to `docs.google.com` (and its `*.googleusercontent.com` redirect target). The rule above does not allow that, so either add a narrowly scoped HTTPS egress path for those hosts or accept that the Sheets import will return "could not reach Google Sheets" and users must upload files instead. The server builds the request URL itself from the sheet ID in the pasted link and refuses redirects to any other host, so the pasted text can't point it elsewhere. Only publicly shared sheets work.

AWS blocks outbound TCP/25 by default for EC2 instances and Lambda elastic network interfaces unless an account is allowlisted; removing that restriction requires an AWS request.[4] **Do not interpret an open security group as SMTP capability.** If direct SMTP diagnosis is ever justified, request an exception for `ap-south-1` with an accurate use case and abuse controls, wait for approval, and then allow narrowly scoped authenticated tests only. Until that approval and an explicit product policy exist, every SMTP-dependent result stays unknown. Prefer a supported external verification provider if mailbox-level evidence is essential; port 25 is not needed for an HTTPS provider API.

If an approved direct SMTP mode is later implemented, use a stable Elastic IP and configure **rDNS** through AWS's port-25/rDNS request process for the EIP. Give the verifier a resolvable, matching forward/reverse hostname; use that hostname as the `EHLO` parameter (with legacy `HELO` compatibility as needed). SMTP defines EHLO/HELO and its domain/address-literal behavior, but consistent host identity does not make a recipient response authoritative.[1][4] AWS's rDNS setup also requires a corresponding forward DNS record. Keep this disabled during the private DNS-only release.

## Future SES bounce/complaint suppression—separate work item

This verifier does **not** require SES and should not send mail. If a separately authorized sending service later uses SES, implement bounce/complaint ingestion as a new, least-privilege pipeline: SES event destination → SNS → SQS → a verifier worker that validates SNS messages, handles retries/dead-lettering idempotently, and stores normalized address-level suppressions in the verifier's database. Check suppressions before any future send; never let bounce processing create sending behavior. SES requires bounce/complaint management and can review or pause sending when those rates are excessive.[3] No such SNS/SQS worker or suppression data exists merely because SES is available in the account.

**Release gate** (check each box on the real instance; none has been checked yet):

- [ ] Private SSH tested with a non-default administrator IP rule.
- [ ] No public API listener (`ss -ltnp` shows only `127.0.0.1:8080`); security groups reviewed; outbound TCP/25 blocked.
- [ ] Wrong key returns 401, both header styles work, and the 429 limit triggers (covered locally by `tests/test_api_security.py`).
- [ ] `docker compose up --build` succeeds and `docker compose ps` reports healthy.
- [ ] Volume backup restored into an isolated instance and a job read back.
- [ ] A restart mid-job resumes it and loses no saved results.
- [ ] With SMTP off, a CSV job returns only `unknown`/`undeliverable` and nothing is `send_eligible`; no outbound messages or probes are observed.
- [ ] With `check_dns=true`, a null-MX domain and a non-existent domain come back `undeliverable`, and a DNS timeout stays `unknown` (covered by fixture tests; confirm once against the real resolver on the instance).
- [ ] Web page loads over the tunnel, the key is accepted, and a CSV job completes end to end.

## Sources

[1] https://www.rfc-editor.org/rfc/rfc5321.txt
[2] https://www.rfc-editor.org/rfc/rfc7505.html
[3] https://docs.aws.amazon.com/ses/latest/dg/reputationdashboardmessages.html
[4] https://repost.aws/knowledge-center/ec2-port-25-throttle
