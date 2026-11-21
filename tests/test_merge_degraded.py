"""tab2_final_scores.csv must exist whenever designs were scored.

In job SPILRH_c70d2d4c the FoldX ranking (SPILRH.all.txt) was written correctly, but
because non_mc_pdbs.csv was missing run_mergeScores.py returned None and wrote nothing
at all -- so 4,795 scored designs produced no score table.

Contract: if FoldX scores exist, emit the table we CAN build, with the Rosetta column
left empty (never 0, never a fabricated value) and the degradation marked in-band.
"""
import csv
import os

import pytest

import conftest  # noqa: F401  (path setup)


ROSETTA_COL = 'Total_Energy'


@pytest.fixture
def foldx_only_job(job_tree, run_in):
    """A job dir with FoldX results and per-design fastas, but no Rosetta aggregate."""
    job_id, job_dir = job_tree
    base = 'SPTEST'
    designs = [('SPTEST1Q672F1', 'ACDEFGHIKL'), ('SPTEST1K331A2', 'MNPQRSTVWY')]

    with open(os.path.join(job_dir, '%s.all.txt' % base), 'w') as f:
        for i, (design, _seq) in enumerate(designs):
            f.write('%s_%d.pdb\t-4%d.5\n' % (design, i + 1, i))

    # Per-design chain-B fasta, as written by clean_pdb.py inside the design folder.
    for design, seq in designs:
        d = os.path.join(job_dir, '%s_iter1_0125' % job_id, '%s.pdbs' % design)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, '%s_B.fasta' % design), 'w') as f:
            f.write('>%s_B\n%s\n' % (design, seq))

    run_in(job_dir)
    return job_id, job_dir, base, designs


def _read_rows(path):
    with open(path, newline='') as f:
        return list(csv.DictReader(f))


def test_degraded_table_is_written_when_rosetta_missing(foldx_only_job):
    job_id, job_dir, base, designs = foldx_only_job
    import run_mergeScores

    run_mergeScores.merge_and_clean_scores(
        '%s.all.txt' % base, 'non_mc_pdbs.csv', 'tab2_final_scores.csv')

    out = os.path.join(job_dir, 'tab2_final_scores.csv')
    assert os.path.exists(out), \
        "no score table written despite FoldX scores being available"
    assert len(_read_rows(out)) == len(designs)


def test_rosetta_column_is_empty_never_zero(foldx_only_job):
    """An empty cell is unambiguous; 0.0 is a plausible score that would pollute rankings."""
    job_id, job_dir, base, designs = foldx_only_job
    import run_mergeScores

    run_mergeScores.merge_and_clean_scores(
        '%s.all.txt' % base, 'non_mc_pdbs.csv', 'tab2_final_scores.csv')

    rows = _read_rows(os.path.join(job_dir, 'tab2_final_scores.csv'))
    assert ROSETTA_COL in rows[0], "degraded table dropped the Rosetta column entirely"
    for r in rows:
        assert r[ROSETTA_COL] == '', \
            "Rosetta score must be empty, got %r" % r[ROSETTA_COL]


def test_degradation_is_marked_in_band(foldx_only_job):
    """A consumer reading only the CSV must be able to tell it is partial."""
    job_id, job_dir, base, designs = foldx_only_job
    import run_mergeScores

    run_mergeScores.merge_and_clean_scores(
        '%s.all.txt' % base, 'non_mc_pdbs.csv', 'tab2_final_scores.csv')

    rows = _read_rows(os.path.join(job_dir, 'tab2_final_scores.csv'))
    assert all(r['energy_available'] == 'False' for r in rows)


def test_sequences_are_recovered_from_fasta(foldx_only_job):
    """pepSeq drives the results table and the T-MAP tree; recover it from the
    per-design *_B.fasta files rather than shipping a table of blank sequences."""
    job_id, job_dir, base, designs = foldx_only_job
    import run_mergeScores

    run_mergeScores.merge_and_clean_scores(
        '%s.all.txt' % base, 'non_mc_pdbs.csv', 'tab2_final_scores.csv')

    rows = _read_rows(os.path.join(job_dir, 'tab2_final_scores.csv'))
    got = {r['pdbId']: r['pepSeq'] for r in rows}
    for design, seq in designs:
        key = next(k for k in got if k.startswith(design))
        assert got[key] == seq


def test_full_merge_still_marks_rosetta_available(job_tree, run_in):
    """The healthy path must be unchanged and flagged as complete."""
    job_id, job_dir = job_tree
    base = 'SPTEST'
    with open(os.path.join(job_dir, '%s.all.txt' % base), 'w') as f:
        f.write('SPTEST1Q672F1_1.pdb\t-40.5\n')
    with open(os.path.join(job_dir, 'non_mc_pdbs.csv'), 'w') as f:
        f.write('pepID,pepSeq,pdb_relative_path,total_score\n')
        f.write('SPTEST1Q672F1_1.pdb,ACDEFGHIKL,%s/x.pdb,-435.7\n' % job_id)
    run_in(job_dir)
    import run_mergeScores

    run_mergeScores.merge_and_clean_scores(
        '%s.all.txt' % base, 'non_mc_pdbs.csv', 'tab2_final_scores.csv')

    rows = _read_rows(os.path.join(job_dir, 'tab2_final_scores.csv'))
    assert rows[0][ROSETTA_COL] == '-435.7'
    assert rows[0]['energy_available'] == 'True'


def test_no_table_when_foldx_also_missing(job_tree, run_in):
    """With neither input there is nothing to salvage -- must not invent a file."""
    job_id, job_dir = job_tree
    run_in(job_dir)
    import run_mergeScores

    result = run_mergeScores.merge_and_clean_scores(
        'SPTEST.all.txt', 'non_mc_pdbs.csv', 'tab2_final_scores.csv')

    assert result is None
    assert not os.path.exists(os.path.join(job_dir, 'tab2_final_scores.csv'))
