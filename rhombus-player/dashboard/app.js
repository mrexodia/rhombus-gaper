'use strict';

const $ = id => document.getElementById(id);
let token = new URLSearchParams(location.hash.slice(1)).get('token') || sessionStorage.getItem('signal-token') || '';
if (location.hash) history.replaceState(null, '', location.pathname);
let snapshot = null;
let paused = false;
let filter = 'all';
let limit = 100;
let busy = false;
let uplink = false;
let revision = 0;
let controlling = false;
let showWorkerHistory = false;
let httpFilter = 'all';
let httpLimit = 100;
let selectedRequest = null;

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text ?? '');
  return element;
}
const timeText = value => value ? new Date(value * 1000).toISOString().slice(11, 19) : '—';
const display = value => String(value ?? '').replaceAll('_', ' ');
const knownStatuses = new Set(['ready', 'solved', 'racing', 'correct', 'incorrect', 'in_flight', 'queued', 'active', 'error', 'unknown', 'needs_review', 'cooldown', 'stopped', 'cancelled', 'acknowledged', 'completed', 'rejected', 'blocked']);
function badge(status) { return node('span', 'badge ' + (knownStatuses.has(status) ? status : ''), display(status)); }
function text(id, value) { $(id).textContent = value; }
function empty(parent, message) { parent.append(node('div', 'empty', message)); }
function auth(message = '') {
  $('auth').hidden = false;
  $('main').hidden = true;
  text('auth-message', message);
}
function connected() {
  $('auth').hidden = true;
  $('main').hidden = false;
}
function redactCached() {
  if (!snapshot) return;
  const flags = snapshot.submissions.map(s => s.flag).filter(f => f && f !== '••••••••');
  function clean(value, key) {
    if (key === 'flag') return '••••••••';
    if (Array.isArray(value)) return value.map(v => clean(v));
    if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, clean(v, k)]));
    if (typeof value === 'string' && ['message', 'error', 'persistence_warning'].includes(key)) {
      for (const flag of flags) value = value.replaceAll(flag, '[redacted]');
    }
    return value;
  }
  snapshot = clean(snapshot);
}
function stopButton(label, payload, explanation) {
  const button = node('button', 'danger compact', label);
  button.type = 'button';
  button.disabled = !snapshot?.can_control || !uplink || paused || controlling;
  button.addEventListener('click', () => sendControl(payload, explanation));
  return button;
}
async function sendControl(payload, explanation) {
  if (!snapshot?.can_control || !uplink || paused || controlling) return;
  if (!confirm(`${explanation}\n\nAlready-sent flag requests may still succeed. Downloads continue. This cannot be undone or restarted from the dashboard. Continue?`)) return;
  controlling = true;
  render();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5000);
  $('control-notice').hidden = false;
  text('control-notice', 'Persisting operator stop request…');
  try {
    const response = await fetch('/api/control', {method: 'POST', headers: {
      Authorization: `Bearer ${token}`, 'Content-Type': 'application/json',
    }, body: JSON.stringify(payload), signal: controller.signal});
    if (!response.ok) throw new Error(await response.text());
    const result = await response.json();
    text('control-notice', `${result.control_id.slice(0, 8)} / ${result.message}`);
  } catch (error) {
    text('control-notice', `Stop NOT confirmed: ${error.message}. Check the acknowledgements below before trying again.`);
  } finally {
    clearTimeout(timer);
    controlling = false;
    render();
    poll();
  }
}
function render() {
  if (!snapshot) return;
  const data = snapshot;
  const query = $('search').value.toLowerCase().trim();
  const match = value => !query || String(value ?? '').toLowerCase().includes(query);
  const names = new Map(data.challenges.map(c => [c.id, c.name]));
  const matchesChallenge = key => match(key) || match(names.get(key));
  const solved = data.challenges.filter(c => c.status === 'solved').length;
  const counts = data.counts;
  text('run-label', `${data.run_name} / ${data.source}`);
  text('connection', paused ? 'FEED PAUSED' : !uplink ? 'UPLINK LOST / RETRYING' : `${data.run_status.toUpperCase()} / UPLINK CONNECTED`);
  $('connection').className = 'connection' + (!uplink ? ' offline' : paused || data.run_status === 'stale' ? ' paused' : '');
  text('updated', `Snapshot ${timeText(data.generated_at)} UTC · ${data.can_control ? 'stop controls available' : 'controls unavailable'}`);
  $('stop-all').disabled = !data.can_control || !uplink || paused || controlling;
  text('m-objectives', `${solved} / ${data.challenges.length}`);
  text('m-objectives-sub', 'Confirmed platform solves');
  $('completion').max = Math.max(1, data.challenges.length);
  $('completion').value = solved;
  text('m-workers', data.workers.filter(w => w.status === 'active').length);
  text('m-workers-sub', !uplink || paused ? 'Last observed—feed not updating' : data.workers.some(w => w.status === 'unconfirmed') ? 'Stale worker records—not confirmed alive' : 'Competing subprocesses');
  text('m-queue', (counts.queued || 0) + (counts.in_flight || 0));
  text('m-queue-sub', `${counts.queued || 0} waiting · ${counts.in_flight || 0} in flight`);
  text('m-errors', (counts.error || 0) + (counts.unknown || 0));
  text('m-files', `${data.challenges.reduce((n, c) => n + c.downloaded, 0)} / ${data.challenges.reduce((n, c) => n + c.files, 0)}`);
  text('m-rates', data.rate_limits);
  text('m-rates-sub', data.cooldown > 0 ? `Cooldown ${data.cooldown.toFixed(1)}s` : 'Server limits respected');
  const notices = [...data.warnings];
  if (!uplink) notices.unshift('Dashboard disconnected. Showing the last received snapshot; process states may be stale.');
  if (data.run_status === 'stale') notices.unshift('Heartbeat missing or older than 5 seconds. Process liveness and pending outcomes are unconfirmed.');
  if (data.run_status === 'waiting') notices.unshift('Waiting for run.json. You can start the dashboard before the orchestrator.');
  if ($('reveal').checked) notices.unshift('SENSITIVE VIEW — candidate flags are visible. Do not share this screen or access link.');
  $('notice').hidden = !notices.length;
  text('notice', notices.join('\n'));

  text('challenge-total', data.challenges.length);
  const grid = $('challenges');
  grid.replaceChildren();
  const challenges = data.challenges.filter(c => match(c.id) || match(c.name) || match(c.category));
  for (const c of challenges) {
    const card = node('article', 'challenge ' + (knownStatuses.has(c.status) ? c.status : ''));
    const top = node('div', 'card-top');
    top.append(node('span', 'category', c.category), badge(c.status));
    const title = node('h3', '', c.name);
    title.title = c.description || c.id;
    const strip = node('div', 'race-strip', c.winner ? `✓ ${c.winner} won the race` : c.workers.length ? `↗ ${c.workers.map(w => w.worker).join(' / ')}` : c.message || '— No active race');
    strip.title = strip.textContent;
    const bottom = node('div', 'card-bottom');
    bottom.append(node('span', '', `${c.submissions} candidates`), node('span', '', `${c.downloaded}/${c.files} files`));
    card.append(top, title, node('div', 'key', c.id), strip, bottom);
    if (!['solved', 'stopped', 'unsolved', 'needs_review', 'error', 'interrupted'].includes(c.status)) {
      card.append(stopButton('Stop race', {action: 'stop_challenge', challenge_key: c.id}, `Stop all current and queued workers for ${c.name}?`));
    }
    grid.append(card);
  }
  if (!challenges.length) empty(grid, data.challenges.length ? 'No objectives match this filter.' : 'Awaiting challenge inventory…');

  const workers = $('workers');
  workers.replaceChildren();
  text('worker-count', showWorkerHistory ? data.worker_history.length : data.workers.length);
  for (const w of (showWorkerHistory ? data.worker_history : data.workers).filter(w => matchesChallenge(w.challenge_key))) {
    const row = node('div', 'worker');
    const top = node('div', 'worker-top');
    top.append(node('span', 'cyan', w.worker), node('span', 'muted', `PID ${w.pid}`));
    row.append(top, node('div', 'worker-bottom', `${names.get(w.challenge_key) || w.challenge_key} · ${display(w.status)}`));
    row.append(node('div', 'worker-bottom', w.status === 'stopped' ? `Exit ${w.code} / ${display(w.reason)} / ${timeText(w.ended_at)} UTC` : `Started ${timeText(w.time)} UTC`));
    if (w.status === 'active') {
      row.append(stopButton('Stop worker', {action: 'stop_worker', challenge_key: w.challenge_key, worker: w.worker, pid: w.pid}, `Stop ${w.worker} (PID ${w.pid}) for ${names.get(w.challenge_key) || w.challenge_key}?`));
    }
    workers.append(row);
  }
  if (!workers.children.length) empty(workers, 'No active workers in this view. Completed processes have been reaped.');
  const gateStatus = data.cooldown > 0 ? 'cooldown' : data.run_status === 'running' ? 'ready' : 'idle';
  $('gate-state').className = 'badge ' + gateStatus;
  text('gate-state', data.cooldown > 0 ? `${data.cooldown.toFixed(1)}s WAIT` : gateStatus.toUpperCase());
  text('gate-caption', data.cooldown > 0 ? 'New requests deferred globally' : 'HTTP requests / 5-second buckets');
  const end = data.generated_at;
  const buckets = Array(32).fill(0);
  for (const request of data.requests.filter(r => r.sent_at)) {
    const index = 31 - Math.floor((end - request.sent_at) / 5);
    if (index >= 0 && index < 32) buckets[index]++;
  }
  const peak = Math.max(1, ...buckets);
  $('traffic-path').setAttribute('d', buckets.map((value, i) => `${i ? 'L' : 'M'}${(i * 320 / 31).toFixed(1)} ${(60 - value * 54 / peak).toFixed(1)}`).join(' '));

  const submissions = data.submissions.filter(s => matchesChallenge(s.challenge_key) && (
    filter === 'all' || (filter === 'queue' && ['queued', 'in_flight'].includes(s.status)) ||
    (filter === 'review' && ['error', 'unknown'].includes(s.status)) || (filter === 'correct' && s.status === 'correct')));
  text('submission-count', `${submissions.length} / ${data.submissions.length}`);
  const body = $('submissions');
  body.replaceChildren();
  for (const s of submissions.slice(0, limit)) {
    const row = node('tr');
    const challenge = node('td', '', names.get(s.challenge_key) || s.challenge_key);
    challenge.append(node('span', 'subline', `${s.worker || 'unknown worker'} · ${s.ticket.slice(0, 8)}`));
    const trace = node('button', 'compact trace-link', 'HTTP trace');
    trace.type = 'button';
    trace.disabled = !data.requests.some(r => r.submission_ticket === s.ticket);
    trace.addEventListener('click', () => {
      $('http-search').value = s.ticket;
      httpFilter = 'all';
      httpLimit = 100;
      selectedRequest = data.requests.find(r => r.submission_ticket === s.ticket)?.request_id;
      for (const button of document.querySelectorAll('[data-http-filter]')) button.setAttribute('aria-pressed', String(button.dataset.httpFilter === 'all'));
      render();
      $('http-detail').scrollIntoView({block: 'center'});
    });
    challenge.append(trace);
    const flag = node('td');
    flag.append(node('code', 'flag-value', $('reveal').checked ? (s.flag || '—') : '••••••••'));
    const outcome = node('td');
    outcome.append(badge(s.status));
    row.append(node('td', '', timeText(s.time)), challenge, flag, outcome, node('td', 'response', s.message || (s.status === 'queued' ? 'Waiting for challenge submission lock' : s.status === 'in_flight' ? 'Awaiting platform response / rate admission' : '—')));
    body.append(row);
  }
  $('no-submissions').hidden = submissions.length > 0;
  $('load-more').hidden = submissions.length <= limit;
  text('ledger-note', `${$('reveal').checked ? 'Sensitive flags revealed' : 'Flags masked'} · ${Math.min(limit, submissions.length)} rows shown · UTC timestamps`);

  const controls = $('controls');
  controls.replaceChildren();
  for (const c of data.controls) {
    const row = node('div', 'control-row');
    row.append(node('time', 'muted', timeText(c.time)), badge(c.status),
      node('span', '', `${display(c.action)} / ${c.challenge_key || 'all challenges'}${c.worker ? ' / ' + c.worker : ''}${c.pid ? ' / PID ' + c.pid : ''}`),
      node('span', 'muted', c.message || 'Waiting for orchestrator acknowledgement…'));
    controls.append(row);
  }
  if (!controls.children.length) empty(controls, 'No operator stop requests. Stopping a worker does not undo accepted submissions.');

  const httpQuery = $('http-search').value.toLowerCase().trim();
  const requests = data.requests.filter(r => (!query || matchesChallenge(r.challenge_key)) &&
    (!httpQuery || [r.path, r.method, r.worker, r.challenge_key, r.request_id, r.operation_id, r.submission_ticket, r.status, r.error].join(' ').toLowerCase().includes(httpQuery)) &&
    (httpFilter === 'all' || (httpFilter === 'errors' && (r.status >= 400 || ['error', 'blocked', 'unknown'].includes(r.stage))) ||
      (httpFilter === 'pending' && ['queued', 'sent', 'response'].includes(r.stage))));
  text('http-count', `${requests.length} / ${data.request_count}`);
  const httpRows = $('http-requests');
  httpRows.replaceChildren();
  for (const r of requests.slice(0, httpLimit)) {
    const row = node('tr', r.request_id === selectedRequest ? 'selected' : '');
    const when = node('td', '', timeText(r.time));
    when.append(node('span', 'subline', r.request_id.slice(0, 8)));
    const target = node('td', 'http-path', `${r.method || '?'} ${r.path || '?'}${r.query ? '?[omitted]' : ''}`);
    const status = node('td');
    status.append(badge(r.status >= 400 || r.stage === 'error' ? 'error' : r.stage === 'blocked' ? 'blocked' : r.status ? 'ready' : 'queued'),
      node('span', 'subline', `${r.status ? 'HTTP ' + r.status : 'No response headers'} / ${r.stage}`));
    const timing = node('td', '', `#${r.attempt || 1} / ${r.latency_ms ?? '—'} ms headers`);
    timing.append(node('span', 'subline', `${r.wait_ms ?? '—'} ms gate · ${r.duration_ms ?? '—'} ms total`));
    const inspectCell = node('td');
    const inspect = node('button', 'compact', 'Inspect');
    inspect.type = 'button';
    inspect.addEventListener('click', () => { selectedRequest = r.request_id; render(); });
    inspectCell.append(inspect);
    row.append(when, target, status, timing, node('td', '', `${r.worker || 'orchestrator'} / ${r.challenge_key || 'session or inventory'}`), inspectCell);
    httpRows.append(row);
  }
  $('no-http').hidden = requests.length > 0;
  $('http-more').hidden = requests.length <= httpLimit;
  text('http-note', `${Math.min(httpLimit, requests.length)} attempts shown · each retry has its own request ID · full trace in http.jsonl`);
  if (selectedRequest) {
    const selected = data.requests.find(r => r.request_id === selectedRequest);
    text('http-detail', selected ? JSON.stringify(selected, null, 2) : 'Request is not available in this snapshot. Inspect http.jsonl locally.');
  }

  const events = $('events');
  const oldScroll = events.scrollTop;
  events.replaceChildren();
  for (const e of data.events.filter(e => !e.challenge_key || matchesChallenge(e.challenge_key))) {
    const kind = String(e.event || 'event');
    const row = node('div', 'event ' + (['rate_limit', 'solved', 'worker_timeout'].includes(kind) ? kind : kind.includes('error') ? 'error' : ''));
    const details = [e.challenge_key, e.worker, e.file, e.message, e.reason, e.action,
      e.pid !== undefined ? `pid=${e.pid}` : null, e.wait !== undefined ? `wait=${e.wait.toFixed(2)}s attempt=${e.attempt}` : null,
      e.code !== undefined ? `exit=${e.code}` : null].filter(v => v !== undefined && v !== null).join(' / ');
    row.append(node('time', '', timeText(e.time)), node('span', 'event-type', kind), node('span', 'event-detail', details || 'Global orchestrator event'));
    events.append(row);
  }
  if (!events.children.length) empty(events, 'Listening for events…');
  events.scrollTop = oldScroll;
  text('event-count', `${data.events.length} / ${data.event_count} EVENTS · NEWEST FIRST`);
}

async function poll() {
  if (busy || paused || !token) return;
  busy = true;
  const version = revision;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);
  try {
    const response = await fetch(`/api/state${$('reveal').checked ? '?reveal=1' : ''}`, {
      headers: {Authorization: `Bearer ${token}`}, cache: 'no-store', signal: controller.signal,
    });
    if (response.status === 401) {
      token = '';
      sessionStorage.removeItem('signal-token');
      redactCached();
      auth('Access token rejected. Use the private link from the current dashboard process.');
      return;
    }
    if (!response.ok) throw new Error(`Telemetry request returned HTTP ${response.status}`);
    const data = await response.json();
    if (version !== revision || paused) return;
    snapshot = data;
    uplink = true;
    connected();
    render();
  } catch (error) {
    uplink = false;
    if (snapshot) render();
    text('connection', 'UPLINK LOST / RETRYING');
    $('connection').className = 'connection offline';
    $('notice').hidden = false;
    text('notice', `${error.message}. Displaying the last received snapshot; process states may be stale.`);
  } finally {
    clearTimeout(timeout);
    busy = false;
  }
}
$('auth-form').addEventListener('submit', event => {
  event.preventDefault();
  token = $('access-token').value.trim();
  $('access-token').value = '';
  sessionStorage.setItem('signal-token', token);
  revision++;
  poll();
});
$('pause').addEventListener('click', () => {
  paused = !paused;
  revision++;
  text('pause', paused ? 'Resume feed' : 'Pause feed');
  render();
  if (!paused) poll();
});
$('reveal').addEventListener('change', () => {
  revision++;
  if (!$('reveal').checked) redactCached();
  render();
  if (!paused) poll();
});
$('search').addEventListener('input', () => { limit = 100; render(); });
for (const button of document.querySelectorAll('[data-filter]')) {
  button.addEventListener('click', () => {
    filter = button.dataset.filter;
    limit = 100;
    for (const other of document.querySelectorAll('[data-filter]')) other.setAttribute('aria-pressed', String(other === button));
    render();
  });
}
$('load-more').addEventListener('click', () => { limit += 100; render(); });
$('stop-all').addEventListener('click', () => sendControl({action: 'stop_all'}, 'Stop ALL current and queued fake workers for this run?'));
$('worker-history').addEventListener('click', () => {
  showWorkerHistory = !showWorkerHistory;
  $('worker-history').setAttribute('aria-pressed', String(showWorkerHistory));
  text('worker-history', showWorkerHistory ? 'Show active' : 'Show history');
  render();
});
$('http-search').addEventListener('input', () => { httpLimit = 100; render(); });
$('http-more').addEventListener('click', () => { httpLimit += 100; render(); });
for (const button of document.querySelectorAll('[data-http-filter]')) {
  button.addEventListener('click', () => {
    httpFilter = button.dataset.httpFilter;
    httpLimit = 100;
    for (const other of document.querySelectorAll('[data-http-filter]')) other.setAttribute('aria-pressed', String(other === button));
    render();
  });
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden && $('reveal').checked) {
    $('reveal').checked = false;
    revision++;
    redactCached();
    render();
  }
});
setInterval(() => text('clock', new Date().toISOString().slice(11, 19) + ' UTC'), 1000);
setInterval(poll, 1000);
if (token) { sessionStorage.setItem('signal-token', token); poll(); } else auth();
