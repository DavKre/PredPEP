# TODO

Open work on the fleet node. Newest first. Items here are decided but not
scheduled; anything still being argued about belongs in a design note instead.

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
