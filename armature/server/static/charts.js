// The scale helpers touch no DOM, so the node tests can load this module.
import { element, table } from './dom.js';

const SVG = 'http://www.w3.org/2000/svg';
const PAD = { left: 52, right: 12, top: 12, bottom: 24 };
const MIN_LABEL_GAP = 44;
const MAX_BAR = 28;
const GAP = 2;

export function niceMax(value) {
  if (!(value > 0)) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  for (const multiple of [1, 2, 2.5, 5, 10]) if (multiple * magnitude >= value - 1e-9) return multiple * magnitude;
  return 10 * magnitude;
}

export function ticks(max, count = 4) {
  return Array.from({ length: count + 1 }, (_, index) => (max * index) / count);
}

export function scale(max, size) {
  return (value) => size - (Math.max(0, Number(value) || 0) / (max || 1)) * size;
}

export function shortDay(isoDay) {
  const [, month, day] = String(isoDay).split('-');
  return `${Number(month)}/${Number(day)}`;
}

// Every nth day gets an axis label, so labels never collide however narrow the chart is.
export function labelEvery(count, width) {
  if (count <= 0) return 1;
  return Math.max(1, Math.ceil(count / Math.max(1, Math.floor(width / MIN_LABEL_GAP))));
}

export function stackTotals(length, series) {
  return Array.from({ length }, (_, index) => series.reduce((sum, item) => sum + (Number(item.values[index]) || 0), 0));
}

// One path per run of non-null points, so a day with no data leaves a gap instead of a false zero.
export function lineSegments(values, x, y) {
  const segments = [];
  let current = [];
  values.forEach((value, index) => {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) {
      if (current.length) segments.push(current);
      current = [];
      return;
    }
    current.push([x(index), y(value)]);
  });
  if (current.length) segments.push(current);
  return segments;
}

function svgNode(tag, attributes = {}, text = null) {
  const node = document.createElementNS(SVG, tag);
  for (const [name, value] of Object.entries(attributes)) if (value !== null && value !== undefined) node.setAttribute(name, String(value));
  if (text !== null) node.textContent = text;
  return node;
}

function axes(svgRoot, { days, width, height, max, tickFormat }) {
  const plotHeight = height - PAD.top - PAD.bottom;
  const y = scale(max, plotHeight);
  for (const tick of ticks(max)) {
    const top = PAD.top + y(tick);
    svgRoot.append(svgNode('line', { x1: PAD.left, x2: width - PAD.right, y1: top, y2: top, class: tick ? 'grid' : 'baseline' }));
    svgRoot.append(svgNode('text', { x: PAD.left - 6, y: top + 4, 'text-anchor': 'end', class: 'tick' }, tickFormat(tick)));
  }
  const band = (width - PAD.left - PAD.right) / Math.max(1, days.length);
  const every = labelEvery(days.length, width - PAD.left - PAD.right);
  days.forEach((day, index) => {
    if ((days.length - 1 - index) % every) return;
    const isLast = index === days.length - 1 && days.length > 1;
    svgRoot.append(svgNode('text', { x: isLast ? width - PAD.right : PAD.left + band * (index + 0.5), y: height - 6, 'text-anchor': isLast ? 'end' : 'middle', class: 'tick' }, shortDay(day)));
  });
  return { band, y, plotHeight };
}

function frame(width, height, label) {
  return svgNode('svg', { viewBox: `0 0 ${width} ${height}`, width, height, role: 'img', 'aria-label': label, class: 'chart-svg' });
}

// Series stack in order from the baseline. Each day's whole column is one hover target with a <title>.
export function stackedBars({ days, series, format = String, tickFormat = format, height = 200, label = '' }) {
  return (width) => {
    const max = niceMax(Math.max(0, ...stackTotals(days.length, series)));
    const svgRoot = frame(width, height, label);
    const { band, y, plotHeight } = axes(svgRoot, { days, width, height, max, tickFormat });
    const barWidth = Math.max(2, Math.min(MAX_BAR, band - GAP * 2));
    days.forEach((day, index) => {
      const left = PAD.left + band * index + (band - barWidth) / 2;
      let base = 0;
      const parts = [];
      for (const item of series) {
        const value = Number(item.values[index]) || 0;
        if (value <= 0) continue;
        const top = PAD.top + y(base + value);
        const bottom = PAD.top + y(base);
        const segmentHeight = Math.max(0, bottom - top - (base > 0 ? GAP : 0));
        svgRoot.append(svgNode('rect', { x: left, y: top, width: barWidth, height: segmentHeight, rx: Math.min(2, barWidth / 2), class: `f-${item.color}` }));
        base += value;
        parts.push(`${item.label} ${format(value)}`);
      }
      const hit = svgNode('rect', { x: PAD.left + band * index, y: PAD.top, width: band, height: plotHeight, class: 'hit' });
      hit.append(svgNode('title', {}, `${day}: ${parts.length ? parts.join(', ') : 'none'}`));
      svgRoot.append(hit);
    });
    return svgRoot;
  };
}

export function lines({ days, series, format = String, tickFormat = format, height = 200, label = '', max: fixedMax = null }) {
  return (width) => {
    const highest = Math.max(0, ...series.flatMap((item) => item.values.filter((value) => value !== null && value !== undefined).map(Number)));
    const max = fixedMax ?? niceMax(highest);
    const svgRoot = frame(width, height, label);
    const { band, y, plotHeight } = axes(svgRoot, { days, width, height, max, tickFormat });
    const x = (index) => PAD.left + band * (index + 0.5);
    const top = (value) => PAD.top + y(value);
    for (const item of series) {
      for (const segment of lineSegments(item.values, x, top)) {
        if (segment.length === 1) svgRoot.append(svgNode('circle', { cx: segment[0][0], cy: segment[0][1], r: 3, class: `f-${item.color}` }));
        else svgRoot.append(svgNode('path', { d: `M${segment.map(([left, upper]) => `${left.toFixed(1)},${upper.toFixed(1)}`).join('L')}`, class: `line st-${item.color}` }));
      }
    }
    days.forEach((day, index) => {
      const parts = series.map((item) => `${item.label} ${item.values[index] === null || item.values[index] === undefined ? 'no data' : format(item.values[index])}`);
      const hit = svgNode('rect', { x: PAD.left + band * index, y: PAD.top, width: band, height: plotHeight, class: 'hit' });
      hit.append(svgNode('title', {}, `${day}: ${parts.join(', ')}`));
      svgRoot.append(hit);
    });
    return svgRoot;
  };
}

// A titled figure with a legend, a text summary for screen readers, and the numbers as a table on request.
export function chartFigure({ title, summary, legend = [], headers, rows, draw, signal }) {
  const holder = element('div', { class: 'chart-plot' });
  const figure = element('figure', { class: 'chart card' },
    element('figcaption', {}, title),
    legend.length > 1 ? element('ul', { class: 'legend', role: 'list' }, legend.map((item) => element('li', {}, element('span', { class: `swatch sw-${item.color}`, 'aria-hidden': 'true' }), item.label))) : null,
    holder,
    element('p', { class: 'note chart-summary' }, summary),
    headers ? element('details', { class: 'chart-table' }, element('summary', {}, 'Show as a table'), table(headers, rows)) : null);
  let lastWidth = 0;
  const paint = () => {
    const width = Math.max(260, Math.floor(holder.clientWidth || 600));
    if (width === lastWidth) return;
    lastWidth = width;
    holder.replaceChildren(draw(width));
  };
  requestAnimationFrame(paint);
  if (typeof ResizeObserver === 'function') {
    const observer = new ResizeObserver(() => paint());
    observer.observe(holder);
    signal?.addEventListener('abort', () => observer.disconnect());
  }
  return figure;
}
