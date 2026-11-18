#!/bin/bash
#
# FoldX analysis driver — one call to run_foldX2.sh per pepspec run.
#
# WHAT CHANGED AND WHY
# --------------------
# The previous version built each run's tag by walking pepSeq.txt and counting
# `declare -i x=1` upward:
#
#     run_foldX2.sh ${PDB_BASE}${res:$i:1}${x}
#
# That assumed two things that are no longer true:
#
#   1. Every peptide position has a pepspec run. It does not: the dashboard now
#      lets a user deselect anchors, so a 23-residue peptide may produce 22 runs
#      (anchor 16 skipped, in the first such job). The old loop would construct
#      SPTYKKL16 and hand it to run_foldX2.sh, which finds no .pdbs directory,
#      greps a file that does not exist, and appends an empty line to
#      ${tag}.min.txt. Noise rather than ruin, but it corrupts the minimum table.
#
#   2. The peptide starts at residue 1. Per the pipeline README this had to be
#      hand-edited in five scripts whenever it did not.
#
# Both disappear if we read the tags that were actually generated.
# run_createflags.py writes flags_manifest.json listing every run it produced,
# with the exact -o tag. That is the authoritative list; derive nothing.
#
# If no manifest is present — an older job directory — it falls back to the old
# counting behaviour so previous results stay reproducible.
#
# Usage (unchanged): run_foldX.sh <folder> <pdb_base> <infile_stem>

FOLDER=$1
PDB_BASE=$2
INFILE=$3

#cd ${FOLDER}

echo "The current directory is: $(pwd)"

MANIFEST="flags_manifest.json"
TAGS=()

if [ -f "${MANIFEST}" ]; then
    # Read tags from the manifest. -I so nothing is imported from the cwd.
    mapfile -t TAGS < <(python3 -I -c "
import json, sys
try:
    m = json.load(open('${MANIFEST}'))
except Exception as e:
    sys.exit(1)
for r in m.get('runs', []):
    t = r.get('output_tag')
    if t:
        print(t)
")
    if [ "${#TAGS[@]}" -eq 0 ]; then
        echo "WARNING: ${MANIFEST} listed no runs; falling back to pepSeq counting." >&2
    else
        echo "FoldX over ${#TAGS[@]} run(s) from ${MANIFEST}."
    fi
fi

if [ "${#TAGS[@]}" -eq 0 ]; then
    # Legacy path: one tag per peptide position, numbering from 1.
    if [ ! -f "${INFILE}.txt" ]; then
        echo "FATAL: no ${MANIFEST} and no ${INFILE}.txt; nothing to analyse." >&2
        exit 1
    fi
    res=$(cat "${INFILE}.txt")
    declare -i x=1
    for (( i=0; i<${#res}; i++ )); do
        TAGS+=("${PDB_BASE}${res:$i:1}${x}")
        x=$((x+1))
    done
    echo "FoldX over ${#TAGS[@]} run(s) derived from ${INFILE}.txt (no manifest)."
fi

# Skip tags whose pepspec run produced no PDBs — a crashed run, or
# -pepspec::save_all_pdbs left false. Reporting them is more useful than
# letting run_foldX2.sh fail quietly on a missing directory.
ANALYSED=0
SKIPPED=()
for tag in "${TAGS[@]}"; do
    if [ -d "${tag}.pdbs" ]; then
        run_foldX2.sh "${tag}" "${PDB_BASE}"
        ANALYSED=$((ANALYSED+1))
    else
        SKIPPED+=("${tag}")
    fi
done

if [ "${#SKIPPED[@]}" -gt 0 ]; then
    echo "NOTE: ${#SKIPPED[@]} run(s) had no .pdbs directory and were skipped: ${SKIPPED[*]}"
fi
echo "FoldX analysed ${ANALYSED} run(s)."

if [ -f "${PDB_BASE}.all.txt" ]; then
    sort -n -k 2 "${PDB_BASE}.all.txt" > "${PDB_BASE}.all.tab"
else
    echo "WARNING: ${PDB_BASE}.all.txt was never written; no FoldX results to sort." >&2
    : > "${PDB_BASE}.all.tab"
fi

# Machine-readable summary so the dashboard can show FoldX coverage rather than
# leaving a silently short table unexplained.
{
    printf '{\n  "tags_total": %d,\n  "analysed": %d,\n' "${#TAGS[@]}" "${ANALYSED}"
    printf '  "skipped_no_pdbs": %d,\n' "${#SKIPPED[@]}"
    printf '  "source": "%s"\n}\n' \
        "$([ -f "${MANIFEST}" ] && echo manifest || echo pepseq_count)"
} > foldx_status.json

exit 0
