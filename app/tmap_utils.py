#!/home/spacepep/miniforge3/envs/predPEP/bin/python

"""TMAP coordinates for peptide sequences: Sequence -> Mol -> MHFP -> layout.

WHY THIS CHANGED
----------------
Every sequence was failing to hash:

    DEBUG: Hashing error at index 790 (WHREL):
           Python integer 2305843009213693951 out of bounds for uint32

That is the `mhfp` package, not the data. mhfp/encoder.py does

    hash_values = np.zeros([n_permutations, 1], dtype=np.uint32)
    hash_values.fill(MHFPEncoder.max_hash)        # max_hash = 2**61 - 1

NumPy 1.x wrapped that silently; NumPy 2 (this env has 2.3.5) raises
OverflowError. mhfp's last release predates NumPy 2, so encode_mol cannot work
here at all -- which means this panel has very likely never worked on this
machine, for any job, regardless of sequence content.

The fix is RDKit's own MHFP encoder (rdkit.Chem.rdMHFPFingerprint.MHFPEncoder),
which is maintained C++, returns values already inside uint32, and needs no
NumPy at all. Verified on this node (rdkit 2025.09.3): EncodeMol on 'WILLL'
returns 1024 values, e.g. [70437984, 97288999, 115101113, 67394026].

mhfp is kept as a fallback for a node whose RDKit is too old to have it, with
the minhash done in PYTHON integers rather than uint32 arrays so the same
overflow cannot recur.

Nothing here touches site-packages: patching mhfp in place would be undone by
any env rebuild, and pinning numpy<2 would risk pandas/RDKit/tmap, which the
five working analytics panels depend on.

DIAGNOSTICS
-----------
The old failure surfaced in the UI as "RDKit cannot build a molecule from a
sequence containing X", a cause this code never checked -- and the peptide in
question was LILEL, entirely canonical. Messages here report what was actually
observed, and the distinct-fingerprint count is logged because a tree over
short peptides can be built and still be meaningless.
"""

import os
import struct
from hashlib import sha1

import tmap as tm
from rdkit import Chem

# Preferred: RDKit's own encoder.
_RDKIT_MHFP = None
try:
    from rdkit.Chem import rdMHFPFingerprint as _rdmhfp
    _RDKIT_MHFP = _rdmhfp
except ImportError:
    pass

# Fallback only.
try:
    from mhfp.encoder import MHFPEncoder as _PyMHFPEncoder
except ImportError:
    _PyMHFPEncoder = None

PERMUTATIONS = 1024
SEED = 42


class _PythonMinHash:
    """mhfp's minhash, done in Python ints.

    Same algorithm and same permutation tables as mhfp, so fingerprints are
    comparable with anything produced by that package -- but the arithmetic
    happens on Python integers, which have no width, instead of being forced
    into a uint32 array that cannot hold max_hash.
    """

    def __init__(self, n_permutations=PERMUTATIONS, seed=SEED):
        if _PyMHFPEncoder is None:
            raise ImportError('neither rdkit.Chem.rdMHFPFingerprint nor mhfp '
                              'is available')
        self.n = n_permutations
        self._enc = _PyMHFPEncoder(n_permutations=n_permutations, seed=seed)
        self.prime = int(_PyMHFPEncoder.prime)
        self.max_hash = int(_PyMHFPEncoder.max_hash)
        # permutations_a/b are (n, 1) arrays in mhfp; flatten to Python ints.
        self.perm_a = [int(v) for v in self._enc.permutations_a.reshape(-1)]
        self.perm_b = [int(v) for v in self._enc.permutations_b.reshape(-1)]

    def encode_mol(self, mol):
        tokens = self._enc.shingling_from_mol(mol)
        values = [self.max_hash] * self.n
        for t in tokens:
            t_h = struct.unpack('<I', sha1(t).digest()[:4])[0]
            for i in range(self.n):
                h = ((self.perm_a[i] * t_h + self.perm_b[i]) % self.prime) % self.max_hash
                if h < values[i]:
                    values[i] = h
        return values


try:                                   # MAP4C: chiral MinHashed Atom-Pair, Orsi et al.
    from mapchiral.mapchiral import encode as _map4c_encode
except Exception:                      # pragma: no cover - absent on an old image
    _map4c_encode = None


# Which fingerprint the similarity views are built on. Both are minhash
# signatures of the same length, compared by position-wise agreement, so the
# distance code and the MDS do not care which one produced them.
#
# MAP4C is atom-pair based and chirality-aware; MHFP is substructure (SMILES
# shingle) based. Measured on a real 640-design job of 4-residue peptides, MHFP
# reduced better: 42% of the distance structure in three axes against 34%, at
# half the time. That is not the same as being more informative -- a finer
# fingerprint can produce distances that genuinely do not fit in three
# dimensions -- so the choice is left switchable rather than argued about.
FINGERPRINT = os.environ.get('PREDPEP_FINGERPRINT', 'map4c').strip().lower()


def _make_encoder():
    """(encode_fn, backend_name). encode_fn(mol) -> list[int] or raises."""
    if FINGERPRINT in ('map4c', 'map4') and _map4c_encode is not None:
        return ((lambda mol: list(_map4c_encode(mol, max_radius=2,
                                                n_permutations=PERMUTATIONS,
                                                seed=SEED))),
                'MAP4C (mapchiral)')
    if _RDKIT_MHFP is not None:
        enc = _RDKIT_MHFP.MHFPEncoder(PERMUTATIONS, SEED)
        return (lambda mol: list(enc.EncodeMol(mol))), 'MHFP (rdkit.rdMHFPFingerprint)'
    py = _PythonMinHash()
    return py.encode_mol, 'MHFP (python-int minhash)'


def generate_tmap_coordinates(sequences):
    """(x, y, s, t, valid_indices). Empty lists when no layout is possible."""
    if not sequences:
        return [], [], [], [], []

    try:
        encode, backend = _make_encoder()
    except ImportError as e:
        print(f'DEBUG: TMAP disabled, no usable MHFP encoder: {e}')
        return [], [], [], [], []

    print(f'DEBUG: TMAP processing {len(sequences)} sequences '
          f'(encoder: {backend}).')

    fps = []
    valid_indices = []
    skipped_blank = 0
    skipped_nomol = 0
    hash_errors = 0
    first_error = None
    distinct = set()

    for i, seq in enumerate(sequences):
        clean_seq = ''.join(str(seq).split()).strip().upper()
        if not clean_seq or clean_seq in ('NAN', 'NONE', 'NULL'):
            skipped_blank += 1
            continue

        mol = Chem.MolFromSequence(clean_seq)
        if mol is None:
            mol = Chem.MolFromFASTA(f'>pep\n{clean_seq}')
        if mol is None:
            skipped_nomol += 1
            continue

        try:
            raw_fp = encode(mol)
            fp_list = [int(h) & 0xFFFFFFFF for h in raw_fp]
            fps.append(tm.VectorUint(fp_list))
            valid_indices.append(i)
            distinct.add(tuple(fp_list))
        except Exception as e:                      # noqa: BLE001
            hash_errors += 1
            if first_error is None:
                first_error = f'{type(e).__name__}: {e} (at {clean_seq})'
            continue

    # One line that accounts for every input, so a thin panel is explained
    # rather than guessed at.
    print(f'DEBUG: TMAP inputs {len(sequences)}: encoded {len(fps)}, '
          f'blank {skipped_blank}, unparseable {skipped_nomol}, '
          f'hash errors {hash_errors}'
          + (f' — first: {first_error}' if first_error else ''))

    if len(fps) < 2:
        print(f'DEBUG: TMAP failed. Only {len(fps)} valid molecule(s).')
        return [], [], [], [], []

    # A tree needs more than one distinct point. Short peptides can encode
    # fine and still collapse: a 5-mer has a small molecular graph, so many
    # designs share a fingerprint.
    if len(distinct) < 2:
        print(f'DEBUG: TMAP aborted — all {len(fps)} fingerprints are '
              'identical, so no tree structure exists.')
        return [], [], [], [], []
    print(f'DEBUG: TMAP {len(distinct)} distinct fingerprint(s) among '
          f'{len(fps)} design(s).')

    try:
        lf = tm.LSHForest(PERMUTATIONS, 64)
        lf.batch_add(fps)
        lf.index()

        cfg = tm.LayoutConfiguration()
        cfg.node_size = 1 / 25
        cfg.k = min(len(fps) - 1, 30)
        cfg.sl_extra_scaling_steps = 5

        # ScalingType member names differ between tmap builds: the original
        # code set RelativeToVisible, which this build does not have, so the
        # layout raised AttributeError on every job. Pick the first name that
        # actually exists and say which, rather than hardcoding one and
        # failing. If none match, leave the library default alone -- a default
        # layout is far better than no tree.
        for name in ('RelativeToDrawing', 'RelativeToAvgLength',
                     'RelativeToDesiredLength', 'Absolute'):
            member = getattr(tm.ScalingType, name, None)
            if member is not None:
                cfg.sl_scaling_type = member
                print(f'DEBUG: TMAP sl_scaling_type = ScalingType.{name}')
                break
        else:
            available = [n for n in dir(tm.ScalingType) if not n.startswith('_')]
            print('DEBUG: TMAP no known ScalingType member; using the library '
                  f'default. Available: {available}')

        # Returns (x, y, s, t) on older tmap and (x, y, s, t, GraphProperties)
        # on newer ones. The original code unpacked exactly four, so this build
        # raised "too many values to unpack (expected 4)" on every job -- the
        # third independent bug in this function, behind the NumPy overflow and
        # the ScalingType name. Take the first four positionally and ignore any
        # extras rather than asserting a tuple length.
        layout = tm.layout_from_lsh_forest(lf, cfg)
        if len(layout) < 4:
            print(f'DEBUG: TMAP layout returned {len(layout)} value(s); '
                  'expected at least 4 (x, y, s, t).')
            return [], [], [], [], []
        x, y, s, t = layout[0], layout[1], layout[2], layout[3]
        if len(layout) > 4:
            print(f'DEBUG: TMAP layout returned {len(layout)} values; '
                  'using the first four and ignoring the rest.')

        print(f'DEBUG: TMAP successful. {len(valid_indices)} sequences in the tree.')
        return list(x), list(y), list(s), list(t), valid_indices

    except Exception as e:                          # noqa: BLE001
        print(f'DEBUG: TMAP layout internal error: {type(e).__name__}: {e}')
        return [], [], [], [], []
