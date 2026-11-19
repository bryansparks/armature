// The trend chart: hand-drawn SVG lines with a crosshair, a keyboard path, end labels, and a table view.
import { element } from '../../dom.js';
import { lineSegments, niceMax, ticks } from '../../charts.js';
import { fixed, isNum, monthDay, short, tickIndexes } from './format.js';
import { svgEl, whenSized } from './ui.js';

const seriesClass = (item) => (item.slot === 'median' ? 'median' : `s${item.slot}`);
const fillClass = (item) => (item.slot === 'median' ? 'median' : `f${item.slot}`);

function legend(series, label) {
  return element('ul', { class: 'in-legend', 'aria-label': label }, series.map((item) => {
    const key = element('span', { class: `in-key ${item.slot === 'median' ? 'in-key-median' : fillClass(item)}`, 'aria-hidden': 'true' });
    return element('li', {}, item.href ? element('a', { href: item.href }, key, item.name) : [key, item.name]);
  }));
}

// The y axis for a largest value: a round top with 4 or 5 evenly spaced, round ticks.
export function yAxis(max) {
  const top = niceMax((max > 0 ? max : 1) * 1.08);
  const lead = top / 10 ** Math.floor(Math.log10(top));
  const values = ticks(top, lead === 2 ? 4 : 5);
  const step = values[1] - values[0];
  const digits = step >= 1 ? 0 : step >= 0.1 ? 1 : 2;
  return { max: top, ticks: values, labels: values.map((value) => (value === 0 ? '0' : fixed(value, digits))) };
}

// series: [{ name, slot (1-8 or 'median'), values, href }]. format turns a value into tooltip and table text.
export function trendChart({ dates, series, label, basis, format, legendLabel = 'Series', tickFormat = null, dateFormat = (iso) => monthDay(iso, false) }) {
  const plot = element('div', { class: 'in-plot' });
  const live = element('p', { class: 'visually-hidden', 'aria-live': 'polite' });
  const rows = dates.map((iso, index) => element('tr', {}, element('td', {}, dateFormat(iso)),
    series.map((item) => element('td', { class: 'num' }, format(item.values[index]))))).reverse();
  const numbers = element('details', { class: 'in-numbers' }, element('summary', {}, 'Show the numbers'),
    element('div', { class: 'in-table-scroll' }, element('table', { class: 'in-table' },
      element('caption', { class: 'visually-hidden' }, `${label} by date`),
      element('thead', {}, element('tr', {}, element('th', { scope: 'col' }, 'Date'), series.map((item) => element('th', { scope: 'col', class: 'num' }, item.name)))),
      element('tbody', {}, rows))));
  const node = element('div', { class: 'in-chart' },
    element('div', { class: 'in-chart-meta' }, series.length > 1 ? legend(series, legendLabel) : element('span'), basis ? element('span', { class: 'in-basis' }, basis) : null),
    plot, live, numbers);
  whenSized(plot, (width) => draw(width));
  let hoverIndex = null;

  function draw(width) {
    while (plot.firstChild) plot.firstChild.remove();
    const count = dates.length;
    const narrow = width < 560;
    const height = narrow ? 220 : 280;
    const m = { top: 12, right: 44, bottom: 30, left: 40 };
    const innerW = width - m.left - m.right;
    const innerH = height - m.top - m.bottom;
    let max = 0;
    for (const item of series) for (const value of item.values) if (isNum(value)) max = Math.max(max, Number(value));
    const axis = yAxis(max);
    const yMax = axis.max;
    const x = (i) => m.left + (count > 1 ? (i * innerW) / (count - 1) : innerW / 2);
    const y = (v) => m.top + innerH * (1 - Number(v) / yMax);
    const svg = svgEl('svg', {
      width, height, viewBox: `0 0 ${width} ${height}`, tabindex: 0, role: 'group',
      'aria-label': `${label}${basis ? `, ${basis.toLowerCase()}` : ''}. Use the left and right arrow keys to read each point.`,
    });
    plot.append(svg);
    const capture = svgEl('rect', { x: m.left, y: m.top, width: innerW + 12, height: innerH, fill: 'transparent' });
    svg.append(capture);
    axis.ticks.forEach((t, n) => {
      const ty = Math.round(y(t)) + 0.5;
      svg.append(svgEl('line', { class: t === 0 ? 'base' : 'grid', x1: m.left, x2: width - m.right + 8, y1: ty, y2: ty }));
      svg.append(svgEl('text', { class: 'tick', x: m.left - 10, y: ty + 4, 'text-anchor': 'end' }, document.createTextNode(tickFormat ? tickFormat(t) : axis.labels[n])));
    });
    const dateTicks = tickIndexes(count, narrow ? 3 : count <= 8 ? count : 5);
    dateTicks.forEach((i, n) => {
      const anchor = dateTicks.length === 1 ? 'middle' : n === 0 ? 'start' : n === dateTicks.length - 1 ? 'end' : 'middle';
      svg.append(svgEl('text', { class: 'tick', x: x(i), y: height - 8, 'text-anchor': anchor }, document.createTextNode(dateFormat(dates[i]))));
    });
    const ends = [];
    for (const item of series) {
      const segments = lineSegments(item.values, x, y);
      const d = segments.map((points) => points.map(([px, py], i) => `${i ? 'L' : 'M'}${px.toFixed(1)} ${py.toFixed(1)}`).join(' ')).join(' ');
      if (!d) continue;
      const group = item.href ? svgEl('a', { class: 'series-link', href: item.href, tabindex: -1, 'aria-hidden': 'true' }) : svgEl('g');
      if (item.href) group.append(svgEl('path', { d, stroke: 'transparent', 'stroke-width': 14, fill: 'none' }));
      group.append(svgEl('path', { class: `series ${seriesClass(item)}`, d }));
      for (const points of segments) if (points.length === 1) group.append(svgEl('circle', { class: fillClass(item), cx: points[0][0], cy: points[0][1], r: 2 }));
      svg.append(group);
      let lastIndex = -1;
      item.values.forEach((value, i) => { if (isNum(value)) lastIndex = i; });
      if (lastIndex >= 0) ends.push({ item, x: x(lastIndex), y: y(item.values[lastIndex]), value: item.values[lastIndex] });
    }
    ends.sort((a, b) => a.y - b.y);
    let previous = -Infinity;
    for (const end of ends) {
      end.labelY = Math.max(end.y, previous + 14);
      previous = end.labelY;
    }
    for (const end of ends) {
      svg.append(svgEl('circle', { class: `end ${fillClass(end.item)}`, cx: end.x, cy: end.y, r: 4 }));
      svg.append(svgEl('text', { class: 'end-label', x: end.x + 10, y: end.labelY + 4 }, document.createTextNode(short(end.value, '', end.value % 1 ? 1 : 0))));
    }
    const hover = svgEl('g', { visibility: 'hidden' });
    const cross = svgEl('line', { class: 'cross', y1: m.top, y2: m.top + innerH });
    hover.append(cross);
    const dots = series.map((item) => {
      const dot = svgEl('circle', { class: `hover-dot ${fillClass(item)}`, r: 4.5 });
      hover.append(dot);
      return dot;
    });
    svg.append(hover);
    const tip = element('div', { class: 'in-tip', hidden: true, 'aria-hidden': 'true' });
    plot.append(tip);

    function show(i, announce = false) {
      hoverIndex = i;
      const cx = Math.round(x(i)) + 0.5;
      cross.setAttribute('x1', cx);
      cross.setAttribute('x2', cx);
      series.forEach((item, n) => {
        const value = item.values[i];
        dots[n].setAttribute('visibility', isNum(value) ? 'visible' : 'hidden');
        if (isNum(value)) {
          dots[n].setAttribute('cx', x(i));
          dots[n].setAttribute('cy', y(value));
        }
      });
      hover.setAttribute('visibility', 'visible');
      const shown = series.map((item) => ({ item, value: item.values[i] }))
        .sort((a, b) => (isNum(b.value) ? Number(b.value) : -Infinity) - (isNum(a.value) ? Number(a.value) : -Infinity));
      while (tip.firstChild) tip.firstChild.remove();
      tip.append(element('div', { class: 'in-tip-date' }, dateFormat(dates[i])), shown.map(({ item, value }) => element('div', { class: 'in-tip-row' },
        element('span', { class: `in-key ${item.slot === 'median' ? 'in-key-median' : fillClass(item)}` }), element('span', {}, item.name), element('b', {}, format(value)))));
      tip.hidden = false;
      const tipW = tip.offsetWidth;
      let left = x(i) + 14;
      if (left + tipW > width) left = x(i) - 14 - tipW;
      tip.style.left = `${Math.max(0, left)}px`;
      tip.style.top = `${m.top}px`;
      if (announce) live.textContent = `${dateFormat(dates[i])}: ${shown.map(({ item, value }) => `${item.name} ${format(value)}`).join(', ')}`;
    }
    function hide() {
      hoverIndex = null;
      hover.setAttribute('visibility', 'hidden');
      tip.hidden = true;
    }
    svg.addEventListener('pointermove', (moveEvent) => {
      const rect = svg.getBoundingClientRect();
      const px = moveEvent.clientX - rect.left;
      if (px < m.left - 8 || px > width - m.right + 12 || count < 1) {
        hide();
        return;
      }
      const i = count > 1 ? Math.round(((px - m.left) / innerW) * (count - 1)) : 0;
      show(Math.max(0, Math.min(count - 1, i)));
    });
    svg.addEventListener('pointerleave', () => { if (document.activeElement !== svg) hide(); });
    svg.addEventListener('focus', () => show(hoverIndex === null ? count - 1 : hoverIndex, true));
    svg.addEventListener('blur', hide);
    svg.addEventListener('keydown', (keyEvent) => {
      let i = hoverIndex === null ? count - 1 : hoverIndex;
      if (keyEvent.key === 'ArrowLeft') i = Math.max(0, i - 1);
      else if (keyEvent.key === 'ArrowRight') i = Math.min(count - 1, i + 1);
      else if (keyEvent.key === 'Home') i = 0;
      else if (keyEvent.key === 'End') i = count - 1;
      else if (keyEvent.key === 'Escape') { hide(); return; }
      else return;
      keyEvent.preventDefault();
      show(i, true);
    });
  }
  return node;
}

// A y axis for small counts: whole-number ticks up to 6, else yAxis.
export function countAxis(max) {
  const top = Math.max(1, Math.ceil(max));
  if (top > 6) return yAxis(max);
  const values = Array.from({ length: top + 1 }, (_, index) => index);
  return { max: top + 0.25, ticks: values, labels: values.map(String) };
}

// A dot plot of two measures, one dot per item and series: x and y are numbers, the labels name each axis.
// series: [{ name, slot, points: [{ x, y, label }] }]. The numbers table under it is the accessible view.
export function dotPlot({ series, label, xLabel, yLabel, xFormat, yFormat }) {
  const plot = element('div', { class: 'in-plot' });
  const all = series.flatMap((item) => item.points.map((point) => ({ ...point, item })))
    .filter((point) => isNum(point.x) && isNum(point.y));
  const labels = [...new Set(all.map((point) => point.label))];
  const valueOf = (item, name, key) => item.points.find((point) => point.label === name)?.[key];
  const numbers = element('details', { class: 'in-numbers' }, element('summary', {}, 'Show the numbers'),
    element('div', { class: 'in-table-scroll' }, element('table', { class: 'in-table' },
      element('caption', { class: 'visually-hidden' }, label),
      element('thead', {}, element('tr', {}, element('th', { scope: 'col' }, 'Pull request'), series.map((item) => [
        element('th', { scope: 'col', class: 'num' }, `${item.name}: ${xLabel}`),
        element('th', { scope: 'col', class: 'num' }, `${item.name}: ${yLabel}`)]))),
      element('tbody', {}, labels.map((name) => element('tr', {}, element('td', {}, name), series.map((item) => [
        element('td', { class: 'num' }, xFormat(valueOf(item, name, 'x'))),
        element('td', { class: 'num' }, yFormat(valueOf(item, name, 'y')))])))))));
  const node = element('div', { class: 'in-chart' },
    element('div', { class: 'in-chart-meta' }, legend(series, 'Series'), element('span', { class: 'in-basis' }, `${yLabel} against ${xLabel.toLowerCase()}`)),
    plot, numbers);
  whenSized(plot, (width) => {
    while (plot.firstChild) plot.firstChild.remove();
    const height = width < 420 ? 200 : 240;
    const m = { top: 12, right: 16, bottom: 34, left: 40 };
    const innerW = width - m.left - m.right;
    const innerH = height - m.top - m.bottom;
    const xAxis = yAxis(Math.max(0, ...all.map((point) => Number(point.x))));
    const yAxisFor = countAxis(Math.max(0, ...all.map((point) => Number(point.y))));
    const x = (v) => m.left + (innerW * Number(v)) / xAxis.max;
    const y = (v) => m.top + innerH * (1 - Number(v) / yAxisFor.max);
    const svg = svgEl('svg', { width, height, viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': `${label}. The table under the chart lists every point.` });
    yAxisFor.ticks.forEach((t, n) => {
      const ty = Math.round(y(t)) + 0.5;
      svg.append(svgEl('line', { class: t === 0 ? 'base' : 'grid', x1: m.left, x2: width - m.right, y1: ty, y2: ty }));
      svg.append(svgEl('text', { class: 'tick', x: m.left - 10, y: ty + 4, 'text-anchor': 'end' }, document.createTextNode(yAxisFor.labels[n])));
    });
    xAxis.ticks.forEach((t, n) => {
      const anchor = n === 0 ? 'start' : n === xAxis.ticks.length - 1 ? 'end' : 'middle';
      svg.append(svgEl('text', { class: 'tick', x: x(t), y: height - 14, 'text-anchor': anchor }, document.createTextNode(xFormat(t))));
    });
    for (const point of all) {
      svg.append(svgEl('circle', { class: `dot ${fillClass(point.item)}`, cx: x(point.x), cy: y(point.y), r: 4.5 },
        svgEl('title', {}, document.createTextNode(`${point.label}, ${point.item.name}: ${xFormat(point.x)}, ${yFormat(point.y)}`))));
    }
    plot.append(svg);
  });
  return node;
}

// Gridlines only, while the data loads.
export function chartSkeleton() {
  return element('div', { class: 'in-skel-plot', 'aria-hidden': 'true' });
}
