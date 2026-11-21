"""Jobs aggregated before the score columns were renamed must keep rendering.

The columns were renamed to say what the number is rather than which engine
produced it -- Rosetta_Total_Score -> Total_Energy, FoldX_Score -> ddG_Score --
because the headers are user-visible in the downloaded CSVs. Every result table
already on disk carries the old names, and there are hundreds of them. If a
reader only knew the new names, those jobs would open with an empty score table
and no error: exactly the silent-degradation failure this suite exists to catch.

So writers emit the new names and readers accept either. These tests pin the
reading half.
"""
import os

import pandas as pd
import pytest

import conftest  # noqa: F401  (path setup)
import job_analytics


LEGACY_HEADER = ('pdbId,pepSeq,pdb_relative_path,FoldX_Score,'
                 'Rosetta_Total_Score,rosetta_available\n')
LEGACY_ROWS = ('a_1.pdb,ACDE,J/J_iter1_aaaa/a.pdbs/a_1.pdb,-2.32,-151.9,True\n'
               'b_2.pdb,FGHI,J/J_iter2_bbbb/b.pdbs/b_2.pdb,-1.11,-149.0,True\n')


def test_normalise_renames_every_legacy_column():
    df = pd.DataFrame(columns=['pdbId', 'FoldX_Score', 'Rosetta_Total_Score',
                               'rosetta_available', 'foldx_valid', 'foldx_unknown'])
    out = job_analytics.normalise_score_columns(df)
    assert set(out.columns) == {'pdbId', 'ddG_Score', 'Total_Energy',
                                'energy_available', 'ddG_valid', 'ddG_unknown'}


def test_normalise_leaves_new_names_alone():
    df = pd.DataFrame(columns=['pdbId', 'ddG_Score', 'Total_Energy'])
    assert list(job_analytics.normalise_score_columns(df).columns) == \
        ['pdbId', 'ddG_Score', 'Total_Energy']


def test_new_name_wins_when_a_table_somehow_carries_both():
    df = pd.DataFrame({'FoldX_Score': [1.0], 'ddG_Score': [2.0]})
    out = job_analytics.normalise_score_columns(df)
    assert list(out['ddG_Score']) == [2.0]
    assert 'FoldX_Score' in out.columns      # untouched, not silently dropped


@pytest.fixture
def legacy_job(tmp_path):
    (tmp_path / 'tab2_final_scores.csv').write_text(LEGACY_HEADER + LEGACY_ROWS)
    return str(tmp_path)


def test_a_legacy_job_loads_with_numeric_scores(legacy_job):
    """The real path: _load_final on a job written by the previous build."""
    df = job_analytics._load_final(legacy_job)
    assert df is not None
    assert 'ddG_Score' in df.columns and 'Total_Energy' in df.columns
    # coerced to numbers, not left as strings -- every panel depends on that
    assert df['ddG_Score'].tolist() == [-2.32, -1.11]
    assert df['Total_Energy'].tolist() == [-151.9, -149.0]
    # and the iteration still comes out of the path, as the panels need
    assert df['iteration'].tolist() == [1, 2]


def test_the_agreement_panel_works_on_a_legacy_job(legacy_job):
    """The panel that needs BOTH scores: the one that would quietly go blank."""
    df = job_analytics._load_final(legacy_job)
    out = job_analytics.agreement(df)
    assert out.get('points'), 'agreement panel is empty on a legacy job'
    assert len(out['points']) == 2
