// app.js — state, API client, tab routing and the four tab renderers of the pyevio viewer.
// The browser is a thin client: pyevio reads and decodes the file on the server, this page renders the JSON.
// One file is active at a time; responses that arrive for an earlier file are dropped (see fileApi).

import { emptyChart, formatNumber, heatmap, heatmapScale, stepChart } from './charts.js';
import { createTree, renderDetail, renderRecordsTable } from './tree.js';

const POLL_INTERVAL_MS = 500;
const RECORDS_PAGE = 200;
const TABS = ['overview', 'plots', 'records', 'structure'];

const $ = id => document.getElementById(id);

// ---------------------------------------------------------------- API client
// The token comes from the URL `pyevio web` printed (?token=…) and goes out as the X-Token header on every
// /api call: static files need none, and API URLs stay free of secrets in logs and bookmarks.
const TOKEN = new URLSearchParams(location.search).get('token') || '';
// `pyevio web path/to/file` adds ?open=<file relative to the served folder>; the page opens it on load.
const START_FILE = new URLSearchParams(location.search).get('open') || '';

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

/** Thrown when a response belongs to a file that is no longer the active one; callers drop it silently. */
class StaleResponse extends Error {}

async function apiFetch(path, options = {}) {
  let response;
  try {
    response = await fetch(path, { ...options, headers: { 'X-Token': TOKEN, 'Content-Type': 'application/json' } });
  } catch (error) {
    throw new ApiError(0, `cannot reach the server (${error.message}); is \`pyevio web\` still running?`);
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(response.status, body.error || `${response.status} ${response.statusText}`);
  return body;
}

/**
 * GET an endpoint of one file. The result is checked against the active file after the response arrives:
 * two opens in flight must not let a slow earlier file overwrite the newer one.
 * A 409 means the analysis is not done; polling resumes and the caller's render is abandoned.
 */
async function fileApi(fileId, path) {
  try {
    const data = await apiFetch(`/api/files/${fileId}${path}`);
    if (fileId !== state.fileId) throw new StaleResponse();
    return data;
  } catch (error) {
    if (error instanceof ApiError && error.status === 409 && fileId === state.fileId) {
      startPolling(fileId);
      throw new StaleResponse();
    }
    throw error;
  }
}

/** Run a renderer; stale responses are dropped, API errors are shown in the status strip. */
async function guarded(action) {
  try {
    await action();
  } catch (error) {
    if (error instanceof StaleResponse) return;
    showError(error.message);
    if (!(error instanceof ApiError)) console.error(error);
  }
}

// ---------------------------------------------------------------- state
const state = freshState();

function freshState(fileId = null, path = '') {
  return {
    fileId, path, summary: null, pollTimer: null, hists: new Map(), rendered: new Set(),
    recordsStart: 0, tree: null, treeLoaded: false, roc: null, port: null, channel: 0, channels: 0,
  };
}

function resetState(fileId, path) {
  stopPolling();
  Object.assign(state, freshState(fileId, path));
}

// ---------------------------------------------------------------- status strip
function showError(message) {
  $('stErr').textContent = message;
  $('stErr').hidden = !message;
}

function showMessage(message) {
  $('stMsg').textContent = message;
}

function showProgress(progress) {
  const bar = $('prog');
  bar.hidden = false;
  bar.firstElementChild.style.width = `${Math.round((progress.fraction || 0) * 100)}%`;
  const done = formatNumber(progress.records_done);
  const total = progress.records_total ? ` / ${formatNumber(progress.records_total)}` : '';
  const eta = progress.eta_s === null || progress.eta_s === undefined ? '' : `, about ${formatSeconds(progress.eta_s)} left`;
  showMessage(`Analyzing — ${done}${total} records${eta} (${formatSeconds(progress.elapsed_s)} elapsed)`);
}

function formatSeconds(seconds) {
  if (seconds < 60) return `${Math.round(seconds)} s`;
  return `${Math.floor(seconds / 60)} min ${Math.round(seconds % 60)} s`;
}

// ---------------------------------------------------------------- opening a file and polling its analysis
let openSerial = 0;  // counts clicks on files; the latest click owns the page, whichever /open answers last

async function openFile(path) {
  const serial = ++openSerial;
  showError('');
  setTabsEnabled(false);
  hideViews();
  $('stFile').textContent = path;
  showMessage('Opening…');
  try {
    const opened = await apiFetch('/api/open', { method: 'POST', body: JSON.stringify({ path }) });
    if (serial !== openSerial) return;  // a later click already took over; this answer is for an older file
    resetState(opened.file_id, path);
    showBrowser(false);
    handleProgress(opened);
  } catch (error) {
    if (serial !== openSerial) return;
    $('prog').hidden = true;
    showMessage('Open failed.');
    showError(error.message);
  }
}

/** Route one /progress (or /open) answer: keep polling, finish, or report the failure. */
function handleProgress(progress) {
  if (progress.state === 'queued' || progress.state === 'running') {
    showProgress(progress);
    startPolling(state.fileId);
    return;
  }
  stopPolling();
  $('prog').hidden = true;
  if (progress.state === 'done') {
    showMessage(progress.cached ? 'Analysis loaded from the cache.' : `Analysis done in ${formatSeconds(progress.elapsed_s)}.`);
    guarded(onAnalysisDone);
  } else if (progress.state === 'cancelled') {
    showMessage('Analysis cancelled: another file was opened.');
  } else {
    showMessage('Analysis failed.');
    showError(progress.error || `analysis ended in state ${progress.state}`);
  }
}

// /summary and /hist answer 409 until the job is done, so the page polls /progress and renders after "done".
function startPolling(fileId) {
  if (state.pollTimer || fileId !== state.fileId) return;
  state.pollTimer = setTimeout(() => guarded(async () => {
    state.pollTimer = null;
    const progress = await fileApi(fileId, '/progress');
    handleProgress(progress);
  }), POLL_INTERVAL_MS);
}

function stopPolling() {
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
}

async function onAnalysisDone() {
  const fileId = state.fileId;
  state.summary = await fileApi(fileId, '/summary');
  document.body.dataset.fileId = fileId;  // marks "tabs ready for this file" for scripts and tests
  setTabsEnabled(true);
  showTab(currentTab());
}

// ---------------------------------------------------------------- tabs
function currentTab() {
  const name = location.hash.replace('#', '');
  return TABS.includes(name) ? name : 'overview';
}

function setTabsEnabled(enabled) {
  document.querySelectorAll('.tab').forEach(button => { button.disabled = !enabled; });
}

/** Hide the tab views and drop the previous file's content, so nothing stale shows while the next file loads. */
function hideViews() {
  TABS.forEach(name => { $(`v-${name}`).hidden = true; });
  ['tiles', 'checks', 'standout', 'fileFacts', 'recordFacts', 'rocTable', 'recordsTable', 'tree', 'detail', 'recPageInfo', 'occupancyScale']
    .forEach(id => $(id).replaceChildren());
  document.querySelectorAll('svg.chart').forEach(svg => svg.replaceChildren());
  delete document.body.dataset.fileId;
}

function showTab(name) {
  if (!state.summary) return;
  document.querySelectorAll('.tab').forEach(button => button.setAttribute('aria-selected', button.dataset.tab === name));
  TABS.forEach(tab => { $(`v-${tab}`).hidden = tab !== name; });
  if (location.hash !== `#${name}`) history.replaceState(null, '', `#${name}`);
  if (state.rendered.has(name)) return;
  state.rendered.add(name);
  guarded(() => RENDERERS[name]());
}

// ---------------------------------------------------------------- Overview
const TILE_FIELDS = [
  ['n_records', 'records'], ['n_events', 'events'], ['n_frames', 'stream frames'], ['n_hits', 'hits'],
  ['hit_rate_hz', 'hit rate (Hz)'], ['hits_per_frame', 'hits per frame'], ['duration_s', 'duration (s)'], ['errors', 'decode errors'],
];

/** Ratios arrive formatted by the server: "n/a" when a denominator is zero, printed as given. */
function formatValue(value) {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.length ? value.join(', ') : 'none';
  return formatNumber(value);
}

function renderOverview() {
  const summary = state.summary;
  renderTiles(summary.totals);
  renderChecks(summary.checks);
  renderStandout(summary);
  renderFacts($('fileFacts'), fileFacts(summary));
  renderFacts($('recordFacts'), recordFacts(summary));
  renderRocTable(summary.rocs);
}

function renderTiles(totals) {
  const tiles = $('tiles');
  tiles.replaceChildren();
  TILE_FIELDS.forEach(([key, label]) => {
    const tile = document.createElement('div');
    tile.className = 'tile';
    tile.dataset.key = key;
    tile.dataset.value = JSON.stringify(totals[key]);
    tile.innerHTML = `<div class="v"></div><div class="k"></div>`;
    tile.firstElementChild.textContent = formatValue(totals[key]);
    tile.lastElementChild.textContent = label;
    tiles.appendChild(tile);
  });
}

function renderChecks(checks) {
  const container = $('checks');
  container.replaceChildren();
  checks.forEach(check => {
    const row = document.createElement('div');
    row.className = 'ck';
    row.dataset.status = check.status;
    const icon = document.createElement('span');
    icon.className = `ico ${check.status}`;
    icon.textContent = { ok: '✓', info: 'i', warn: '!', error: '✕' }[check.status] || '?';
    const body = document.createElement('div');
    body.innerHTML = `<b></b> <span class="val"></span> <span class="d"></span>`;
    body.querySelector('b').textContent = check.name;
    body.querySelector('.val').textContent = formatValue(check.value);
    body.querySelector('.d').textContent = check.detail ? `— ${check.detail}` : '';
    row.append(icon, body);
    container.appendChild(row);
  });
}

/** Errors first, then warnings, then the first decode messages; "all checks pass" when there is nothing. */
function renderStandout(summary) {
  const list = $('standout');
  list.replaceChildren();
  const notable = [...summary.checks.filter(check => check.status === 'error'), ...summary.checks.filter(check => check.status === 'warn')];
  notable.forEach(check => {
    const item = document.createElement('li');
    item.innerHTML = `<b></b>: <span></span>`;
    item.firstElementChild.textContent = check.name;
    item.lastElementChild.textContent = `${formatValue(check.value)}${check.detail ? ` — ${check.detail}` : ''}`;
    list.appendChild(item);
  });
  summary.totals.error_messages.slice(0, 3).forEach(message => {
    const item = document.createElement('li');
    item.textContent = message;
    list.appendChild(item);
  });
  if (!list.children.length) {
    const item = document.createElement('li');
    item.textContent = summary.totals.n_frames ? 'All checks pass.' : 'No stream frames (0xFF60) in this file; the plots are empty. All file checks pass.';
    list.appendChild(item);
  }
}

function fileFacts(summary) {
  const file = summary.file;
  const stats = summary.stats;
  return [
    ['path', file.path], ['size', `${formatNumber(file.size)} B (${(file.size / 1e6).toFixed(1)} MB)`],
    ['header layout', file.header_layout], ['byte order', file.endian === '<' ? 'little-endian' : 'big-endian'],
    ['file type, version', `${file.file_type}, ${file.version}`], ['records (scanned)', file.record_count],
    ['records (file header)', file.header_record_count], ['trailer', file.has_trailer], ['trailer position', file.trailer_position],
    ['first record offset', file.first_record_offset], ['index array length', file.index_array_length],
    ['user header length', file.user_header_length],
    ['analysis', `${stats.cached ? 'from cache' : `${stats.wall_seconds.toFixed(1)} s on ${stats.workers} workers`}, stats version ${stats.version}`],
  ];
}

function recordFacts(summary) {
  const records = summary.records;
  const frames = summary.frames;
  const truncated = summary.scan_checks.find(check => check[0] === 'truncated_tail');
  return [
    ['events declared / found', `${formatNumber(records.events)} / ${formatNumber(records.events_found)}`],
    ['records with errors', records.with_errors], ['compressed records', records.compressed],
    ['records without index', records.without_index], ['truncated records', records.truncated],
    ['salvaged events (truncated tail)', truncated ? truncated[3] : 'none'],
    ['last-record bit set', records.last_record_bit],
    ['record size min / mean / max', `${formatNumber(records.min_size)} / ${formatValue(records.mean_size)} / ${formatNumber(records.max_size)} B`],
    ['stream frames', frames.n_frames], ['first / last frame', `${frames.first_frame} / ${frames.last_frame}`],
    ['frame stride', frames.stride], ['frame period', `${formatNumber(frames.frame_period_ns)} ns`],
    ['missing / duplicate / out-of-order', `${frames.missing_frames} / ${frames.duplicate_frames} / ${frames.out_of_order}`],
    ['timestamp-step outliers', frames.dts_outliers],
    ['duration / live time', `${formatValue(summary.totals.duration_s)} s / ${formatValue(summary.totals.live_time_s)} s`],
  ];
}

function renderFacts(table, facts) {
  table.replaceChildren();
  facts.forEach(([name, value]) => {
    const row = table.insertRow();
    row.insertCell().textContent = name;
    row.insertCell().textContent = formatValue(value);
  });
}

function renderRocTable(rocs) {
  const table = $('rocTable');
  table.replaceChildren();
  const head = table.createTHead().insertRow();
  ['ROC', 'modules', 'ports', 'hits', 'non-fADC words', 'empty banks'].forEach((title, column_i) => {
    const cell = document.createElement('th');
    cell.textContent = title;
    if (column_i < 3) cell.className = 'l';
    head.appendChild(cell);
  });
  const body = table.createTBody();
  rocs.forEach(roc => {
    const row = body.insertRow();
    [roc.roc, roc.modules.join(', '), roc.ports.join(', '), formatNumber(roc.hits), formatNumber(roc.non_fadc_words), formatNumber(roc.empty_banks)]
      .forEach((value, column_i) => {
        const cell = row.insertCell();
        cell.textContent = value;
        if (column_i < 3) cell.className = 'l';
      });
  });
  if (!rocs.length) body.insertRow().insertCell().textContent = 'no ROC banks';
}

// ---------------------------------------------------------------- Plots
const NS_PER_S = 1e-9;
const NS_PER_US = 1e-3;

/** One histogram, cached per file and selection. */
async function getHist(name, selection = {}) {
  const query = Object.entries(selection).filter(([, value]) => value !== null && value !== undefined)
    .map(([key, value]) => `${key}=${value}`).join('&');
  const key = `${name}?${query}`;
  if (!state.hists.has(key)) state.hists.set(key, fileApi(state.fileId, `/hist/${name}${query ? `?${query}` : ''}`));
  return state.hists.get(key);
}

async function renderPlots() {
  const [hitsPerFrame, rate, occupancy] = await Promise.all([getHist('hits_per_frame'), getHist('rate'), getHist('occupancy')]);
  drawHitsPerFrame(hitsPerFrame);
  drawRate($('chartRate'), rate, $('rateSub'));
  drawOccupancy(occupancy);
  state.channels = occupancy.channels;  // channels per port, as the server's histogram axis has it
  populateRocSelect();
}

function drawHitsPerFrame(hist) {
  const frames = hist.counts.reduce((sum, count) => sum + count, 0);
  $('hpfSub').textContent = frames ? `${formatNumber(frames)} frames; last bin collects frames at or above its left edge` : '';
  if (!frames) return emptyChart($('chartHitsPerFrame'), 'no stream frames');
  stepChart($('chartHitsPerFrame'), { edges: hist.edges, values: hist.counts, xLabel: hist.x_unit, yLabel: hist.y_unit });
}

/** Rate over time: the server's bin edges in ns from the first frame, drawn in seconds (axis title names the unit). */
function drawRate(svg, hist, subtitle) {
  const edgesS = hist.edges_ns.map(edge => edge * NS_PER_S);
  if (subtitle) {
    const binSeconds = edgesS.length > 1 ? edgesS[1] - edgesS[0] : 0;
    subtitle.textContent = hist.counts.length ? `${hist.counts.length} bins of ${formatNumber(binSeconds)} s; empty bins are frames with no hits or missing frames` : '';
  }
  if (!hist.counts.length || !state.summary.totals.n_frames) return emptyChart(svg, 'no stream frames');
  if (!hist.counts.some(count => count > 0)) return emptyChart(svg, 'no hits');
  stepChart(svg, { edges: edgesS, values: hist.rate_hz, xLabel: 'time since the first frame (s)', yLabel: 'hit rate (Hz)' });
}

function drawCharge(svg, hist) {
  if (!hist.counts.some(count => count > 0)) return emptyChart(svg, 'no hits');
  stepChart(svg, { edges: hist.edges, values: hist.counts, xLabel: hist.x_unit, yLabel: hist.y_unit, logY: true });
}

function drawTimeInFrame(svg, hist) {
  if (!hist.counts.some(count => count > 0)) return emptyChart(svg, 'no hits');
  stepChart(svg, { edges: hist.edges_ns.map(edge => edge * NS_PER_US), values: hist.counts, xLabel: 'time in frame (µs)', yLabel: hist.y_unit });
}

/** ROC × port heatmap of the whole file: the per-channel counts of each port summed. */
function drawOccupancy(hist) {
  const svg = $('chartOccupancy');
  if (!hist.rocs.length) {
    heatmapScale($('occupancyScale'), 0);
    return emptyChart(svg, 'no stream frames');
  }
  const values = hist.counts.map(ports => ports.map(channels => channels.reduce((sum, count) => sum + count, 0)));
  const maximum = heatmap(svg, {
    rows: hist.rocs.map(roc => `ROC ${roc}`), cols: hist.ports, values, rowTitle: 'ROC', colTitle: 'payload port', valueLabel: 'hits',
    isDead: (roc_i, port_i) => hist.module_id[roc_i][port_i] === 255,
    onCell: (roc_i, port_i) => selectRoc(hist.rocs[roc_i], hist.ports[port_i]),
  });
  heatmapScale($('occupancyScale'), maximum, 'hits');
}

function populateRocSelect() {
  const rocs = state.summary.rocs;
  const select = $('selRoc');
  select.replaceChildren(...rocs.map(roc => new Option(`${roc.roc} (${roc.modules.join(', ') || 'no modules'})`, roc.roc)));
  $('rocPanel').hidden = $('channelPanel').hidden = !rocs.length;
  if (rocs.length) selectRoc(rocs[0].roc, rocs[0].ports[0]);
}

function rocRow(roc) {
  return state.summary.rocs.find(row => row.roc === roc);
}

/** Select a ROC (and a port): redraws the per-ROC charts and the port/channel selectors. */
function selectRoc(roc, port = null) {
  const row = rocRow(roc);
  if (!row) return;
  state.roc = roc;
  $('selRoc').value = String(roc);
  $('rocInfo').textContent = `${formatNumber(row.hits)} hits, ports ${row.ports.join(', ') || 'none'}, ${row.empty_banks} empty banks, ${formatNumber(row.non_fadc_words)} non-fADC words`;
  $('selPort').replaceChildren(...row.ports.map(value => new Option(String(value), value)));
  $('selCh').replaceChildren(...Array.from({ length: state.channels }, (_, channel) => new Option(String(channel), channel)));
  guarded(renderRocCharts);
  selectChannel(port !== null && row.ports.includes(port) ? port : row.ports[0], 0);
}

async function renderRocCharts() {
  const roc = state.roc;
  const [occupancy, rate, charge, time] = await Promise.all([
    getHist('occupancy', { roc }), getHist('rate', { roc }), getHist('charge', { roc }), getHist('time_in_frame', { roc })]);
  if (roc !== state.roc) return;
  const rocIndex = occupancy.rocs.indexOf(roc);
  heatmap($('chartRocMap'), {
    rows: occupancy.ports.map(port => `port ${port}`), cols: Array.from({ length: occupancy.channels }, (_, channel) => channel),
    values: occupancy.counts, rowTitle: 'payload port', colTitle: 'channel', valueLabel: 'hits',
    isDead: port_i => occupancy.module_id[rocIndex][port_i] === 255,
    onCell: (port_i, channel) => selectChannel(occupancy.ports[port_i], channel),
  });
  drawRate($('chartRocRate'), rate, $('rocRateSub'));
  drawCharge($('chartRocCharge'), charge);
  drawTimeInFrame($('chartRocTime'), time);
}

function selectChannel(port, channel) {
  if (port === undefined) return;
  state.port = port;
  state.channel = channel;
  $('selPort').value = String(port);
  $('selCh').value = String(channel);
  $('chInfo').textContent = `ROC ${state.roc}, port ${port}, channel ${channel}`;
  guarded(renderChannelCharts);
}

async function renderChannelCharts() {
  const selection = { roc: state.roc, port: state.port, ch: state.channel };
  const [rate, charge, time] = await Promise.all([getHist('rate', selection), getHist('charge', selection), getHist('time_in_frame', selection)]);
  if (selection.roc !== state.roc || selection.port !== state.port || selection.ch !== state.channel) return;
  drawRate($('chartChRate'), rate, null);
  drawCharge($('chartChCharge'), charge);
  drawTimeInFrame($('chartChTime'), time);
}

/** Step through the ROC's channels: port by port, `state.channels` channels each. */
function stepChannel(direction) {
  const ports = rocRow(state.roc).ports;
  const perPort = state.channels;
  const flat = ports.indexOf(state.port) * perPort + state.channel + direction;
  if (flat < 0 || flat >= ports.length * perPort) return;
  selectChannel(ports[Math.floor(flat / perPort)], flat % perPort);
}

// ---------------------------------------------------------------- Records
async function renderRecords() {
  await loadRecordsPage(0);
}

async function loadRecordsPage(start) {
  const page = await fileApi(state.fileId, `/records?from=${start}&count=${RECORDS_PAGE}`);
  state.recordsStart = page.start;
  renderRecordsTable($('recordsTable'), page.rows, openRecordInStructure);
  const last = page.start + page.rows.length;
  $('recPageInfo').textContent = page.total ? `records ${formatNumber(page.start)} – ${formatNumber(last - 1)} of ${formatNumber(page.total)}` : 'no records';
  $('recPrev').disabled = page.start === 0;
  $('recNext').disabled = last >= page.total;
  $('recGo').max = Math.max(0, page.total - 1);
}

// ---------------------------------------------------------------- Structure
async function renderStructure() {
  await ensureTree();
}

async function ensureTree() {
  if (state.treeLoaded) return;
  const fileId = state.fileId;
  const root = await fileApi(fileId, '/structure');
  state.tree = createTree($('tree'), {
    loadChildren: (nodeId, start) => fileApi(fileId, `/structure?node=${encodeURIComponent(nodeId)}&from=${start}`).catch(dropStale),
    onSelect: node => guarded(() => renderDetail($('detail'), node, loadWords(fileId))),
  });
  state.tree.reset(root);
  state.treeLoaded = true;
}

const dropStale = error => { if (error instanceof StaleResponse) return null; throw error; };

/** /words reader bound to one file; a record index selects that record's buffer (the only view of LZ4/gzip events). */
function loadWords(fileId) {
  return ({ offset, count, record }) => {
    const query = `offset=${offset}&count=${count}${record === null ? '' : `&record=${record}`}`;
    return fileApi(fileId, `/words?${query}`).catch(dropStale);
  };
}

async function openRecordInStructure(row) {
  state.rendered.add('structure');
  showTab('structure');
  await guarded(async () => {
    await ensureTree();
    await state.tree.openRecord(row.id);
  });
}

const RENDERERS = { overview: renderOverview, plots: renderPlots, records: renderRecords, structure: renderStructure };

// ---------------------------------------------------------------- folder browser
let browserDir = '';
let browserParent = null;

function showBrowser(visible) {
  $('browser').hidden = !visible;
  $('browseBtn').setAttribute('aria-expanded', String(visible));
}

async function browse(dir) {
  const listing = await apiFetch(`/api/ls?dir=${encodeURIComponent(dir)}&all=${$('brAll').checked}`);
  browserDir = listing.dir;
  browserParent = listing.parent;
  $('brPath').textContent = `/${listing.dir}`;
  $('brUp').disabled = listing.parent === null;
  const list = $('brList');
  list.replaceChildren();
  listing.dirs.forEach(name => list.appendChild(browserItem('dir', name, '', () => guarded(() => browse(joinPath(listing.dir, name))))));
  listing.files.forEach(file => list.appendChild(browserItem('file', file.name, `${(file.size / 1e6).toFixed(1)} MB`,
    () => openFile(joinPath(listing.dir, file.name)))));
  if (!list.children.length) {
    const empty = document.createElement('div');
    empty.className = 'bempty';
    empty.textContent = 'No folders or EVIO files here. Tick "all files" to list every file.';
    list.appendChild(empty);
  }
}

function joinPath(dir, name) {
  return dir ? `${dir}/${name}` : name;
}

function browserItem(kind, name, size, onClick) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = `bitem ${kind}`;
  button.innerHTML = `<span class="bk"></span><span class="bn"></span><span class="bs"></span>`;
  button.children[0].textContent = kind;
  button.children[1].textContent = name;
  button.children[2].textContent = size;
  button.addEventListener('click', onClick);
  return button;
}

// ---------------------------------------------------------------- wiring
function init() {
  document.querySelectorAll('.tab').forEach(button => button.addEventListener('click', () => showTab(button.dataset.tab)));
  addEventListener('hashchange', () => showTab(currentTab()));
  $('browseBtn').addEventListener('click', () => showBrowser($('browser').hidden));
  $('brUp').addEventListener('click', () => guarded(() => browse(browserParent)));
  $('brAll').addEventListener('change', () => guarded(() => browse(browserDir)));
  $('selRoc').addEventListener('change', event => selectRoc(Number(event.target.value)));
  $('selPort').addEventListener('change', event => selectChannel(Number(event.target.value), state.channel));
  $('selCh').addEventListener('change', event => selectChannel(state.port, Number(event.target.value)));
  $('chPrev').addEventListener('click', () => stepChannel(-1));
  $('chNext').addEventListener('click', () => stepChannel(1));
  $('recPrev').addEventListener('click', () => guarded(() => loadRecordsPage(Math.max(0, state.recordsStart - RECORDS_PAGE))));
  $('recNext').addEventListener('click', () => guarded(() => loadRecordsPage(state.recordsStart + RECORDS_PAGE)));
  $('recGo').addEventListener('change', event => guarded(() => loadRecordsPage(Math.max(0, Number(event.target.value) || 0))));
  if (!TOKEN) {
    showError('No token in the page URL. Open the URL that `pyevio web` printed (it ends in ?token=…).');
    return;
  }
  guarded(() => browse(''));
  if (START_FILE) guarded(() => openFile(START_FILE));
}

init();
