#!/home/spacepep/miniforge3/envs/predPEP/bin/python
"""Read-only audit of the NCAA residue library's rotamer coverage.

Answers four questions that decide what, if anything, needs generating:

  1. Which installed residues have a .rotlib and which do not.
  2. Whether each .params file actually POINTS at its library. Rosetta finds an
     NCAA rotamer library only through the params file's own NCAA_ROTLIB_PATH
     field -- there is no command-line flag for it, and nothing in the pepspec
     pipeline passes one. A .rotlib sitting in a directory no params file names
     is dead weight: Rosetta will not load it, and the residue is packed from
     its input conformation alone.
  3. How many ROTAMERIC chi angles each residue has, since that is what sets
     the cost of generating a library (proton chi are sampled separately and
     need no library).
  4. Whether this Rosetta build ships MakeRotLib.

Writes nothing, changes nothing, runs in seconds.

  python3 rotlib_audit.py                 # uses the app's own defaults
  python3 rotlib_audit.py --json out.json # machine-readable too
"""

import argparse
import json
import os
import re
import sys

DEFAULT_PARAMS = '/opt/spacepep-createFF/all_aas3/generated_params_rotlib'
DEFAULT_ROTLIBS = '/opt/spacepep-createFF/all_aas3/generated_rotlibs'
ROSETTA_ROOT = '/usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408'


def parse_params(path):
    """The handful of fields that bear on rotamer handling."""
    out = {'name': None, 'aa': None, 'chi': 0, 'proton_chi': set(),
           'rotlib_path': None, 'rotlib_bins': None, 'rotamer_aa': None,
           'nrchi_symmetric': False, 'semirotameric': False}
    try:
        with open(path, errors='replace') as f:
            for line in f:
                parts = line.split()
                if not parts:
                    continue
                key = parts[0]
                if key == 'NAME' and len(parts) > 1:
                    out['name'] = parts[1]
                elif key == 'AA' and len(parts) > 1:
                    out['aa'] = parts[1]
                elif key == 'CHI':
                    out['chi'] += 1
                elif key == 'PROTON_CHI' and len(parts) > 1:
                    try:
                        out['proton_chi'].add(int(parts[1]))
                    except ValueError:
                        pass
                elif key == 'NCAA_ROTLIB_PATH' and len(parts) > 1:
                    out['rotlib_path'] = parts[1]
                elif key == 'NCAA_ROTLIB_NUM_ROTAMER_BINS':
                    out['rotlib_bins'] = parts[1:]
                elif key == 'ROTAMER_AA' and len(parts) > 1:
                    out['rotamer_aa'] = parts[1]
                elif key == 'NCAA_SEMIROTAMERIC':
                    out['semirotameric'] = True
                elif key == 'NRCHI_SYMMETRIC':
                    out['nrchi_symmetric'] = True
    except OSError as e:
        out['error'] = str(e)
    out['rotameric_chi'] = max(out['chi'] - len(out['proton_chi']), 0)
    return out


def rotlib_shape(path, probe_lines=400):
    """Backbone-dependent or not, and how many rotamer records.

    A Dunbrack-style backbone-dependent library carries phi and psi in its
    leading columns, so the first two numeric fields vary across the file. A
    backbone-independent one has no such columns. Decided by looking, not by
    assuming -- the file format has been misread here before.
    """
    try:
        with open(path, errors='replace') as f:
            head = [ln for _, ln in zip(range(probe_lines), f)]
        size = os.path.getsize(path)
    except OSError as e:
        return {'error': str(e)}
    rows = [ln.split() for ln in head
            if ln.strip() and not ln.lstrip().startswith('#')]
    rows = [r for r in rows if len(r) >= 3]
    if not rows:
        return {'bytes': size, 'kind': 'empty or unparsed', 'lines': len(head)}

    def numeric(tok):
        try:
            float(tok)
            return True
        except ValueError:
            return False

    # A MakeRotLib record leads with the residue NAME, then phi and psi:
    #   0A1 -180.0 -180.0 9999 1 1 0 0 0.843 62.1 180.0 ...
    # so drop a leading non-numeric token before looking for the angles. Not
    # doing this reads every real library as "non-Dunbrack".
    rows = [r[1:] if r and not numeric(r[0]) else r for r in rows]
    rows = [r for r in rows if len(r) >= 3]
    if not rows:
        return {'bytes': size, 'kind': 'empty or unparsed', 'lines': len(head)}

    first_two_numeric = sum(1 for r in rows if numeric(r[0]) and numeric(r[1]))
    kind = 'unknown'
    if first_two_numeric >= max(1, len(rows) // 2):
        phis = {r[0] for r in rows if numeric(r[0])}
        psis = {r[1] for r in rows if numeric(r[1])}
        kind = ('backbone-dependent' if len(phis) > 1 or len(psis) > 1
                else 'backbone-independent (single phi/psi)')
    else:
        kind = 'backbone-independent or non-Dunbrack layout'
    return {'bytes': size, 'kind': kind, 'columns': len(rows[0]),
            'sample': ' '.join(rows[0][:8])}


def find_makerotlib():
    hits = []
    for root in (os.path.join(ROSETTA_ROOT, 'main', 'source', 'bin'),
                 os.path.join(ROSETTA_ROOT, 'main', 'bin'), '/usr/local/bin'):
        try:
            for fn in os.listdir(root):
                if fn.lower().startswith('makerotlib'):
                    hits.append(os.path.join(root, fn))
        except OSError:
            continue
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--params', default=os.environ.get('PREDPEP_PARAMS_DIR',
                                                       DEFAULT_PARAMS))
    ap.add_argument('--rotlibs', default=os.environ.get('PREDPEP_ROTLIB_DIR',
                                                        DEFAULT_ROTLIBS))
    ap.add_argument('--json')
    a = ap.parse_args()

    print('params  : %s' % a.params)
    print('rotlibs : %s' % a.rotlibs)
    for d in (a.params, a.rotlibs):
        if not os.path.isdir(d):
            print('  !! not a directory -- nothing to audit here')

    try:
        pfiles = sorted(fn for fn in os.listdir(a.params)
                        if fn.endswith('.params'))
    except OSError as e:
        print('cannot read params dir: %s' % e)
        return 1
    try:
        libs = {fn[:-7] for fn in os.listdir(a.rotlibs)
                if fn.endswith('.rotlib')}
    except OSError:
        libs = set()

    print('\n%d .params, %d .rotlib\n' % (len(pfiles), len(libs)))

    rows, orphan_libs = [], set(libs) - {fn[:-7] for fn in pfiles}
    for fn in pfiles:
        code = fn[:-7]
        p = parse_params(os.path.join(a.params, fn))
        p['code'] = code
        p['has_lib_file'] = code in libs

        # Does the params file name a library, and does that name resolve?
        ref = p['rotlib_path']
        p['points_at'] = None
        p['resolves'] = None
        if ref:
            cand = ref if os.path.isabs(ref) else None
            tries = [cand] if cand else [
                os.path.join(a.params, ref),
                os.path.join(a.rotlibs, ref),
                os.path.join(a.rotlibs, os.path.basename(ref)),
            ]
            hit = next((t for t in tries if t and os.path.exists(t)), None)
            p['points_at'] = ref
            p['resolves'] = bool(hit)

        rows.append(p)

    # ---- the four findings
    needs = [r for r in rows if r['rotameric_chi'] > 0]
    # One bucket each, by cause rather than by symptom: a params file naming a
    # library that is not there is a BROKEN POINTER, not separately a residue
    # with NO LIBRARY, even though both are true of it.
    wired = [r for r in needs if r['rotlib_path'] and r['resolves']]
    lib_broken = [r for r in needs if r['rotlib_path'] and not r['resolves']]
    lib_unreferenced = [r for r in needs
                        if not r['rotlib_path'] and r['has_lib_file']]
    no_lib = [r for r in needs if not r['rotlib_path'] and not r['has_lib_file']
              and not r['rotamer_aa']]
    inherits = [r for r in rows if r['rotamer_aa']]
    no_chi = [r for r in rows if r['rotameric_chi'] == 0]

    def names(rs, n=18):
        s = sorted(r['code'] for r in rs)
        return ', '.join(s[:n]) + (' ... (+%d)' % (len(s) - n) if len(s) > n else '')

    print('WIRED UP          %4d  params names a library that exists' % len(wired))
    print('LIBRARY IGNORED   %4d  .rotlib on disk, no NCAA_ROTLIB_PATH in params'
          % len(lib_unreferenced))
    if lib_unreferenced:
        print('                        %s' % names(lib_unreferenced))
    print('BROKEN POINTER    %4d  params names a library that is not there'
          % len(lib_broken))
    if lib_broken:
        print('                        %s' % names(lib_broken))
    print('NO LIBRARY        %4d  rotameric chi, no library and no inheritance'
          % len(no_lib))
    if no_lib:
        print('                        %s' % names(no_lib))
    print('INHERITS          %4d  ROTAMER_AA borrows a canonical library'
          % len(inherits))
    if inherits:
        print('                        %s' % names(inherits))
    print('NO CHI            %4d  nothing to rotamerise (needs no library)'
          % len(no_chi))

    if orphan_libs:
        print('\nORPHAN LIBRARIES  %4d  .rotlib with no .params of that name at all'
              % len(orphan_libs))
        print('                        %s' % ', '.join(sorted(orphan_libs)[:18]))

    # ---- cost of generating what is missing
    print('\nrotameric chi among residues needing work '
          '(no library, broken pointer, or library not referenced):')
    hist = {}
    for r in no_lib + lib_broken + lib_unreferenced:
        hist[r['rotameric_chi']] = hist.get(r['rotameric_chi'], 0) + 1
    for k in sorted(hist):
        print('   %d chi : %3d residue(s)' % (k, hist[k]))

    # ---- what an existing library actually looks like
    sample = sorted(libs)[:3]
    if sample:
        print('\nshape of existing libraries:')
        for code in sample:
            sh = rotlib_shape(os.path.join(a.rotlibs, '%s.rotlib' % code))
            print('   %-5s %s' % (code, sh))

    mk = find_makerotlib()
    print('\nMakeRotLib binary : %s' % (', '.join(mk) if mk else 'NOT FOUND'))
    print('python            : %s' % sys.version.split()[0])

    if a.json:
        for r in rows:
            r['proton_chi'] = sorted(r['proton_chi'])
        with open(a.json, 'w') as f:
            json.dump({'params_dir': a.params, 'rotlib_dir': a.rotlibs,
                       'residues': rows,
                       'orphan_libraries': sorted(orphan_libs),
                       'makerotlib': mk}, f, indent=2)
        print('\nwrote %s' % a.json)
    return 0


if __name__ == '__main__':
    sys.exit(main())
