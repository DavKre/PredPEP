# TODO

Open work on the fleet node. Newest first. Items here are decided but not
scheduled; anything still being argued about belongs in a design note instead.

---

## ~~Make the Results columns resizable~~ — DONE 23 Nov 2026

Drag handle between the designs table and the structure column, plus a native
resize grip on the 3D canvas; both persist per browser. `app/static/js/resizable.js`,
wired from `results.js` in two lines so upstream's file barely diverges.

---

## ~~Add a date column to the Operations table~~ — DONE 23 Nov 2026

**Done.** `Submitted` sits between Best ΔΔG and Elapsed, formatted with the
`fmtWhen` that was already in `api.js` and already imported here, and added to
the search haystack so the box finds what the column shows.

**What:** the ops jobs table shows Job · Owner · Cores·len · Status · Iterations ·
Best ΔΔG · Elapsed. No date. v1.4.0's Jobs tab had both **Date** and
**Completed** columns and they were dropped in v1.6.0 in favour of "Elapsed",
which answers how long it took but not when it ran.

**Where:** the header `<th>` list in `app/templates/index.html` (ops table) and
the row builder in `app/static/js/ops.js` (~l.147, the `<tr class="pick">`
template). The data is already in `/jobs`: `submitted_at` and `completed_at`,
both ISO-8601 UTC.

**Watch for:** `ops.js` ~l.56 builds the row's search haystack
(`[j.job_id, j.protein_symbol, j.user_name, ...]`) — add the formatted date
there too or the column will be visible but not searchable. Render in local
time (`toLocaleString`) as the old Jobs tab did; the API speaks UTC.

---

## ~~Make the left rail collapsible~~ — DONE 23 Nov 2026

**Done.** Chevron at the top of the rail, collapses to a 62px icon strip,
remembered per browser, off below 900px where the rail is already a band. Link
text moves to `title` so the icons keep their names.

**What:** the nav rail is always open. On a laptop it costs width that the
Results split and the analytics figures could use.

**Where:** `<aside class="rail">` in `app/templates/index.html` (~l.54) and the
`.rail` rules in `app/static/css/style.css` (~l.753).

**Shape of the change:** a toggle button in `.rail-brand`, a `rail--collapsed`
class on the aside, width driven by a CSS variable so `.main` reflows on its
own. Collapse to an icon strip rather than to zero — navigation that vanishes
entirely is hard to get back. Persist in `localStorage` wrapped in try/catch,
like `resizable.js` does, and mirror the state in `aria-expanded`.

**Watch for:** the Results split and every Plotly figure are sized in pixels, so
the main area changing width must end in a resize call — `resizable.js` already
exposes the pattern (`onResize` → `viewer.resize()`), and `console.js` has
`resize(fig.node)`.

---

## ~~Analytics: keep the thumbnail visible when a figure is focused~~ — DONE 23 Nov 2026

**What:** clicking a thumbnail in the figure rail blanks that thumbnail. Asked
for: leave the preview visible.

**Done.** The diagnosis below was half right and half wrong, which is worth
keeping. The node move is real, but it was **not** the whole cause: there was
also an explicit rule collapsing the slot, which the entry said did not exist —

    .fig-thumb--on .fig-thumb__slot { display: none; }

Both are now handled: the rule is gone, and `leaveStill()` puts a
`Plotly.toImage` still of the figure into the slot it vacated. A figure that has
not drawn yet, or whose snapshot fails, falls back to `.fig-thumb--blank`, which
collapses that one slot as before rather than leaving an empty box.

**Why it happens:** `focus(key)` in `app/static/js/console.js` (l.171) **moves
the one Plotly DOM node**, it does not copy it —

    if (focusSlot) { focusSlot.append(fig.node); resize(fig.node); }

and the previous figure is appended back into its own thumbnail on the way out.
So the rail slot the figure came from is genuinely empty; nothing is being
hidden by CSS and there is no class to stop toggling.

**The fix that does not cost a second chart:** leave a static snapshot behind.
Before moving the node, `Plotly.toImage(fig.node, {format:'png', ...})` and drop
the result into the thumbnail slot as an `<img>`; remove it when the live node
returns. One rendered figure stays live, the rail stays legible.

**Avoid:** rendering each figure twice. There are eight of them and Plotly is
4 MB; two live instances per figure is what the thumbnail-moving design was
built to avoid in the first place.

---

## Read the score table out of the job zip when it has been swept

**What:** `/results_data/<job>` and `job_analytics._load_final()` look for
`tab2_final_scores.csv` on disk and give up when it is gone. The post-job sweep
removes it, so a finished job keeps only its `.zip` -- and on the node here that
is **60 of 71 jobs**, whose Results and Analytics views are therefore empty even
though the data exists.

It does exist. Checked inside two archives:

    SPAARH_9b25f4c4 (127M):   tab2_final_scores.csv  150951 bytes
    SPAZOH_19495934 (478M):   tab2_final_scores.csv  611319 bytes

**Why it is reasonable:** v1.6.0 already does exactly this for models --
`/stream_final_pdb` falls back to reading the member straight out of the archive,
described in the dev's notes as "a seek, not an extraction". The same trick on
one small CSV would restore the interactive views for every archived job at no
disk cost.

**Where:** `job_analytics._load_final()` (add a zipfile fallback after the
`os.path.exists` check) and the `/results_data` route in `predPEP.py`, which
reads the file directly. Both already normalise legacy column names, so old
archives need nothing extra.

**Not a regression** -- those jobs render identically on the v1.4.0 node, which
is to say not at all. This would be an improvement over both.

---

## ~~Replace the TMAP fingerprint in the UI with MAP4 / MAP4C~~ — DONE 21 Nov 2026

Implemented as MAP4C (`mapchiral==0.0.7`, Orsi et al.), default on, with
`PREDPEP_FINGERPRINT=mhfp` to switch back without a rebuild. **One claim below
turned out to be wrong and is corrected here:** RDKit's MHFP is *not* blind to
chirality — its shingles are SMILES substrings, so stereo markers are in them.
Measured on L-AAA against its D variant, MHFP put them at distance 0.571 and
MAP4C at 0.131. And on a real 640-design job MHFP reduced *better*: 42% of the
distance structure in three axes against MAP4C's 34%, at half the runtime.
Lower is not automatically worse — a finer fingerprint can produce distances
that genuinely do not fit in three dimensions — but it is not the improvement
this entry assumed, which is why the switch exists. Worth re-measuring on a
job with long peptides; everything here was 4-mers.

<details><summary>original entry</summary>


**What:** the similarity view currently fingerprints peptides with **MHFP**.
Replace it with **MAP4**, or **MAP4C** if chirality is wanted.

**Where it lives** — one function, two consumers:

| | |
|---|---|
| producer | `app/tmap_utils.py::_make_encoder()` — returns `(encode, backend)`; prefers RDKit's `rdMHFPFingerprint.MHFPEncoder`, falls back to the `mhfp` package via `_PyMHFP` |
| consumer 1 | `app/job_analytics.py` seqmap panel (~l.382–481): `from tmap_utils import _make_encoder`, then classical MDS on Jaccard distance. Reports `method = 'classical MDS on MHFP Jaccard (<backend>)'` |
| consumer 2 | `generate_tmap_coordinates()` in `tmap_utils.py`, behind `GET /get_tmap_tree/<job_id>` — the similarity tree |

Because both go through `_make_encoder()`, swapping the encoder there changes
both panels at once. The MDS, the distance metric (Jaccard on minhash) and the
plotting are all unaffected — MAP4 is likewise a minhashed fingerprint compared
by Jaccard, so only the hashing step changes.

**Why it is worth doing.** MHFP shingles are substructure-based, and the code
already admits where that falls down (`job_analytics.py` ~l.495):

> Short peptides are prone to this: a 5-mer has few MHFP shingles, so most pairs
> sit at a similar distance and there is no low-dimensional structure to find.

MAP4 is atom-pair based and was designed to stay informative on peptides, which
is exactly the failure above. The measured seqmap on a real job carried only 42%
of the distance structure in three axes, and anything under 25% trips the panel's
own `low_confidence` flag.

**MAP4C specifically** matters now that jobs design with D-residues: an
enantiomer pair is the same graph, so an achiral fingerprint places a D design
and its L parent at distance zero. If the similarity view is meant to separate
them, the chirality-aware variant is the one to take.

**Watch out for:**

- **It is a new dependency.** MAP4 is not in RDKit and not in the image's conda
  environment, which is lifted wholesale from the dev's v1.6.0 image (see
  `docs/UPGRADE-v1.6.0-node.md`). Adding a package is the first divergence in
  that environment, so pin it and record it in the Dockerfile next to the
  existing dependency assertion.
- **Keep the fallback shape.** `_make_encoder()` returning `(encode, backend)`
  with a named backend string is what lets the panel say which implementation ran
  and degrade to a note instead of an exception. Preserve that.
- **The `method` string is user-visible** in the seqmap note; update it, and
  check `docs/` references to "MHFP" so the documentation does not keep
  describing the old one.
- Old jobs are unaffected: the fingerprint is computed at view time from
  `tab2_final_scores.csv`, not stored, so nothing needs migrating.
</details>
