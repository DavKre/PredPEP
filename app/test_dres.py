#!/home/spacepep/miniforge3/envs/predPEP/bin/python
"""Checks for the D-residue and Rosetta-supplied halves of the catalogue.

Builds a fake params/rotlib pair from the real CSV, deliberately OMITTING the
fifteen codes that have no params on hakg, so the fixture reproduces the real
node: eight of those must come back as Rosetta-supplied, five must be withheld
as name collisions, and CSE and NAL must be absent.
"""
import os
import sys
import csv
import json
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import residue_catalogue as rc

HERE = os.path.dirname(os.path.abspath(__file__))
CSV = os.path.join(HERE, 'catalogue.csv')

# Measured on hakg: offerable-category codes with no .params file.
GAP = ('004', 'ABA', 'BB8', 'BCS', 'BIF', 'CSE', 'DAB', 'DPP', 'HLU',
       'IGL', 'NAL', 'OAS', 'ORN', 'MOT', 'MPH')
NO_ROTLIB = ('STA', 'AZD')

fails = []


def check(label, got, want):
    ok = got == want
    print('  %s  %s: %r%s' % ('ok  ' if ok else 'FAIL', label, got,
                              '' if ok else '   (want %r)' % (want,)))
    if not ok:
        fails.append(label)


def build_fakes(tmp):
    pdir, rdir = os.path.join(tmp, 'params'), os.path.join(tmp, 'rotlib')
    os.makedirs(pdir)
    os.makedirs(rdir)
    with open(CSV, newline='', errors='replace') as f:
        rows = list(csv.DictReader(f))
    n = 0
    for r in rows:
        cat = (r.get('CATEGORY') or '').strip()
        code = (r.get('L_FOLDER') or '').strip()
        if cat not in rc.NONCANONICAL_CATEGORIES or not code or code in GAP:
            continue
        with open(os.path.join(pdir, code + '.params'), 'w') as fh:
            fh.write('NAME %s\nIO_STRING %s X\n'
                     'PROPERTIES PROTEIN ALPHA_AA L_AA\n' % (code, code))
        if code not in NO_ROTLIB:
            open(os.path.join(rdir, code + '.rotlib'), 'w').close()
        n += 1
    return pdir, rdir, n


tmp = tempfile.mkdtemp()
try:
    pdir, rdir, n_local = build_fakes(tmp)
    os.environ.pop('PREDPEP_D_RESIDUES', None)
    # Isolate from whatever createFF has installed on this node. Groups 1-18
    # assume no retired list and no het-code map; without this they pick up
    # the real files and fail on the node while passing in a bare checkout --
    # which is exactly what happened.
    os.environ['PREDPEP_RETIRED_CSV'] = os.path.join(tmp, 'no-retired.csv')
    os.environ['PREDPEP_D_NAMES_CSV'] = os.path.join(tmp, 'no-names.csv')
    # Groups 1-18 exercise the Rosetta-supplied path, which is withheld by
    # default because the pre-talaris type set cannot resolve it. Group 23
    # tests that default.
    os.environ['PREDPEP_ALLOW_ROSETTA_NCAA'] = '1'
    cat = rc.load_catalogue(CSV, pdir, rdir)
    assert not cat['error'], cat['error']
    rc._cache = cat

    by_code = {e['code']: e for e in cat['noncanonical']}
    groups = {g['category']: g['codes'] for g in cat['groups']}
    local_l = [e['code'] for e in cat['noncanonical']
               if e['hand'] == 'L' and e['source'] == 'local']
    some_l = local_l[0]

    print('\n[0] the fixture is isolated from the node\'s createFF files')
    check('no retired list in force', cat['counts']['retired'], 0)
    check('enforcement reported off', cat['retired_enforced'], False)
    check('no het-code map in force', rc.load_d_het_codes(), {})

    print('\n[1] the five groups, in GROUP_ORDER')
    check('group order', [g['category'] for g in cat['groups']],
          list(rc.GROUP_ORDER))

    print('\n[2] glycine is achiral and stays that way')
    check('GLY offered as L canonical',
          'GLY' in {e['code'] for e in cat['canonical']}, True)
    check('DGLY not invented', 'DGLY' in by_code, False)
    check('19 D-canonicals (20 minus glycine)',
          len(groups[rc.D_CANONICAL_CATEGORY]), 19)

    print('\n[3] the D-canonicals are SELECTABLE, not read-only')
    check('DALA offerable', 'DALA' in by_code, True)
    check('DALA not in the read-only section',
          'DALA' in {e['code'] for e in cat['canonical']}, False)
    check('ALA still read-only',
          'ALA' in {e['code'] for e in cat['canonical']}, True)

    print('\n[4] the eight Rosetta-supplied residues are recovered')
    for c in ('ABA', 'BB8', 'BCS', 'DAB', 'DPP', 'IGL', 'ORN'):
        check('%s offered' % c, c in by_code, True)
        check('%s needs no params of ours' % c, cat['params_for'][c], None)
        check('%s marked as Rosetta\'s' % c, by_code[c]['source'], 'rosetta')
    check('none of them has local params',
          [c for c in rc.ROSETTA_SUPPLIED if c in os.listdir(pdir)], [])
    check('rotlib is unknown, not False',
          by_code['ABA']['rotlib'], None)

    print('\n[5] BIF is offered under its REAL type name')
    check('BiphenylAlanine offered', 'BiphenylAlanine' in by_code, True)
    check('the bare code is not', 'BIF' in by_code, False)
    check('csv_code still records BIF',
          by_code['BiphenylAlanine']['csv_code'], 'BIF')
    check('CODE_RE would have rejected it',
          bool(rc.CODE_RE.match('BiphenylAlanine')), False)
    check('TYPE_NAME_RE accepts it',
          bool(rc.TYPE_NAME_RE.match('BiphenylAlanine')), True)
    check('its mirror prefixes the type name, not the code',
          'DBiphenylAlanine' in by_code, True)
    check('DBIF not invented', 'DBIF' in by_code, False)

    print('\n[6] the five name collisions are withheld, with a reason')
    coll = {c['code']: c['reason'] for c in cat['collisions']}
    check('all five withheld', sorted(coll), ['004', 'HLU', 'MOT', 'MPH', 'OAS'])
    check('none offered as L', [c for c in coll if c in by_code], [])
    check('none offered as D', [c for c in coll if 'D' + c in by_code], [])
    check('HLU reason names homoleucine', 'homoleucine' in coll['HLU'], True)
    check('MPH reason names the phosphonate',
          'PHOSPHONATE' in coll['MPH'], True)
    check('counted', cat['counts']['collisions'], 5)

    print('\n[7] CSE and NAL are in neither set')
    for c in ('CSE', 'NAL'):
        check('%s not offered' % c, c in by_code, False)
        check('%s not withheld as a collision' % c, c in coll, False)
        check('%s mirror absent' % c, 'D' + c in by_code, False)

    print('\n[8] params_for: the palette name is not the params file')
    check('an L noncanonical maps to itself',
          cat['params_for'][some_l], some_l)
    check('its mirror maps to the L parent',
          cat['params_for']['D' + some_l], some_l)
    check('DALA needs nothing', cat['params_for']['DALA'], None)
    check('DABA needs nothing (Rosetta mirrors its own ABA)',
          cat['params_for']['DABA'], None)

    print('\n[9] params_paths resolves, de-duplicates, builds no stray path')
    both = rc.params_paths([some_l, 'D' + some_l])
    check('L and D pass one file', len(both), 1)
    check('and it is the L parent', os.path.basename(both[0]),
          '%s.params' % some_l)
    check('a Rosetta-supplied code contributes nothing',
          rc.params_paths(['ABA', 'BiphenylAlanine']), [])
    check('a D-canonical contributes nothing', rc.params_paths(['DALA']), [])
    check('an unknown code contributes nothing', rc.params_paths(['ZZZ']), [])
    check('a path traversal attempt contributes nothing',
          rc.params_paths(['../../etc/passwd', 'a/b']), [])

    print('\n[10] rotamer libraries inherit through the mirror')
    check('STA has none', by_code['STA']['rotlib'], False)
    check('DSTA inherits that', by_code['DSTA']['rotlib'], False)
    check('a D-canonical has one', by_code['DALA']['rotlib'], True)
    check('a D of a Rosetta NCAA is unknown', by_code['DABA']['rotlib'], None)

    print('\n[11] FoldX: False for every D entry, and the list matches the count')
    check('foldx_known agrees with foldx_scorable',
          len(cat['foldx_known']), cat['counts']['foldx_scorable'])
    check('no withheld code appears in foldx_known',
          [c for c in cat['foldx_known']
           if c in rc.NAME_COLLISIONS or c in rc.PENDING_BUILD], [])
    check('all D foldx are False',
          {e['foldx'] for e in cat['noncanonical'] if e['hand'] == 'D'},
          {False})

    print('\n[12] the het-code gap is reported, not truncated')
    check('DALA inherits DAL from the CSV', by_code['DALA']['pdb_code'], 'DAL')
    no_het = sorted(e['code'] for e in cat['noncanonical']
                    if e['hand'] == 'D' and not e['pdb_code'])
    check('DGME among them', 'DGME' in no_het, True)
    check('DOAS not among them (OAS is withheld)', 'DOAS' in no_het, False)
    check('count matches the list', cat['counts']['d_without_pdb_code'],
          len(no_het))

    print('\n[13] validation accepts type names, still refuses L canonicals')
    acc, rej = rc.validate_selection(
        ['DALA', 'BiphenylAlanine', 'D' + some_l, 'ALA', 'HLU', 'no pe!'], cat)
    check('DALA accepted', 'DALA' in acc, True)
    check('BiphenylAlanine accepted', 'BiphenylAlanine' in acc, True)
    check('ALA refused as always-available',
          [r['reason'] for r in rej if r['value'] == 'ALA'],
          ['canonical residue, always available and not selectable here'])
    check('a withheld collision is refused',
          [r['reason'] for r in rej if r['value'] == 'HLU'],
          ['no installed params for this residue'])
    check('whitespace refused',
          [r['value'] for r in rej if r['value'] == 'no pe!'], ['no pe!'])

    print('\n[14] the palette file accepts long type names')
    p = os.path.join(tmp, 'palette.txt')
    check('writes all three',
          rc.write_palette_file(['DALA', 'BiphenylAlanine', some_l], p), 3)
    check('contents', [l.strip() for l in open(p) if not l.startswith('#')],
          ['DALA', 'BiphenylAlanine', some_l])
    try:
        rc.write_palette_file(['oops; rm -rf /'], p)
        check('refuses a shell-ish name', 'no raise', 'ValueError')
    except ValueError:
        check('refuses a shell-ish name', 'ValueError', 'ValueError')

    print('\n[15] counts add up')
    check('L offerable == local params + Rosetta-supplied',
          cat['counts']['l_offerable'], n_local + len(rc.ROSETTA_SUPPLIED))
    check('every L has a D except glycine',
          cat['counts']['d_offerable'],
          cat['counts']['l_offerable'] + 19)
    check('rosetta_supplied counts both hands',
          cat['counts']['rosetta_supplied'],
          2 * len(rc.ROSETTA_SUPPLIED) + 19)

    print('\n[16] the kill switch removes the D half only')
    os.environ['PREDPEP_D_RESIDUES'] = '0'
    off = rc.load_catalogue(CSV, pdir, rdir)
    check('no D entries', off['counts']['d_offerable'], 0)
    check('L side untouched', off['counts']['l_offerable'],
          cat['counts']['l_offerable'])
    check('Rosetta-supplied L still there',
          'BiphenylAlanine' in {e['code'] for e in off['noncanonical']}, True)
    check('collisions still reported', off['counts']['collisions'], 5)
    check('only the two L groups', [g['category'] for g in off['groups']],
          list(rc.NONCANONICAL_CATEGORIES))
    os.environ.pop('PREDPEP_D_RESIDUES', None)

    print('\n[17] public_payload stays JSON-able and leaks no server path')
    rc._cache = cat
    pay = rc.public_payload()
    json.dumps(pay)
    check('no params_dir', 'params_dir' in pay, False)
    check('no params_for either', 'params_for' in pay, False)
    check('collisions exposed', len(pay['collisions']), 5)
    check('d_residues reported', pay['d_residues'], True)

    print('\n[18] the rename window: pending, then self-clearing')
    ren = {'004': '400', 'MOT': 'TOM', 'OAS': 'SAO',
           'MPH': 'HPM', 'HLU': 'ULH'}
    rcsv = os.path.join(tmp, 'renamed.csv')
    with open(CSV, newline='', errors='replace') as f:
        rows = list(csv.DictReader(f))
    flds = list(rows[0].keys())
    for r in rows:
        c = (r.get('L_FOLDER') or '').strip()
        if c in ren:
            r['L_FOLDER'] = ren[c]
            # D_FOLDER is the het code for the D form of the MOLECULE, which
            # our internal rename does not change. It stays.
    with open(rcsv, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=flds)
        w.writeheader()
        w.writerows(rows)

    pend = rc.load_catalogue(rcsv, pdir, rdir)
    pmap = {p['code']: p['reason'] for p in pend['pending']}
    offered = {e['code'] for e in pend['noncanonical']}
    check('all five pending', sorted(pmap),
          ['400', 'HPM', 'SAO', 'TOM', 'ULH'])
    check('none offered yet', [c for c in pmap if c in offered], [])
    check('no mirrors either', [c for c in pmap if 'D' + c in offered], [])
    check('the old collisions are gone', pend['counts']['collisions'], 0)
    check('the reason names the old code', '004' in pmap['400'], True)
    check('the eight Rosetta ones are unaffected',
          'BiphenylAlanine' in offered, True)

    open(os.path.join(pdir, '400.params'), 'w').close()
    built = rc.load_catalogue(rcsv, pdir, rdir)
    bcodes = {e['code']: e for e in built['noncanonical']}
    check('400 offered once its params land', '400' in bcodes, True)
    check('and no longer pending',
          '400' in {p['code'] for p in built['pending']}, False)
    check('the other four still pending', built['counts']['pending'], 4)
    check('its mirror appears too', 'D400' in bcodes, True)
    check('mirror keeps the molecule\'s het code',
          bcodes['D400']['pdb_code'], '04D')
    check('and resolves to the renamed params',
          built['params_for']['D400'], '400')
    os.remove(os.path.join(pdir, '400.params'))

    print('\n[18b] one hand per molecule, enforced server-side')
    pairs = rc.validate_selection([some_l, 'D' + some_l, 'DALA'], cat)
    check('both members rejected', sorted(x['value'] for x in pairs[1]),
          sorted([some_l, 'D' + some_l]))
    check('the unpaired one survives', pairs[0], ['DALA'])
    check('the reason names the other hand',
          ('D' + some_l) in [x['reason'] for x in pairs[1]
                             if x['value'] == some_l][0], True)
    check('either hand alone is fine',
          rc.validate_selection(['D' + some_l], cat)[0], ['D' + some_l])
    check('BIF pair joins on l_code, not on the name',
          sorted(x['value'] for x in
                 rc.validate_selection(['BiphenylAlanine',
                                        'DBiphenylAlanine'], cat)[1]),
          ['BiphenylAlanine', 'DBiphenylAlanine'])

    print('\n[19] retired residues are withheld, with the keeper named')
    ret_csv = os.path.join(tmp, 'retired.csv')
    with open(ret_csv, 'w', newline='') as f:
        f.write('code,stereo,keep,reason\n')
        f.write('SEP,L,SE2,phosphoserine monoanion; the dianion SE2 '
                'dominates at pH 7.4\n')
        f.write('MHO,L,SME,identical to SME atom for atom\n')
        f.write('DAP,L,DPP,neutral side-chain amine\n')
    het_csv = os.path.join(tmp, 'names.csv')
    with open(het_csv, 'w', newline='') as f:
        f.write('code,name,was,L_partner\n')
        f.write('MGD,5-o-methyl-glutamic acid,DGME,GME\n')
        f.write('GAD,glutamine hydroxamate,DHGA,HGA\n')
    os.environ['PREDPEP_RETIRED_CSV'] = ret_csv
    os.environ['PREDPEP_D_NAMES_CSV'] = het_csv
    os.environ.pop('PREDPEP_ALLOW_RETIRED', None)

    r = rc.load_catalogue(CSV, pdir, rdir)
    rcodes = {e['code'] for e in r['noncanonical']}
    rmap = {x['code']: x for x in r['retired']}
    check('SEP withheld', 'SEP' in rmap, True)
    check('and not offered', 'SEP' in rcodes, False)
    check('nor its mirror', 'DSEP' in rcodes, False)
    check('the keeper is named', rmap['SEP']['keep'], 'SE2')
    check('the keeper IS offered', 'SE2' in rcodes, True)
    check('MHO withheld in favour of SME', rmap['MHO']['keep'], 'SME')
    check('DAP has no params so is not double-reported',
          'DAP' in rmap, False)
    check('enforcement is advertised', r['retired_enforced'], True)
    check('a withheld retired code fails validation',
          [x['value'] for x in rc.validate_selection(['SEP'], r)[1]], ['SEP'])

    print('\n[20] the het codes for the eight come from residue_names.csv')
    het = {e['code']: e['pdb_code'] for e in r['noncanonical']}
    check('DGME picks up MGD', het.get('DGME'), 'MGD')
    check('DHGA picks up GAD', het.get('DHGA'), 'GAD')
    check('an unmapped 4-char value still reports absent',
          het.get('DMEN'), '')
    check('3-char values are untouched', het.get('DALA'), 'DAL')

    print('\n[21] PREDPEP_ALLOW_RETIRED=1 hands the decision back')
    os.environ['PREDPEP_ALLOW_RETIRED'] = '1'
    a = rc.load_catalogue(CSV, pdir, rdir)
    acodes = {e['code'] for e in a['noncanonical']}
    check('SEP offered again', 'SEP' in acodes, True)
    check('nothing reported as retired', a['counts']['retired'], 0)
    check('enforcement flag is off', a['retired_enforced'], False)
    check('the keeper is still there too', 'SE2' in acodes, True)
    for k in ('PREDPEP_ALLOW_RETIRED', 'PREDPEP_RETIRED_CSV',
              'PREDPEP_D_NAMES_CSV'):
        os.environ.pop(k, None)

    print('\n[22] a missing retired/names file withholds nothing')
    os.environ['PREDPEP_RETIRED_CSV'] = os.path.join(tmp, 'nope.csv')
    os.environ['PREDPEP_D_NAMES_CSV'] = os.path.join(tmp, 'nope2.csv')
    m = rc.load_catalogue(CSV, pdir, rdir)
    check('no error', m['error'], None)
    check('nothing retired', m['counts']['retired'], 0)
    check('enforcement honestly reported off', m['retired_enforced'], False)
    check('SEP offered', 'SEP' in {e['code'] for e in m['noncanonical']}, True)
    for k in ('PREDPEP_RETIRED_CSV', 'PREDPEP_D_NAMES_CSV'):
        os.environ.pop(k, None)

    print('\n[23] the l-ncaa set is withheld by default: pre-talaris cannot load it')
    os.environ.pop('PREDPEP_ALLOW_ROSETTA_NCAA', None)
    os.environ['PREDPEP_RETIRED_CSV'] = os.path.join(tmp, 'nope.csv')
    os.environ['PREDPEP_D_NAMES_CSV'] = os.path.join(tmp, 'nope2.csv')
    u = rc.load_catalogue(CSV, pdir, rdir)
    ucodes = {e['code'] for e in u['noncanonical']}
    unav = {x['code']: x['reason'] for x in u['unavailable']}
    check('all eight withheld', sorted(unav), sorted(rc.ROSETTA_SUPPLIED))
    check('none offered as L', [c for c in unav if c in ucodes], [])
    check('BiphenylAlanine gone too', 'BiphenylAlanine' in ucodes, False)
    check('and its mirror', 'DBiphenylAlanine' in ucodes, False)
    check('the reason names the cause',
          'pre-talaris' in unav['ABA'], True)
    check('counted', u['counts']['unavailable'], 8)
    check('16 fewer than with the override',
          cat['counts']['noncanonical'] - u['counts']['noncanonical'], 16)
    check('the D-canonicals are unaffected', 'DALA' in ucodes, True)
    check('and local NCAAs are unaffected', 'D0A1' in ucodes, True)
    for k in ('PREDPEP_RETIRED_CSV', 'PREDPEP_D_NAMES_CSV'):
        os.environ.pop(k, None)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print('\nFAILURES: ' + (', '.join(fails) if fails else 'none'))
sys.exit(1 if fails else 0)
