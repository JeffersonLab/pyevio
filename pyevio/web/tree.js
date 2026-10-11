// tree.js — the records table, the lazy structure tree and the hex view.
// Every label here prints a field the server sent (/records, /structure, /words). The viewer never reads a bank
// header itself: the record index for /words?record= is parsed from the node id, and the only format knowledge is
// how many words of a dump to highlight as "header" (2 for a bank, 1 for a segment, header_length for a record).

import { formatNumber } from './charts.js';

const HEX_WORDS_FIRST = 64;
const HEX_WORDS_STEP = 256;

export const hex = (value, digits = 4) => '0x' + Number(value).toString(16).padStart(digits, '0');

function element(tag, className, textContent) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (textContent !== undefined) node.textContent = textContent;
  return node;
}

// ---------------------------------------------------------------- records table
const RECORD_COLUMNS = [
  { title: '#', value: row => row.index, left: true },
  { title: 'offset', value: row => hex(row.offset, 8) },
  { title: 'size (B)', value: row => formatNumber(row.size) },
  { title: 'record #', value: row => row.record_number },
  { title: 'events declared / found', value: row => `${row.event_count} / ${row.events_found}`,
    className: row => (row.events_found !== row.event_count ? 'bad' : '') },
  { title: 'header type', value: row => (row.is_trailer ? `${row.header_type} (trailer)` : row.header_type) },
  { title: 'compression', value: row => row.compression },
  { title: 'last-record bit', value: row => (row.bit_info.is_last_record ? 'set' : '—') },
  { title: 'index', value: row => (row.index_array_length ? `${row.index_array_length} B` : 'none') },
  { title: 'truncated', value: row => (row.is_truncated ? 'yes' : '—'), className: row => (row.is_truncated ? 'bad' : '') },
  { title: 'header quirk', value: row => row.header_quirk ?? '—', className: row => (row.header_quirk ? 'warn' : ''), left: true },
  { title: 'errors', value: row => (row.errors.length ? row.errors.length : '—'), className: row => (row.errors.length ? 'bad' : ''),
    title_of: row => row.errors.join('\n') },
];

/** Fill `table` with the rows of one /records page; `onRow(row)` runs when a row is clicked. */
export function renderRecordsTable(table, rows, onRow) {
  table.replaceChildren();
  const head = table.createTHead().insertRow();
  RECORD_COLUMNS.forEach(column => head.appendChild(element('th', column.left ? 'l' : '', column.title)));
  const body = table.createTBody();
  rows.forEach(row => {
    const tableRow = body.insertRow();
    tableRow.className = 'click';
    tableRow.dataset.id = row.id;
    RECORD_COLUMNS.forEach(column => {
      const cell = tableRow.insertCell();
      cell.textContent = column.value(row);
      cell.className = [column.left ? 'l' : '', column.className ? column.className(row) : ''].join(' ').trim();
      if (column.title_of) cell.title = column.title_of(row);
    });
    tableRow.addEventListener('click', () => onRow(row));
  });
}

// ---------------------------------------------------------------- node labels
const ROLE_LABELS = {
  stream_info: 'stream info', roc_bank: 'ROC bank', roc_stream_info: 'ROC stream info', payload_hits: 'payload',
  payload_empty: 'payload (empty)', payload_words: 'payload', payload_unmapped: 'payload (unmapped port)',
  time_slice: 'time slice', aggregation_info: 'aggregation info (AIS)', payload_map: 'payload-port map',
};

/** `{kind, name, meta}` for one tree row, from the node's fields. */
export function nodeLabel(node) {
  switch (node.kind) {
    case 'file':
      return { kind: 'file', name: 'file', meta: `${node.header_layout}, ${node.record_count} records${node.has_trailer ? ' + trailer' : ''}` };
    case 'trailer':
      return { kind: 'trailer', name: 'trailer', meta: `${formatNumber(node.size)} B at ${hex(node.offset, 8)}` };
    case 'record':
      return { kind: 'record', name: `record ${node.index}`, meta: recordMeta(node) };
    case 'event':
      return { kind: 'event', name: `event ${node.index}`, meta: `tag ${hex(node.tag)} type ${hex(node.data_type, 2)} num ${node.num}, ${node.length + 1} words` };
    case 'hit':
      return { kind: 'hit', name: `hit ${node.index}`, meta: `ch ${node.channel}  t ${node.t_ns} ns  q ${node.charge}  ${node.word}${node.flag ? '  FLAG' : ''}` };
    default:
      return { kind: node.structure === 'Bank' ? 'bank' : 'segment', name: bankName(node), meta: bankMeta(node) };
  }
}

function recordMeta(node) {
  const parts = [`${node.event_count} events`, `${formatNumber(node.size)} B`];
  if (node.compression !== 'none') parts.push(node.compression);
  if (node.is_truncated) parts.push('TRUNCATED');
  if (node.errors && node.errors.length) parts.push(`${node.errors.length} errors`);
  return parts.join(', ');
}

function bankName(node) {
  const role = ROLE_LABELS[node.role];
  if (node.role === 'roc_bank') return `${role} ${node.roc} stream ${node.stream}`;
  if (node.port !== undefined) return `${role} port ${node.port}`;
  return role || `${node.structure} ${hex(node.tag)}`;
}

function bankMeta(node) {
  const parts = [`tag ${hex(node.tag)}`, `type ${hex(node.data_type, 2)}`];
  if (node.num !== null && node.num !== undefined) parts.push(`num ${node.num}`);
  parts.push(`${node.length} words`);
  if (node.module) parts.push(node.module);
  if (node.n_words !== undefined && node.role === 'payload_hits') parts.push(`${node.n_words} hits`);
  if (node.entries) parts.push(`${node.entries.length} entries`);
  return parts.join('  ');
}

// ---------------------------------------------------------------- tree
/**
 * A lazy tree over /structure. `loadChildren(nodeId, start)` returns the server page `{total, start, children}`;
 * `onSelect(node)` runs when a row is clicked. Rows carry data-id and data-role for tests and styling.
 */
export function createTree(container, { loadChildren, onSelect }) {
  const entries = new Map();  // node id → {node, element, kidsElement, loaded, total}
  let selectedRow = null;

  function reset(rootPage) {
    container.replaceChildren();
    entries.clear();
    const rootElement = renderNode(rootPage.node, container);
    const entry = entries.get(rootPage.node.id);
    entry.expanded = true;
    entry.kidsElement.hidden = false;
    appendPage(entry, rootPage);
    rootElement.querySelector('.tw').textContent = '▾';
  }

  function renderNode(node, parent) {
    const label = nodeLabel(node);
    const nodeElement = element('div', 'node');
    nodeElement.dataset.id = node.id;
    if (node.role) nodeElement.dataset.role = node.role;
    const row = element('div', 'row');
    row.tabIndex = 0;
    row.append(element('span', 'tw', node.has_children ? '▸' : ''), element('span', `kind k-${label.kind}`, label.kind),
      element('span', 'nm', label.name), element('span', 'meta', label.meta));
    const kidsElement = element('div', 'kids');
    kidsElement.hidden = true;
    nodeElement.append(row, kidsElement);
    parent.appendChild(nodeElement);
    const entry = { node, element: nodeElement, row, kidsElement, loaded: 0, total: null, expanded: false };
    entries.set(node.id, entry);
    row.addEventListener('click', () => { select(entry); toggle(entry); });
    row.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); row.click(); } });
    return nodeElement;
  }

  function select(entry) {
    if (selectedRow) selectedRow.classList.remove('sel');
    selectedRow = entry.row;
    selectedRow.classList.add('sel');
    onSelect(entry.node);
  }

  async function toggle(entry) {
    if (!entry.node.has_children) return;
    entry.expanded = !entry.expanded;
    entry.kidsElement.hidden = !entry.expanded;
    entry.row.querySelector('.tw').textContent = entry.expanded ? '▾' : '▸';
    if (entry.expanded && entry.total === null) await loadMore(entry);
  }

  async function loadMore(entry) {
    const page = await loadChildren(entry.node.id, entry.loaded);
    if (!page) return;  // a stale response: the file changed under this tree
    appendPage(entry, page);
  }

  function appendPage(entry, page) {
    const moreButton = entry.kidsElement.querySelector('.more');
    if (moreButton) moreButton.remove();
    page.children.forEach(child => renderNode(child, entry.kidsElement));
    entry.loaded += page.children.length;
    entry.total = page.total;
    if (page.errors && page.errors.length && !entry.kidsElement.querySelector('.errs')) {
      const list = element('ul', 'errs');
      page.errors.forEach(message => list.appendChild(element('li', '', message)));
      entry.kidsElement.prepend(list);
    }
    if (entry.loaded < entry.total) {
      const more = element('button', 'more', `show more (${formatNumber(entry.loaded)} of ${formatNumber(entry.total)} loaded)`);
      more.type = 'button';
      more.addEventListener('click', () => loadMore(entry));
      entry.kidsElement.appendChild(more);
    }
  }

  /** Expand and select a top-level record (`r12`) or the trailer (`t`), loading root pages until it is present. */
  async function openRecord(recordId) {
    const root = entries.get('');
    if (!root) return;
    while (!entries.has(recordId) && root.loaded < root.total) await loadMore(root);
    const entry = entries.get(recordId);
    if (!entry) return;
    select(entry);
    if (!entry.expanded) await toggle(entry);
    entry.row.scrollIntoView({ block: 'center' });
  }

  return { reset, openRecord };
}

// ---------------------------------------------------------------- detail pane
const HIDDEN_FIELDS = new Set(['id', 'has_children', 'kind', 'entries', 'bit_info', 'errors']);

/**
 * Show a node's fields, its decoded entries (payload-port map) and its raw words.
 * `loadWords({offset, count, record})` fetches /words; hit rows have no offset and show their fields only.
 */
export async function renderDetail(container, node, loadWords) {
  container.replaceChildren();
  container.appendChild(element('div', 'path', node.id || 'file'));
  container.appendChild(fieldsTable(node));
  if (node.bit_info) {
    container.appendChild(element('h3', '', 'bit info'));
    container.appendChild(fieldsTable(node.bit_info));
  }
  if (node.errors && node.errors.length) {
    const list = element('ul', 'errs');
    node.errors.forEach(message => list.appendChild(element('li', '', message)));
    container.appendChild(list);
  }
  if (node.entries) container.appendChild(entriesTable(node.entries));
  const words = wordsRequest(node);
  if (words) await appendHex(container, node, words, loadWords);
}

function fieldsTable(object) {
  const table = element('table', 'fields');
  Object.entries(object).forEach(([name, value]) => {
    if (HIDDEN_FIELDS.has(name) || value === undefined) return;
    const row = table.insertRow();
    row.insertCell().textContent = name;
    row.insertCell().textContent = fieldText(name, value);
  });
  return table;
}

function fieldText(name, value) {
  if (value === null) return '—';
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  if (Array.isArray(value)) return value.join(', ');
  if (typeof value === 'number' && (name === 'tag' || name === 'offset' || name === 'declared_end')) return `${hex(value)} (${formatNumber(value)})`;
  if (typeof value === 'number' && name === 'data_type') return hex(value, 2);
  return formatNumber(value);
}

function entriesTable(entries) {
  const table = element('table', 't');
  const head = table.createTHead().insertRow();
  ['entry', 'port', 'module id', 'module'].forEach(title => head.appendChild(element('th', '', title)));
  const body = table.createTBody();
  entries.forEach((entry, entry_i) => {
    const row = body.insertRow();
    [entry_i, entry.port, entry.module_id, entry.module].forEach(value => { row.insertCell().textContent = value; });
  });
  return table;
}

/** Which words to fetch for a node: record headers are file offsets, banks index their record's buffer. */
function wordsRequest(node) {
  if (node.kind === 'hit' || node.kind === 'file' || node.offset === undefined) return null;
  const totalWords = Math.floor(node.size / 4);
  if (node.kind === 'record' || node.kind === 'trailer') {
    return { offset: node.offset, record: null, totalWords, headerWords: node.header_length };
  }
  const recordMatch = /^r(\d+)\//.exec(node.id);
  return { offset: node.offset, record: recordMatch ? Number(recordMatch[1]) : null, totalWords,
    headerWords: node.structure === 'Bank' ? 2 : 1 };
}

async function appendHex(container, node, request, loadWords) {
  const heading = element('h3', '', 'raw words');
  const hexElement = element('div', 'hex');
  const note = element('p', 'small');
  container.append(heading, hexElement, note);
  let shown = 0;
  const showMore = async () => {
    const count = Math.min(shown ? HEX_WORDS_STEP : HEX_WORDS_FIRST, request.totalWords - shown);
    const page = await loadWords({ offset: request.offset + shown * 4, count, record: request.record });
    if (!page) return;
    renderHexWords(hexElement, page.words, request.offset + shown * 4, request.headerWords - shown);
    shown += page.count;
    note.replaceChildren();
    note.textContent = `${formatNumber(shown)} of ${formatNumber(request.totalWords)} words, byte order ${page.endian === '<' ? 'little' : 'big'}-endian; offsets ${request.record === null ? 'in the file' : `in record ${request.record}'s buffer`}. `;
    if (shown < request.totalWords && page.count > 0) {
      const button = element('button', 'more', `show ${Math.min(HEX_WORDS_STEP, request.totalWords - shown)} more`);
      button.type = 'button';
      button.addEventListener('click', showMore);
      note.appendChild(button);
    }
  };
  await showMore();
}

/** Four words per row, the row's byte offset first; the first `headerWords` words are marked as the header. */
export function renderHexWords(container, words, baseOffset, headerWords) {
  for (let word_i = 0; word_i < words.length; word_i += 4) {
    container.appendChild(element('span', 'a', hex(baseOffset + word_i * 4, 8)));
    for (let column_i = 0; column_i < 4; column_i++) {
      const index = word_i + column_i;
      container.appendChild(element('span', index < headerWords ? 'h' : '', index < words.length ? words[index] : ''));
    }
  }
}
