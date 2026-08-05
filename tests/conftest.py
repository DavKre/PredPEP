"""Shared fixtures for the predPEP node tests.

The pipeline scripts hardcode BASE_RESULT_FOLDER = /tmp/pepspec/results and write
their outputs into the *current working directory* (run_iteMAN.py chdir()s into the
job dir before invoking them). The fixtures below reproduce that layout faithfully
so the tests exercise the real code paths rather than a rewritten copy of them.
"""
import os
import shutil
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPELINE = os.path.join(REPO_ROOT, 'pipeline')
APP = os.path.join(REPO_ROOT, 'app')

# At import time, not in a fixture: test modules import these at collection.
for _p in (PIPELINE, APP):
    if _p not in sys.path:
        sys.path.insert(0, _p)

BASE_RESULT_FOLDER = '/tmp/pepspec/results'

# Bytes lifted from the corrupted region of SPILRH1Q672F1.spec (job SPILRH_c70d2d4c):
# a short burst of invalid UTF-8 start bytes in the middle of otherwise clean ASCII.
CORRUPT_BURST = b'hbond_bb_sc:   -32.208\xf8hbo\xf8d_s\xee:  \xf8 -5\xeb595\xf8dsl\xee_ss\xf8dst\xed   \xf8-3.\xeb55'


def spec_line(job_id, pep_id, seq, total_score):
    """One .spec record: <pdb path> <pepSeq> then key: value score pairs."""
    pdb = '%s/%s/%s.pdbs/%s_10.pdb' % (BASE_RESULT_FOLDER, job_id, pep_id, pep_id)
    return ('%s %s total_score: %.3f fa_atr: -1100.5 fa_rep: 250.25 '
            'hbond_bb_sc: -32.208 dslf_fa13: 0.0\n' % (pdb, seq, total_score))


@pytest.fixture
def job_tree(tmp_path_factory):
    """Build a job dir under the real BASE_RESULT_FOLDER and yield (job_id, job_dir).

    Cleans up only the directories it created.
    """
    created_base = not os.path.exists(BASE_RESULT_FOLDER)
    os.makedirs(BASE_RESULT_FOLDER, exist_ok=True)
    job_id = 'SPTEST_%s' % os.urandom(4).hex()
    job_dir = os.path.join(BASE_RESULT_FOLDER, job_id)
    os.makedirs(job_dir)
    try:
        yield job_id, job_dir
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
        if created_base:
            shutil.rmtree('/tmp/pepspec', ignore_errors=True)


@pytest.fixture
def run_in(monkeypatch):
    """chdir into a directory for the duration of a test, like run_iteMAN.py does."""
    def _run_in(path):
        monkeypatch.chdir(path)
    return _run_in
