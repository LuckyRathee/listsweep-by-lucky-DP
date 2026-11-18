# ListSweep by DigitalPatron: AWS EC2 deployment guide

This supplement covers deploying the conservative email-verification API.

**Status: deployed to one EC2 instance in `ap-south-1` (Mumbai), reachable only through an AWS Session Manager tunnel.** The app runs from this repository's `docker-compose.yml`; the Docker files built and ran unchanged on Ubuntu 24.04 (arm64). Mailbox (SMTP) checks are **off**, because AWS blocks outbound port 25 until you request that it be lifted. **Not yet verified:** opening the page through the tunnel from a Mac (needs the Session Manager plugin installed there), restoring a backup, restarting in the middle of a job, and anything that uses SMTP. No account numbers, instance ids or IP addresses are recorded in this repository.

**Storage is SQLite on a Docker named volume.** PostgreSQL is *not* implemented: `verifier/store.py` uses `sqlite3` only, and there is no `DATABASE_URL` setting. Moving to PostgreSQL is a separate work item (store rewrite, migrations, backup/restore), not a configuration change. Names, ports, and environment variables below match the code as of this revision.

## What is actually deployed, and how it differs from the plan below

- **Server:** one `t4g.small` (Graviton/ARM, 2 vCPU, 2 GiB), Ubuntu 24.04 arm64, 20 GB encrypted gp3 disk, IMDSv2 required. Every resource is tagged `Project=listsweep`.
- **Network (differs from the layout below):** the default VPC's public subnet with an Elastic IP and a security group with **zero inbound rules**, instead of a private subnet with a NAT gateway (which would add about $40 a month). Nothing on the internet can open a connection to the server. Outbound is narrowed to HTTPS, HTTP (apt mirrors), DNS and time sync; outbound TCP/25 was opened on the security group afterwards, but AWS still blocks it at the platform level until its request is approved (a test connection to Google's mail servers timed out).
- **Access:** AWS Session Manager only. There is no SSH key pair, and the SSH service is switched off. The instance role has only `AmazonSSMManagedInstanceCore`.
- **Secrets:** the first admin's settings were stored as a temporary encrypted SSM parameter, read once by the server into a root-only `.env` (mode 600), and then the parameter and the extra permission that allowed the read were deleted.
- **Code:** cloned from the public repository into `/opt/email-verifier`. Updates are manual; there is no auto-update.
- **Backups:** a Data Lifecycle Manager policy takes a daily snapshot of the tagged disk (21:00 UTC) and keeps 7. It is configured and enabled, but the first snapshot had not run when this was written, and a restore has not been tried.
- **Mailbox checks:** off (`VERIFIER_SMTP_ENABLED` unset). The app offers syntax and domain checks only, and nothing is send-eligible.
- **Cost:** about $14 a month: instance $8.20, disk $1.80, public IPv4 address $3.65, snapshots under $1 (Mumbai prices at deployment time).

## Operating the deployment

Replace `<instance-id>` with the id from `aws ec2 describe-instances --region ap-south-1 --filters Name=tag:Project,Values=listsweep`.

**Open the app (tunnel).** Install the plugin once (it asks for your Mac password): `brew install --cask session-manager-plugin`. Then:

```sh
aws ssm start-session --region ap-south-1 --target <instance-id> \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8080"],"localPortNumber":["18080"]}'
```

Leave it running and open `http://localhost:18080/`. Use the name `localhost` here, not `127.0.0.1`: browsers share cookies across ports on the same host name, so if you also run ListSweep on your own computer at `http://127.0.0.1:8080/`, signing in to one would sign you out of the other. Stop the tunnel with Ctrl-C.

**Open a shell on the server:** `aws ssm start-session --region ap-south-1 --target <instance-id>`.

**Update the app** (from that shell): `cd /opt/email-verifier && sudo git pull --ff-only && sudo docker compose up --build -d`. Check it with `sudo docker compose ps`.

**Stop and start.** `aws ec2 stop-instances --region ap-south-1 --instance-ids <instance-id>` stops the compute charge, but the disk and the public address are still billed. `start-instances` brings it back, and the app restarts by itself (Docker starts at boot and the container uses `restart: unless-stopped`).

**Change the admin password.** Sign in, open **Account**, and change it. Then remove the password line from the server's settings file: `sudo sed -i '/^EMAIL_VERIFIER_ADMIN_PASSWORD=/d' /opt/email-verifier/.env`. It is only used to create the account the first time.

**Turn on mailbox checks** (only after the steps in the next section). Add these three lines to `/opt/email-verifier/.env`, then run `sudo docker compose up -d`:

```
VERIFIER_SMTP_ENABLED=true
VERIFIER_HELO_DOMAIN=probe.example.com
VERIFIER_MAIL_FROM=probe@probe.example.com
```

**Delete everything** (find it first with `aws resourcegroupstaggingapi get-resources --region ap-south-1 --tag-filters Key=Project,Values=listsweep`):

```sh
aws ec2 terminate-instances --region ap-south-1 --instance-ids <instance-id>     # the disk is deleted with it
aws ec2 wait instance-terminated --region ap-south-1 --instance-ids <instance-id>
aws ec2 release-address --region ap-south-1 --allocation-id <eipalloc-id>
aws ec2 delete-security-group --region ap-south-1 --group-id <sg-id>
aws dlm delete-lifecycle-policy --region ap-south-1 --policy-id <policy-id>
# snapshots are not deleted with the server:
aws ec2 describe-snapshots --region ap-south-1 --owner-ids self --filters Name=tag:Project,Values=listsweep --query 'Snapshots[].SnapshotId' --output text
aws ec2 delete-snapshot --region ap-south-1 --snapshot-id <snap-id>               # once per snapshot
aws iam remove-role-from-instance-profile --instance-profile-name listsweep-ec2-profile --role-name listsweep-ec2-role
aws iam delete-instance-profile --instance-profile-name listsweep-ec2-profile
aws iam detach-role-policy --role-name listsweep-ec2-role --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam delete-role --role-name listsweep-ec2-role
# the backup service role was created for this project; delete it only if nothing else uses it:
aws iam list-attached-role-policies --role-name AWSDataLifecycleManagerDefaultRole
```

## Steps only you can do before mailbox checks will work

1. **Ask AWS to lift the port-25 block.** Submit AWS's "Request to remove email sending limitations" form (linked from the AWS article in the sources, reference [4]). Give the Elastic IP address as the address to un-throttle, and the same hostname as the reverse DNS name (step 3). Suggested use case: *"We verify that recipient mailboxes exist before contacting business leads. The server opens an SMTP connection to the recipient's mail server, issues MAIL FROM and RCPT TO, and disconnects without sending a message. It limits itself to a few connections per mail server, identifies itself with a real hostname and an SPF-protected domain, and never sends mail. Abuse reports go to <your address>."* Approval can take a day or more and is not guaranteed for new accounts.
2. **Create DNS records for a dedicated probe name.** Pick a subdomain you control, for example `probe.example.com`: an `A` record pointing to the server's Elastic IP, and a `TXT` record `v=spf1 -all` on that name. The app requires the sender address and the HELO name to be on the same domain, so use `probe@probe.example.com`. Do not reuse a domain you send real email from.
3. **Set reverse DNS (PTR) for the Elastic IP** to that same name, once the `A` record from step 2 exists. Try it yourself first: `aws ec2 modify-address-attribute --region ap-south-1 --allocation-id <eipalloc-id> --domain-name probe.example.com`. If AWS refuses, include it in the form from step 1. A matching forward and reverse name makes remote servers much more willing to answer.
4. **Open outbound port 25 on the firewall.** *Already done for this deployment; the rule is tagged `Project=listsweep`.* Approval alone is not enough, because the security group also has to allow it. For reference, the command was:

   ```sh
   aws ec2 authorize-security-group-egress --region ap-south-1 --group-id <sg-id> \
     --ip-permissions 'IpProtocol=tcp,FromPort=25,ToPort=25,IpRanges=[{CidrIp=0.0.0.0/0,Description=mailbox checks}]'
   ```

   To close it again, run the same command as `revoke-security-group-egress`. Until step 1 is approved, a connection to port 25 on any mail server still times out, so leave `VERIFIER_SMTP_ENABLED` off.
5. **Then** set the three lines in `.env` as shown above and run `sudo docker compose up -d`.

Even then, large providers (Microsoft 365 in particular) often refuse unfamiliar senders, so expect `unknown` results for some addresses.

## Recommended Mumbai-region layout (the hardened plan; the deployment above is a simpler variant)

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
# Set EMAIL_VERIFIER_ADMIN_EMAIL and EMAIL_VERIFIER_ADMIN_PASSWORD (the first admin, created on first start).
# Optionally add a server-wide EMAIL_VERIFIER_API_KEY (32+ characters):
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

The repository's `docker-compose.yml` runs one service, `api`, with these settings:

| Variable | Value / purpose |
| --- | --- |
| `EMAIL_VERIFIER_ENV=production` | The app refuses to start unless an admin account or an API key is configured. |
| `EMAIL_VERIFIER_ADMIN_EMAIL`, `EMAIL_VERIFIER_ADMIN_PASSWORD` | Required in the compose file: the first admin account, created on startup if missing. After the first start you can remove the password line. Never commit `.env`. |
| `EMAIL_VERIFIER_API_KEY` | Optional server-wide key, 32+ characters, for agents and scripts. Never reuse an SSH secret. |
| `EMAIL_VERIFIER_DB=/data/jobs.sqlite3` | SQLite file on the `verifier_data` named volume. |
| `EMAIL_VERIFIER_RATE_LIMIT_PER_MINUTE` | Per-key request cap, default 120 (`0` disables). Fixed window, in-process, so it is only correct with the single uvicorn worker the image uses. |
| `VERIFIER_SMTP_ENABLED=false` | No SMTP probing. Leave it `false` (see below). |

Accounts and keys: people sign in with an email and password (salted hashes, a 12-hour HttpOnly session cookie, CSRF protection, lockout after repeated failures), admins manage people and the activity log on the Admin page. Besides the server key, the **API** tab creates and deletes named per-agent keys (stored hashed in the same SQLite volume, so back them up with it). Use a separate key for each agent and delete it to revoke it.

The port is published on `127.0.0.1:8080` only. Send the key as `X-API-Key: <key>` **or** `Authorization: Bearer <key>`; both are accepted and compared in constant time. `/healthz` is the only unauthenticated path. Reaching the API over the SSH tunnel is not a substitute for the key, and TLS in front of it would not be either; keep the key and any IP restrictions.

API routes: `POST /v1/verify/email` (one address, answered immediately, nothing stored), `POST /v1/verify/csv`, `POST /v1/verify/xlsx` and `POST /v1/verify/sheet` (all return 202 with `job_id`; options `check_dns`, `allow_smtp`, `concurrency`), `GET /v1/config`, `GET /v1/jobs`, `GET /v1/jobs/{id}`, `POST /v1/jobs/{id}/cancel`, `POST /v1/jobs/{id}/recheck`, `GET /v1/jobs/{id}/results` and `GET /v1/jobs/{id}/results.csv` (both accept `send_only=true`). Limits: 2,000 unique addresses, 1.5 MB per CSV, 5 MB per `.xlsx`. Jobs interrupted by a restart are resumed on startup.

**Web interface.** The same service serves a browser page at `/` (static files under `verifier/ui/`, copied into the image with the rest of `verifier/`). The page shell is the only unauthenticated content; every data route needs a sign-in session or an API key, and `/docs` stays behind that. Open it through the same SSH tunnel at `http://127.0.0.1:8080/` and sign in. The browser uses an HttpOnly session cookie (not an API key); responses carry a strict Content-Security-Policy.

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

What the code does today: with SMTP off (the default and the only supported production mode here) a job performs **syntax normalisation only, with no DNS lookup**, and every syntactically valid address is stored as `unknown` / `verification_disabled`; invalid syntax is `undeliverable` / `invalid_syntax`. An explicit **`check_dns=true`** option (the "+ Domain records" level of the "How thorough" control in the web interface) adds MX, null-MX and NXDOMAIN evidence: valid-looking addresses on a mail-capable domain become `unknown` / `mailbox_check_disabled`, null-MX and missing domains become `undeliverable`, and lookup failures stay `unknown` / `dns_timeout`. The API default is still no network access. Nothing in either mode can make an address `send_eligible`. Separately, the principle below holds for any future DNS evidence: it is **not mailbox proof**. An MX or A/AAAA lookup identifies a possible receiving host; that alone does not prove an individual mailbox exists. A domain with exactly one DNS MX record `0 .` explicitly accepts no mail and must not trigger connections to fallback addresses—return the application's non-deliverable/null-MX outcome.[2]

With `VERIFIER_SMTP_ENABLED=false`, a request with `allow_smtp=true` is refused with HTTP 403. When SMTP is on, timeouts or blocked SMTP must remain reported as **unknown/unavailable**, not converted to "valid" or "invalid." **Policy conflict to resolve before ever enabling SMTP:** `verifier/smtp.py` already sends a second `RCPT TO` for a random control address at the same domain to detect catch-all servers, which is the "random RCPT" probing this paragraph discourages. It is unreachable while SMTP is off. Decide explicitly whether that control probe is acceptable before setting `VERIFIER_SMTP_ENABLED=true`. Do not add `VRFY`, further catch-all, or bulk probing: RCPT responses are server-policy-dependent, `VRFY` may be restricted because it discloses information, and none justifies a deliverability guarantee.[1] The service must not send messages, enumerate addresses, retry aggressively, or provide campaigns, list upload for outreach, or bulk-send features. Initially permit only DNS egress and explicitly required update/image endpoints; keep outbound TCP/25 blocked at both security-group/network-firewall and application layers.

**Egress for the Google Sheets import.** `POST /v1/verify/sheet` makes an outbound HTTPS request to `docs.google.com` (and its `*.googleusercontent.com` redirect target). The rule above does not allow that, so either add a narrowly scoped HTTPS egress path for those hosts or accept that the Sheets import will return "could not reach Google Sheets" and users must upload files instead. The server builds the request URL itself from the sheet ID in the pasted link and refuses redirects to any other host, so the pasted text can't point it elsewhere. Only publicly shared sheets work.

AWS blocks outbound TCP/25 by default for EC2 instances and Lambda elastic network interfaces unless an account is allowlisted; removing that restriction requires an AWS request.[4] **Do not interpret an open security group as SMTP capability.** If direct SMTP diagnosis is ever justified, request an exception for `ap-south-1` with an accurate use case and abuse controls, wait for approval, and then allow narrowly scoped authenticated tests only. Until that approval and an explicit product policy exist, every SMTP-dependent result stays unknown. Prefer a supported external verification provider if mailbox-level evidence is essential; port 25 is not needed for an HTTPS provider API.

If an approved direct SMTP mode is later implemented, use a stable Elastic IP and configure **rDNS** through AWS's port-25/rDNS request process for the EIP. Give the verifier a resolvable, matching forward/reverse hostname; use that hostname as the `EHLO` parameter (with legacy `HELO` compatibility as needed). SMTP defines EHLO/HELO and its domain/address-literal behavior, but consistent host identity does not make a recipient response authoritative.[1][4] AWS's rDNS setup also requires a corresponding forward DNS record. Keep this disabled during the private DNS-only release.

## Future SES bounce/complaint suppression—separate work item

This verifier does **not** require SES and should not send mail. If a separately authorized sending service later uses SES, implement bounce/complaint ingestion as a new, least-privilege pipeline: SES event destination → SNS → SQS → a verifier worker that validates SNS messages, handles retries/dead-lettering idempotently, and stores normalized address-level suppressions in the verifier's database. Check suppressions before any future send; never let bounce processing create sending behavior. SES requires bounce/complaint management and can review or pause sending when those rates are excessive.[3] No such SNS/SQS worker or suppression data exists merely because SES is available in the account.

**Release gate** (status after the first deployment):

- [x] No inbound path from the internet: the security group has zero inbound rules, the SSH service is disabled (and stays off after a reboot), and `ss -ltnp` on the server shows only `127.0.0.1:8080` plus local DNS. Outbound TCP/25 is allowed by the security group (needed for mailbox checks) but blocked by AWS until approved; there is still no way in from the internet.
- [x] `docker compose up --build` succeeded on arm64 and `docker compose ps` reports healthy; the container runs as a non-root user and owns its data volume.
- [x] On the server: `/healthz` is ok, the sign-in page is served, requests without sign-in get 401, a wrong password gets 401, and the admin account signs in and out.
- [x] Domain checks work against the real resolver: a real domain returns `unknown / mailbox_check_disabled` with its mail servers, and a non-existent domain returns `undeliverable / domain_not_found`.
- [x] A reboot brings the app back by itself (Docker enabled at boot, `restart: unless-stopped`) and sign-in still works, so the data volume persisted.
- [x] Mailbox checks are off, so no address can be `send_eligible` (the `/v1/config` response reports `smtp_available: false`).
- [ ] The page loads through the Session Manager tunnel from a Mac and a CSV job completes end to end (needs the plugin on the Mac).
- [ ] The first daily snapshot exists, and one is restored into an isolated instance with a job read back (the policy is enabled; no snapshot had run yet).
- [ ] A restart in the middle of a job resumes it and loses no saved results (covered by a local test, not tried here).
- [ ] A null-MX domain and a DNS timeout against the real resolver (covered by fixture tests).
- [ ] Anything that uses SMTP: blocked until AWS lifts the port-25 block.

## Sources

[1] https://www.rfc-editor.org/rfc/rfc5321.txt
[2] https://www.rfc-editor.org/rfc/rfc7505.html
[3] https://docs.aws.amazon.com/ses/latest/dg/reputationdashboardmessages.html
[4] https://repost.aws/knowledge-center/ec2-port-25-throttle
