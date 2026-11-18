'use strict';
// All user-supplied text (CSV cells, filenames, errors) is rendered with textContent only.

const $ = (id) => document.getElementById(id);
const PAGE_SIZE = 50;
const REASONS = {
  smtp_rcpt_accepted: 'Server accepted this address and rejected a random control address',
  smtp_mailbox_rejected: 'Server rejected the mailbox',
  smtp_temporary_failure: 'Temporary failure (greylisting or busy server)',
  smtp_ambiguous_response: 'Server gave an unclear answer that does not say whether the mailbox exists',
  smtp_control_inconclusive: 'Mailbox check was inconclusive',
  smtp_non_public_target: 'Mail server is not on the public internet',
  smtp_unreachable: 'Mail server could not be reached',
  smtp_unresolved: 'Mail server name did not resolve',
  smtp_connection_dropped: 'The mail server hung up before the control check finished',
  smtp_blocked_by_policy: "The recipient's mail server refused this machine (for example a spam blocklist), so the mailbox was not checked",
  catch_all_domain: 'Domain accepts every address, so this one is unproven',
  null_mx: 'Domain publishes a null MX and accepts no mail',
  domain_not_found: 'Domain does not exist',
  disposable_domain: 'Disposable email domain',
  invalid_syntax: 'Not a valid email address',
  suppressed_prior_bounce: 'Hard-bounced before',
  mailbox_check_disabled: 'Domain can receive mail; mailbox not checked',
  verification_disabled: 'Syntax only; no network checks were run',
  verification_error: 'Check failed; result unknown',
  dns_timeout: 'DNS lookup timed out',
  dns_no_nameservers: 'No DNS server answered',
  dns_failure: 'DNS lookup failed',
  no_mail_host: 'Domain exists but has no mail server or address record',
  queued: 'Waiting to be checked',
};
const STATUS_LABEL = {
  deliverable_candidate: 'Candidate', unknown: 'Unknown', undeliverable: 'Undeliverable',
};
const ROUTES = [
  ['GET /healthz', 'Liveness, no key needed'],
  ['GET /v1/config', 'Limits and which checks are available'],
  ['POST /v1/verify/email', 'JSON {email, check_dns, allow_smtp}; one address, answered immediately'],
  ['POST /v1/verify/csv', 'Upload a CSV (multipart: file, check_dns, allow_smtp, concurrency)'],
  ['POST /v1/verify/xlsx', 'Upload an Excel .xlsx (same fields)'],
  ['POST /v1/verify/sheet', 'JSON {url, check_dns, allow_smtp, concurrency} for a public Google Sheet'],
  ['GET /v1/jobs', 'Recent jobs'],
  ['GET /v1/jobs/{id}', 'Job status and counts'],
  ['GET /v1/jobs/{id}/results', 'Results as JSON (send_only, offset, limit)'],
  ['GET /v1/jobs/{id}/results.csv', 'Results as CSV (send_only)'],
];

const state = {
  config: null, source: 'file', file: null, jobId: null, rows: [], filter: 'all', query: '',
  page: 0, pollTimer: null,
};

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

function getKey() {
  try { return sessionStorage.getItem('ev_key') || ''; } catch { return ''; }
}
function setKey(value) {
  try { value ? sessionStorage.setItem('ev_key', value) : sessionStorage.removeItem('ev_key'); } catch { /* no storage */ }
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

function setConnected(ok, text) {
  const pill = $('conn');
  pill.textContent = text;
  pill.className = ok ? 'pill ok' : 'pill';
  $('start').disabled = !ok || !startReady();
  refreshSingle();
}

async function loadConfig() {
  try {
    state.config = await getJson('/v1/config');
    setConnected(true, state.config.auth_required ? 'Connected' : 'Connected (no key required)');
    $('smtp-row').hidden = !state.config.smtp_available;
    $('single-smtp-row').hidden = !state.config.smtp_available;
    $('smtp-off').hidden = state.config.smtp_available;
    $('opt-conc').max = state.config.concurrency_range[1];
    notice('');
    return true;
  } catch (error) {
    state.config = null;
    setConnected(false, error.status === 401 ? 'Key needed' : 'Not connected');
    if (error.status === 401) {
      notice(getKey() ? 'That API key was not accepted.' : 'Enter your API key above and press Connect.', 'info');
    } else {
      notice(error.message);
    }
    return false;
  }
}

/* ---------- single address ---------- */
const VERDICT = {
  deliverable_candidate: ['Deliverable candidate', 'The server accepted this exact address. That is evidence, not a guarantee that mail will arrive.'],
  unknown: ['Unknown', 'Not enough evidence to call this address valid or invalid. Unknown is never treated as valid.'],
  undeliverable: ['Undeliverable', 'This address is very unlikely to receive mail.'],
};

function refreshSingle() {
  $('single-go').disabled = !state.config || !$('single-email').value.trim();
}

function fact(label, value) { return [el('dt', { text: label }), el('dd', { text: value })]; }

function renderSingle(r) {
  const [headline, meaning] = VERDICT[r.status] || [r.status, ''];
  $('sr-badge').className = `badge big ${r.status}`;
  $('sr-badge').textContent = headline;
  $('sr-text').textContent = `${REASONS[r.reason] || r.reason}. ${meaning}`;
  const yesNo = (value) => (value ? 'Yes' : 'No');
  const rows = [
    ...fact('Address', r.email),
    ...fact('Syntax', r.syntax_valid ? 'Valid' : 'Not a valid email address'),
    ...(r.domain ? fact('Domain', r.domain) : []),
    ...fact('Mail records (DNS)', r.dns_checked ? (r.mx_hosts.length ? `Checked: ${r.mx_hosts.slice(0, 3).join(', ')}` : 'Checked') : 'Not checked'),
    ...fact('Mailbox (SMTP)', r.smtp_checked ? (r.smtp_code ? `Checked (server replied ${r.smtp_code})` : 'Checked') : 'Not checked'),
    ...fact('Role address', yesNo(r.role_based)),
    ...fact('Disposable domain', yesNo(r.disposable)),
    ...fact('Catch-all domain', yesNo(r.catch_all)),
    ...fact('Send-eligible', yesNo(r.send_eligible)),
  ];
  $('sr-facts').replaceChildren(...rows);
  $('single-result').hidden = false;
}

async function verifyOne(event) {
  event.preventDefault();
  const email = $('single-email').value.trim();
  if (!email) return;
  notice('');
  $('single-go').disabled = true;
  $('single-go').textContent = 'Checking…';
  try {
    const result = await getJson('/v1/verify/email', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        email, check_dns: $('single-dns').checked,
        allow_smtp: !$('single-smtp-row').hidden && $('single-smtp').checked,
      }),
    });
    renderSingle(result);
  } catch (error) {
    $('single-result').hidden = true;
    notice(error.message);
  } finally {
    $('single-go').textContent = 'Verify';
    refreshSingle();
  }
}

/* ---------- tabs ---------- */
function showTab(name) {
  for (const tab of ['verify', 'jobs', 'api']) {
    $(`tab-${tab}`).setAttribute('aria-selected', String(tab === name));
    $(`panel-${tab}`).hidden = tab !== name;
  }
  if (name === 'jobs') loadJobs();
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

function startReady() {
  if (state.source === 'file') return Boolean(state.file);
  return $('sheet-url').value.trim().length > 0;
}
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

/* ---------- start a job ---------- */
async function startJob() {
  notice('');
  $('start').disabled = true;
  const checkDns = $('opt-dns').checked;
  const allowSmtp = !$('smtp-row').hidden && $('opt-smtp').checked;
  const concurrency = Math.max(1, Math.min(25, parseInt($('opt-conc').value, 10) || 4));
  try {
    let job;
    if (state.source === 'file') {
      const form = new FormData();
      form.append('file', state.file);
      form.append('check_dns', String(checkDns));
      form.append('allow_smtp', String(allowSmtp));
      form.append('concurrency', String(concurrency));
      const route = state.file.name.toLowerCase().endsWith('.xlsx') ? '/v1/verify/xlsx' : '/v1/verify/csv';
      job = await getJson(route, { method: 'POST', body: form });
    } else {
      job = await getJson('/v1/verify/sheet', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: $('sheet-url').value.trim(), check_dns: checkDns, allow_smtp: allowSmtp, concurrency }),
      });
    }
    state.jobId = job.job_id;
    $('results').hidden = true;
    watchJob(job.job_id);
  } catch (error) {
    notice(error.message);
  } finally {
    refreshStart();
  }
}

function watchJob(jobId, delay = 1000) {
  clearTimeout(state.pollTimer);
  $('progress-card').hidden = false;
  const tick = async () => {
    try {
      const job = await getJson(`/v1/jobs/${encodeURIComponent(jobId)}`);
      if (jobId !== state.jobId) return;
      $('progress-title').textContent = job.status === 'completed' ? 'Finished' : 'Verifying';
      $('progress').max = Math.max(job.total, 1);
      $('progress').value = job.processed;
      $('progress-text').textContent = `${job.processed} of ${job.total} checked`;
      if (job.status === 'completed' || job.status === 'failed') {
        if (job.status === 'failed') notice(`Job failed: ${job.error || 'unknown error'}`);
        await showResults(job);
        return;
      }
      state.pollTimer = setTimeout(tick, 1500);
    } catch (error) {
      if (error.status === 429) {
        state.pollTimer = setTimeout(tick, Math.max(error.retryAfter, 5) * 1000);
      } else {
        notice(error.message);
      }
    }
  };
  state.pollTimer = setTimeout(tick, delay);
}

/* ---------- results ---------- */
async function showResults(job) {
  const rows = [];
  for (let offset = 0; offset < 4000; offset += 1000) {
    const batch = await getJson(`/v1/jobs/${encodeURIComponent(job.job_id || job.id)}/results?offset=${offset}&limit=1000`);
    rows.push(...batch);
    if (batch.length < 1000) break;
  }
  state.rows = rows;
  state.filter = 'all';
  state.query = '';
  state.page = 0;
  $('search').value = '';
  $('progress-card').hidden = true;
  $('results').hidden = false;
  renderTiles(job, rows);
  renderChips();
  renderTable();
  $('results').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function tile(value, label) { return el('div', { class: 'tile' }, [el('b', { text: String(value) }), el('span', { text: label })]); }

function renderTiles(job, rows) {
  const count = (status) => rows.filter((r) => r.status === status).length;
  const tiles = $('tiles');
  tiles.replaceChildren(
    tile(rows.length, 'Addresses'),
    tile(count('deliverable_candidate'), 'Candidates'),
    tile(count('unknown'), 'Unknown'),
    tile(count('undeliverable'), 'Undeliverable'),
    tile(rows.filter((r) => r.send_eligible).length, 'Send-eligible'),
    tile(job.duplicate_count || 0, 'Duplicates removed'),
  );
}

function renderChips() {
  const chips = $('chips');
  const options = [['all', 'All'], ['deliverable_candidate', 'Candidates'], ['unknown', 'Unknown'], ['undeliverable', 'Undeliverable']];
  chips.replaceChildren(...options.map(([value, label]) => {
    const chip = el('button', { type: 'button', class: 'chip', 'aria-pressed': String(state.filter === value), text: label });
    chip.addEventListener('click', () => { state.filter = value; state.page = 0; renderChips(); renderTable(); });
    return chip;
  }));
}

function filtered() {
  const query = state.query.toLowerCase();
  return state.rows.filter((r) => (state.filter === 'all' || r.status === state.filter)
    && (!query || `${r.email} ${r.original_email} ${r.reason}`.toLowerCase().includes(query)));
}

function renderTable() {
  const rows = filtered();
  const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  state.page = Math.min(state.page, pages - 1);
  const slice = rows.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE);
  $('rows').replaceChildren(...slice.map((r) => {
    const flags = [...(r.flags || [])];
    return el('tr', {}, [
      el('td', { class: 'email', text: r.email }),
      el('td', {}, el('span', { class: `badge ${r.status}`, text: STATUS_LABEL[r.status] || r.status })),
      el('td', { text: REASONS[r.reason] || r.reason }),
      el('td', { text: flags.length ? flags.join(', ').replaceAll('_', ' ') : '' }),
      el('td', { text: r.send_eligible ? 'Yes' : 'No' }),
    ]);
  }));
  $('page-info').textContent = rows.length ? `Page ${state.page + 1} of ${pages} (${rows.length} shown)` : 'No matching addresses';
  $('prev').disabled = state.page === 0;
  $('next').disabled = state.page >= pages - 1;
}

async function download(sendOnly) {
  if (!state.jobId) return;
  try {
    const response = await api(`/v1/jobs/${encodeURIComponent(state.jobId)}/results.csv?send_only=${sendOnly}`);
    const url = URL.createObjectURL(await response.blob());
    const link = el('a', { href: url, download: sendOnly ? 'send-eligible.csv' : 'verification-results.csv' });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  } catch (error) {
    notice(error.message);
  }
}

/* ---------- jobs tab ---------- */
async function loadJobs() {
  try {
    const jobs = await getJson('/v1/jobs?limit=50');
    $('no-jobs').hidden = jobs.length > 0;
    $('job-rows').replaceChildren(...jobs.map((job) => {
      const counts = job.counts || {};
      const summary = `${counts.deliverable_candidate || 0} candidate, ${counts.unknown || 0} unknown, ${counts.undeliverable || 0} undeliverable`;
      const open = el('button', { type: 'button', class: 'btn small', text: 'Open' });
      open.addEventListener('click', async () => {
        state.jobId = job.id;
        showTab('verify');
        if (job.status === 'completed') {
          try { await showResults(job); } catch (error) { notice(error.message); }
        } else {
          $('results').hidden = true;
          watchJob(job.id, 0);
        }
      });
      return el('tr', {}, [
        el('td', { text: new Date(job.created_at).toLocaleString() }),
        el('td', { text: (job.options && job.options.filename) || 'pasted list' }),
        el('td', {}, el('span', { class: `badge ${job.status}`, text: job.status })),
        el('td', { text: String(job.total) }),
        el('td', { text: summary }),
        el('td', {}, open),
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
    ['Check a CSV file', `curl -H "X-API-Key: YOUR_KEY" \\\n  -F file=@leads.csv -F check_dns=true \\\n  ${base}/v1/verify/csv`],
    ['Check an Excel file', `curl -H "X-API-Key: YOUR_KEY" \\\n  -F file=@leads.xlsx -F check_dns=true \\\n  ${base}/v1/verify/xlsx`],
    ['Check a public Google Sheet', `curl -H "X-API-Key: YOUR_KEY" -H "Content-Type: application/json" \\\n  -d '{"url": "https://docs.google.com/spreadsheets/d/SHEET_ID/edit", "check_dns": true}' \\\n  ${base}/v1/verify/sheet`],
    ['Poll the job and download results', `curl -H "X-API-Key: YOUR_KEY" ${base}/v1/jobs/JOB_ID\ncurl -H "X-API-Key: YOUR_KEY" "${base}/v1/jobs/JOB_ID/results.csv?send_only=true" -o send-eligible.csv`],
    ['Python', `import time, requests\n\nBASE, HEADERS = "${base}", {"X-API-Key": "YOUR_KEY"}\n\nwith open("leads.csv", "rb") as f:\n    job = requests.post(f"{BASE}/v1/verify/csv", headers=HEADERS,\n                        files={"file": f}, data={"check_dns": "true"}).json()\n\nwhile True:\n    status = requests.get(f"{BASE}/v1/jobs/{job['job_id']}", headers=HEADERS).json()\n    if status["status"] in ("completed", "failed"):\n        break\n    time.sleep(2)\n\nrows = requests.get(f"{BASE}/v1/jobs/{job['job_id']}/results", headers=HEADERS).json()`],
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
function init() {
  for (const name of ['verify', 'jobs', 'api']) $(`tab-${name}`).addEventListener('click', () => showTab(name));
  $('src-file').addEventListener('click', () => setSource('file'));
  $('src-sheet').addEventListener('click', () => setSource('sheet'));
  $('file').addEventListener('change', (event) => chooseFile(event.target.files[0]));
  const drop = $('drop');
  for (const type of ['dragenter', 'dragover']) drop.addEventListener(type, (event) => { event.preventDefault(); drop.classList.add('over'); });
  for (const type of ['dragleave', 'drop']) drop.addEventListener(type, (event) => { event.preventDefault(); drop.classList.remove('over'); });
  drop.addEventListener('drop', (event) => chooseFile(event.dataTransfer.files[0]));
  $('sheet-url').addEventListener('input', refreshStart);
  $('single-form').addEventListener('submit', verifyOne);
  $('single-email').addEventListener('input', refreshSingle);
  $('start').addEventListener('click', startJob);
  $('search').addEventListener('input', (event) => { state.query = event.target.value; state.page = 0; renderTable(); });
  $('prev').addEventListener('click', () => { state.page -= 1; renderTable(); });
  $('next').addEventListener('click', () => { state.page += 1; renderTable(); });
  $('dl-all').addEventListener('click', () => download(false));
  $('dl-send').addEventListener('click', () => download(true));
  $('refresh-jobs').addEventListener('click', loadJobs);
  $('key-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    setKey($('api-key').value.trim());
    $('api-key').value = '';
    await loadConfig();
  });
  buildApiTab();
  loadConfig();
}

init();
