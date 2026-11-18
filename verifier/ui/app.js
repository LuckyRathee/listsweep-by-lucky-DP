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
  ['POST /v1/jobs/{id}/recheck', "JSON {scope: 'unknown' | 'all'}; start a new job from a finished one"],
];

const state = {
  config: null, needKey: false, source: 'file', file: null,
  job: null, jobId: null, rows: [], leads: [], filter: 'all', query: '', group: null,
  sort: { key: 'status', dir: 'asc' }, page: 0, expanded: new Set(), pollTimer: null,
};

/* ---------- helpers ---------- */
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
const getKey = () => { try { return sessionStorage.getItem('ev_key') || ''; } catch { return ''; } };
const setKey = (value) => { try { value ? sessionStorage.setItem('ev_key', value) : sessionStorage.removeItem('ev_key'); } catch { /* no storage */ } };

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
  const key = getKey();
  if (key) headers['X-API-Key'] = key;
  let response;
  try {
    response = await fetch(path, { ...options, headers });
  } catch {
    throw new ApiError(0, 'Could not reach the server');
  }
  if (!response.ok) {
    let detail = response.statusText;
    try { const body = await response.json(); detail = typeof body.detail === 'string' ? body.detail : detail; } catch { /* not json */ }
    if (response.status === 401) detail = 'The API key is missing or wrong';
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

function updateKeyUI() {
  $('key-form').hidden = !state.needKey;
  $('forget-key').hidden = !getKey();
}

async function loadConfig() {
  setConnected('pending', 'Connecting…');
  try {
    state.config = await getJson('/v1/config');
    state.needKey = state.config.auth_required;
    setConnected('ok', state.config.auth_required ? 'Connected' : 'Connected, no key needed');
    $('opt-conc').max = state.config.concurrency_range[1];
    refreshDepths();
    updateKeyUI();
    notice('');
    return true;
  } catch (error) {
    state.config = null;
    if (error.status === 401) {
      state.needKey = true;
      setConnected('bad', 'Key needed');
      notice(getKey() ? 'That API key was not accepted.' : 'Enter your API key above and press Connect.', 'info');
    } else {
      setConnected('bad', 'Server unreachable');
      notice(error.message);
    }
    updateKeyUI();
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
  const mailboxOk = Boolean(state.config && state.config.smtp_available);
  for (const [container, hint] of [[$('single-depth'), $('single-hint')], [$('bulk-depth'), $('bulk-hint')]]) {
    const mailbox = container.querySelector('[data-depth=mailbox]');
    mailbox.disabled = !mailboxOk;
    mailbox.title = mailboxOk ? '' : 'Mailbox checks are switched off on this server';
    const current = container.dataset.value;
    // Until the user picks one, default to the most thorough check this server offers.
    if (!container.dataset.touched || (current === 'mailbox' && !mailboxOk)) {
      container.choose(mailboxOk ? 'mailbox' : 'domain');
    }
    if (!mailboxOk) {
      hint.textContent = `${DEPTHS.find((d) => d[0] === container.dataset.value)[2]} Mailbox checks are switched off on this server.`;
    }
  }
}
const depthOptions = (container) => {
  const depth = container.dataset.value || 'syntax';
  return { check_dns: depth !== 'syntax', allow_smtp: depth === 'mailbox' };
};

/* ---------- single address ---------- */
function refreshSingle() { $('single-go').disabled = !state.config || !$('single-email').value.trim(); }
function fact(label, value) { return [el('dt', { text: label }), el('dd', { text: value })]; }

function renderSingle(r) {
  const [headline, caveat] = VERDICT[r.status] || [r.status, ''];
  $('sr-badge').className = `badge big ${r.status}`;
  $('sr-badge').textContent = headline;
  $('sr-text').textContent = `${REASONS[r.reason] || r.reason}.`;
  $('sr-caveat').textContent = caveat;
  const yesNo = (value) => (value ? 'Yes' : 'No');
  const mailbox = r.smtp_outcome === 'answered'
    ? (r.smtp_code ? `Checked (the server replied ${r.smtp_code})` : 'Checked')
    : (SMTP_OUTCOME[r.smtp_outcome] || 'Not checked');
  const dns = r.dns_checked
    ? (r.mx_hosts.length ? `Checked: ${r.mx_hosts.slice(0, 3).join(', ')}` : 'Checked')
    : (r.syntax_valid ? 'Not checked' : 'Not reached');
  $('sr-facts').replaceChildren(
    ...fact('Address', r.email),
    ...fact('Syntax', r.syntax_valid ? 'Valid' : 'Not a valid email address'),
    ...(r.domain ? fact('Domain', r.domain) : []),
    ...fact('Mail records (DNS)', dns),
    ...fact('Mailbox (SMTP)', mailbox),
    ...fact('Role address', yesNo(r.role_based)),
    ...fact('Disposable domain', yesNo(r.disposable)),
    ...fact('Catch-all domain', yesNo(r.catch_all)),
    ...fact('Send-eligible', yesNo(r.send_eligible)),
  );
  $('single-result').hidden = false;
}

async function verifyOne(event) {
  event.preventDefault();
  const email = $('single-email').value.trim();
  if (!email) return;
  notice('');
  $('single-result').hidden = true;   // never show a previous address's result for a new check
  $('single-go').disabled = true;
  $('single-go').textContent = 'Checking…';
  try {
    renderSingle(await postJson('/v1/verify/email', { email, ...depthOptions($('single-depth')) }));
  } catch (error) {
    notice(error.message);
  } finally {
    $('single-go').textContent = 'Verify';
    refreshSingle();
  }
}

/* ---------- routing and tabs ---------- */
const TABS = ['verify', 'jobs', 'api'];

function showTab(name) {
  for (const tab of TABS) {
    const selected = tab === name;
    $(`tab-${tab}`).setAttribute('aria-selected', String(selected));
    $(`tab-${tab}`).tabIndex = selected ? 0 : -1;
    $(`panel-${tab}`).hidden = !selected;
  }
  if (name === 'jobs') loadJobs();
}

function route() {
  const hash = location.hash;
  const job = hash.match(/^#\/job\/([A-Za-z0-9-]{8,64})$/);
  if (job) {
    showTab('verify');
    if (job[1] !== state.jobId || $('results').hidden) openJob(job[1]);
    return;
  }
  const tab = hash.match(/^#\/(verify|jobs|api)$/);
  showTab(tab ? tab[1] : 'verify');
}
const go = (hash) => { if (location.hash === hash) route(); else location.hash = hash; };
const quietHash = (hash) => history.replaceState(null, '', hash);

async function openJob(jobId) {
  clearTimeout(state.pollTimer);
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
function refreshStart() { $('start').disabled = !state.config || !startReady(); }

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
  state.jobId = job.job_id;
  state.job = null;
  quietHash(`#/job/${job.job_id}`);
  $('results').hidden = true;
  watchJob(job.job_id);
}

async function startJob() {
  notice('');
  $('start').disabled = true;
  const options = depthOptions($('bulk-depth'));
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
    refreshStart();
  }
}

function watchJob(jobId, delay = 1000, failures = 0) {
  clearTimeout(state.pollTimer);
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
        notice(`${error.message}. Open the job again from the Jobs tab to resume watching it.`);
      }
    }
  };
  state.pollTimer = setTimeout(tick, delay);
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
      { scope, ...depthOptions($('bulk-depth')) }));
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

const tile = (value, label) => el('div', { class: 'tile' }, [el('b', { text: String(value) }), el('span', { text: label })]);
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
  renderTiles();
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

function renderTiles() {
  $('tiles').replaceChildren(
    tile(state.rows.length, 'Addresses'),
    tile(count('deliverable_candidate'), 'Candidates'),
    tile(count('unknown'), 'Unknown'),
    tile(count('undeliverable'), 'Undeliverable'),
    tile(state.rows.filter((r) => r.send_eligible).length, 'Send-eligible'),
    tile((state.job && state.job.duplicate_count) || 0, 'Duplicates removed'),
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
  if (key === 'status') return STATUS_RANK[r.status] ?? 9;
  if (key === 'reason') return shortReason(r).toLowerCase();
  if (key === 'send') return r.send_eligible ? 0 : 1;
  if (key.startsWith('lead:')) return String((r.metadata || {})[key.slice(5)] || '').toLowerCase();
  return 0;
}

function columns() {
  return [{ key: 'email', label: 'Email' }, ...state.leads.map((name) => ({ key: `lead:${name}`, label: name })),
    { key: 'status', label: 'Status' }, { key: 'reason', label: 'Reason' }, { key: 'send', label: 'Send' }];
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
      el('td', {}, el('span', { class: `badge ${r.status}`, text: STATUS_LABEL[r.status] || r.status })),
      el('td', { text: shortReason(r) }),
      el('td', { text: r.send_eligible ? 'Yes' : 'No' }),
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

/* ---------- API tab ---------- */
function buildApiTab() {
  const base = location.origin;
  $('base-url').textContent = base;
  const examples = [
    ['Check one address', `curl -H "X-API-Key: YOUR_KEY" -H "Content-Type: application/json" \\\n  -d '{"email": "name@company.com", "check_dns": true, "allow_smtp": true}' \\\n  ${base}/v1/verify/email`],
    ['Check a CSV file', `curl -H "X-API-Key: YOUR_KEY" \\\n  -F file=@leads.csv -F check_dns=true \\\n  ${base}/v1/verify/csv`],
    ['Check an Excel file', `curl -H "X-API-Key: YOUR_KEY" \\\n  -F file=@leads.xlsx -F check_dns=true \\\n  ${base}/v1/verify/xlsx`],
    ['Check a public Google Sheet', `curl -H "X-API-Key: YOUR_KEY" -H "Content-Type: application/json" \\\n  -d '{"url": "https://docs.google.com/spreadsheets/d/SHEET_ID/edit", "check_dns": true}' \\\n  ${base}/v1/verify/sheet`],
    ['Poll, download, cancel or re-check a job', `curl -H "X-API-Key: YOUR_KEY" ${base}/v1/jobs/JOB_ID\ncurl -H "X-API-Key: YOUR_KEY" "${base}/v1/jobs/JOB_ID/results.csv?send_only=true" -OJ\ncurl -X POST -H "X-API-Key: YOUR_KEY" ${base}/v1/jobs/JOB_ID/cancel\ncurl -X POST -H "X-API-Key: YOUR_KEY" -H "Content-Type: application/json" \\\n  -d '{"scope": "unknown"}' ${base}/v1/jobs/JOB_ID/recheck`],
    ['Python', `import time, requests\n\nBASE, HEADERS = "${base}", {"X-API-Key": "YOUR_KEY"}\n\nwith open("leads.csv", "rb") as f:\n    job = requests.post(f"{BASE}/v1/verify/csv", headers=HEADERS,\n                        files={"file": f}, data={"check_dns": "true"}).json()\n\nwhile True:\n    status = requests.get(f"{BASE}/v1/jobs/{job['job_id']}", headers=HEADERS).json()\n    if status["status"] in ("completed", "failed", "cancelled"):\n        break\n    time.sleep(2)\n\nrows = requests.get(f"{BASE}/v1/jobs/{job['job_id']}/results", headers=HEADERS).json()`],
  ];
  $('examples').replaceChildren(...examples.map(([title, code]) => {
    const copy = el('button', { type: 'button', class: 'btn small', text: 'Copy' });
    copy.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(code); copy.textContent = 'Copied'; } catch { copy.textContent = 'Copy failed'; }
      setTimeout(() => { copy.textContent = 'Copy'; }, 1500);
    });
    return el('div', { class: 'example' }, [
      el('div', { class: 'toolbar' }, [el('h3', { text: title }), el('span', { class: 'grow' }), copy]),
      el('pre', { text: code }),
    ]);
  }));
  $('route-rows').replaceChildren(...ROUTES.map(([route, purpose]) =>
    el('tr', {}, [el('td', {}, el('code', { text: route })), el('td', { text: purpose })])));
}

/* ---------- wiring ---------- */
function initTabs() {
  for (const name of TABS) $(`tab-${name}`).addEventListener('click', () => go(`#/${name}`));
  $('tab-verify').parentElement.addEventListener('keydown', (event) => {
    const at = TABS.findIndex((name) => $(`tab-${name}`) === document.activeElement);
    if (at < 0) return;
    const next = { ArrowRight: (at + 1) % TABS.length, ArrowLeft: (at + TABS.length - 1) % TABS.length, Home: 0, End: TABS.length - 1 }[event.key];
    if (next === undefined) return;
    event.preventDefault();
    $(`tab-${TABS[next]}`).focus();
    go(`#/${TABS[next]}`);
  });
}

function init() {
  initTabs();
  buildDepth($('single-depth'), $('single-hint'));
  buildDepth($('bulk-depth'), $('bulk-hint'));
  $('single-depth').choose('domain');
  $('bulk-depth').choose('domain');
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
  $('single-email').addEventListener('input', () => { $('single-result').hidden = true; refreshSingle(); });
  $('search').addEventListener('input', (event) => { state.query = event.target.value; state.page = 0; renderTable(); });
  $('prev').addEventListener('click', () => { state.page -= 1; renderTable(); });
  $('next').addEventListener('click', () => { state.page += 1; renderTable(); });
  $('dl-all').addEventListener('click', () => download(false));
  $('dl-send').addEventListener('click', () => download(true));
  $('recheck-unknown').addEventListener('click', () => recheck('unknown'));
  $('recheck-stale').addEventListener('click', () => recheck('all'));
  $('refresh-jobs').addEventListener('click', loadJobs);
  $('key-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const value = $('api-key').value.trim();
    if (value) setKey(value);
    $('api-key').value = '';
    await loadConfig();
    route();
  });
  $('forget-key').addEventListener('click', async () => {
    setKey('');
    $('results').hidden = true;
    $('single-result').hidden = true;
    await loadConfig();
  });
  window.addEventListener('hashchange', route);
  buildApiTab();
  loadConfig().then(route);
}

init();
