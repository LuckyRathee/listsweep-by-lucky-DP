'use strict';
// All user-supplied text (CSV cells, filenames, errors) is rendered with textContent only.

const $ = (id) => document.getElementById(id);
const PAGE_SIZE = 50;
const POLL_MS = 1500;

/* ---------- wording ---------- */
const REASONS = {
  smtp_rcpt_accepted: 'The server accepted this exact address and rejected a random one',
  smtp_mailbox_rejected: 'The server says this mailbox does not exist',
  smtp_temporary_failure: 'Temporary failure (greylisting or a busy server)',
  smtp_ambiguous_response: 'The server gave an unclear answer that does not say whether the mailbox exists',
  smtp_control_inconclusive: 'The mailbox check was inconclusive',
  smtp_non_public_target: 'The mail server is not on the public internet',
  smtp_unreachable: 'The mail server could not be reached',
  smtp_unresolved: 'The mail server name did not resolve',
  smtp_connection_dropped: 'The mail server hung up before the control check finished',
  smtp_blocked_by_policy: "The recipient's mail server refused this machine (for example a spam blocklist), so the mailbox was not checked",
  catch_all_domain: 'The domain accepts every address, so this one is unproven',
  null_mx: 'The domain publishes a null MX and accepts no mail',
  domain_not_found: 'The domain does not exist',
  no_mail_host: 'The domain exists but has no mail server or address record',
  disposable_domain: 'Disposable email domain',
  invalid_syntax: 'Not a valid email address',
  suppressed_prior_bounce: 'This address hard-bounced before',
  mailbox_check_disabled: 'The domain can receive mail; the mailbox was not checked',
  verification_disabled: 'Syntax only; no network checks were run',
  verification_error: 'The check failed, so the result is unknown',
  dns_timeout: 'The DNS lookup timed out',
  dns_no_nameservers: 'No DNS server answered',
  dns_failure: 'The DNS lookup failed',
  queued: 'Not checked yet',
};
const SHORT = {
  smtp_rcpt_accepted: 'Accepted', smtp_mailbox_rejected: 'Mailbox missing', smtp_temporary_failure: 'Temporary failure',
  smtp_ambiguous_response: 'Unclear reply', smtp_control_inconclusive: 'Inconclusive', smtp_non_public_target: 'Private server',
  smtp_unreachable: 'Unreachable', smtp_unresolved: 'Server not found', smtp_connection_dropped: 'Connection dropped',
  smtp_blocked_by_policy: 'Blocked by server', catch_all_domain: 'Catch-all domain', null_mx: 'Accepts no mail',
  domain_not_found: 'No such domain', no_mail_host: 'No mail server', disposable_domain: 'Disposable',
  invalid_syntax: 'Invalid address', suppressed_prior_bounce: 'Bounced before', mailbox_check_disabled: 'Domain OK, not checked',
  verification_disabled: 'Syntax only', verification_error: 'Check failed', dns_timeout: 'DNS timeout',
  dns_no_nameservers: 'DNS unavailable', dns_failure: 'DNS failed', queued: 'Not checked',
};
const STATUS_LABEL = { deliverable_candidate: 'Candidate', unknown: 'Unknown', undeliverable: 'Undeliverable' };
const STATUS_RANK = { deliverable_candidate: 0, unknown: 1, undeliverable: 2 };
const VERDICT = {
  deliverable_candidate: ['Deliverable candidate', 'Evidence, not a guarantee that mail will arrive.'],
  unknown: ['Unknown', 'Unknown is never treated as valid.'],
  undeliverable: ['Undeliverable', 'This address is very unlikely to receive mail.'],
};
const SMTP_OUTCOME = {
  not_run: 'Not run for this check',
  refused: "Refused by the recipient's server",
  unreachable: 'Could not connect to the mail server',
  skipped: 'Not reached (stopped at an earlier step)',
};
const DEPTHS = [
  ['syntax', 'Syntax only', 'Checks the address format only. Nothing leaves this server.'],
  ['domain', '+ Domain records', "Also looks up the domain's mail servers (DNS). Finds domains that cannot receive mail."],
  ['mailbox', '+ Mailbox check', "Also asks the recipient's mail server whether the mailbox exists. Slower: large lists can take several minutes, and some servers refuse or throttle checks."],
];
const GROUPS = [
  { reasons: ['smtp_rcpt_accepted'], tone: 'ok', text: (n) => `${n} confirmed as deliverable candidates` },
  { reasons: ['smtp_blocked_by_policy'], tone: 'warn', text: (n) => `${n} not checked: the recipient's mail server refused this network, often because the sending IP is on a spam blocklist. Try again from a different network or server.` },
  { reasons: ['smtp_unreachable', 'smtp_unresolved', 'smtp_non_public_target'], tone: 'warn', text: (n) => `${n} had a mail server that could not be reached` },
  { reasons: ['smtp_ambiguous_response', 'smtp_control_inconclusive', 'smtp_connection_dropped', 'smtp_temporary_failure'], tone: 'warn', text: (n) => `${n} got unclear or temporary answers; re-checking later often helps` },
  { reasons: ['catch_all_domain'], tone: 'warn', text: (n) => `${n} are on catch-all domains, which accept every address, so the mailbox cannot be confirmed` },
  { reasons: ['mailbox_check_disabled', 'verification_disabled'], tone: '', text: (n) => `${n} were not mailbox-checked (syntax or domain checks only)` },
  { reasons: ['dns_timeout', 'dns_no_nameservers', 'dns_failure', 'verification_error'], tone: 'warn', text: (n) => `${n} hit a lookup or check error; re-checking may resolve them` },
  { reasons: ['invalid_syntax', 'domain_not_found', 'null_mx', 'no_mail_host', 'smtp_mailbox_rejected', 'disposable_domain', 'suppressed_prior_bounce'], tone: 'bad', text: (n) => `${n} are undeliverable` },
  { reasons: ['queued'], tone: '', text: (n) => `${n} were never checked (the job was cancelled)` },
];
const ROUTES = [
  ['GET /healthz', 'Liveness, no key needed'],
  ['GET /v1/config', 'Limits and which checks are available'],
  ['POST /v1/verify/email', 'JSON {email, check_dns, allow_smtp}; one address, answered immediately'],
  ['POST /v1/verify/csv', 'Upload a CSV (multipart: file, check_dns, allow_smtp, concurrency)'],
  ['POST /v1/verify/xlsx', 'Upload an Excel .xlsx (same fields)'],
  ['POST /v1/verify/sheet', 'JSON {url, check_dns, allow_smtp, concurrency} for a public Google Sheet'],
  ['GET /v1/jobs', 'Recent jobs'],
  ['GET /v1/jobs/{id}', 'Job status, counts and reason counts'],
  ['GET /v1/jobs/{id}/results', 'Results as JSON (send_only, offset, limit)'],
  ['GET /v1/jobs/{id}/results.csv', 'Results as CSV (send_only)'],
  ['POST /v1/jobs/{id}/cancel', 'Stop a queued or running job; finished rows are kept'],
  ['GET /v1/keys', 'List API keys (name, prefix, dates; never the key itself)'],
  ['POST /v1/keys', 'JSON {name}; create a key. The key is returned once'],
  ['DELETE /v1/keys/{id}', 'Delete a key; it stops working immediately'],
  ['GET /v1/mcp-config', 'MCP settings filled in for this server'],
  ['POST /v1/jobs/{id}/recheck', "JSON {scope: 'unknown' | 'all'}; start a new job from a finished one"],
];

const state = {
  config: null, source: 'file', file: null,
  job: null, jobId: null, rows: [], leads: [], filter: 'all', query: '', group: null,
  sort: { key: 'status', dir: 'asc' }, page: 0, expanded: new Set(), pollTimer: null,
  auth: { mode: 'open', user: null, csrf: '', role: null },
};

/* ---------- helpers ---------- */
let busyCount = 0;
function setBusy(on) {
  busyCount = Math.max(0, busyCount + (on ? 1 : -1));
  document.body.classList.toggle('busy', busyCount > 0);   // drives the sweep line
}

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === 'text') node.textContent = value;
    else if (key === 'class') node.className = value;
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) node.append(child);
  return node;
}

function notice(message, kind = 'error') {
  const box = $('notice');
  box.textContent = message || '';
  box.className = kind === 'info' ? 'notice info' : 'notice';
  box.hidden = !message;
}

class ApiError extends Error {
  constructor(status, message, retryAfter) { super(message); this.status = status; this.retryAfter = retryAfter; }
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.auth.csrf && !['GET', 'HEAD', undefined].includes(options.method)) headers['X-CSRF-Token'] = state.auth.csrf;
  let response;
  try {
    response = await fetch(path, { ...options, headers });
  } catch {
    throw new ApiError(0, 'Could not reach the server');
  }
  if (!response.ok) {
    let detail = response.statusText;
    try { const body = await response.json(); detail = typeof body.detail === 'string' ? body.detail : detail; } catch { /* not json */ }
    if (response.status === 401 && detail === 'login_required') {
      detail = 'Your session has ended. Sign in again.';
      showLogin(detail);
    } else if (response.status === 401) {
      detail = 'Sign-in is not set up on this server.';
      showLocked();
    }
    if (response.status === 403 && detail === 'password_change_required') {
      detail = 'Choose a new password to continue.';
      if (state.auth.user) state.auth.user.must_change = true;
      go('#/account');
    }
    if (response.status === 403 && detail === 'admin_required') detail = 'Only an admin can do that.';
    if (response.status === 403 && detail === 'csrf_failed') detail = 'Your session needs refreshing. Reload the page.';
    if (response.status === 429) detail = 'Too many requests; slowing down';
    throw new ApiError(response.status, detail, Number(response.headers.get('Retry-After')) || 0);
  }
  return response;
}
const getJson = async (path, options) => (await api(path, options)).json();
const postJson = (path, body) => getJson(path, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}),
});

/* ---------- connection and key ---------- */
function setConnected(kind, text) {
  const pill = $('conn');
  pill.textContent = text;
  pill.className = kind === 'ok' ? 'pill ok' : (kind === 'pending' ? 'pill pending' : 'pill');
  refreshStart();
  refreshSingle();
}


async function loadConfig() {
  setConnected('pending', 'Connecting…');
  try {
    state.config = await getJson('/v1/config');
    setConnected('ok', state.auth.mode === 'open' ? 'Connected, no sign-in needed' : 'Connected');
    $('opt-conc').max = state.config.concurrency_range[1];
    refreshDepths();
    notice('');
    return true;
  } catch (error) {
    state.config = null;
    if (state.auth.mode === 'login') {
      if (error.status !== 401) { setConnected('bad', 'Server unreachable'); notice(error.message); }
      return false;
    }
    if (error.status === 401) {
      showLocked();
    } else {
      setConnected('bad', 'Server unreachable');
      notice(error.message);
    }
    return false;
  }
}

/* ---------- depth controls ---------- */
function buildDepth(container, hint) {
  container.replaceChildren(...DEPTHS.map(([value, label], index) => el('button', {
    type: 'button', role: 'radio', 'data-depth': value, 'aria-checked': 'false',
    tabindex: index === 0 ? '0' : '-1', text: label,
  })));
  const buttons = () => [...container.querySelectorAll('button')];
  const choose = (value) => {
    container.dataset.value = value;
    for (const button of buttons()) {
      const on = button.dataset.depth === value;
      button.setAttribute('aria-checked', String(on));
      button.tabIndex = on ? 0 : -1;
    }
    hint.textContent = DEPTHS.find((d) => d[0] === value)[2];
  };
  container.addEventListener('click', (event) => {
    const button = event.target.closest('button');
    if (button && !button.disabled) { container.dataset.touched = '1'; choose(button.dataset.depth); }
  });
  container.addEventListener('keydown', (event) => {
    const enabled = buttons().filter((b) => !b.disabled);
    const at = enabled.findIndex((b) => b.dataset.depth === container.dataset.value);
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
    if (!step || at < 0) return;
    event.preventDefault();
    const next = enabled[(at + step + enabled.length) % enabled.length];
    container.dataset.touched = '1';
    choose(next.dataset.depth);
    next.focus();
  });
  container.choose = choose;
}

function refreshDepths() {
  const container = $('depth');
  const mailboxOk = Boolean(state.config && state.config.smtp_available);
  const mailbox = container.querySelector('[data-depth=mailbox]');
  mailbox.disabled = !mailboxOk;
  mailbox.title = mailboxOk ? '' : 'Mailbox checks are switched off on this server';
  const current = container.dataset.value;
  // Until the user picks one, default to the most thorough check this server offers.
  if (!container.dataset.touched || (current === 'mailbox' && !mailboxOk)) {
    container.choose(mailboxOk ? 'mailbox' : 'domain');
  }
  if (!mailboxOk) {
    $('depth-hint').textContent = `${DEPTHS.find((d) => d[0] === container.dataset.value)[2]} Mailbox checks are switched off on this server.`;
  }
}
const depthOptions = (container) => {
  const depth = container.dataset.value || 'syntax';
  return { check_dns: depth !== 'syntax', allow_smtp: depth === 'mailbox' };
};

/* ---------- single address ---------- */
function refreshSingle() { /* buttons stay enabled; verifyOne explains what is missing */ }
function fact(label, value) { return [el('dt', { text: label }), el('dd', { text: value })]; }

function step(label, tone, detail) {
  return el('li', { class: tone }, [el('b', { text: label }), el('span', { text: detail })]);
}

function renderSingle(r) {
  const [headline, caveat] = VERDICT[r.status] || [r.status, ''];
  $('sr-badge').className = `badge big ${r.status}`;
  $('sr-badge').textContent = headline;
  $('sr-text').textContent = `${REASONS[r.reason] || r.reason}.`;
  $('sr-caveat').textContent = caveat;

  const cannotReceive = ['null_mx', 'domain_not_found', 'no_mail_host'].includes(r.reason);
  const dnsFailed = ['dns_timeout', 'dns_no_nameservers', 'dns_failure'].includes(r.reason);
  let domain;
  if (!r.syntax_valid) domain = step('Domain', '', 'Not reached');
  else if (!r.dns_checked) domain = step('Domain', '', 'Not checked');
  else if (cannotReceive) domain = step('Domain', 'bad', 'Cannot receive mail');
  else if (dnsFailed) domain = step('Domain', 'warn', 'Lookup failed');
  else domain = step('Domain', 'done', 'Mail servers found');

  let mailbox;
  if (r.smtp_outcome === 'answered') {
    mailbox = r.status === 'deliverable_candidate' ? step('Mailbox', 'done', 'Accepted')
      : r.status === 'undeliverable' ? step('Mailbox', 'bad', 'Rejected') : step('Mailbox', 'warn', 'Unclear answer');
  } else if (r.smtp_outcome === 'refused') mailbox = step('Mailbox', 'warn', 'Refused by the server');
  else if (r.smtp_outcome === 'unreachable') mailbox = step('Mailbox', 'warn', 'Could not connect');
  else if (r.smtp_outcome === 'skipped') mailbox = step('Mailbox', '', 'Not reached');
  else mailbox = step('Mailbox', '', 'Not run');
  $('sr-steps').replaceChildren(
    step('Syntax', r.syntax_valid ? 'done' : 'bad', r.syntax_valid ? 'Valid' : 'Not valid'), domain, mailbox);

  const flags = [r.role_based && 'Role address', r.disposable && 'Disposable domain', r.catch_all && 'Catch-all domain'].filter(Boolean);
  $('sr-facts').replaceChildren(
    ...fact('Address', r.email),
    ...(r.mx_hosts && r.mx_hosts.length ? fact('Mail servers', r.mx_hosts.slice(0, 3).join(', ')) : []),
    ...(r.smtp_outcome === 'answered' && r.smtp_code ? fact('Server reply', String(r.smtp_code)) : []),
    ...(flags.length ? fact('Flags', flags.join(', ')) : []),
    ...fact('Send-eligible', r.send_eligible ? 'Yes' : 'No'),
  );
  $('single-result').hidden = false;
}

async function verifyOne(event) {
  event.preventDefault();
  const email = $('single-email').value.trim();
  if (!email) { notice('Enter an email address to check.', 'info'); $('single-email').focus(); return; }
  if (!state.config) { notice('Not connected to the server yet. Check the connection in the sidebar.'); return; }
  notice('');
  $('single-result').hidden = true;   // never show a previous address's result for a new check
  $('single-go').disabled = true;
  $('single-go').textContent = 'Checking…';
  setBusy(true);
  try {
    renderSingle(await postJson('/v1/verify/email', { email, ...depthOptions($('depth')) }));
  } catch (error) {
    notice(error.message);
  } finally {
    setBusy(false);
    $('single-go').disabled = false;
    $('single-go').textContent = 'Verify';
  }
}

/* ---------- routing and tabs ---------- */
const TABS = ['verify', 'jobs', 'api', 'admin', 'account'];
const NAV_TABS = () => [...document.querySelectorAll('.tabs [role=tab]:not([hidden])')];

function showTab(name) {
  for (const tab of TABS) {
    const selected = tab === name;
    const button = document.querySelector(`.tabs [role=tab]#tab-${tab}`);
    if (button) { button.setAttribute('aria-selected', String(selected)); button.tabIndex = selected ? 0 : -1; }
    $(`panel-${tab}`).hidden = !selected;
  }
  if (name === 'jobs') loadJobs();
  if (name === 'api') loadApiPanel();
  if (name === 'admin') loadAdminPanel();
  if (name === 'account') renderAccount();
}

function route() {
  const hash = location.hash;
  const job = hash.match(/^#\/job\/([A-Za-z0-9-]{8,64})$/);
  if (job) {
    showTab('verify');
    if (job[1] !== state.jobId || $('results').hidden) openJob(job[1]);
    return;
  }
  const tab = hash.match(/^#\/(verify|jobs|api|admin|account)$/);
  let name = tab ? tab[1] : 'verify';
  if (state.auth.user && state.auth.user.must_change) name = 'account';          // must choose a password first
  else if (name === 'admin' && state.auth.role !== 'admin') name = 'verify';
  else if (name === 'account' && !state.auth.user) name = 'verify';
  showTab(name);
}
const go = (hash) => { if (location.hash === hash) route(); else location.hash = hash; };
const quietHash = (hash) => history.replaceState(null, '', hash);

async function openJob(jobId) {
  clearTimeout(state.pollTimer);
  stopWatching();
  try {
    const job = await getJson(`/v1/jobs/${encodeURIComponent(jobId)}`);
    state.jobId = jobId;
    if (job.status === 'queued' || job.status === 'running') {
      $('results').hidden = true;
      watchJob(jobId, 0);
    } else {
      $('progress-card').hidden = true;
      await showResults(job);
    }
  } catch (error) {
    notice(error.status === 404 ? 'That job no longer exists.' : error.message);
  }
}

/* ---------- source + file ---------- */
function setSource(source) {
  state.source = source;
  $('src-file').classList.toggle('on', source === 'file');
  $('src-sheet').classList.toggle('on', source === 'sheet');
  $('src-file').setAttribute('aria-pressed', String(source === 'file'));
  $('src-sheet').setAttribute('aria-pressed', String(source === 'sheet'));
  $('file-source').hidden = source !== 'file';
  $('sheet-source').hidden = source !== 'sheet';
  refreshStart();
}
const startReady = () => (state.source === 'file' ? Boolean(state.file) : $('sheet-url').value.trim().length > 0);
function refreshStart() { /* buttons stay enabled; startJob explains what is missing */ }

function chooseFile(file) {
  if (!file) return;
  const lower = file.name.toLowerCase();
  if (!lower.endsWith('.csv') && !lower.endsWith('.xlsx')) {
    notice('Choose a .csv or .xlsx file. Legacy .xls files are not supported; save as .xlsx or CSV.');
    return;
  }
  notice('');
  state.file = file;
  $('drop').classList.add('has');
  $('drop-title').textContent = file.name;
  $('drop-sub').textContent = `${(file.size / 1024).toFixed(1)} KB. Click to choose a different file.`;
  refreshStart();
}

/* ---------- jobs: start, watch, cancel, re-check ---------- */
function beginWatching(job) {
  stopWatching();
  state.jobId = job.job_id;
  state.job = null;
  quietHash(`#/job/${job.job_id}`);
  $('results').hidden = true;
  watchJob(job.job_id);
}

async function startJob() {
  if (state.source === 'file' && !state.file) { notice('Choose a CSV or Excel file first.', 'info'); return; }
  if (state.source === 'sheet' && !$('sheet-url').value.trim()) { notice('Paste a Google Sheets link first.', 'info'); $('sheet-url').focus(); return; }
  if (!state.config) { notice('Not connected to the server yet. Check the connection in the sidebar.'); return; }
  notice('');
  $('start').disabled = true;
  const options = depthOptions($('depth'));
  const concurrency = Math.max(1, Math.min(25, parseInt($('opt-conc').value, 10) || 4));
  try {
    let job;
    if (state.source === 'file') {
      const form = new FormData();
      form.append('file', state.file);
      form.append('check_dns', String(options.check_dns));
      form.append('allow_smtp', String(options.allow_smtp));
      form.append('concurrency', String(concurrency));
      const route = state.file.name.toLowerCase().endsWith('.xlsx') ? '/v1/verify/xlsx' : '/v1/verify/csv';
      job = await getJson(route, { method: 'POST', body: form });
    } else {
      job = await postJson('/v1/verify/sheet', { url: $('sheet-url').value.trim(), ...options, concurrency });
    }
    beginWatching(job);
  } catch (error) {
    notice(error.message);
  } finally {
    $('start').disabled = false;
  }
}

function watchJob(jobId, delay = 1000, failures = 0) {
  clearTimeout(state.pollTimer);
  if (!state.watching) { state.watching = true; setBusy(true); }
  $('progress-card').hidden = false;
  $('cancel-job').disabled = false;
  const tick = async () => {
    try {
      const job = await getJson(`/v1/jobs/${encodeURIComponent(jobId)}`);
      if (jobId !== state.jobId) return;
      notice('');
      $('progress-title').textContent = 'Verifying';
      $('progress').max = Math.max(job.total, 1);
      $('progress').value = job.processed;
      $('progress-text').textContent = `${job.processed} of ${job.total} checked`;
      if (['completed', 'failed', 'cancelled'].includes(job.status)) {
        stopWatching();
        $('progress-card').hidden = true;
        if (job.status === 'failed') notice(`Job failed: ${job.error || 'unknown error'}`);
        await showResults(job);
        return;
      }
      state.pollTimer = setTimeout(tick, POLL_MS);
    } catch (error) {
      if (error.status === 429) {
        state.pollTimer = setTimeout(tick, Math.max(error.retryAfter, 5) * 1000);
      } else if (failures < 5 && error.status !== 404 && error.status !== 401) {
        notice('Connection problem, retrying…', 'info');
        watchJob(jobId, 3000, failures + 1);
      } else {
        stopWatching();
        notice(`${error.message}. Open the job again from the Jobs tab to resume watching it.`);
      }
    }
  };
  state.pollTimer = setTimeout(tick, delay);
}

function stopWatching() {
  if (state.watching) { state.watching = false; setBusy(false); }
}

async function cancelCurrent(jobId = state.jobId) {
  if (!jobId) return;
  const tracked = jobId === state.jobId;
  if (tracked) $('cancel-job').disabled = true;
  try {
    await postJson(`/v1/jobs/${encodeURIComponent(jobId)}/cancel`);
    if (tracked) watchJob(jobId, 200);
    loadJobs();
  } catch (error) {
    if (tracked) $('cancel-job').disabled = false;
    notice(error.message);
    loadJobs();
  }
}

async function recheck(scope) {
  if (!state.jobId) return;
  notice('');
  try {
    // Uses the "How thorough" setting above, so a shallow job can be re-checked more deeply.
    beginWatching(await postJson(`/v1/jobs/${encodeURIComponent(state.jobId)}/recheck`,
      { scope, ...depthOptions($('depth')) }));
  } catch (error) {
    notice(error.message);
  }
}

/* ---------- results ---------- */
// Rebuilding the table/chips would drop keyboard focus; remember what had it and restore it.
function captureFocus() {
  const active = document.activeElement;
  let selector = null;
  if (active && active.dataset) {
    if (active.dataset.pos) selector = `#rows tr.row[data-pos="${CSS.escape(active.dataset.pos)}"]`;
    else if (active.dataset.key && active.closest('#head-row')) selector = `#head-row button[data-key="${CSS.escape(active.dataset.key)}"]`;
    else if (active.dataset.value && active.closest('#chips')) selector = `#chips button[data-value="${CSS.escape(active.dataset.value)}"]`;
  }
  return () => { if (selector) { const node = document.querySelector(selector); if (node) node.focus({ preventScroll: true }); } };
}

const shortReason = (r) => SHORT[r.reason] || r.reason;

async function showResults(job) {
  const rows = [];
  for (let offset = 0; offset < 4000; offset += 1000) {
    const batch = await getJson(`/v1/jobs/${encodeURIComponent(job.job_id || job.id)}/results?offset=${offset}&limit=1000`);
    rows.push(...batch);
    if (batch.length < 1000) break;
  }
  Object.assign(state, {
    job, jobId: job.job_id || job.id, rows, filter: 'all', query: '', group: null, page: 0,
    expanded: new Set(), sort: { key: 'status', dir: 'asc' },
  });
  const leads = [];
  for (const r of rows) for (const key of Object.keys(r.metadata || {})) if (!leads.includes(key) && leads.length < 3) leads.push(key);
  state.leads = leads;
  $('search').value = '';
  $('progress-card').hidden = true;
  $('results').hidden = false;
  renderHeadline();
  renderStale();
  renderSummary();
  renderChips();
  renderTable();
  $('recheck-unknown').hidden = !rows.some((r) => r.status === 'unknown' || r.reason === 'queued');
  $('results-title').textContent = job.status === 'cancelled' ? 'Results (cancelled)' : 'Results';
  $('results').scrollIntoView({ behavior: 'smooth', block: 'start' });
  $('results-title').focus({ preventScroll: true });
}

function count(status) { return state.rows.filter((r) => r.status === status).length; }

function renderHeadline() {
  const total = state.rows.length;
  const ok = count('deliverable_candidate'), unknown = count('unknown'), bad = count('undeliverable');
  const eligible = state.rows.filter((r) => r.send_eligible).length;
  const duplicates = (state.job && state.job.duplicate_count) || 0;
  const bar = el('div', { class: 'propbar', role: 'img', 'aria-label': `${ok} candidates, ${unknown} unknown, ${bad} undeliverable` });
  for (const [cls, n] of [['p-ok', ok], ['p-unknown', unknown], ['p-bad', bad]]) {
    if (!n) continue;
    const seg = el('i', { class: cls });
    seg.style.width = `${(n / Math.max(total, 1)) * 100}%`;
    bar.append(seg);
  }
  const legend = el('ul', { class: 'legend' }, [
    el('li', { class: 'ok' }, [el('b', { text: String(ok) }), ' candidates']),
    el('li', { class: 'unknown' }, [el('b', { text: String(unknown) }), ' unknown']),
    el('li', { class: 'bad' }, [el('b', { text: String(bad) }), ' undeliverable']),
  ]);
  const extra = [`${eligible} send-eligible`];
  if (duplicates) extra.push(`${duplicates} duplicate${duplicates === 1 ? '' : 's'} removed`);
  $('headline').replaceChildren(
    el('div', { class: 'headline-num' }, [el('b', { text: String(ok) }), el('span', { text: `of ${total} are deliverable candidates` })]),
    el('div', {}, [bar, legend]),
    el('p', { class: 'headline-extra', text: extra.join(' · ') }),
  );
}

function renderStale() {
  const engine = state.job && state.job.options && state.job.options.engine;
  const old = state.config && engine !== state.config.engine;
  $('stale').hidden = !old;
  if (old) {
    $('stale-text').textContent = 'This job was run by an older version of the checker. Some Undeliverable or Unknown results may be different now. Re-checking runs every address again with the current rules.';
  }
}

function renderSummary() {
  const reasonCounts = {};
  for (const r of state.rows) reasonCounts[r.reason] = (reasonCounts[r.reason] || 0) + 1;
  const items = [];
  for (const group of GROUPS) {
    const n = group.reasons.reduce((sum, reason) => sum + (reasonCounts[reason] || 0), 0);
    if (!n) continue;
    const show = el('button', { type: 'button', text: 'Show' });
    show.addEventListener('click', () => {
      state.group = { reasons: new Set(group.reasons), text: group.text(n) };
      state.filter = 'all'; state.page = 0;
      renderChips(); renderTable();
      $('table').scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
    items.push(el('li', { class: group.tone }, [el('span', { text: group.text(n) }), show]));
  }
  $('run-summary').replaceChildren(...items);
  $('run-summary-card').hidden = items.length === 0;
}

function renderChips() {
  const options = [['all', 'All', state.rows.length], ['deliverable_candidate', 'Candidates', count('deliverable_candidate')],
    ['unknown', 'Unknown', count('unknown')], ['undeliverable', 'Undeliverable', count('undeliverable')]];
  $('chips').replaceChildren(...options.map(([value, label, n]) => {
    const chip = el('button', { type: 'button', class: 'chip', 'data-value': value, 'aria-pressed': String(state.filter === value), text: `${label} ${n}` });
    chip.addEventListener('click', () => { state.filter = value; state.page = 0; const restore = captureFocus(); renderChips(); renderTable(); restore(); });
    return chip;
  }));
}

function matches(r) {
  if (state.filter !== 'all' && r.status !== state.filter) return false;
  if (state.group && !state.group.reasons.has(r.reason)) return false;
  const query = state.query.toLowerCase();
  if (!query) return true;
  const haystack = [r.email, r.original_email, r.reason, shortReason(r), REASONS[r.reason] || '', ...Object.values(r.metadata || {})];
  return haystack.join(' ').toLowerCase().includes(query);
}

function sortValue(r, key) {
  if (key === 'email') return r.email.toLowerCase();
  if (key === 'status') return (STATUS_RANK[r.status] ?? 9) * 1000 + shortReason(r).toLowerCase().charCodeAt(0);
  if (key === 'send') return r.send_eligible ? 0 : 1;
  if (key.startsWith('lead:')) return String((r.metadata || {})[key.slice(5)] || '').toLowerCase();
  return 0;
}

function columns() {
  return [{ key: 'email', label: 'Email' }, ...state.leads.map((name) => ({ key: `lead:${name}`, label: name })),
    { key: 'status', label: 'Result' }, { key: 'send', label: 'Send' }];
}

function renderHead() {
  $('head-row').replaceChildren(...columns().map((col) => {
    const active = state.sort.key === col.key;
    const th = el('th', { scope: 'col', 'aria-sort': active ? (state.sort.dir === 'asc' ? 'ascending' : 'descending') : 'none' });
    const button = el('button', { type: 'button', 'data-key': col.key, text: col.label });
    button.addEventListener('click', () => {
      state.sort = { key: col.key, dir: active && state.sort.dir === 'asc' ? 'desc' : 'asc' };
      const restore = captureFocus();
      renderTable();
      restore();
    });
    th.append(button);
    return th;
  }));
}

function detailRow(r, span) {
  const entries = [['Result', REASONS[r.reason] || r.reason]];
  if (r.original_email && r.original_email !== r.email) entries.push(['As entered', r.original_email]);
  if (r.smtp_code) entries.push(['Server reply code', String(r.smtp_code)]);
  if (r.mx_host) entries.push(['Mail server', r.mx_host]);
  if (r.flags && r.flags.length) entries.push(['Flags', r.flags.join(', ').replaceAll('_', ' ')]);
  if (r.checked_at) entries.push(['Checked', new Date(r.checked_at).toLocaleString()]);
  for (const [name, value] of Object.entries(r.metadata || {})) if (value) entries.push([name, value]);
  const grid = el('dl', { class: 'detail-grid' });
  for (const [label, value] of entries) grid.append(el('dt', { text: label }), el('dd', { text: value }));
  return el('tr', { class: 'detail' }, el('td', { colspan: String(span) }, grid));
}

function renderTable() {
  renderHead();
  const cols = columns();
  const rows = state.rows.filter(matches);
  const direction = state.sort.dir === 'asc' ? 1 : -1;
  rows.sort((a, b) => {
    const x = sortValue(a, state.sort.key), y = sortValue(b, state.sort.key);
    return (x < y ? -1 : x > y ? 1 : 0) * direction || a.position - b.position;
  });
  const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  state.page = Math.min(state.page, pages - 1);
  const out = [];
  for (const r of rows.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE)) {
    const open = state.expanded.has(r.position);
    const tr = el('tr', { class: 'row', tabindex: '0', 'data-pos': String(r.position), 'aria-expanded': String(open) });
    const toggle = () => { open ? state.expanded.delete(r.position) : state.expanded.add(r.position); const restore = captureFocus(); renderTable(); restore(); };
    tr.addEventListener('click', toggle);
    tr.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); toggle(); } });
    tr.append(
      el('td', { class: 'email', text: r.email }),
      ...state.leads.map((name) => el('td', { text: (r.metadata || {})[name] || '' })),
      el('td', { class: 'result' }, [
        el('span', { class: `badge ${r.status}`, text: STATUS_LABEL[r.status] || r.status }),
        el('span', { class: 'why', text: shortReason(r) }),
      ]),
      el('td', { class: r.send_eligible ? 'send yes' : 'send', title: r.send_eligible ? 'Send-eligible' : 'Not send-eligible', 'aria-label': r.send_eligible ? 'Send-eligible' : 'Not send-eligible', text: r.send_eligible ? '\u2713' : '\u2013' }),
    );
    out.push(tr);
    if (open) out.push(detailRow(r, cols.length));
  }
  $('rows').replaceChildren(...out);
  $('page-info').textContent = rows.length ? `Page ${state.page + 1} of ${pages} (${rows.length} shown)` : 'No matching addresses';
  $('prev').disabled = state.page === 0;
  $('next').disabled = state.page >= pages - 1;
  const note = $('filter-note');
  note.hidden = !state.group;
  if (state.group) {
    const clear = el('button', { type: 'button', text: 'Clear' });
    clear.addEventListener('click', () => { state.group = null; state.page = 0; renderTable(); });
    note.replaceChildren(document.createTextNode(`Showing: ${state.group.text.replace(/\.$/, '')}. `), clear);
  }
}

async function download(sendOnly, jobId = state.jobId) {
  if (!jobId) return;
  $('dl-menu').open = false;
  try {
    const response = await api(`/v1/jobs/${encodeURIComponent(jobId)}/results.csv?send_only=${sendOnly}`);
    const named = /filename="([^"]+)"/.exec(response.headers.get('Content-Disposition') || '');
    const url = URL.createObjectURL(await response.blob());
    const link = el('a', { href: url, download: named ? named[1] : (sendOnly ? 'send-eligible.csv' : 'results.csv') });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  } catch (error) {
    notice(error.message);
  }
}

/* ---------- jobs tab ---------- */
function checksLabel(options = {}) {
  if (options.allow_smtp) return 'Syntax, domain, mailbox';
  return options.check_dns ? 'Syntax, domain' : 'Syntax only';
}

async function loadJobs() {
  try {
    const jobs = await getJson('/v1/jobs?limit=50');
    $('no-jobs').hidden = jobs.length > 0;
    $('job-rows').replaceChildren(...jobs.map((job) => {
      const counts = job.counts || {};
      const summary = `${counts.deliverable_candidate || 0} candidate, ${counts.unknown || 0} unknown, ${counts.undeliverable || 0} undeliverable`;
      const outdated = state.config && (job.options || {}).engine !== state.config.engine;
      const open = el('button', { type: 'button', class: 'btn small', text: 'Open' });
      open.addEventListener('click', () => go(`#/job/${job.id}`));
      const actions = el('div', { class: 'actions' }, [open]);
      if (job.status === 'queued' || job.status === 'running') {
        const stop = el('button', { type: 'button', class: 'btn small', text: 'Cancel' });
        stop.addEventListener('click', () => cancelCurrent(job.id));
        actions.append(stop);
      } else {
        const save = el('button', { type: 'button', class: 'btn small', text: 'Download' });
        save.addEventListener('click', () => download(false, job.id));
        actions.append(save);
      }
      const status = el('td', {}, el('span', { class: `badge ${job.status}`, text: job.status }));
      if (outdated) status.append(el('span', { class: 'tag', title: 'Run by an older version of the checker', text: 'older version' }));
      return el('tr', {}, [
        el('td', { text: new Date(job.created_at).toLocaleString() }),
        el('td', { text: (job.options && job.options.filename) || 'pasted list' }),
        el('td', { text: checksLabel(job.options) }),
        status,
        el('td', { text: String(job.total) }),
        el('td', { text: summary }),
        el('td', {}, actions),
      ]);
    }));
  } catch (error) {
    notice(error.message);
  }
}

/* ---------- API and agents ---------- */
// Copy with a fallback: the async clipboard API can be blocked (permissions, non-secure address),
// so fall back to selecting the text and using the older copy command.
async function copyText(text, field) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch { /* fall through */ }
  let temp = null;
  try {
    const source = field || (temp = el('textarea', { readonly: '', 'aria-hidden': 'true', tabindex: '-1' }));
    if (temp) { temp.value = text; document.body.append(temp); }
    source.focus();
    source.select();
    const ok = document.execCommand('copy');
    if (temp) temp.remove();
    return ok;
  } catch {
    if (temp) temp.remove();
    return false;
  }
}

function copyButton(getText, label = 'Copy') {
  const button = el('button', { type: 'button', class: 'btn small', text: label });
  button.addEventListener('click', async () => {
    button.textContent = (await copyText(getText())) ? 'Copied' : 'Copy failed';
    setTimeout(() => { button.textContent = label; }, 1500);
  });
  return button;
}

function codeBlock(title, code) {
  return el('div', { class: 'example' }, [
    el('div', { class: 'toolbar' }, [el('h3', { text: title }), el('span', { class: 'grow' }), copyButton(() => code)]),
    el('pre', { text: code }),
  ]);
}

const shellQuote = (value) => (/^[A-Za-z0-9_\/.:@%+=,-]+$/.test(value) ? value : `'${value.replace(/'/g, "'\\''")}'`);
const when = (iso) => (iso ? new Date(iso).toLocaleString() : 'Never');

async function loadKeys() {
  try {
    const { keys, server_key: serverKey, max_keys: maxKeys } = await getJson('/v1/keys');
    state.keys = keys;
    $('key-rows').replaceChildren(...keys.map((key) => {
      const del = el('button', { type: 'button', class: 'btn small', text: 'Delete' });
      let armed = null;
      del.addEventListener('click', async () => {
        if (!armed) {
          del.textContent = 'Confirm delete';
          armed = setTimeout(() => { armed = null; del.textContent = 'Delete'; }, 4000);
          return;
        }
        clearTimeout(armed);
        await deleteKey(key);
      });
      const name = el('td', { text: key.name });
      return el('tr', {}, [name, el('td', { class: 'keyid', text: `${key.prefix}…` }), el('td', { text: when(key.created_at) }),
        el('td', { text: when(key.last_used_at) }), el('td', {}, del)]);
    }));
    $('key-empty').hidden = keys.length > 0;
    $('key-empty').textContent = serverKey
      ? 'No keys yet. The server key set at startup still works.'
      : 'No keys yet, so ListSweep is open to anyone who can reach it. Create a key to switch authentication on.';
    $('key-server').hidden = !serverKey;
    $('key-server').textContent = 'A server key is also set when the server starts. It always works and cannot be deleted here.';
    $('key-create').querySelector('button').disabled = keys.length >= maxKeys;
  } catch (error) {
    if (error.status !== 401) notice(error.message);
  }
}

async function createKey(event) {
  event.preventDefault();
  const name = $('key-name').value.trim();
  if (!name) { notice('Give the key a name, for example the agent that will use it.', 'info'); $('key-name').focus(); return; }
  notice('');
  if (state.auth.mode === 'open') {
    notice('Add the first admin account in People above before creating API keys. Creating a key switches protection on.', 'info');
    return;
  }
  try {
    const created = await postJson('/v1/keys', { name });
    $('key-new').value = created.key;
    $('key-new').dataset.name = created.name;
    $('key-reveal').hidden = false;
    $('key-reveal').scrollIntoView({ block: 'nearest' });
    $('key-note').textContent = 'Store it somewhere safe. ListSweep keeps only a fingerprint of it, so it cannot be shown again.';
    $('key-name').value = '';
    $('key-new').focus();
    $('key-new').select();
    await loadKeys();
    renderHttpConnect(created.key.slice(0, 8));
  } catch (error) {
    notice(error.message);
  }
}

async function deleteKey(key) {
  try {
    await api(`/v1/keys/${encodeURIComponent(key.id)}`, { method: 'DELETE' });
    hideReveal();
    notice(`Deleted "${key.name}". It stopped working immediately.`, 'info');
    await loadKeys();
  } catch (error) {
    notice(error.message);
  }
}

function hideReveal() {
  $('key-reveal').hidden = true;
  $('key-new').value = '';
}

function renderHttpConnect() {
  const base = location.origin;
  $('how-http').replaceChildren(
    el('li', { text: 'Create a key above and copy it. Make one per agent.' }),
    el('li', {}, [document.createTextNode('Point the agent at this address: '), el('code', { text: base }),
      document.createTextNode(base.includes('127.0.0.1') || base.includes('localhost') ? '. That only works on this computer. For another computer, run ListSweep on a server (see the deployment guide) and use its address.' : '.')]),
    el('li', {}, [document.createTextNode('Have it send the key in a header on every request: '), el('code', { text: 'X-API-Key: YOUR_KEY' }),
      document.createTextNode(' (or '), el('code', { text: 'Authorization: Bearer YOUR_KEY' }), document.createTextNode(').')]),
    el('li', { text: 'Give it the instructions below, or the examples further down.' }),
  );
  const text = [
    'You can verify email addresses with ListSweep.',
    `Base URL: ${base}`,
    'Authentication: send the header "X-API-Key: YOUR_KEY" on every request (or "Authorization: Bearer YOUR_KEY").',
    '',
    'Check one address: POST /v1/verify/email with JSON {"email": "name@company.com", "check_dns": true, "allow_smtp": true}',
    'Check a list: POST /v1/verify/csv (multipart field "file"), then poll GET /v1/jobs/{job_id} until status is "completed",',
    '  then read GET /v1/jobs/{job_id}/results (add ?send_only=true for send-eligible rows only).',
    'Stop a job: POST /v1/jobs/{job_id}/cancel. Re-run unresolved addresses: POST /v1/jobs/{job_id}/recheck.',
    '',
    'How to read results: "deliverable_candidate" is evidence, not a guarantee. "unknown" is never valid.',
    'Only rows with send_eligible=true are reasonable to contact, and that is a filter, not consent to send.',
    'If reason is "smtp_blocked_by_policy", the recipient server refused our network, so retry later instead of treating it as invalid.',
  ].join('\n');
  $('agent-instructions').replaceChildren(codeBlock('Instructions to paste into your agent', text));
}

async function loadMcpConfig() {
  try {
    const cfg = await getJson('/v1/mcp-config');
    const json = JSON.stringify({ mcpServers: { listsweep: { command: cfg.command, args: cfg.args, env: cfg.env } } }, null, 2);
    const envFlags = Object.entries(cfg.env).map(([k, v]) => `-e ${shellQuote(`${k}=${v}`)}`).join(' ');
    const command = `claude mcp add listsweep ${envFlags} -- ${shellQuote(cfg.command)} ${cfg.args.join(' ')}`;
    $('how-mcp').replaceChildren(
      el('li', { text: 'Run your agent on the same computer as ListSweep. The paths below come from the computer running this server.' }),
      el('li', { text: "Add the settings below to your agent's MCP configuration. For Claude Desktop that is the claude_desktop_config.json file; for Claude Code, run the command instead." }),
      el('li', { text: 'Restart the agent. It should now list tools such as verify_emails.' }),
      el('li', { text: 'No key is needed: the agent starts ListSweep as its own local process. Never expose it to a network. Use HTTP with a key for that.' }),
      el('li', { text: 'The database path in the settings makes jobs your agent runs appear in the Jobs tab here.' }),
    );
    $('mcp-blocks').replaceChildren(codeBlock('MCP settings (JSON)', json), codeBlock('Or add it to Claude Code', command));
    $('mcp-tools').replaceChildren(...cfg.tools.map((tool) => el('tr', {}, [el('td', {}, el('code', { text: tool.name })), el('td', { text: tool.description })])));
  } catch (error) {
    if (error.status === 403) {
      $('how-mcp').replaceChildren(el('li', { text: 'The MCP settings include this server\'s file paths, so only an admin can see them. Ask an admin.' }));
      $('mcp-blocks').replaceChildren();
      $('mcp-tools').replaceChildren();
    } else if (error.status !== 401) notice(error.message);
  }
}

function buildApiTab() {
  $('base-url').textContent = location.origin;
  const base = location.origin;
  const examples = [
    ['Check one address', `curl -H "X-API-Key: YOUR_KEY" -H "Content-Type: application/json" \\\n  -d '{"email": "name@company.com", "check_dns": true, "allow_smtp": true}' \\\n  ${base}/v1/verify/email`],
    ['Check a CSV file', `curl -H "X-API-Key: YOUR_KEY" \\\n  -F file=@leads.csv -F check_dns=true \\\n  ${base}/v1/verify/csv`],
    ['Check an Excel file', `curl -H "X-API-Key: YOUR_KEY" \\\n  -F file=@leads.xlsx -F check_dns=true \\\n  ${base}/v1/verify/xlsx`],
    ['Check a public Google Sheet', `curl -H "X-API-Key: YOUR_KEY" -H "Content-Type: application/json" \\\n  -d '{"url": "https://docs.google.com/spreadsheets/d/SHEET_ID/edit", "check_dns": true}' \\\n  ${base}/v1/verify/sheet`],
    ['Poll, download, cancel or re-check a job', `curl -H "X-API-Key: YOUR_KEY" ${base}/v1/jobs/JOB_ID\ncurl -H "X-API-Key: YOUR_KEY" "${base}/v1/jobs/JOB_ID/results.csv?send_only=true" -OJ\ncurl -X POST -H "X-API-Key: YOUR_KEY" ${base}/v1/jobs/JOB_ID/cancel\ncurl -X POST -H "X-API-Key: YOUR_KEY" -H "Content-Type: application/json" \\\n  -d '{"scope": "unknown"}' ${base}/v1/jobs/JOB_ID/recheck`],
    ['Python', `import time, requests\n\nBASE, HEADERS = "${base}", {"X-API-Key": "YOUR_KEY"}\n\nwith open("leads.csv", "rb") as f:\n    job = requests.post(f"{BASE}/v1/verify/csv", headers=HEADERS,\n                        files={"file": f}, data={"check_dns": "true"}).json()\n\nwhile True:\n    status = requests.get(f"{BASE}/v1/jobs/{job['job_id']}", headers=HEADERS).json()\n    if status["status"] in ("completed", "failed", "cancelled"):\n        break\n    time.sleep(2)\n\nrows = requests.get(f"{BASE}/v1/jobs/{job['job_id']}/results", headers=HEADERS).json()`],
  ];
  $('examples').replaceChildren(...examples.map(([title, code]) => codeBlock(title, code)));
  $('route-rows').replaceChildren(...ROUTES.map(([route, purpose]) =>
    el('tr', {}, [el('td', {}, el('code', { text: route })), el('td', { text: purpose })])));
  renderHttpConnect();
}

function loadApiPanel() {
  const admin = state.auth.role === 'admin';
  $('keys-pointer').hidden = false;
  $('keys-pointer-text').textContent = admin
    ? 'Create and delete API keys on the Admin page.'
    : 'Ask an admin for an API key. They are created on the Admin page.';
  loadMcpConfig();
}

/* ---------- sign-in, account, admin ---------- */
function showLogin(message = '') {
  stopWatching();
  clearTimeout(state.pollTimer);
  Object.assign(state.auth, { user: null, csrf: '', role: null });
  document.body.classList.add('locked');
  $('who').hidden = true;
  $('tab-admin').hidden = true;
  $('results').hidden = true;
  $('single-result').hidden = true;
  $('progress-card').hidden = true;
  $('login-password').value = '';
  const error = $('login-error');
  error.hidden = !message;
  error.textContent = message;
  $('locked').hidden = true;
  $('login').hidden = false;
  setConnected('pending', 'Signed out');
  (message && $('login-email').value ? $('login-password') : $('login-email')).focus();
}

function showLocked() {
  stopWatching();
  clearTimeout(state.pollTimer);
  Object.assign(state.auth, { user: null, csrf: '', role: null });
  document.body.classList.add('locked');
  $('login').hidden = true;
  $('locked').hidden = false;
  $('who').hidden = true;
  $('tab-admin').hidden = true;
  setConnected('bad', 'Sign-in not set up');
}

function unlockApp() {
  $('locked').hidden = true;
  $('login').hidden = true;
  document.body.classList.remove('locked');
}

function updateWho() {
  const user = state.auth.user;
  $('who').hidden = !user;
  if (user) {
    $('who-email').textContent = user.email;
    $('who-role').textContent = user.role;
  }
  $('tab-admin').hidden = state.auth.role !== 'admin';
}

// Who am I? Works for a signed-in person, an API key, or an open (no sign-in) server.
async function refreshIdentity() {
  try {
    const me = await getJson('/v1/auth/me');
    state.auth.role = me.role;
    state.auth.user = me.method === 'user' ? { email: me.email, role: me.role, must_change: me.must_change } : null;
    if (me.csrf) state.auth.csrf = me.csrf;
  } catch {
    state.auth.role = null;
    state.auth.user = null;
  }
  updateWho();
  return Boolean(state.auth.role);
}

async function startup() {
  setConnected('pending', 'Connecting…');
  let status;
  try {
    status = await (await fetch('/v1/auth/status')).json();
  } catch {
    setConnected('bad', 'Server unreachable');
    notice('Could not reach the server.');
    return;
  }
  state.auth.mode = status.login_required ? 'login' : (status.key_required ? 'keys' : 'open');
  if (state.auth.mode === 'keys') { showLocked(); return; }
  if (state.auth.mode === 'login') {
    if (!(await refreshIdentity()) || !state.auth.user) { showLogin(); return; }
    unlockApp();
  } else {
    unlockApp();
  }
  await loadConfig();
  if (state.auth.mode !== 'login') await refreshIdentity();
  updateWho();
  route();
}

async function signIn(event) {
  event.preventDefault();
  const email = $('login-email').value.trim();
  const password = $('login-password').value;
  const error = $('login-error');
  error.hidden = true;
  if (!email || !password) {
    error.textContent = 'Enter your email and password.';
    error.hidden = false;
    return;
  }
  $('login-go').disabled = true;
  $('login-go').textContent = 'Signing in…';
  try {
    const response = await fetch('/v1/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, password }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new ApiError(response.status, typeof body.detail === 'string' ? body.detail : 'Could not sign in');
    state.auth.csrf = body.csrf;
    state.auth.mode = 'login';
    $('login-password').value = '';
    await startup();
    if (body.must_change) go('#/account');
  } catch (failure) {
    error.textContent = failure.message;
    error.hidden = false;
    $('login-password').select();
  } finally {
    $('login-go').disabled = false;
    $('login-go').textContent = 'Sign in';
  }
}

async function signOut() {
  try { await api('/v1/auth/logout', { method: 'POST' }); } catch { /* the session may already be gone */ }
  history.replaceState(null, '', '#/verify');
  $('login-email').value = '';
  showLogin();
}

function renderAccount() {
  const user = state.auth.user;
  $('account-lede').textContent = user && user.must_change
    ? 'Choose a new password to continue. The one you were given is temporary.'
    : `Signed in as ${user ? user.email : ''}.`;
  $('pw-error').hidden = true;
}

async function changePassword(event) {
  event.preventDefault();
  const error = $('pw-error');
  error.hidden = true;
  const current = $('pw-current').value, next = $('pw-new').value;
  const fail = (message) => { error.textContent = message; error.hidden = false; };
  if (!current || !next) return fail('Fill in every field.');
  if (next !== $('pw-confirm').value) return fail('The new passwords do not match.');
  try {
    await api('/v1/auth/password', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ current_password: current, new_password: next }),
    });
    for (const id of ['pw-current', 'pw-new', 'pw-confirm']) $(id).value = '';
    const wasForced = state.auth.user && state.auth.user.must_change;
    if (state.auth.user) state.auth.user.must_change = false;
    notice('Password changed. Your other sessions were signed out.', 'info');
    if (wasForced) go('#/verify');
  } catch (failure) {
    fail(failure.message);
  }
  return undefined;
}

/* --- admin: people --- */
function hideUserReveal() {
  $('user-reveal').hidden = true;
  $('user-secret').value = '';
}

function showUserSecret(title, secret, note) {
  $('user-reveal-title').textContent = title;
  $('user-secret').value = secret;
  $('user-note').textContent = note;
  $('user-reveal').hidden = false;
  $('user-reveal').scrollIntoView({ block: 'nearest' });
  $('user-secret').focus();
  $('user-secret').select();
}

function confirmButton(label, confirmLabel, action) {
  const button = el('button', { type: 'button', class: 'btn small', text: label });
  let armed = null;
  button.addEventListener('click', async () => {
    if (!armed) {
      button.textContent = confirmLabel;
      armed = setTimeout(() => { armed = null; button.textContent = label; }, 4000);
      return;
    }
    clearTimeout(armed);
    armed = null;
    button.textContent = label;
    await action();
  });
  return button;
}

async function loadUsers() {
  try {
    const { users, first_account: first } = await getJson('/v1/users');
    state.users = users;
    $('people-hint').textContent = first
      ? 'Nobody has an account yet, so anyone who can open this page can use ListSweep. Add the first admin account to switch sign-in on.'
      : 'People who can sign in. Admins also manage people and API keys. Everyone shares the same jobs and results.';
    $('user-role').value = first ? 'admin' : $('user-role').value;
    $('user-role').disabled = first;
    $('user-password').placeholder = first ? 'Password (required)' : 'Password (optional)';
    const meId = state.auth.user ? users.find((u) => u.email === state.auth.user.email) : null;
    $('user-rows').replaceChildren(...users.map((user) => {
      const isMe = Boolean(meId && meId.id === user.id);
      const role = el('select', { 'aria-label': `Role for ${user.email}` }, [el('option', { value: 'user', text: 'User' }), el('option', { value: 'admin', text: 'Admin' })]);
      role.value = user.role;
      role.disabled = isMe;
      role.addEventListener('change', () => updateUser(user, { role: role.value }, () => { role.value = user.role; }));
      const email = el('td', { text: user.email });
      if (isMe) email.append(el('span', { class: 'tag you', text: 'you' }));
      if (user.must_change) email.append(el('span', { class: 'tag you', text: 'must set password' }));
      const actions = el('div', { class: 'actions' });
      if (!isMe) {
        actions.append(
          el('button', { type: 'button', class: 'btn small', text: user.status === 'active' ? 'Disable' : 'Enable' }),
          confirmButton('Reset password', 'Confirm reset', () => resetPassword(user)),
          confirmButton('Delete', 'Confirm delete', () => removeUser(user)),
        );
        actions.firstChild.addEventListener('click', () => updateUser(user, { status: user.status === 'active' ? 'disabled' : 'active' }));
      }
      return el('tr', {}, [email, el('td', {}, role),
        el('td', {}, el('span', { class: `badge ${user.status === 'active' ? 'completed' : 'cancelled'}`, text: user.status })),
        el('td', { text: when(user.last_login_at) }), el('td', {}, actions)]);
    }));
  } catch (error) {
    if (error.status !== 401) notice(error.message);
  }
}

async function createUser(event) {
  event.preventDefault();
  const email = $('user-email').value.trim();
  const password = $('user-password').value;
  if (!email) { notice('Enter the email address of the person to add.', 'info'); $('user-email').focus(); return; }
  notice('');
  try {
    const wasFirst = state.auth.mode === 'open';
    if (wasFirst && !password) { notice('Choose a password for the first admin.', 'info'); $('user-password').focus(); return; }
    const created = await postJson('/v1/users', { email, role: wasFirst ? 'admin' : $('user-role').value, ...(password ? { password } : {}) });
    $('user-email').value = '';
    $('user-password').value = '';
    if (wasFirst) {
      // Sign in as the new admin straight away so the page keeps working now that sign-in is on.
      const response = await fetch('/v1/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, password }) });
      if (response.ok) { state.auth.csrf = (await response.json()).csrf; state.auth.mode = 'login'; await startup(); notice('Sign-in is now on. You are signed in as the new admin.', 'info'); go('#/admin'); return; }
    }
    if (created.temporary_password) {
      showUserSecret(`Temporary password for ${created.email}`, created.temporary_password,
        'Give it to them securely. It is shown once, and they must replace it the first time they sign in.');
    } else {
      hideUserReveal();
      notice(`Added ${created.email}. They must choose their own password the first time they sign in.`, 'info');
    }
    await loadUsers();
  } catch (error) {
    notice(error.message);
  }
}

async function updateUser(user, change, undo) {
  try {
    await getJson(`/v1/users/${encodeURIComponent(user.id)}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(change),
    });
    notice('');
  } catch (error) {
    notice(error.message);
    if (undo) undo();
  }
  await loadUsers();
}

async function resetPassword(user) {
  try {
    const result = await postJson(`/v1/users/${encodeURIComponent(user.id)}/reset-password`);
    showUserSecret(`New temporary password for ${user.email}`, result.temporary_password,
      'They were signed out everywhere. It is shown once, and they must replace it when they sign in.');
    await loadUsers();
  } catch (error) {
    notice(error.message);
  }
}

async function removeUser(user) {
  try {
    await api(`/v1/users/${encodeURIComponent(user.id)}`, { method: 'DELETE' });
    hideUserReveal();
    notice(`Deleted ${user.email}.`, 'info');
  } catch (error) {
    notice(error.message);
  }
  await loadUsers();
}

/* --- admin: activity --- */
const ACTIVITY_TEXT = {
  'auth.login': ['Signed in', 'completed'], 'auth.login_failed': ['Failed sign-in', 'failed'],
  'auth.login_blocked': ['Sign-in blocked', 'failed'], 'auth.logout': ['Signed out', 'cancelled'],
  'auth.password_changed': ['Changed password', 'cancelled'],
  'user.created': ['Added a person', 'cancelled'], 'user.updated': ['Changed a person', 'cancelled'],
  'user.password_reset': ['Reset a password', 'cancelled'], 'user.deleted': ['Deleted a person', 'cancelled'],
  'system.admin_created': ['First admin created', 'cancelled'],
  'key.created': ['Created an API key', 'cancelled'], 'key.deleted': ['Deleted an API key', 'cancelled'],
  'job.started': ['Started a job', 'cancelled'], 'job.cancelled': ['Cancelled a job', 'cancelled'],
  'job.rechecked': ['Re-checked a job', 'cancelled'],
};
const ACTIVITY_FILTERS = [['all', 'All'], ['signins', 'Sign-ins'], ['people', 'People'], ['keys', 'API keys'], ['jobs', 'Jobs']];
const ACTIVITY_PAGE = 50;

function ago(iso) {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}

function renderActivityChips() {
  $('activity-chips').replaceChildren(...ACTIVITY_FILTERS.map(([value, label]) => {
    const chip = el('button', { type: 'button', class: 'chip', 'aria-pressed': String(state.activity.category === value), text: label });
    chip.addEventListener('click', () => { state.activity.category = value; loadActivity(true); });
    return chip;
  }));
}

function renderActivity() {
  const { items, total } = state.activity;
  $('activity-rows').replaceChildren(...items.map((item) => {
    const [label, tone] = ACTIVITY_TEXT[item.action] || [item.action, 'cancelled'];
    const who = el('td', { text: item.actor });
    if (['key', 'server', 'system'].includes(item.actor_type)) who.append(el('span', { class: 'tag', text: item.actor_type }));
    const what = el('td', {}, [el('span', { class: `badge ${tone}`, text: label })]);
    if (item.target) what.append(el('span', { class: 'why', text: item.target }));
    const extra = [item.detail, item.address && item.action.startsWith('auth.') ? `from ${item.address}` : ''].filter(Boolean).join(' · ');
    return el('tr', {}, [el('td', { title: new Date(item.at).toLocaleString(), text: ago(item.at) }), who, what, el('td', { text: extra })]);
  }));
  $('activity-empty').hidden = items.length > 0;
  $('activity-more').hidden = items.length >= total;
  $('activity-count').textContent = total ? `Showing ${items.length} of ${total}` : '';
}

async function loadActivity(reset = true) {
  if (!state.activity) state.activity = { category: 'all', items: [], total: 0 };
  if (reset) state.activity.items = [];
  renderActivityChips();
  try {
    const query = new URLSearchParams({ category: state.activity.category, limit: String(ACTIVITY_PAGE), offset: String(state.activity.items.length) });
    const data = await getJson(`/v1/activity?${query}`);
    state.activity.items = [...state.activity.items, ...data.items];
    state.activity.total = data.total;
    renderActivity();
  } catch (error) {
    if (error.status !== 401) notice(error.message);
  }
}

function loadAdminPanel() {
  loadUsers();
  loadKeys();
  loadActivity(true);
}

/* ---------- wiring ---------- */
function initTabs() {
  for (const button of document.querySelectorAll('.tabs [role=tab]')) {
    button.addEventListener('click', () => go(`#/${button.id.replace('tab-', '')}`));
  }
  $('tab-verify').parentElement.addEventListener('keydown', (event) => {
    const tabs = NAV_TABS();
    const at = tabs.indexOf(document.activeElement);
    if (at < 0) return;
    const forward = (at + 1) % tabs.length, back = (at + tabs.length - 1) % tabs.length;
    const next = { ArrowRight: forward, ArrowDown: forward, ArrowLeft: back, ArrowUp: back, Home: 0, End: tabs.length - 1 }[event.key];
    if (next === undefined) return;
    event.preventDefault();
    tabs[next].focus();
    go(`#/${tabs[next].id.replace('tab-', '')}`);
  });
}

function init() {
  initTabs();
  buildDepth($('depth'), $('depth-hint'));
  $('depth').choose('domain');
  $('src-file').addEventListener('click', () => setSource('file'));
  $('src-sheet').addEventListener('click', () => setSource('sheet'));
  $('file').addEventListener('change', (event) => chooseFile(event.target.files[0]));
  const drop = $('drop');
  for (const type of ['dragenter', 'dragover']) drop.addEventListener(type, (event) => { event.preventDefault(); drop.classList.add('over'); });
  for (const type of ['dragleave', 'drop']) drop.addEventListener(type, (event) => { event.preventDefault(); drop.classList.remove('over'); });
  drop.addEventListener('drop', (event) => chooseFile(event.dataTransfer.files[0]));
  $('sheet-url').addEventListener('input', refreshStart);
  $('start').addEventListener('click', startJob);
  $('cancel-job').addEventListener('click', () => cancelCurrent());
  $('single-form').addEventListener('submit', verifyOne);
  $('single-email').addEventListener('input', () => { $('single-result').hidden = true; });
  $('search').addEventListener('input', (event) => { state.query = event.target.value; state.page = 0; renderTable(); });
  $('prev').addEventListener('click', () => { state.page -= 1; renderTable(); });
  $('next').addEventListener('click', () => { state.page += 1; renderTable(); });
  $('dl-all').addEventListener('click', () => download(false));
  $('dl-send').addEventListener('click', () => download(true));
  $('recheck-unknown').addEventListener('click', () => recheck('unknown'));
  $('recheck-stale').addEventListener('click', () => recheck('all'));
  $('refresh-jobs').addEventListener('click', loadJobs);
  $('key-create').addEventListener('submit', createKey);
  $('login-form').addEventListener('submit', signIn);
  $('pw-toggle').addEventListener('click', () => {
    const show = $('login-password').type === 'password';
    $('login-password').type = show ? 'text' : 'password';
    $('pw-toggle').textContent = show ? 'Hide' : 'Show';
    $('pw-toggle').setAttribute('aria-pressed', String(show));
  });
  $('who-signout').addEventListener('click', signOut);
  $('who-account').addEventListener('click', () => go('#/account'));
  $('pw-form').addEventListener('submit', changePassword);
  $('user-create').addEventListener('submit', createUser);
  $('user-done').addEventListener('click', hideUserReveal);
  $('activity-refresh').addEventListener('click', () => loadActivity(true));
  $('activity-more').addEventListener('click', () => loadActivity(false));
  $('user-copy').addEventListener('click', async () => {
    const copied = await copyText($('user-secret').value, $('user-secret'));
    $('user-copy').textContent = copied ? 'Copied' : 'Press Ctrl+C';
    if (!copied) { $('user-secret').focus(); $('user-secret').select(); }
    setTimeout(() => { $('user-copy').textContent = 'Copy'; }, 2500);
  });
  $('key-copy').addEventListener('click', async () => {
    const copied = await copyText($('key-new').value, $('key-new'));
    $('key-copy').textContent = copied ? 'Copied' : 'Press Ctrl+C';
    if (!copied) { $('key-new').focus(); $('key-new').select(); }   // leave it selected for a manual copy
    setTimeout(() => { $('key-copy').textContent = 'Copy'; }, 2500);
  });
  $('key-download').addEventListener('click', () => {
    const name = ($('key-new').dataset.name || 'api-key').replace(/[^A-Za-z0-9._-]+/g, '-');
    const url = URL.createObjectURL(new Blob([`${$('key-new').value}\n`], { type: 'text/plain' }));
    const link = el('a', { href: url, download: `listsweep-${name}.txt` });
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  });
  $('key-done').addEventListener('click', () => hideReveal());
  window.addEventListener('beforeunload', (event) => {
    if (!$('key-reveal').hidden || !$('user-reveal').hidden) { event.preventDefault(); event.returnValue = ''; }   // the key cannot be shown again
  });
  window.addEventListener('hashchange', route);
  document.addEventListener('click', (event) => { if (!event.target.closest('#dl-menu')) $('dl-menu').open = false; });
  document.addEventListener('keydown', (event) => {
    if (!(event.metaKey || event.ctrlKey) || event.altKey) return;
    const tab = { 1: 'verify', 2: 'jobs', 3: 'api' }[event.key];
    if (tab) { event.preventDefault(); go(`#/${tab}`); }
    else if (event.key.toLowerCase() === 'k') { event.preventDefault(); go('#/verify'); setTimeout(() => $('single-email').focus(), 80); }
  });
  buildApiTab();
  startup();
}

init();
