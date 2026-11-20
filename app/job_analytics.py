"""job_analytics.py — server-side analytics for one job.

Everything the Analytics view draws is computed here, with pandas and numpy,
from files the pipeline already writes:

  tab2_final_scores.csv   per-design FoldX + Rosetta total (the merge output)
  all_model_scores.csv    every Rosetta score term, per model (run_catSPEC.py)
  iteration_metrics.json  live per-iteration progress, when the manager wrote it

The per-iteration curve is derived from pdb_relative_path, which embeds the
iteration folder (<job>/<job>_iterN_xxxx/...). The running minimum over those
per-iteration minima IS the convergence curve, so no new pipeline output is
needed for the historical chart -- iteration_metrics.json only adds live
wave-level detail while a job is still running.

PCA is a plain numpy SVD rather than scikit-learn: one less import on the web
path, and the result is identical for this use.
"""

import glob
import json
import os
import re

import numpy as np
import pandas as pd

FINAL_SCORES = 'tab2_final_scores.csv'
ALL_SCORES = 'all_model_scores.csv'
METRICS_FILE = 'iteration_metrics.json'

# The terms worth correlating, in the order run_anaSCORES.py chose them.
SCORE_TERMS = ['binding_score', 'interface_score', 'total_score', 'fa_atr',
               'fa_rep', 'fa_sol', 'fa_dun', 'hbond_sc', 'pro_close', 'ref']

# Short labels so the heatmap axis stays legible.
TERM_LABEL = {
    'binding_score': 'bind', 'interface_score': 'ifce', 'total_score': 'total',
    'fa_atr': 'fa_atr', 'fa_rep': 'fa_rep', 'fa_sol': 'fa_sol',
    'fa_dun': 'fa_dun', 'hbond_sc': 'hbond', 'pro_close': 'pro_cl', 'ref': 'ref',
}

CANONICAL_3 = {'ALA', 'ARG', 'ASN', 'ASP', 'CYS', 'GLN', 'GLU', 'GLY', 'HIS', 'ILE',
               'LEU', 'LYS', 'MET', 'PHE', 'PRO', 'SER', 'THR', 'TRP', 'TYR', 'VAL'}

_ITER_RE = re.compile(r'_iter(\d+)_')


# The catalogue object this set was built from, held by reference so the
# identity check below cannot be fooled by a recycled id().
_D_HET = {'cat': None, 'set': frozenset()}


def _d_het_codes():
    """Het codes that denote a D residue, from the catalogue rather than the
    code string.

    The conventions differ: D-canonicals carry a prefix (DAL, DSE, DHI),
    D-non-canonicals a suffix (D0A1 -> A1D). A first-character test finds 30 of
    216; adding a last-character test pulls in four L residues that end in D
    (APD, AZD, BHD, LED). The catalogue carries `hand`, so neither guess is
    needed.

    get_catalogue() is cached and reload() replaces it, so comparing identity
    rebuilds the set exactly when the catalogue changes and never otherwise.
    """
    try:
        import residue_catalogue as rc
        cat = rc.get_catalogue()
    except Exception:                               # noqa: BLE001
        return frozenset()
    if _D_HET['cat'] is not cat:
        _D_HET['cat'] = cat
        _D_HET['set'] = frozenset(
            str(e.get('pdb_code') or '').upper()
            for e in (cat.get('noncanonical') or [])
            if e.get('hand') == 'D' and e.get('pdb_code'))
    return _D_HET['set']


def _l_het_codes():
    """Het codes that are definitely L, so the prefix test cannot claim them.

    Eight catalogue codes begin with D while being L residues -- DAB, DPP and
    others -- and their het codes begin with D too.
    """
    try:
        import residue_catalogue as rc
        cat = rc.get_catalogue()
    except Exception:                               # noqa: BLE001
        return frozenset()
    out = {str(e.get('pdb_code') or '').upper()
           for e in (cat.get('noncanonical') or [])
           if e.get('hand') == 'L' and e.get('pdb_code')}
    # Withheld entries carry no hand or pdb_code, but an L category and a code
    # that is the het code. DAB and DPP are here, and both start with D.
    for key in ('unavailable', 'retired', 'collisions', 'pending'):
        for e in (cat.get(key) or []):
            code = str(e.get('code') or '').upper()
            if code and str(e.get('category') or '').startswith('L'):
                out.add(code)
    return frozenset(out)


def _iteration_of(path):
    m = _ITER_RE.search(str(path or ''))
    return int(m.group(1)) if m else None


def _residues(row):
    """Residue codes for one design, as (code, kind) pairs.

    Prefers an explicit three-letter column when the pipeline provides one: the
    one-letter pepSeq maps EVERY non-canonical to 'X' (their params carry
    IO_STRING '<CODE> X'), so distinct residues are indistinguishable there.
    """
    explicit = row.get('pepResidues') or row.get('pep_residues')
    if isinstance(explicit, str) and explicit.strip():
        out = []
        for code in re.split(r'[\s\-,]+', explicit.strip()):
            if not code:
                continue
            c = code.upper()
            if c in CANONICAL_3:
                kind = ''
            elif (c in _d_het_codes()
                  or (len(c) == 3 and c.startswith('D')
                      and c not in _l_het_codes())):
                # Union: the catalogue holds the official PDB id (D-serine =
                # DSN) but Rosetta writes its own name3 (DSE). No L residue has
                # a het code starting with D, so the prefix test adds no false
                # positives; four L residues END in D, so no suffix test.
                kind = 'dres'
            else:
                kind = 'ncaa'
            out.append((c, kind))
        return out
    seq = str(row.get('pepSeq') or '').upper()
    return [(ch, 'ncaa' if ch == 'X' else '') for ch in seq if ch.isalpha()]


def _load_final(job_dir):
    path = os.path.join(job_dir, FINAL_SCORES)
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path)
    except Exception:
        return None
    if 'FoldX_Score' in df.columns:
        df['FoldX_Score'] = pd.to_numeric(df['FoldX_Score'], errors='coerce')
    if 'Rosetta_Total_Score' in df.columns:
        df['Rosetta_Total_Score'] = pd.to_numeric(df['Rosetta_Total_Score'], errors='coerce')
    if 'pdb_relative_path' in df.columns:
        df['iteration'] = df['pdb_relative_path'].map(_iteration_of)
    else:
        df['iteration'] = None
    return df


def _load_all_scores(job_dir):
    path = os.path.join(job_dir, ALL_SCORES)
    if not os.path.exists(path):
        return None
    try:
        return pd.read_csv(path)
    except Exception:
        return None


# ------------------------------------------------------------------ evolution

def evolution(job_dir, df_final):
    """Per-iteration best/median FoldX energy, plus the running best."""
    # Live metrics win while a job is running: they carry the current iteration's
    # wave counts, which the finished CSV cannot.
    live = read_metrics(job_dir)
    if live and live.get('iterations'):
        rows = []
        for r in live['iterations']:
            rows.append({
                'iteration': r.get('iteration'),
                'best': r.get('best_score'),
                'median': r.get('median_score'),
                'count': r.get('design_count'),
            })
        if rows:
            return {'iterations': rows, 'source': METRICS_FILE}

    if df_final is None or 'FoldX_Score' not in df_final.columns:
        return {'iterations': [], 'source': None}

    df = df_final.dropna(subset=['FoldX_Score'])
    df = df[df['iteration'].notna()]
    if df.empty:
        return {'iterations': [], 'source': None}

    rows = []
    running = None
    for it, group in df.groupby('iteration', sort=True):
        best = float(group['FoldX_Score'].min())
        running = best if running is None else min(running, best)
        rows.append({
            'iteration': int(it),
            'best': round(running, 3),          # running minimum = convergence
            'iteration_best': round(best, 3),   # best found in this iteration alone
            'median': round(float(group['FoldX_Score'].median()), 3),
            'count': int(len(group)),
        })
    return {'iterations': rows, 'source': FINAL_SCORES}


# ------------------------------------------------------------------ agreement

def agreement(df_final, cap=1500):
    """FoldX against Rosetta total, one point per design."""
    if df_final is None:
        return {'points': []}
    cols = {'FoldX_Score', 'Rosetta_Total_Score'}
    if not cols.issubset(df_final.columns):
        return {'points': []}

    df = df_final.dropna(subset=['FoldX_Score', 'Rosetta_Total_Score'])
    if df.empty:
        return {'points': []}

    # Cap what crosses the wire: a 4,795-design run does not need every point
    # to show the shape, and the browser should not have to draw them.
    if len(df) > cap:
        df = df.sample(cap, random_state=42)

    points = []
    for _, row in df.iterrows():
        kinds = [k for _, k in _residues(row)]
        points.append({
            'x': round(float(row['FoldX_Score']), 3),
            'y': round(float(row['Rosetta_Total_Score']), 2),
            'flag': any(kinds),
        })
    return {'points': points, 'sampled': len(df) < len(df_final)}


# ---------------------------------------------------------------- correlation

def correlation(df_all):
    """Pearson correlation between the Rosetta score terms."""
    if df_all is None:
        return {'labels': [], 'matrix': []}

    present = [c for c in SCORE_TERMS if c in df_all.columns]
    if len(present) < 2:
        return {'labels': [], 'matrix': []}

    num = df_all[present].apply(pd.to_numeric, errors='coerce')
    # A constant column has no correlation to report (and divides by zero).
    num = num.loc[:, num.std(numeric_only=True) > 0]
    num = num.dropna(axis=1, how='all')
    if num.shape[1] < 2:
        return {'labels': [], 'matrix': []}

    corr = num.corr(method='pearson').fillna(0.0)
    labels = [TERM_LABEL.get(c, c) for c in corr.columns]
    matrix = [[round(float(v), 3) for v in row] for row in corr.values]
    return {'labels': labels, 'matrix': matrix}


# ------------------------------------------------------------------------ PCA

def pca(df_all, df_final=None, cap=1500, components=3):
    """Designs projected onto the leading principal components of their terms.

    Returns three components by default so the same computation serves both the
    2D panel (x, y) and the 3D cluster plot (x, y, z) -- the third axis is one
    more column of the SVD already being done, so it costs nothing extra.

    'variance' stays the fraction explained by the FIRST TWO components, which
    is what the existing 2D panel label reports; 'variance_axes' gives the
    per-axis fractions and 'variance3' the first three, for the 3D panel.
    """
    if df_all is None:
        return {'points': [], 'variance': None}

    present = [c for c in SCORE_TERMS if c in df_all.columns]
    if len(present) < 3:
        return {'points': [], 'variance': None}

    num = df_all[present].apply(pd.to_numeric, errors='coerce')
    num = num.loc[:, num.std(numeric_only=True) > 0]
    num = num.dropna(axis=1, how='all')
    if num.shape[1] < 2:
        return {'points': [], 'variance': None}

    num = num.fillna(num.mean())
    rows = num.dropna(axis=0)
    if len(rows) < 3:
        return {'points': [], 'variance': None}

    keep_index = rows.index
    if len(rows) > cap:
        rows = rows.sample(cap, random_state=42)
        keep_index = rows.index

    X = rows.to_numpy(dtype=float)
    X = X - X.mean(axis=0)
    sd = X.std(axis=0)
    sd[sd == 0] = 1.0
    X = X / sd

    # Economy SVD: the right singular vectors are the principal axes.
    try:
        U, S, _Vt = np.linalg.svd(X, full_matrices=False)
    except np.linalg.LinAlgError:
        return {'points': [], 'variance': None}

    k = max(2, min(int(components), len(S)))
    scores = U[:, :k] * S[:k]
    total = float((S ** 2).sum()) or 1.0
    variance = float((S[:2] ** 2).sum() / total)
    variance3 = float((S[:k] ** 2).sum() / total)
    variance_axes = [round(float(S[i] ** 2 / total), 4) for i in range(k)]

    # Flag designs carrying a non-canonical residue, matched by pepID where the
    # per-model table and the final table can be joined.
    flagged = set()
    if df_final is not None and 'pepID' in df_all.columns:
        for _, row in df_final.iterrows():
            if any(k for _, k in _residues(row)):
                flagged.add(str(row.get('pdbId')))

    ids = df_all.loc[keep_index, 'pepID'] if 'pepID' in df_all.columns else None
    points = []
    for i, idx in enumerate(keep_index):
        pid = str(ids.loc[idx]) if ids is not None else ''
        pt = {
            'x': round(float(scores[i, 0]), 3),
            'y': round(float(scores[i, 1]), 3),
            'flag': pid in flagged,
            # Carried so the 3D panel can hover-label a point and click through
            # to that design in the Results table.
            'id': pid,
        }
        if scores.shape[1] > 2:
            pt['z'] = round(float(scores[i, 2]), 3)
        points.append(pt)
    return {'points': points, 'variance': round(variance, 3),
            'variance3': round(variance3, 3),
            'variance_axes': variance_axes,
            'components': int(scores.shape[1]),
            'terms': [TERM_LABEL.get(c, c) for c in num.columns]}


# ----------------------------------------------------------------------- logo

def seqmap(df_final, cap=1200, dims=3):
    """Designs embedded in 3D by SEQUENCE similarity, not by score.

    Complements pca(), which maps energy space: two designs can score alike and
    be sequence-wise unrelated, and the pair of plots side by side is the point.

    Method, and why this one:
      * MHFP minhash fingerprints, the same ones the similarity tree uses, via
        tmap_utils. The fraction of matching minhash positions between two
        fingerprints is an unbiased estimate of their Jaccard similarity, so
        1 - that fraction is a usable distance with no extra machinery.
      * Classical MDS (Torgerson): double-centre the squared-distance matrix and
        take the leading eigenvectors. Exact, deterministic, and NumPy-only.

    UMAP would fit the same slot -- it takes a precomputed distance matrix -- and
    gives tighter local clusters. It is not used here because umap-learn needs
    numba, which pins NumPy hard; this environment is on NumPy 2.3.5 and the
    similarity tree was broken for months by exactly that kind of conflict in
    mhfp. If UMAP is wanted later, it reads `d` below and nothing else changes.

    Returns {'points': [...], 'stress': float|None, 'method': str, 'note': str}.
    """
    out = {'points': [], 'stress': None, 'method': None, 'note': ''}
    if df_final is None or 'pepSeq' not in df_final.columns:
        out['note'] = 'No pepSeq column, so sequence similarity cannot be computed.'
        return out

    try:
        from tmap_utils import _make_encoder
        from rdkit import Chem
    except ImportError as e:
        out['note'] = (f'Sequence map needs the fingerprint encoder ({e}). '
                       'The score-space PCA is unaffected.')
        return out

    df = df_final
    if 'FoldX_Score' in df.columns and len(df) > cap:
        # Keep the best-scoring designs rather than a random slice: the map is
        # read to find structure among candidates worth looking at.
        df = df.nsmallest(cap, 'FoldX_Score')
    elif len(df) > cap:
        df = df.head(cap)

    try:
        encode, backend = _make_encoder()
    except Exception as e:                          # noqa: BLE001
        out['note'] = f'No usable MHFP encoder ({type(e).__name__}: {e}).'
        return out

    fps, ids, seqs = [], [], []
    for _, row in df.iterrows():
        seq = ''.join(str(row.get('pepSeq') or '').split()).strip().upper()
        if not seq or seq in ('NAN', 'NONE', 'NULL'):
            continue
        mol = Chem.MolFromSequence(seq) or Chem.MolFromFASTA(f'>p\n{seq}')
        if mol is None:
            continue
        try:
            fps.append([int(h) & 0xFFFFFFFF for h in encode(mol)])
        except Exception:                           # noqa: BLE001
            continue
        ids.append(str(row.get('pdbId') or ''))
        seqs.append(seq)

    if len(fps) < 4:
        out['note'] = (f'Only {len(fps)} design(s) produced a fingerprint; '
                       'an embedding needs at least four.')
        return out

    F = np.asarray(fps, dtype=np.uint32)

    # Pairwise Jaccard distance from minhash agreement, in blocks so a large
    # design set does not allocate an n x n x permutations array.
    n, m = F.shape
    d = np.zeros((n, n), dtype=np.float32)
    block = max(1, 2_000_000 // max(1, n * m // 8 or 1))
    for start in range(0, n, block):
        stop = min(n, start + block)
        agree = (F[start:stop, None, :] == F[None, :, :]).sum(axis=2)
        d[start:stop] = 1.0 - (agree / float(m))
    np.fill_diagonal(d, 0.0)
    d = (d + d.T) / 2.0                             # enforce symmetry

    if float(d.max()) <= 0.0:
        out['note'] = ('Every design has an identical fingerprint, so there is '
                       'no sequence structure to embed.')
        return out

    # Classical MDS.
    D2 = (d.astype(np.float64)) ** 2
    J = np.eye(n) - np.ones((n, n)) / n
    B = -0.5 * J.dot(D2).dot(J)
    k = min(int(dims), n - 1)

    # Only the top k eigenpairs are needed, and asking for all n is both slower
    # and -- under gunicorn's preload_app -- pathologically so: numpy's
    # np.linalg.eigh took 2.6s standalone and ~80s inside a forked worker,
    # because OpenBLAS's thread pool does not survive the fork. py-spy caught
    # it sitting in eigh. SciPy's driver can return a subset, which is a
    # fraction of the work; the trace of B still gives the denominator for the
    # explained fraction, so nothing is lost by not computing every eigenvalue.
    vals = vecs = None
    total = float(np.trace(B))
    try:
        from scipy.linalg import eigh as _seigh
        sub = _seigh(B, subset_by_index=[n - k, n - 1])
        vals, vecs = sub[0][::-1], sub[1][:, ::-1]
    except Exception as e:                          # noqa: BLE001
        print(f'DEBUG: subset eigh unavailable ({type(e).__name__}: {e}); '
              'falling back to the full decomposition.')
        try:
            allvals, allvecs = np.linalg.eigh(B)
        except np.linalg.LinAlgError as err:
            out['note'] = f'MDS eigendecomposition failed: {err}'
            return out
        order = np.argsort(allvals)[::-1]
        vals, vecs = allvals[order][:k], allvecs[:, order][:, :k]
        total = float(np.clip(allvals, 0.0, None).sum())

    pos_vals = np.clip(vals[:k], 0.0, None)
    coords = vecs[:, :k] * np.sqrt(pos_vals)

    # trace(B) is the sum of all eigenvalues, so it is the right denominator
    # whether or not every eigenvalue was computed.
    total = total or 1.0
    explained = float(pos_vals.sum() / total)

    out['method'] = f'classical MDS on MHFP Jaccard ({backend})'
    out['explained'] = round(explained, 3)
    out['points'] = [
        {'x': round(float(coords[i, 0]), 4),
         'y': round(float(coords[i, 1]), 4),
         'z': round(float(coords[i, 2]), 4) if k > 2 else 0.0,
         'id': ids[i], 'seq': seqs[i]}
        for i in range(n)
    ]
    out['note'] = (f'{n} design(s) embedded; the leading {k} axes carry '
                   f'{explained:.0%} of the distance structure.')

    # A low fraction means the distances genuinely do not fit in three
    # dimensions, so proximity in the plot carries little information. Short
    # peptides are prone to this: a 5-mer has few MHFP shingles, so most pairs
    # sit at a similar distance and there is no low-dimensional structure to
    # find. Said plainly, because a 3D scatter always LOOKS like clusters.
    if explained < 0.25:
        out['low_confidence'] = True
        out['note'] += (' That is low: these distances do not reduce well to '
                        'three dimensions, so clusters in this plot are mostly '
                        'projection artefacts rather than real sequence '
                        'families. Short peptides commonly behave this way.')
    else:
        out['low_confidence'] = False
    return out


def template_peptide(job_dir):
    """The peptide the job started from, for sequence-similarity colouring.

    Read from design_spec.json, which predPEP.py writes at submission. Returns
    {} for a job that predates the spec -- the similarity fields then simply do
    not appear, rather than comparing against a guess.
    """
    try:
        with open(os.path.join(job_dir, 'design_spec.json')) as f:
            spec = json.load(f)
    except (OSError, ValueError):
        return {}
    pep = (spec or {}).get('peptide') or {}
    res = pep.get('residues') or []
    if not res:
        return {}
    return {
        'chain': pep.get('chain'),
        'start_resseq': pep.get('start_resseq'),
        'length': len(res),
        'residues': [str(c).upper() for c in res],
        'disulfides': pep.get('disulfides') or [],
    }


# Descriptor name -> the RDKit callable that produces it. Resolved at call time
# and each one guarded individually, because a missing or renamed function in
# one RDKit version should cost that single descriptor, not all of them.
def _rdkit_descriptor_fns():
    out = {}
    try:
        from rdkit.Chem import Crippen, rdMolDescriptors as rd
    except ImportError:
        return out
    cands = {
        'tpsa': lambda m: rd.CalcTPSA(m),
        'logp': lambda m: Crippen.MolLogP(m),
        'mol_hbd': lambda m: rd.CalcNumHBD(m),
        'mol_hba': lambda m: rd.CalcNumHBA(m),
        'rotatable': lambda m: rd.CalcNumRotatableBonds(m),
    }
    # Probe each on a trivial molecule so a broken one is dropped here rather
    # than failing 800 times inside the loop.
    try:
        from rdkit import Chem
        probe = Chem.MolFromSequence('AG')
    except Exception:                               # noqa: BLE001
        return out
    if probe is None:
        return out
    for name, fn in cands.items():
        try:
            float(fn(probe))
            out[name] = fn
        except Exception as e:                      # noqa: BLE001
            print(f'DEBUG: descriptor {name} unavailable ({type(e).__name__}: {e})')
    return out


def descriptors(df_final, cap=2000):
    """Whole-molecule descriptors per design: {pdbId: {tpsa, logp, ...}}.

    Computed with RDKit from the one-letter sequence, which means a design
    containing a non-canonical residue gets NO entry: the one-letter column
    writes every non-canonical as X and RDKit cannot build a molecule from it.
    An absent entry colours grey, which is the honest outcome -- the alternative
    would be a descriptor computed over a molecule missing a side chain.

    Results are memoised per SEQUENCE, not per design: many designs in a job
    share a sequence, and the descriptors depend only on it.
    """
    out = {'by_id': {}, 'available': [], 'note': ''}
    if df_final is None or 'pepSeq' not in df_final.columns:
        out['note'] = 'No pepSeq column, so molecule descriptors cannot be computed.'
        return out

    fns = _rdkit_descriptor_fns()
    if not fns:
        out['note'] = ('RDKit descriptor functions are unavailable in this '
                       'environment; the sequence-derived descriptors in the '
                       'browser are unaffected.')
        return out

    try:
        from rdkit import Chem
    except ImportError:
        out['note'] = 'RDKit is unavailable.'
        return out

    df = df_final.head(cap) if len(df_final) > cap else df_final
    per_seq = {}
    skipped = 0
    for _, row in df.iterrows():
        pid = str(row.get('pdbId') or '')
        seq = ''.join(str(row.get('pepSeq') or '').split()).strip().upper()
        if not pid or not seq:
            continue
        if seq not in per_seq:
            mol = Chem.MolFromSequence(seq)
            if mol is None:
                per_seq[seq] = None
            else:
                vals = {}
                for name, fn in fns.items():
                    try:
                        vals[name] = round(float(fn(mol)), 3)
                    except Exception:               # noqa: BLE001
                        pass
                per_seq[seq] = vals or None
        if per_seq[seq] is None:
            skipped += 1
            continue
        out['by_id'][pid] = per_seq[seq]

    out['available'] = sorted(fns)
    unique = len(per_seq)
    out['note'] = (f'{len(out["by_id"])} design(s) described from {unique} unique '
                   f'sequence(s)')
    if skipped:
        out['note'] += (f'; {skipped} skipped because RDKit could not build a '
                        'molecule (a non-canonical residue is written "X" in the '
                        'one-letter column)')
    print('DEBUG: descriptors ' + out['note'])
    return out


def logo(df_final, top=100, max_stack=3):
    """Residue frequency per position among the best designs.

    Three-letter codes throughout: a one-letter logo cannot distinguish the
    non-canonicals, which all render as X.
    """
    if df_final is None or 'FoldX_Score' not in df_final.columns:
        return {'columns': []}

    df = df_final.dropna(subset=['FoldX_Score']).nsmallest(top, 'FoldX_Score')
    if df.empty:
        return {'columns': []}

    per_pos = {}
    for _, row in df.iterrows():
        for i, (code, kind) in enumerate(_residues(row)):
            per_pos.setdefault(i, []).append((code, kind))

    columns = []
    for i in sorted(per_pos):
        entries = per_pos[i]
        counts = {}
        kinds = {}
        for code, kind in entries:
            counts[code] = counts.get(code, 0) + 1
            kinds[code] = kind
        total = len(entries) or 1
        stack = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:max_stack]
        columns.append({
            'pos': i + 1,
            'stack': [{
                'code': code,
                'freq': round(100.0 * n / total, 1),
                'kind': kinds.get(code, ''),
            } for code, n in stack],
        })
    return {'columns': columns, 'designs': int(len(df))}


# ------------------------------------------------------------------- sampling

RUN_STATUS_FILE = 'runs_status.json'
_ITER_DIR_RE = re.compile(r'_iter(\d+)')

# A pepspec run killed by a signal reports 128+signum through the shell. These
# are the ones seen in practice; anything else is reported as its raw code.
_EXIT_NAME = {
    134: 'SIGABRT (assertion failed)',
    137: 'SIGKILL (killed, usually out of memory)',
    139: 'SIGSEGV (segfault)',
    143: 'SIGTERM (asked to stop)',
}


def _exit_label(code):
    return _EXIT_NAME.get(code, 'exit %d' % code)


def sampling(job_dir):
    """Per-iteration pepspec run losses, or None when nothing recorded them.

    run_pepSpecPipe.sh writes runs_status.json into each iteration directory as
    that iteration's runs are reaped, and _post_zip_cleanup keeps the file (it
    is in scheduler._KEEP_ITER_FILES), so this reads identically for a running
    job and a finished one.

    Why this matters enough to surface: pepspec crashes intermittently, and a
    crashed run yields fewer models rather than failing the job. The anchors
    sampled by that run are then ranked against anchors that got the full
    sampling budget, so a silent loss biases the ranking toward whatever
    happened to survive. Until now the only trace was a line in a log.

    Returns None, not zeros, when no status file exists: a job from before the
    pipeline recorded this has *unknown* losses, which is a different claim
    from "none", and the readout renders the two differently.
    """
    paths = sorted(glob.glob(os.path.join(job_dir, '*_iter*', RUN_STATUS_FILE)))
    if not paths:
        return None

    by_iter, codes = {}, {}
    total = failed = unreadable = 0

    for path in paths:
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            # Truncated or unreadable. Skipping undercounts, but inventing a
            # zero would be a lie; `unreadable` below says how often it happened
            # so a thin number is explained rather than quietly wrong.
            unreadable += 1
            continue
        if not isinstance(data, dict):
            unreadable += 1
            continue

        name = os.path.basename(os.path.dirname(path))
        m = _ITER_DIR_RE.search(name)
        try:
            n_total = int(data.get('runs_total') or 0)
            n_failed = int(data.get('runs_failed') or 0)
        except (TypeError, ValueError):
            unreadable += 1
            continue
        # A failure count larger than the total means the file is inconsistent;
        # trust neither rather than reporting an impossible rate.
        if n_total < 0 or n_failed < 0 or n_failed > n_total:
            unreadable += 1
            continue

        for run in (data.get('runs') or []):
            if not isinstance(run, dict):
                continue
            try:
                code = int(run.get('exit_code'))
            except (TypeError, ValueError):
                continue
            if code != 0:
                codes[code] = codes.get(code, 0) + 1

        total += n_total
        failed += n_failed
        # One iteration branches into up to three parallel runs, each with its
        # own {base}_iter{N}_{uuid} directory and its own status file. Group by
        # the iteration number, or iteration 2's "2 failed" and "5 failed"
        # would read as two separate iterations instead of 7 of 42 in one.
        key = int(m.group(1)) if m else None
        row = by_iter.get(key)
        if row is None:
            row = by_iter[key] = {'iteration': key, 'branches': 0,
                                  'runs_total': 0, 'runs_failed': 0,
                                  'crash_log': False, 'dirs': []}
        row['branches'] += 1
        row['runs_total'] += n_total
        row['runs_failed'] += n_failed
        row['crash_log'] = row['crash_log'] or bool(data.get('crash_log_present'))
        row['dirs'].append(name)

    iterations = sorted(by_iter.values(),
                        key=lambda r: (r['iteration'] is None, r['iteration']))

    worst = None
    for row in iterations:
        if row['runs_failed'] and (worst is None or row['runs_failed'] > worst['runs_failed']):
            worst = row

    return {
        'runs_total': total,
        'runs_failed': failed,
        'loss_pct': round(100.0 * failed / total, 1) if total else None,
        'iterations': iterations,
        'exit_codes': [{'code': c, 'label': _exit_label(c), 'count': n}
                       for c, n in sorted(codes.items(), key=lambda kv: -kv[1])],
        'worst_iteration': worst,
        'unreadable': unreadable,
    }


# -------------------------------------------------------------------- metrics

def read_metrics(job_dir):
    """iteration_metrics.json, or {} when the manager never wrote one."""
    try:
        with open(os.path.join(job_dir, METRICS_FILE)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def best_score(job_dir):
    """Best (lowest) FoldX energy seen so far, for the jobs table.

    Reads the manager's metrics first, then the merged score table, then the
    FoldX aggregate itself -- which exists even mid-run, so a running job shows
    a best score before any CSV has been produced.
    """
    live = read_metrics(job_dir)
    if isinstance(live.get('global_best'), (int, float)):
        return round(float(live['global_best']), 3)

    df = _load_final(job_dir)
    if df is not None and 'FoldX_Score' in df.columns:
        series = df['FoldX_Score'].dropna()
        if not series.empty:
            return round(float(series.min()), 3)

    # {PDB_BASE}.all.txt is "<model> <score>" per line, sorted, so the first
    # parseable value is the minimum -- but do not trust the sort blindly.
    for path in sorted(glob.glob(os.path.join(job_dir, '*.all.txt'))):
        try:
            best = None
            with open(path, errors='replace') as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    try:
                        v = float(parts[1])
                    except ValueError:
                        continue
                    best = v if best is None else min(best, v)
            if best is not None:
                return round(best, 3)
        except OSError:
            continue
    return None


# ------------------------------------------------------------------- assemble

def bundle(job_dir):
    """Everything /jobs/<id>/analytics returns."""
    df_final = _load_final(job_dir)
    df_all = _load_all_scores(job_dir)

    if df_final is None and df_all is None:
        return {'success': False,
                'error': 'No score tables for this job yet (%s missing).' % FINAL_SCORES}

    iters = []
    if df_final is not None:
        iters = sorted({int(v) for v in df_final['iteration'].dropna().unique()})

    out = {
        'success': True,
        'design_count': int(len(df_final)) if df_final is not None else None,
        'iteration_count': len(iters) or None,
        'evolution': evolution(job_dir, df_final),
        'agreement': agreement(df_final),
        'correlation': correlation(df_all),
        'pca': pca(df_all, df_final),
        'seqmap': seqmap(df_final),
        'logo': logo(df_final),
        # For the colour-by control: the template to measure similarity
        # against, and the molecule-level descriptors the browser cannot
        # compute without RDKit.
        'template': template_peptide(job_dir),
    }

    # Sampling losses. Promoted to top-level runs_total/runs_failed because the
    # readout strip reads them there; the nested object carries the per-iteration
    # breakdown the explain panel needs. Absent entirely when unrecorded, so the
    # strip omits the cell rather than showing a zero it cannot stand behind.
    samp = sampling(job_dir)
    if samp:
        out['runs_total'] = samp['runs_total']
        out['runs_failed'] = samp['runs_failed']
        out['sampling'] = samp
    desc = descriptors(df_final)
    out['descriptors'] = desc.get('by_id') or {}
    out['descriptor_meta'] = {k: v for k, v in desc.items() if k != 'by_id'}

    # Say plainly which inputs were available, so a thin panel is explained
    # rather than looking broken.
    out['sources'] = {
        FINAL_SCORES: df_final is not None,
        ALL_SCORES: df_all is not None,
        METRICS_FILE: bool(read_metrics(job_dir)),
        RUN_STATUS_FILE: samp is not None,
    }
    return out
