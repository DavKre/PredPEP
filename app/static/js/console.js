/* Analytics console layout.
   ------------------------------------------------------------------------
   Arranges the eight figures as an instrument: a computed readout strip, a
   rail of live thumbnails, one focused figure, and an explanation on demand.

   This module moves figure NODES. It never draws one. Every figure keeps the
   DOM id its draw function in analytics.js already targets, so not one of
   those functions changes, and a figure that fails to draw still has a slot.

   Plotly measures its container, so a chart in a display:none box comes out
   zero-pixels wide. Nothing here is ever hidden: the thumbnails are small,
   not absent, and every move is followed by a resize so the chart refills
   whichever box it has landed in. */

const POOL = 'figPool';
const RAIL = 'figRail';
const FOCUS = 'figFocus';
const PIN = 'figPinned';

/* Figures that never enter the rail: they sit under the focused figure, always
   visible. Position preference is context for whatever map is in focus -- which
   residue won each position, read WHILE looking at where the designs sit -- so
   making it a thing you switch TO means switching away from the figure it
   annotates. Everything else is an alternative view of the same designs and
   belongs in the rail. */
const PINNED = new Set(['logo']);

const $ = id => document.getElementById(id);

/* Explanations. "shows" is static — what the figure is, true of any job.
   "caveat" is what makes it misread.

   There is deliberately no stored "conclusion" here. A conclusion is computed
   from the job in conclusionFor() below, and where it cannot be computed the
   panel says so and stops. A canned finding with a number dropped into it
   reads exactly like a real one and nobody can tell the difference, which is
   worse than no finding at all. */
const EXPLAIN = {
  evolution: {
    shows: 'Best and median ΔΔG for every design in each iteration. '
         + 'The gap between the two lines is the spread of the population: if '
         + 'only the best line falls, one design improved; if both fall, the '
         + 'whole search moved.',
    caveat: 'The ΔΔG scorer cannot handle a non-canonical residue — it drops the residue '
          + 'and returns a value for the rest of the peptide. Designs carrying '
          + 'one are therefore not comparable on this axis.',
    with: ['agreement'],
  },
  agreement: {
    shows: 'Each design placed by its ΔΔG against its interface '
         + 'score. Two independent estimates of the same thing: where they '
         + 'disagree, at least one is wrong about that design.',
    caveat: 'Only designs with both scores appear. Anything the ΔΔG scorer could not '
          + 'evaluate is absent from the plot entirely rather than shown as '
          + 'an outlier.',
    with: ['evolution'],
  },
  heatmap: {
    shows: 'Pearson correlation between the energy terms across the '
         + 'designs. Terms that move together are measuring overlapping '
         + 'physics; a term that correlates with nothing is carrying its own '
         + 'signal.',
    caveat: 'Correlation across designs in one job, not a general property of '
          + 'the score function.',
    with: ['pca'],
  },
  pca: {
    shows: 'Every design placed by its energy score vector, projected onto '
         + 'the first two principal components. Points near each other scored '
         + 'alike across all terms, not merely on the total.',
    caveat: 'The projection is fitted on this job alone, so PC1 here and PC1 '
          + 'in another job are different axes and the positions cannot be '
          + 'compared between jobs.',
    with: ['pca3d', 'seqmap'],
  },
  pca3d: {
    shows: 'The same score-space projection carried to three components, '
         + 'which separates groups that overlap in two.',
    caveat: 'Fitted per job, as above. Depth is a projection: two points that '
          + 'look adjacent may be far apart along the axis into the screen.',
    with: ['pca', 'seqmap'],
  },
  seqmap: {
    shows: 'Designs placed by sequence similarity (MHFP fingerprints, '
         + 'classical MDS) rather than by score. Read it against score space: '
         + 'a group that clusters in both is one family, a group that clusters '
         + 'on score but scatters here scores alike for unrelated reasons.',
    caveat: 'Sequence distance, not structural distance. Two peptides can '
          + 'differ at many positions and still adopt the same backbone.',
    with: ['pca3d', 'tmap'],
  },
  logo: {
    shows: 'How often each residue appears at each position among the top '
         + 'designs. A tall single block is a position the search fixed; an '
         + 'even stack is one it never decided.',
    caveat: 'Built from three-letter codes on purpose. A one-letter logo '
          + 'collapses every non-canonical to X, which hides the residues the '
          + 'NCAA pass exists to find.',
    with: ['seqmap'],
  },
  tmap: {
    shows: 'Designs as a tree, joined by sequence similarity. Branches are '
         + 'families; a long isolated branch is a design unlike anything else '
         + 'the search produced.',
    caveat: 'The layout is an embedding. Branch LENGTH carries similarity; '
          + 'the angle a branch happens to take does not.',
    with: ['seqmap'],
  },
};

const state = {
  figures: [],      // [{key, name, node}] -- the rail, switchable
  pinned: [],       // [{key, name, node}] -- always on screen, below the focus
  focus: null,
  explainOpen: false,
  /* Which figure the explanation describes. null means "whatever is focused",
     which is the normal case; a key here means the reader opened the panel
     from a pinned figure's own ?, and it stays on that figure until they open
     it from the focus again. Without this the panel would silently describe
     something other than the ? they pressed. */
  explainKey: null,
  conclusion: () => null,
  onFocus: null,
};

/* Both lists. Cross-links in EXPLAIN and the panel lookup must see pinned
   figures too, or a `with` entry naming one would silently vanish. */
const allFigures = () => state.figures.concat(state.pinned);

/* ------------------------------------------------------------------ layout */

function thumbButton(fig) {
  const b = document.createElement('button');
  b.type = 'button';
  b.dataset.fig = fig.key;
  b.setAttribute('role', 'tab');
  b.className = 'fig-thumb';
  b.innerHTML = `<span class="fig-thumb__name">${fig.name}</span>`
              + `<span class="fig-thumb__slot"></span>`;
  return b;
}

/* The design box the thumbnail figure is laid out at before being scaled down.
   Wide enough that Plotly's margins leave a real plot area; the slot itself is
   too narrow for that. Kept in step with the .fig-thumb__slot rule. */
const THUMB_W = 420;

/* Renamed from 'predpep-analytics-explain'. See setExplain: the old name holds
   a '0' written by the old build's own init for everyone who loaded it. */
const EXPLAIN_KEY = 'predpep-explain-2';

/* Fit the figure to its slot. The slot's width is whatever the rail is, which
   changes with the viewport, so the factor is computed rather than written
   into the stylesheet. */
function fitThumb(slot) {
  if (!slot) return;
  const w = slot.clientWidth;
  if (w > 0) slot.style.setProperty('--thumb-scale', (w / THUMB_W).toFixed(4));
}

/* Plotly sizes to the container it is in, so every move is followed by a
   resize. Guarded: a figure that failed to draw has no Plotly state, and
   asking for a resize on it throws. */
function resize(node) {
  if (!node || !window.Plotly) return;
  node.querySelectorAll('.js-plotly-plot').forEach(p => {
    try { window.Plotly.Plots.resize(p); } catch (e) { /* not a live plot */ }
  });
}

/* Keeping a thumbnail readable while its figure is away ---------------------

   focus() MOVES the figure node into #figFocus -- there is one Plotly instance
   per figure and that is deliberate: eight live instances of a 4MB library is
   not a trade worth making for a 176x63 preview. So the slot the figure came
   from is genuinely empty, and it used to be collapsed to hide that.

   Instead, leave a still image of the figure behind. Plotly can hand us a PNG
   of exactly what it drew, so the thumbnail keeps showing what the reader just
   clicked, the rail stops jumping, and nothing is rendered twice. */

function clearStill(slot) {
  if (!slot) return;
  slot.querySelectorAll('.fig-thumb__still').forEach(n => n.remove());
  const btn = slot.closest('.fig-thumb');
  if (btn) btn.classList.remove('fig-thumb--blank');
}

function leaveStill(fig, slot) {
  if (!slot) return;
  const btn = slot.closest('.fig-thumb');
  const blank = () => { if (btn) btn.classList.add('fig-thumb--blank'); };
  const plot = fig.node && fig.node.querySelector('.js-plotly-plot');

  // A figure that never drew, or one that is not a Plotly plot at all, has no
  // image to give. Collapse that slot rather than leave an empty bordered box,
  // which reads as a figure that failed.
  if (!plot || !window.Plotly || !window.Plotly.toImage) { blank(); return; }

  window.Plotly.toImage(plot, { format: 'png', width: THUMB_W, height: 150 })
    .then(url => {
      // The reader may have clicked elsewhere while this was rendering, in
      // which case the live node is already back and must not be covered.
      if (state.focus !== fig.key) return;
      clearStill(slot);
      const img = document.createElement('img');
      img.className = 'fig-thumb__still';
      img.alt = '';                 // decorative: the name is in the button
      img.src = url;
      slot.append(img);
    })
    .catch(blank);
}

export function focus(key) {
  const fig = state.figures.find(f => f.key === key);
  if (!fig) return;

  // Return whatever is in the focus slot to its own rail thumbnail.
  const focusSlot = $(FOCUS);
  if (state.focus && state.focus !== key) {
    const prev = state.figures.find(f => f.key === state.focus);
    const slot = $(RAIL).querySelector(`[data-fig="${state.focus}"] .fig-thumb__slot`);
    if (prev && slot) { clearStill(slot); slot.append(prev.node); fitThumb(slot); resize(prev.node); }
  }

  if (focusSlot) { focusSlot.append(fig.node); resize(fig.node); }
  state.focus = key;

  // The slot this figure just left keeps a still of it, so the item the reader
  // clicked goes on showing its preview instead of collapsing.
  leaveStill(fig, $(RAIL).querySelector(`[data-fig="${key}"] .fig-thumb__slot`));

  $(RAIL).querySelectorAll('.fig-thumb').forEach(b => {
    const on = b.dataset.fig === key;
    b.setAttribute('aria-selected', String(on));
    b.classList.toggle('fig-thumb--on', on);
  });

  const title = $('figFocusTitle');
  if (title) title.textContent = fig.name;

  // Only re-render when the panel is following the focus. If the reader opened
  // it from the pinned figure's ?, switching maps must not swap the subject
  // out from under them.
  if (state.explainOpen && !state.explainKey) renderExplain();
  if (state.onFocus) state.onFocus(key);
}

/* ---------------------------------------------------------------- explain */

function section(label, body) {
  return `<div${label === 'What it shows' ? '' : ' style="border-top:1px solid var(--line-soft);padding-top:14px"'}>`
       + `<div class="stat-label" style="margin-bottom:6px">${label}</div>`
       + `<p class="chart-note">${body}</p></div>`;
}

function renderExplain() {
  const body = $('figExplainBody');
  const panel = $('figExplain');
  if (!body || !panel) return;
  const key = state.explainKey || state.focus;
  const fig = allFigures().find(f => f.key === key);
  const meta = fig && EXPLAIN[fig.key];
  if (!meta) { body.innerHTML = '<p class="chart-note">No description for this figure.</p>'; return; }

  const t = $('figExplainTitle');
  if (t) t.textContent = fig.name;

  let html = section('What it shows', meta.shows);

  /* The computed part. conclusionFor returns null when the job does not carry
     what the sentence would need, and the panel then says which figure to
     read instead of asserting a finding. */
  const said = state.conclusion(fig.key);
  html += `<div style="border-top:1px solid var(--line-soft);padding-top:14px">`
        + `<div class="stat-label" style="margin-bottom:6px">What it says here</div>`
        + (said
            ? `<p class="chart-note" style="color:var(--ink-2)">${said}</p>`
            : `<p class="chart-note">Nothing is computed for this figure yet, `
              + `so there is no finding to state. Read the figure itself.</p>`)
        + `</div>`;

  const links = (meta.with || [])
    .map(k => allFigures().find(f => f.key === k))
    .filter(Boolean);
  if (links.length) {
    html += `<div style="border-top:1px solid var(--line-soft);padding-top:14px">`
          + `<div class="stat-label" style="margin-bottom:6px">Read with</div>`
          + `<div style="display:flex;flex-direction:column;gap:6px">`
          + links.map(f => `<button class="btn-sm" data-goto-fig="${f.key}"`
              + ` style="justify-content:flex-start">${f.name}</button>`).join('')
          + `</div></div>`;
  }

  html += section('Caveat', meta.caveat);
  body.innerHTML = html;
}

/* `remember` is false when the caller is restoring state rather than acting on
   a choice. It matters: the previous version wrote to localStorage on EVERY
   call including the one in init(), so simply loading the page stored "closed"
   -- and a later default of "open on a first visit" then never fired, because
   the key was not absent, it held a '0' nobody had chosen. The key is also
   renamed, so those auto-written values do not go on suppressing the panel for
   everyone who ever loaded the old build. */
export function setExplain(open, key, remember) {
  state.explainOpen = !!open;
  // undefined = leave the subject alone; null = follow the focused figure.
  if (key !== undefined) state.explainKey = key;
  if (!state.explainOpen) state.explainKey = null;
  const panel = $('figExplain');
  const btn = $('figExplainToggle');
  const pinBtn = $('figPinnedExplain');
  if (panel) panel.hidden = !state.explainOpen;
  // Each ? reports whether the panel is currently showing ITS figure, so two
  // buttons controlling one panel do not both claim to be expanded.
  if (btn) btn.setAttribute('aria-expanded',
    String(state.explainOpen && !state.explainKey));
  if (pinBtn) pinBtn.setAttribute('aria-expanded',
    String(state.explainOpen && PINNED.has(state.explainKey)));
  if (state.explainOpen) renderExplain();
  // The focused figure AND the pinned one just gained or lost ~312px of width:
  // both sit in the column the panel takes its space from.
  const fig = state.figures.find(f => f.key === state.focus);
  setTimeout(() => {
    if (fig) resize(fig.node);
    state.pinned.forEach(p => resize(p.node));
  }, 0);
  if (remember) {
    try { localStorage.setItem(EXPLAIN_KEY, state.explainOpen ? '1' : '0'); }
    catch (e) { /* private window, or storage disabled */ }
  }
}

/* ------------------------------------------------------------------ strip */

/* Each cell renders an em dash when its value is null, never a zero. A zero
   is a measurement; a dash is the absence of one, and a readout that shows 0
   for "not computed" is telling the reader something false. */
export function strip(cells) {
  const host = $('analyticsStrip');
  if (!host) return;
  if (!cells || !cells.length) {
    host.innerHTML = '<p class="empty" style="flex:1 1 100%">Nothing to report yet.</p>';
    return;
  }
  host.innerHTML = cells.map(c => `
    <div style="flex:1 1 0;min-width:120px;padding:9px 13px;border-left:1px solid var(--line)">
      <div class="stat-label">${c.label}</div>
      <div style="font-family:var(--value-font);font-size:19px;font-weight:600;
                  color:${c.tone ? `var(--${c.tone}-ink)` : 'var(--ink)'};
                  letter-spacing:-.02em;font-variant-numeric:tabular-nums;
                  text-shadow:var(--glow-value)">${c.value == null ? '—' : c.value}</div>
      ${c.sub ? `<div class="sub-note">${c.sub}</div>` : ''}
    </div>`).join('');
}

/* -------------------------------------------------------------------- init */

export function init(opts) {
  opts = opts || {};
  state.conclusion = opts.conclusion || (() => null);
  state.onFocus = opts.onFocus || null;

  const pool = $(POOL), rail = $(RAIL);
  if (!pool || !rail) return;

  const all = [...pool.querySelectorAll('figure[data-fig]')].map(node => ({
    key: node.dataset.fig,
    name: node.dataset.name || node.dataset.fig,
    node,
  }));
  state.figures = all.filter(f => !PINNED.has(f.key));
  state.pinned = all.filter(f => PINNED.has(f.key));

  rail.innerHTML = '';
  state.figures.forEach(fig => rail.append(thumbButton(fig)));
  // Move every rail figure out of the pool and into its thumbnail.
  state.figures.forEach(fig => {
    const slot = rail.querySelector(`[data-fig="${fig.key}"] .fig-thumb__slot`);
    if (slot) { slot.append(fig.node); fitThumb(slot); resize(fig.node); }
  });

  /* The pinned figures go straight under the focus card. Its card stays hidden
     unless one actually arrives, so a markup change that drops the pinned slot
     leaves an empty card rather than a broken one -- and a figure named in
     PINNED but absent from the pool simply does not appear. */
  const pinSlot = $(PIN);
  const pinCard = $('figPinnedCard');
  if (pinSlot && state.pinned.length) {
    state.pinned.forEach(fig => { pinSlot.append(fig.node); resize(fig.node); });
    if (pinCard) pinCard.hidden = false;
    const pinTitle = $('figPinnedTitle');
    if (pinTitle) pinTitle.textContent = state.pinned[0].name;
  } else if (state.pinned.length) {
    // No slot in the markup: keep them in the rail rather than losing them.
    state.pinned.forEach(fig => {
      state.figures.push(fig);
      rail.append(thumbButton(fig));
      const slot = rail.querySelector(`[data-fig="${fig.key}"] .fig-thumb__slot`);
      if (slot) { slot.append(fig.node); fitThumb(slot); resize(fig.node); }
    });
    state.pinned = [];
  }

  rail.addEventListener('click', e => {
    const b = e.target.closest('.fig-thumb');
    if (b) focus(b.dataset.fig);
  });
  // Arrow keys along the rail, since it is a tablist.
  rail.addEventListener('keydown', e => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    const i = state.figures.findIndex(f => f.key === state.focus);
    const next = state.figures[i + (e.key === 'ArrowDown' ? 1 : -1)];
    if (next) { e.preventDefault(); focus(next.key);
                rail.querySelector(`[data-fig="${next.key}"]`).focus(); }
  });

  /* The focus ? follows the focused figure; the pinned ? pins the panel to its
     own. Pressing either while it already shows that figure closes it, so each
     button behaves as its own toggle rather than as half of a shared one. */
  const toggle = $('figExplainToggle');
  if (toggle) toggle.addEventListener('click', () => {
    const mine = state.explainOpen && !state.explainKey;
    setExplain(!mine, null, true);
  });
  const pinToggle = $('figPinnedExplain');
  if (pinToggle) pinToggle.addEventListener('click', () => {
    const key = state.pinned.length ? state.pinned[0].key : null;
    const mine = state.explainOpen && state.explainKey === key;
    setExplain(!mine, mine ? null : key, true);
  });
  const close = $('figExplainClose');
  if (close) close.addEventListener('click', () => setExplain(false, null, true));

  const body = $('figExplainBody');
  if (body) body.addEventListener('click', e => {
    const b = e.target.closest('[data-goto-fig]');
    if (!b) return;
    const key = b.dataset.gotoFig;
    if (PINNED.has(key)) {
      // A pinned figure cannot be focused -- it is already on screen. Point the
      // panel at it and scroll it into view instead of doing nothing.
      setExplain(true, key, true);
      $(PIN)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    } else {
      focus(key);
    }
  });

  /* Evolution first: it answers "did this job get anywhere", which is the
     question you have before any of the others. */
  focus(state.figures.length ? state.figures[0].key : null);

  /* Open on a first visit. The panel is where a figure says what it means, and
     a reader who does not know it exists will not press a 22px "?" to find
     out -- so the default has to be the state that teaches it. Closing it is
     remembered, so anyone who does not want it only says so once. */
  let saved = null;
  try { saved = localStorage.getItem(EXPLAIN_KEY); } catch (e) {}
  setExplain(saved == null ? true : saved === '1', undefined, false);

  window.addEventListener('predpep:themechange', () => {
    allFigures().forEach(f => resize(f.node));
  });

  /* The rail's width follows the viewport, and the scale factor is derived
     from it, so a resize that changes the rail must recompute every slot. */
  let t = null;
  window.addEventListener('resize', () => {
    clearTimeout(t);
    t = setTimeout(() => {
      rail.querySelectorAll('.fig-thumb__slot').forEach(fitThumb);
      allFigures().forEach(f => resize(f.node));
    }, 150);
  });
}

/* Called by analytics.js once a job's data has arrived, so the explain panel
   stops showing the previous job's finding. */
export function refresh() {
  if (state.explainOpen) renderExplain();
  allFigures().forEach(f => resize(f.node));
}

export function current() { return state.focus; }
