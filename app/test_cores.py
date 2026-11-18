"""Check _effective_cores: cap at available parallelism, never raise."""
import ast, os, sys
sys.path.insert(0, '/home/claude')

SRC = open('/home/claude/predPEP.py').read()
BASE = '/tmp/claude-0/-home-claude/369a0ed5-9794-56c5-abbd-e942b9176f23/scratchpad'
residues = {'/fake/21.pdb': 21, '/fake/9.pdb': 9, '/fake/none.pdb': None}
ns = {'os': os, 'count_peptide_residues': lambda p: residues.get(p, 30)}
for node in ast.parse(SRC).body:
    if isinstance(node, ast.FunctionDef) and node.name == '_effective_cores':
        exec(compile(ast.Module([node], []), '<x>', 'exec'), ns)
    if isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', '') == 'CORE_FLOOR':
        exec(compile(ast.Module([node], []), '<x>', 'exec'), ns)
eff = ns['_effective_cores']

fails = []


def check(label, got, want):
    ok = got == want
    print(f'  {"ok  " if ok else "FAIL"}  {label}: {got!r}' + ('' if ok else f'   (want {want!r})'))
    if not ok:
        fails.append(label)


print('[1] anchors are the bound when the spec names them')
# s2 as it actually ran: 2 anchors, 8 cores asked for
c, note = eff(8, '/fake/9.pdb', {'anchor_positions': [1, 9]})
check('2 anchors, 8 asked -> 2', c, 2)
check('reason mentions anchors', 'anchor' in (note or ''), True)
c, note = eff(8, '/fake/21.pdb', {'anchor_positions': [1, 21]})
check('s1: 2 anchors, 8 asked -> 2', c, 2)

print('\n[2] a request at or below the bound is left alone')
check('8 asked, 30 anchors -> 8', eff(8, '/fake/21.pdb',
      {'anchor_positions': list(range(1, 31))})[0], 8)
check('no note when untouched', eff(8, '/fake/21.pdb',
      {'anchor_positions': list(range(1, 31))})[1], None)
check('2 asked, 30 anchors -> 2 (user choice respected)',
      eff(2, '/fake/21.pdb', {'anchor_positions': list(range(1, 31))})[0], 2)
check('1 asked is not raised to the floor',
      eff(1, '/fake/21.pdb', {'anchor_positions': list(range(1, 31))})[0], 1)

print('\n[3] residues are the fallback with no anchor list')
check('no spec, 9 residues, 8 asked -> 8 (9 > 8)',
      eff(8, '/fake/9.pdb', None)[0], 8)
check('no spec, 9 residues, 16 asked -> 9',
      eff(16, '/fake/9.pdb', None)[0], 9)
check('reason mentions residues',
      'residue' in (eff(16, '/fake/9.pdb', None)[1] or ''), True)
check('empty spec behaves as no spec',
      eff(16, '/fake/9.pdb', {})[0], 9)
check('spec with empty anchor list falls back to residues',
      eff(16, '/fake/9.pdb', {'anchor_positions': []})[0], 9)

print('\n[4] the floor of 2, and unknown residue counts')
check('1 anchor, 8 asked -> 2 (floor)', eff(8, '/fake/21.pdb',
      {'anchor_positions': [5]})[0], 2)
check('unreadable pdb, no spec -> unchanged',
      eff(8, '/fake/none.pdb', None)[0], 8)

print('\n[5] legacy v1 spec uses its resSeq anchors')
check('v1 anchors counted', eff(8, '/fake/21.pdb', {'anchors': [1, 21, 22, 30]})[0], 4)

print()
print('FAILURES: ' + (', '.join(fails) if fails else 'none'))
sys.exit(1 if fails else 0)
