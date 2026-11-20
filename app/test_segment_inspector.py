"""Exercise _submit_segmented + _queue_one_job without starting Flask.

The two functions are lifted out of predPEP.py's source and run against stubbed
scheduler/auth/logger, so this checks the job-creation logic itself: how many
sub-jobs, what each one's spec and job.json say, the parent linkage, and that
the parent result directory does not survive as a phantom job.
"""
import ast, json, os, shutil, sys, tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.environ.get('PREDPEP_APP_DIR', '/opt/spacepep-predPEP'))
import segment_inspector, split_segments

if len(sys.argv) < 2:
    print('usage: test_submit_split.py <two-segment-complex.pdb>', file=sys.stderr)
    sys.exit(2)
PDB_IN = sys.argv[1]
if not os.path.isfile(PDB_IN):
    print(f'ERROR: no such file: {PDB_IN}', file=sys.stderr)
    sys.exit(2)

APP_DIR = os.environ.get('PREDPEP_APP_DIR', '/opt/spacepep-predPEP')
SRC = open(os.path.join(APP_DIR, 'predPEP.py')).read()
root = tempfile.mkdtemp(prefix='submit_')
RESULTS = os.path.join(root, 'results'); os.makedirs(RESULTS)
UPLOADS = os.path.join(root, 'uploads'); os.makedirs(UPLOADS)

enqueued = []


class _Sched:
    @staticmethod
    def enqueue(job_id, cmd, cpus):
        enqueued.append({'job_id': job_id, 'cmd': cmd, 'cpus': cpus})
        return len(enqueued) - 1


class _Auth:
    @staticmethod
    def audit(*a, **k):
        pass


class _Logger:
    def warning(self, m): pass
    def info(self, m): pass


class _App:
    logger = _Logger()


ns = {
    'json': json, 'os': os, 'shutil': shutil, 'sys': sys,
    'datetime': datetime, 'timezone': timezone,
    'segment_inspector': segment_inspector, 'split_segments': split_segments,
    'INSPECTOR_IMPORT_ERROR': None, 'SPLITTER_IMPORT_ERROR': None,
    'PEPTIDE_CHAIN': 'B',
    'BASE_RESULT_FOLDER': RESULTS, 'BASE_UPLOAD_FOLDER': UPLOADS,
    'PYTHON_EXECUTABLE': '/usr/bin/python3', 'MANAGER_SCRIPT_PATH': '/bin/true',
    'scheduler': _Sched, 'auth': _Auth, 'predPEP': _App(),
    'count_peptide_residues': lambda p: 21,
    '_chain_b_codes': lambda p: ['CYS'],
}
for node in ast.parse(SRC).body:
    if isinstance(node, ast.FunctionDef) and node.name in (
            '_queue_one_job', '_submit_segmented', '_segment_description',
            '_prepare_spec_for_pdb'):
        exec(compile(ast.Module([node], []), '<x>', 'exec'), ns)

fails = []


def check(label, got, want):
    ok = got == want
    print(f'  {"ok  " if ok else "FAIL"}  {label}: {got!r}' + ('' if ok else f'   (want {want!r})'))
    if not ok:
        fails.append(label)


# --- set the submission up exactly as upload_file does
parent = 'SPNECT_abc12345'
up = os.path.join(UPLOADS, parent); os.makedirs(up)
res = os.path.join(RESULTS, parent); os.makedirs(res)
pdb = os.path.join(up, 'SPNECT.pdb')
shutil.copy2(PDB_IN, pdb)

spec_in = {'spec_version': 1, 'mode': 'all', 'anchors': [1, 21, 99, 107],
           'flags': {'n_peptides': 10}, 'disulfides': {'mode': 'preserve'},
           'peptide': {'chain': 'B', 'disulfides': [[1, 99], [21, 107]]},
           'post_filters': {'require_disulfide_intact': True,
                            'require_native_at': []},
           'extra_res_fa': []}

print('[1] the guard now translates instead of refusing')
spec, err, report = ns['_prepare_spec_for_pdb'](pdb, dict(spec_in))
check('not refused', err, None)
check('anchor_positions translated', spec['anchor_positions'], [1, 21, 22, 30])
check('disulfides as positions',
      spec['peptide']['disulfide_positions'], [[1, 22], [21, 30]])
check('still reported segmented',
      report['chains']['B']['is_segmented'], True)

print('\n[2] the split creates two linked sub-jobs')
meta = {'protein_symbol': 'NECT', 'user_name': 'kapila',
        'user_email': None, 'pdb_filename': 'SPNECT.pdb', 'submitted_by': 'test'}
payload, err = ns['_submit_segmented'](
    parent, pdb, up, res, spec, '8', meta, report, report['chains']['B'])
check('no error', err, None)
check('two jobs queued', len(enqueued), 2)
check('sub-job ids', [j['job_id'] for j in enqueued],
      [f'{parent}_s1', f'{parent}_s2'])
check('payload marks it split', payload['split'], True)
check('payload parent', payload['parent_job'], parent)
check('labels', [j['label'] for j in payload['jobs']], ['sub-job 1', 'sub-job 2'])
check('segment sequences', [j['peptide'] for j in payload['jobs']],
      ['CAASGRFSAFYNGWSTNSAAC', 'CAASWHWNC'])
check('caveat present on both',
      all(j['caveat'] for j in payload['jobs']), True)

print('\n[3] the parent result dir is gone (no phantom job in the table)')
check('parent removed', os.path.exists(res), False)
check('parent upload kept (provenance)', os.path.isdir(up), True)
check('split inputs kept', os.path.isdir(os.path.join(up, '_segments')), True)

print('\n[4] each sub-job directory is an ordinary, complete job')
for k, seq, rng in ((1, 'CAASGRFSAFYNGWSTNSAAC', [1, 21]),
                    (2, 'CAASWHWNC', [99, 107])):
    d = os.path.join(RESULTS, f'{parent}_s{k}')
    jj = json.load(open(os.path.join(d, 'job.json')))
    ss = json.load(open(os.path.join(d, 'design_spec.json')))
    check(f's{k} job.json id', jj['job_id'], f'{parent}_s{k}')
    check(f's{k} status', jj['status'], 'queued')
    check(f's{k} parent in job.json', jj['segment']['parent_job'], parent)
    check(f's{k} label', jj['segment']['label'], f'sub-job {k}')
    check(f's{k} source range', jj['segment']['source_resseq'], rng)
    check(f's{k} receptor chains', ss['segment']['receptor_chains'], ['A', 'C'])
    check(f's{k} peptide chain stays B', ss['segment']['peptide_chain'], 'B')
    check(f's{k} sequence', ss['segment']['sequence'], seq)
    check(f's{k} spec has no stale resSeq anchors', 'anchors' in ss, False)
    check(f's{k} structure report stored',
          os.path.exists(os.path.join(d, 'structure_report.json')), True)
    check(f's{k} pdb staged',
          os.path.exists(os.path.join(UPLOADS, f'{parent}_s{k}', 'SPNECT.pdb')), True)
    # design summary must survive a spec with no 'anchors' key
    check(f's{k} design summary anchors', jj['design']['anchors'],
          ss['anchor_positions'])

print('\n[5] each sub-job PDB is a clean single-peptide complex')
for k in (1, 2):
    p = os.path.join(UPLOADS, f'{parent}_s{k}', 'SPNECT.pdb')
    r = segment_inspector.inspect(p, peptide_chain='B')
    check(f's{k} chain B continuous', r['chains']['B']['is_segmented'], False)
    check(f's{k} three chains', sorted(r['chains']), ['A', 'B', 'C'])

print('\n[6] anchors landed in each segment\'s own numbering')
for k, want in ((1, [1, 21]), (2, [1, 9])):
    ss = json.load(open(os.path.join(RESULTS, f'{parent}_s{k}',
                                     'design_spec.json')))
    check(f's{k} anchor_positions', ss['anchor_positions'], want)

print()
print('FAILURES: ' + (', '.join(fails) if fails else 'none'))
shutil.rmtree(root, ignore_errors=True)
sys.exit(1 if fails else 0)
