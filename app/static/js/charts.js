// charts.js — small SVG chart helpers.
//
// Hand-rolled rather than Plotly: no CDN, no 3 MB bundle, and the node works
// air-gapped. Every chart is an <svg role="img"> with an aria-label describing
// the shape of the data, because a screen reader gets nothing from the paths.

function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name);
  return (v && v.trim()) || fallback;
}

/* Read from the stylesheet on every access rather than frozen at import, so a
   theme change is picked up by the next redraw instead of needing a reload.
   The fallbacks are the original light-theme literals, so a missing token
   degrades to exactly what this file drew before. */
const C = {
  get accent()     { return cssVar('--accent',     '#2456C9'); },
  get accentSoft() { return cssVar('--accent-ink', '#7E99D8'); },
  get ok()         { return cssVar('--ok',         '#1E7A4C'); },
  get warn()       { return cssVar('--warn',       '#C77418'); },
  get grey()       { return cssVar('--ink-3',      '#9AA3AD'); },
  get axis()       { return cssVar('--line',       '#D3D7D0'); },
  get grid()       { return cssVar('--line-soft',  '#E6E8E3'); },
  get ink3()       { return cssVar('--ink-3',      '#5B6168'); },
  /* The end-of-line marker is a filled dot punched out of the chart ground,
     so it has to be the panel colour, not white. */
  get surface()    { return cssVar('--surface',    '#ffffff'); },
  /* Score ramp, owned by the stylesheet so the theme can reverse its
     lightness direction. Index 0 is the WEAK end in both themes. */
  get ramp() {
    const raw = cssVar('--ramp-seq', '');
    const parts = raw.split(',').map(x => x.trim()).filter(Boolean);
    return parts.length >= 2
      ? parts
      : ['#9AA3AD', '#7E93B8', '#5E7FBE', '#3A62BC', '#1F4CB0', '#12337F'];
  },
};

const NS = 'http://www.w3.org/2000/svg';

function el(name, attrs, text) {
  const n = document.createElementNS(NS, name);
  for (const k in attrs) if (attrs[k] != null) n.setAttribute(k, attrs[k]);
  if (text != null) n.textContent = text;
  return n;
}

function svgRoot(w, h, label) {
  const s = el('svg', {
    viewBox: '0 0 ' + w + ' ' + h,
    width: '100%', height: h,
    role: 'img', 'aria-label': label, class: 'chart'
  });
  return s;
}

function mount(host, svg) {
  const node = typeof host === 'string' ? document.getElementById(host) : host;
  if (!node) return null;
  node.replaceChildren(svg);
  return node;
}

function empty(host, message) {
  const node = typeof host === 'string' ? document.getElementById(host) : host;
  if (!node) return;
  const p = document.createElement('p');
  p.className = 'empty';
  p.textContent = message;
  node.replaceChildren(p);
}

function scale(domain, range) {
  const [d0, d1] = domain, [r0, r1] = range;
  const span = (d1 - d0) || 1;
  return v => r0 + ((v - d0) / span) * (r1 - r0);
}

function extent(values) {
  let lo = Infinity, hi = -Infinity;
  for (const v of values) {
    if (!isFinite(v)) continue;
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  if (!isFinite(lo)) return [0, 1];
  if (lo === hi) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.08;
  return [lo - pad, hi + pad];
}

/* ------------------------------------------------------------ line chart */
/* series: [{name, values:[n|null], color, dashed}]  xLabels: [string] */

export function renderLine(host, { series, xLabels, yLabel, label }) {
  const pts = series.flatMap(s => s.values).filter(v => isFinite(v));
  if (!pts.length) return empty(host, 'No data yet.');

  const W = 320, H = 170, L = 44, R = 10, T = 12, B = 36;
  const [lo, hi] = extent(pts);
  const x = scale([0, Math.max(1, xLabels.length - 1)], [L, W - R]);
  const y = scale([lo, hi], [H - B, T]);
  const svg = svgRoot(W, H, label || 'Line chart');

  // Lower (more negative) energies are better, so the axis is not inverted —
  // the reader is told which way is good by the label, not by a flipped axis.
  for (let i = 0; i <= 3; i++) {
    const v = lo + (hi - lo) * (i / 3);
    svg.append(el('line', { x1: L, y1: y(v), x2: W - R, y2: y(v), stroke: C.grid }));
    svg.append(el('text', {
      x: L - 6, y: y(v) + 3, 'text-anchor': 'end',
      'font-size': 9, fill: C.ink3, 'font-family': 'monospace'
    }, v.toFixed(0)));
  }
  svg.append(el('line', { x1: L, y1: T, x2: L, y2: H - B, stroke: C.axis }));
  svg.append(el('line', { x1: L, y1: H - B, x2: W - R, y2: H - B, stroke: C.axis }));

  series.forEach(s => {
    const pairs = s.values
      .map((v, i) => (isFinite(v) ? x(i).toFixed(1) + ',' + y(v).toFixed(1) : null))
      .filter(Boolean);
    if (pairs.length < 1) return;
    svg.append(el('polyline', {
      points: pairs.join(' '), fill: 'none',
      stroke: s.color || C.accent, 'stroke-width': s.dashed ? 2 : 2.5,
      'stroke-dasharray': s.dashed ? '4 3' : null, 'stroke-linejoin': 'round'
    }));
    const last = s.values.map((v, i) => [v, i]).filter(p => isFinite(p[0])).pop();
    if (last && !s.dashed) {
      svg.append(el('circle', {
        cx: x(last[1]), cy: y(last[0]), r: 4,
        fill: C.surface, stroke: s.color || C.accent, 'stroke-width': 2.5
      }));
    }
  });

  xLabels.forEach((t, i) => {
    if (xLabels.length > 8 && i % 2) return;
    svg.append(el('text', {
      x: x(i), y: H - B + 15, 'text-anchor': 'middle',
      'font-size': 9, fill: C.ink3
    }, t));
  });
  if (yLabel) {
    svg.append(el('text', {
      x: L, y: T - 2, 'font-size': 9, fill: C.ink3
    }, yLabel));
  }
  return mount(host, svg);
}

/* --------------------------------------------------------------- scatter */
/* points: [{x, y, flag}] — flag marks a point to distinguish (e.g. has NCAA) */

export function renderScatter(host, { points, xLabel, yLabel, label, trend }) {
  const ok = points.filter(p => isFinite(p.x) && isFinite(p.y));
  if (ok.length < 2) return empty(host, 'Not enough paired scores.');

  const W = 320, H = 170, L = 48, R = 10, T = 12, B = 36;
  const [xlo, xhi] = extent(ok.map(p => p.x));
  const [ylo, yhi] = extent(ok.map(p => p.y));
  const x = scale([xlo, xhi], [L, W - R]);
  const y = scale([ylo, yhi], [H - B, T]);
  const svg = svgRoot(W, H, label || 'Scatter plot');

  svg.append(el('line', { x1: L, y1: T, x2: L, y2: H - B, stroke: C.axis }));
  svg.append(el('line', { x1: L, y1: H - B, x2: W - R, y2: H - B, stroke: C.axis }));
  [ylo, yhi].forEach(v => svg.append(el('text', {
    x: L - 6, y: y(v) + 3, 'text-anchor': 'end',
    'font-size': 9, fill: C.ink3, 'font-family': 'monospace'
  }, v.toFixed(0))));

  if (trend) {
    svg.append(el('line', {
      x1: x(xlo), y1: y(trend.at(xlo)), x2: x(xhi), y2: y(trend.at(xhi)),
      stroke: C.axis, 'stroke-width': 1.5, 'stroke-dasharray': '4 3'
    }));
  }

  ok.forEach(p => svg.append(el('circle', {
    cx: x(p.x).toFixed(1), cy: y(p.y).toFixed(1), r: 3.2,
    fill: p.flag ? C.warn : C.accentSoft, opacity: 0.75
  })));

  if (xLabel) svg.append(el('text', {
    x: (L + W - R) / 2, y: H - 6, 'text-anchor': 'middle', 'font-size': 9, fill: C.ink3
  }, xLabel));
  if (yLabel) svg.append(el('text', { x: 4, y: T + 4, 'font-size': 9, fill: C.ink3 }, yLabel));
  return mount(host, svg);
}

/* --------------------------------------------------------------- heatmap */
/* labels: [string]  matrix: [[number]] — symmetric correlation, -1..1 */

export function renderHeatmap(host, { labels, matrix, label }) {
  if (!labels || !labels.length) return empty(host, 'No score terms available.');
  const node = typeof host === 'string' ? document.getElementById(host) : host;
  if (!node) return;

  const wrap = document.createElement('div');
  wrap.setAttribute('role', 'img');
  wrap.setAttribute('aria-label', label || 'Correlation matrix of energy terms');
  wrap.style.cssText = 'display:flex;flex-direction:column;gap:3px';

  // Diverging: blue positive, amber negative. Magnitude rides on alpha so the
  // two directions differ in lightness as well as hue.
  const cell = v => {
    const m = Math.min(1, Math.abs(v));
    const a = (0.12 + m * 0.78).toFixed(2);
    return v >= 0 ? 'rgba(36,86,201,' + a + ')' : 'rgba(199,116,24,' + a + ')';
  };

  matrix.forEach((row, ri) => {
    const line = document.createElement('div');
    line.style.cssText = 'display:flex;gap:3px;align-items:center';
    const lab = document.createElement('span');
    lab.textContent = labels[ri];
    lab.style.cssText = 'width:64px;flex:none;text-align:right;font-size:10.5px;' +
                        'color:' + C.ink3 + ';font-family:monospace;overflow:hidden';
    line.append(lab);
    row.forEach((v, ci) => {
      const c = document.createElement('span');
      c.style.cssText = 'flex:1 1 0;min-width:0;height:22px;border-radius:2px;background:' + cell(v);
      c.title = labels[ri] + ' · ' + labels[ci] + ' = ' + v.toFixed(2);
      line.append(c);
    });
    wrap.append(line);
  });

  const foot = document.createElement('div');
  foot.style.cssText = 'display:flex;gap:3px;align-items:center';
  const pad = document.createElement('span');
  pad.style.cssText = 'width:64px;flex:none';
  foot.append(pad);
  labels.forEach(l => {
    const s = document.createElement('span');
    s.textContent = l;
    s.style.cssText = 'flex:1 1 0;min-width:0;font-size:10px;color:' + C.ink3 +
                      ';font-family:monospace;text-align:center;overflow:hidden';
    foot.append(s);
  });
  wrap.append(foot);

  node.replaceChildren(wrap);
  return node;
}

/* ------------------------------------------------------------------ logo */
/* columns: [{pos, stack:[{code, freq, kind}]}] — kind '' | 'ncaa' | 'dres' */

/* Side-chain chemistry, for the logo's colouring. The three-letter code is
   still what identifies the residue; the class is what lets a column be read
   without reading it -- a position that settled on "something basic" looks
   like one colour even when the residues under it differ.
   GLY and PRO are their own class rather than hydrophobic: what matters about
   them is backbone behaviour, not side-chain polarity, and grouping them with
   LEU would say something false about a position. CYS is listed polar, which
   is the convention, but it is also the residue that forms the staples -- read
   a CYS column with that in mind rather than as ordinary polarity. */
const AA_CLASS = {
  ALA: 'hydro', VAL: 'hydro', LEU: 'hydro', ILE: 'hydro', MET: 'hydro',
  PHE: 'arom', TYR: 'arom', TRP: 'arom',
  SER: 'polar', THR: 'polar', CYS: 'polar', ASN: 'polar', GLN: 'polar',
  LYS: 'basic', ARG: 'basic', HIS: 'basic',
  ASP: 'acid', GLU: 'acid',
  GLY: 'special', PRO: 'special',
};

export function renderLogo(host, columns) {
  if (!columns || !columns.length) return empty(host, 'No sequences to summarise.');
  const node = typeof host === 'string' ? document.getElementById(host) : host;
  if (!node) return;

  const wrap = document.createElement('div');
  /* The class scopes the chemistry colours to this figure. .res is shared with
     the Results view's sequence strips, where recolouring every residue by
     polarity is a separate decision nobody has made. */
  wrap.className = 'logo';
  wrap.style.cssText = 'display:flex;gap:6px;align-items:flex-end;overflow-x:auto;padding-bottom:4px';

  columns.forEach(col => {
    const c = document.createElement('div');
    c.style.cssText = 'flex:1 1 0;min-width:46px;display:flex;flex-direction:column;' +
                      'justify-content:flex-end;gap:2px';
    col.stack.forEach(s => {
      const b = document.createElement('span');
      /* kind wins over chemistry. A non-canonical is the thing the NCAA pass
         exists to surface, so it keeps its own colour rather than being
         absorbed into whichever class its side chain resembles. A canonical
         with no entry in the table gets no class and falls back to the plain
         residue style -- better unmarked than wrongly grouped. */
      const cls = AA_CLASS[String(s.code || '').toUpperCase()];
      b.className = 'res' + (s.kind ? ' ' + s.kind : (cls ? ' aa-' + cls : ''));
      b.textContent = s.code;
      b.title = s.code + ' · ' + Math.round(s.freq) + '%';
      /* Height already carries frequency; weight carries it a second time, so
         a column reads at a glance rather than by comparing block heights
         across the row. A residue the search almost fixed looks solid; one it
         tried twice looks faint.

         A non-canonical keeps full weight whatever its frequency. It is
         usually rare BY CONSTRUCTION -- the palette offers it at a handful of
         positions -- and it is the residue the NCAA pass exists to find, so
         fading it in proportion to how seldom it appears would hide exactly
         the thing the figure is for. */
      /* Floor at 0.62, not 0. Frequency is already carried by height, so this
         is a second, weaker cue -- and a rare residue faded to near-invisible
         stops being readable at all, which costs more than the cue gains. */
      const faint = s.kind ? 1 : 0.62 + 0.38 * Math.min(1, Math.max(0, s.freq / 100));
      b.style.cssText = 'display:grid;place-items:center;overflow:hidden;height:' +
                        Math.max(16, Math.round(s.freq * 0.92)) + 'px;' +
                        'opacity:' + faint.toFixed(3);
      c.append(b);
    });
    const p = document.createElement('span');
    p.textContent = col.pos;
    p.style.cssText = 'border-top:1px solid #DCDFD8;padding-top:4px;font-size:10.5px;' +
                      'color:' + C.ink3 + ';font-family:monospace;text-align:center';
    c.append(p);
    wrap.append(c);
  });

  /* A key, because a colour nobody can decode is decoration. Only the classes
     actually present are listed: a peptide with no acidic residue should not
     have the reader looking for red. */
  const present = new Set();
  columns.forEach(col => col.stack.forEach(s => {
    if (s.kind) { present.add(s.kind); return; }
    const c = AA_CLASS[String(s.code || '').toUpperCase()];
    if (c) present.add(c);
  }));
  const LABELS = [
    ['hydro', 'hydrophobic'], ['arom', 'aromatic'], ['polar', 'polar'],
    ['basic', 'basic'], ['acid', 'acidic'], ['special', 'Gly / Pro'],
    ['ncaa', 'non-canonical'], ['dres', 'D-residue'],
  ];
  const key = document.createElement('div');
  key.className = 'logo-key';
  LABELS.filter(([k]) => present.has(k)).forEach(([k, label]) => {
    const item = document.createElement('span');
    const sw = document.createElement('span');
    sw.className = 'res ' + (k === 'ncaa' || k === 'dres' ? k : 'aa-' + k);
    sw.textContent = '  ';
    item.append(sw, document.createTextNode(' ' + label));
    key.append(item);
  });

  const outer = document.createElement('div');
  outer.append(wrap);
  if (key.childElementCount) outer.append(key);
  node.replaceChildren(outer);
  return node;
}

/* ------------------------------------------------------------------ tree */
/* TMAP output: x[], y[], s[], t[] (edge index pairs), values[] for colour */

export function renderTree(host, { x, y, s, t, values, label }) {
  if (!x || x.length < 2) return empty(host, 'No tree layout available.');

  const W = 320, H = 220, P = 12;
  const [xlo, xhi] = extent(x), [ylo, yhi] = extent(y);
  const sx = scale([xlo, xhi], [P, W - P]);
  const sy = scale([ylo, yhi], [H - P, P]);
  const svg = svgRoot(W, H, label || 'Sequence similarity tree');

  if (s && t && s.length === t.length) {
    const d = [];
    for (let i = 0; i < s.length; i++) {
      const a = s[i], b = t[i];
      if (x[a] == null || x[b] == null) continue;
      d.push('M' + sx(x[a]).toFixed(1) + ' ' + sy(y[a]).toFixed(1) +
             'L' + sx(x[b]).toFixed(1) + ' ' + sy(y[b]).toFixed(1));
    }
    svg.append(el('path', { d: d.join(''), stroke: C.axis, 'stroke-width': 1.2, fill: 'none' }));
  }

  // Colour by score where available: light grey (weak) to deep blue (strong).
  const vals = (values || []).filter(isFinite);
  const [vlo, vhi] = vals.length ? extent(vals) : [0, 1];
  const ramp = C.ramp;
  const colourFor = v => {
    if (!isFinite(v)) return C.grey;
    const f = (v - vlo) / ((vhi - vlo) || 1);
    return ramp[Math.min(ramp.length - 1, Math.max(0, Math.round((1 - f) * (ramp.length - 1))))];
  };

  for (let i = 0; i < x.length; i++) {
    svg.append(el('circle', {
      cx: sx(x[i]).toFixed(1), cy: sy(y[i]).toFixed(1), r: 3.2,
      fill: colourFor(values ? values[i] : NaN)
    }));
  }
  return mount(host, svg);
}

/* Least-squares fit, for the agreement plot's trend line. */
export function linearFit(points) {
  const ok = points.filter(p => isFinite(p.x) && isFinite(p.y));
  const n = ok.length;
  if (n < 2) return null;
  let sx = 0, sy = 0, sxy = 0, sxx = 0, syy = 0;
  for (const p of ok) { sx += p.x; sy += p.y; sxy += p.x * p.y; sxx += p.x * p.x; syy += p.y * p.y; }
  const denom = n * sxx - sx * sx;
  if (!denom) return null;
  const m = (n * sxy - sx * sy) / denom;
  const b = (sy - m * sx) / n;
  const r = (n * sxy - sx * sy) /
            Math.sqrt(Math.max(1e-12, (n * sxx - sx * sx) * (n * syy - sy * sy)));
  return { at: v => m * v + b, slope: m, intercept: b, r };
}

export { C as chartColors };
