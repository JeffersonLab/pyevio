// charts.js — plain SVG charts drawn from arrays the server sends: a step chart over bin edges and a heatmap.
// Colors come from the CSS custom properties in style.css (var(--…) works inside SVG attributes), so the
// dark-mode variant needs no code here. No library: the viewer must load on a machine without internet.

const SVG_NS = 'http://www.w3.org/2000/svg';
const WIDTH = 480;
const HEIGHT = 240;
const MARGIN = { left: 60, right: 12, top: 10, bottom: 42 };

// ---------------------------------------------------------------- number formatting
export function formatNumber(value) {
  if (typeof value !== 'number') return String(value);
  if (Number.isInteger(value)) return value.toLocaleString('en-US');
  const magnitude = Math.abs(value);
  if (magnitude !== 0 && (magnitude < 0.001 || magnitude >= 1e15)) return value.toExponential(3);
  return Number(value.toPrecision(4)).toLocaleString('en-US', { maximumFractionDigits: 6 });
}

export function formatCompact(value) {
  if (value >= 1e9) return (value / 1e9).toPrecision(3) + 'G';
  if (value >= 1e6) return (value / 1e6).toPrecision(3) + 'M';
  if (value >= 1e3) return (value / 1e3).toPrecision(3) + 'k';
  return formatNumber(value);
}

/** A tick label with as many decimals as the tick step needs, and no more. */
function formatTick(value, step) {
  if (value === 0) return '0';
  if (Math.abs(value) >= 1e4) return formatCompact(value);
  const decimals = step >= 1 ? 0 : Math.min(9, Math.ceil(-Math.log10(step)));
  return value.toFixed(decimals);
}

/** Round tick positions covering [low, high]: 1, 2 or 5 times a power of ten. */
function niceTicks(low, high, count) {
  const span = high - low || 1;
  const roughStep = span / count;
  const power = 10 ** Math.floor(Math.log10(roughStep));
  const multiplier = [1, 2, 5, 10].find(candidate => candidate * power >= roughStep);
  const step = multiplier * power;
  const ticks = [];
  for (let tick = Math.ceil(low / step) * step; tick <= high + step * 1e-9; tick += step) ticks.push(Number(tick.toFixed(12)));
  return { ticks, step };
}

// ---------------------------------------------------------------- SVG helpers
function svgElement(tag, attributes, parent) {
  const element = document.createElementNS(SVG_NS, tag);
  for (const name in attributes) element.setAttribute(name, attributes[name]);
  if (parent) parent.appendChild(element);
  return element;
}

function text(parent, x, y, content, attributes = {}) {
  const node = svgElement('text', { x, y, ...attributes }, parent);
  node.textContent = content;
  return node;
}

export function clearChart(svg) {
  svg.replaceChildren();
  svg.setAttribute('viewBox', `0 0 ${WIDTH} ${HEIGHT}`);
  svg.style.width = '';  // a heatmap may have pinned the width in pixels; step charts fill their panel
}

/** A chart with nothing to draw shows why, instead of an empty frame. */
export function emptyChart(svg, message) {
  clearChart(svg);
  text(svg, WIDTH / 2, HEIGHT / 2, message, { 'text-anchor': 'middle', class: 'axis-title' });
}

// ---------------------------------------------------------------- tooltip
const tip = document.getElementById('tip');

export function showTip(event, html) {
  tip.innerHTML = html;
  tip.hidden = false;
  const box = tip.getBoundingClientRect();
  let x = event.clientX + 14;
  let y = event.clientY + 14;
  if (x + box.width > innerWidth - 8) x = event.clientX - box.width - 14;
  if (y + box.height > innerHeight - 8) y = event.clientY - box.height - 14;
  tip.style.left = x + 'px';
  tip.style.top = y + 'px';
}

export function hideTip() {
  tip.hidden = true;
}

/** Mouse position in the SVG's own coordinate system. */
function svgPoint(svg, event) {
  const point = svg.createSVGPoint();
  point.x = event.clientX;
  point.y = event.clientY;
  return point.matrixTransform(svg.getScreenCTM().inverse());
}

// ---------------------------------------------------------------- step chart
/**
 * Draw `values[i]` as a horizontal step between `edges[i]` and `edges[i + 1]`.
 * `edges` has one more entry than `values` and is already in the unit `xLabel` names (the caller scales
 * nanoseconds to seconds). `logY` draws a decade axis; zero bins sit on the baseline.
 * `formatX` and `formatY` are used for the tooltip; axis ticks use a step-aware default.
 */
export function stepChart(svg, { edges, values, xLabel, yLabel, logY = false, formatX = formatNumber, formatY = formatNumber }) {
  clearChart(svg);
  if (!values || values.length === 0 || edges.length !== values.length + 1) {
    emptyChart(svg, 'no data');
    return;
  }
  const plot = { x0: MARGIN.left, x1: WIDTH - MARGIN.right, y0: HEIGHT - MARGIN.bottom, y1: MARGIN.top };
  const xLow = edges[0];
  const xHigh = edges[edges.length - 1] === xLow ? xLow + 1 : edges[edges.length - 1];
  const scaleX = value => plot.x0 + (value - xLow) / (xHigh - xLow) * (plot.x1 - plot.x0);
  const yAxis = logY ? logAxis(values) : linearAxis(values);
  const scaleY = value => plot.y0 - (yAxis.transform(value) - yAxis.low) / (yAxis.high - yAxis.low) * (plot.y0 - plot.y1);

  drawYAxis(svg, plot, yAxis, scaleY, yLabel);
  drawXAxis(svg, plot, xLow, xHigh, scaleX, xLabel);

  let path = '';
  values.forEach((value, bin_i) => {
    const y = scaleY(value).toFixed(1);
    path += `${bin_i ? 'L' : 'M'}${scaleX(edges[bin_i]).toFixed(1)},${y}L${scaleX(edges[bin_i + 1]).toFixed(1)},${y}`;
  });
  svgElement('path', { d: path, class: 'series' }, svg);
  attachBinHover(svg, plot, { edges, values, scaleX, scaleY, formatX, formatY, xLabel, yLabel });
}

/** Linear axis from 0 to the first tick at or above the maximum, so the top of the data has a label. */
function linearAxis(values) {
  const maximum = Math.max(...values) || 1;
  const { ticks, step } = niceTicks(0, maximum, 4);
  const high = Math.ceil(maximum / step) * step;
  if (ticks[ticks.length - 1] < high) ticks.push(high);
  return { low: 0, high, ticks, transform: value => value, formatTick: value => formatTick(value, step) };
}

/** Decade axis from below the smallest positive value (so a count of 1 is not on the baseline) to above the largest. */
function logAxis(values) {
  const positive = values.filter(value => value > 0);
  const lowDecade = positive.length ? Math.floor(Math.log10(Math.min(...positive) * 0.5)) : 0;
  const highDecade = Math.max(lowDecade + 1, positive.length ? Math.ceil(Math.log10(Math.max(...positive))) : 1);
  const ticks = [];
  for (let decade = lowDecade; decade <= highDecade; decade++) ticks.push(10 ** decade);
  return { low: lowDecade, high: highDecade, ticks, transform: value => (value > 0 ? Math.log10(value) : lowDecade),
    formatTick: value => formatCompact(value) };
}

function drawYAxis(svg, plot, axis, scaleY, label) {
  axis.ticks.forEach(tick => {
    const y = scaleY(tick);
    svgElement('line', { x1: plot.x0, x2: plot.x1, y1: y, y2: y, class: 'grid' }, svg);
    text(svg, plot.x0 - 6, y + 4, axis.formatTick(tick), { 'text-anchor': 'end' });
  });
  text(svg, 12, (plot.y0 + plot.y1) / 2, label, { class: 'axis-title', 'text-anchor': 'middle',
    transform: `rotate(-90 12 ${(plot.y0 + plot.y1) / 2})` });
}

function drawXAxis(svg, plot, low, high, scaleX, label) {
  const { ticks, step } = niceTicks(low, high, 5);
  ticks.forEach(tick => text(svg, scaleX(tick), plot.y0 + 16, formatTick(tick, step), { 'text-anchor': 'middle' }));
  svgElement('line', { x1: plot.x0, x2: plot.x1, y1: plot.y0, y2: plot.y0, class: 'axis' }, svg);
  text(svg, (plot.x0 + plot.x1) / 2, HEIGHT - 4, label, { class: 'axis-title', 'text-anchor': 'middle' });
}

/** Crosshair and tooltip for the bin under the mouse. */
function attachBinHover(svg, plot, { edges, values, scaleX, scaleY, formatX, formatY, xLabel, yLabel }) {
  const crosshair = svgElement('line', { y1: plot.y1, y2: plot.y0, class: 'hover-line', visibility: 'hidden' }, svg);
  const dot = svgElement('circle', { r: 4, class: 'hover-dot', visibility: 'hidden' }, svg);
  const hitArea = svgElement('rect', { x: plot.x0, y: plot.y1, width: plot.x1 - plot.x0, height: plot.y0 - plot.y1, class: 'hit-area' }, svg);
  hitArea.addEventListener('mousemove', event => {
    const x = svgPoint(svg, event).x;
    let bin_i = edges.findIndex((edge, i) => i < values.length && scaleX(edge) <= x && x < scaleX(edges[i + 1]));
    if (bin_i < 0) bin_i = x < scaleX(edges[0]) ? 0 : values.length - 1;
    const centerX = (scaleX(edges[bin_i]) + scaleX(edges[bin_i + 1])) / 2;
    crosshair.setAttribute('x1', centerX);
    crosshair.setAttribute('x2', centerX);
    crosshair.setAttribute('visibility', 'visible');
    dot.setAttribute('cx', centerX);
    dot.setAttribute('cy', scaleY(values[bin_i]));
    dot.setAttribute('visibility', 'visible');
    showTip(event, `${xLabel}: ${formatX(edges[bin_i])} – ${formatX(edges[bin_i + 1])}<br>${yLabel}: ${formatY(values[bin_i])}`);
  });
  hitArea.addEventListener('mouseleave', () => {
    hideTip();
    crosshair.setAttribute('visibility', 'hidden');
    dot.setAttribute('visibility', 'hidden');
  });
}

// ---------------------------------------------------------------- heatmap
/**
 * A grid of `values[row_i][col_i]` colored by one sequential ramp (--heat-lo → --heat-hi).
 * `isDead(row_i, col_i)` marks cells with no meaning (a port absent from a ROC's map); they are grey and have
 * no value. `onCell(row_i, col_i)` runs on click. Returns the maximum value, for the color-scale legend.
 */
export function heatmap(svg, { rows, cols, values, rowTitle, colTitle, valueLabel, isDead = () => false, onCell = null }) {
  svg.replaceChildren();
  if (!rows.length || !cols.length) {
    emptyChart(svg, 'no data');
    return 0;
  }
  const labelWidth = 70;
  const cellWidth = Math.max(14, Math.min(40, Math.floor((WIDTH - labelWidth - MARGIN.right) / cols.length)));
  const cellHeight = Math.max(14, Math.min(26, cellWidth));
  const plot = { x0: labelWidth, y0: 22 };
  const height = plot.y0 + rows.length * cellHeight + 36;
  const width = Math.max(WIDTH, plot.x0 + cols.length * cellWidth + MARGIN.right);
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.style.width = `${width}px`;  // one SVG unit per pixel: a small grid must not scale its labels up
  svg.style.maxWidth = '100%';
  const maximum = Math.max(0, ...values.flatMap((row, row_i) => row.filter((_, col_i) => !isDead(row_i, col_i))));

  cols.forEach((label, col_i) => text(svg, plot.x0 + col_i * cellWidth + cellWidth / 2, plot.y0 - 6, String(label), { 'text-anchor': 'middle' }));
  rows.forEach((label, row_i) => text(svg, plot.x0 - 6, plot.y0 + row_i * cellHeight + cellHeight / 2 + 4, String(label), { 'text-anchor': 'end' }));
  text(svg, plot.x0 + cols.length * cellWidth / 2, height - 6, colTitle, { class: 'axis-title', 'text-anchor': 'middle' });
  text(svg, 12, plot.y0 + rows.length * cellHeight / 2, rowTitle, { class: 'axis-title', 'text-anchor': 'middle',
    transform: `rotate(-90 12 ${plot.y0 + rows.length * cellHeight / 2})` });

  rows.forEach((rowLabel, row_i) => cols.forEach((colLabel, col_i) => {
    const dead = isDead(row_i, col_i);
    const value = values[row_i][col_i];
    const cell = svgElement('rect', { x: plot.x0 + col_i * cellWidth + 1, y: plot.y0 + row_i * cellHeight + 1, width: cellWidth - 2,
      height: cellHeight - 2, rx: 2, class: dead ? 'cell-dead' : 'cell' }, svg);
    if (!dead) cell.style.fill = heatColor(value, maximum);
    cell.addEventListener('mousemove', event => showTip(event, dead
      ? `${rowTitle} ${rowLabel}, ${colTitle} ${colLabel}: not in the map`
      : `${rowTitle} ${rowLabel}, ${colTitle} ${colLabel}<br>${valueLabel}: ${formatNumber(value)}`));
    cell.addEventListener('mouseleave', hideTip);
    if (onCell && !dead) {
      cell.style.cursor = 'pointer';
      cell.addEventListener('click', () => onCell(row_i, col_i));
    }
  }));
  return maximum;
}

/** One hue, light to dark, mixed in the browser so both color schemes use their own ramp ends. */
function heatColor(value, maximum) {
  const fraction = maximum > 0 ? Math.sqrt(value / maximum) : 0;  // sqrt: a few hot channels must not flatten the rest
  return `color-mix(in oklab, var(--heat-hi) ${Math.round(fraction * 100)}%, var(--heat-lo))`;
}

export function heatmapScale(container, maximum, valueLabel) {
  container.replaceChildren();
  if (maximum <= 0) return;
  const low = document.createElement('span');
  low.textContent = '0';
  const ramp = document.createElement('span');
  ramp.className = 'ramp';
  const high = document.createElement('span');
  high.textContent = `${formatNumber(maximum)} ${valueLabel} (square-root scale)`;
  container.append(low, ramp, high);
}
