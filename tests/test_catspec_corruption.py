"""Regression tests for the SPILRH_c70d2d4c incident.

One corrupted .spec file out of 480 aborted the whole Rosetta aggregation with a
UnicodeDecodeError, so non_mc_pdbs.csv was never written, the merge was skipped and
tab2_final_scores.csv never appeared -- while the job still reported success.

These tests pin the aggregation half of that chain: a single unreadable file must
cost at most that file, never the run.
"""
import os

import pandas as pd
import pytest

from conftest import CORRUPT_BURST, spec_line


def _write_clean_spec(path, job_id, tag, n=3):
    with open(path, 'w') as f:
        for i in range(n):
            f.write(spec_line(job_id, '%s%d' % (tag, i), 'ACDEFGHIKLMNPQRSTVWY', -400.0 - i))


def _write_corrupt_spec(path, job_id, tag):
    """A large mostly-ASCII .spec with a short invalid-UTF-8 burst in the middle,
    mirroring SPILRH1Q672F1.spec (16 non-ASCII bytes inside a 61-byte window)."""
    good = spec_line(job_id, '%s_ok' % tag, 'ACDEFGHIKLMNPQRSTVWY', -410.0).encode()
    with open(path, 'wb') as f:
        f.write(good * 40)                       # clean ASCII before the burst
        f.write(good.rstrip(b'\n'))              # start of the damaged record
        f.write(CORRUPT_BURST)                   # <-- invalid UTF-8
        f.write(b'\n')
        f.write(good * 40)                       # clean ASCII after the burst


@pytest.fixture
def spec_tree(job_tree):
    """479 clean .spec files + 1 corrupted one, as in the incident."""
    job_id, job_dir = job_tree
    for it, tag in ((1, 'AAA'), (2, 'BBB')):
        d = os.path.join(job_dir, '%s_iter%d_0125' % (job_id, it))
        os.makedirs(d)
        _write_clean_spec(os.path.join(d, '%s%d.spec' % (tag, it)), job_id, tag)
    bad_dir = os.path.join(job_dir, '%s_iter2_0125' % job_id)
    _write_corrupt_spec(os.path.join(bad_dir, 'SPILRH1Q672F1.spec'), job_id, 'BAD')
    return job_id, job_dir


def test_corrupt_spec_does_not_abort_aggregation(spec_tree, run_in):
    """The whole point: one bad file must not take out the other 479."""
    job_id, job_dir = spec_tree
    run_in(job_dir)
    import run_catSPEC

    run_catSPEC.process_peptide_spec_files()

    assert os.path.exists(os.path.join(job_dir, 'non_mc_pdbs.csv')), \
        "non_mc_pdbs.csv missing -- the merge step will be skipped and " \
        "tab2_final_scores.csv will never be written"


def test_clean_files_are_all_still_aggregated(spec_tree, run_in):
    """Data from the readable files must survive the bad one."""
    job_id, job_dir = spec_tree
    run_in(job_dir)
    import run_catSPEC

    run_catSPEC.process_peptide_spec_files()

    df = pd.read_csv(os.path.join(job_dir, 'all_model_scores.csv'))
    # 2 clean files x 3 records, plus the readable records of the corrupt file.
    assert len(df) >= 6
    assert df['total_score'].notna().all()


def test_corrupt_records_are_not_silently_scored(spec_tree, run_in):
    """Numbers parsed out of a corrupted line are not trustworthy: the damaged
    record must be dropped, not imported with mangled column names/values."""
    job_id, job_dir = spec_tree
    run_in(job_dir)
    import run_catSPEC

    run_catSPEC.process_peptide_spec_files()

    df = pd.read_csv(os.path.join(job_dir, 'all_model_scores.csv'))
    assert not any('�' in str(c) for c in df.columns), \
        "replacement characters leaked into column names: %s" % list(df.columns)


def test_unreadable_file_is_skipped_not_fatal(spec_tree, run_in):
    """A file that cannot be opened at all (permissions, I/O error) is also
    per-file recoverable -- errors='replace' alone would not cover this."""
    job_id, job_dir = spec_tree
    unreadable = os.path.join(job_dir, '%s_iter1_0125' % job_id, 'locked.spec')
    with open(unreadable, 'w') as f:
        f.write('placeholder\n')
    os.chmod(unreadable, 0o000)
    run_in(job_dir)
    import run_catSPEC

    try:
        run_catSPEC.process_peptide_spec_files()
        assert os.path.exists(os.path.join(job_dir, 'non_mc_pdbs.csv'))
    finally:
        os.chmod(unreadable, 0o644)
