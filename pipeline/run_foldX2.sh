#!/bin/bash

# FoldX binary: resolved once, by path, and never as a bare name on PATH.
#
# This script called the binary by bare name. On hakg that name is a symlink to
# a REAL FoldX 5 (/usr/local/foldx26Linux64_0/); inside the container image it
# was a shim pointing at FoldX Pro. The same script therefore scored with two
# different force fields depending on where it ran, and nothing in the output
# recorded which. Pro is named explicitly here and logged on every run.
#
# Measured 11 November across 15 designs of one split: Pro reads 0.46 kcal/mol
# lower than FoldX 5 on average, sd 0.42 -- scatter nearly as large as the
# offset, and enough to reorder the leaders. No score produced before this
# change may share a ranking or a plot with one produced after it.
FOLDX_BIN="${PREDPEP_FOLDX:-/usr/local/foldxpro_20261231/foldxpro20261231}"
if [ ! -x "$FOLDX_BIN" ]; then
    echo "ERROR: FoldX binary not executable: $FOLDX_BIN" >&2
    echo "       Set PREDPEP_FOLDX, or mount /usr/local/foldxpro_20261231." >&2
    exit 3
fi
echo "FoldX binary: $FOLDX_BIN" >&2
#
# FoldX AnalyseComplex over one pepspec run's output PDBs.
#
# WHAT CHANGED AND WHY
# --------------------
#   1. The model loop was `for ((i=1; i<=10; i++))`, hardcoded to 10 because
#      n_peptides was hardcoded to 10 in run_createflag2_2.sh. The dashboard now
#      lets a user set n_peptides, so with n_peptides 20 the old loop analysed
#      the first 10 models and silently ignored the rest — the scores table
#      would look complete while missing half the data. It now iterates over the
#      PDBs that exist.
#
#   2. `grep` on a missing ${FOLDER1}.txt appended an empty line to
#      ${FOLDER1}.min.txt, putting a blank row in the minimum table. The
#      post-processing is now guarded on the file existing and being non-empty.
#
#   3. `rm *.fxout` failed noisily when FoldX wrote none. Scoped and quieted.
#
#   4. Each model's PDB is removed after analysis as before, but with the copy
#      and the analysis both checked, so a FoldX failure on one model does not
#      leave a stale PDB that the next run's glob would pick up.
#
# Usage (unchanged): run_foldX2.sh <run_tag> <pdb_base>

export PATH="/usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408/main/tools/protein_tools/scripts:$PATH"

FOLDER1=$1
FOLDER2=$2

if [ -z "${FOLDER1}" ] || [ -z "${FOLDER2}" ]; then
    echo "usage: run_foldX2.sh <run_tag> <pdb_base>" >&2
    exit 1
fi

mkdir -p foldX_ana
cd foldX_ana/ || exit 1

SRC="../${FOLDER1}.pdbs"
if [ ! -d "${SRC}" ]; then
    echo "NOTE: ${FOLDER1}.pdbs does not exist; nothing to analyse for ${FOLDER1}."
    cd ../
    exit 0
fi

# Every model pepspec saved, however many that is. Numeric sort so model 2
# precedes model 10 and the output order matches the model order.
mapfile -t MODELS < <(ls -1 "${SRC}/${FOLDER1}"_*.pdb 2>/dev/null | sort -V)

if [ "${#MODELS[@]}" -eq 0 ]; then
    echo "NOTE: ${SRC} contains no ${FOLDER1}_*.pdb files."
    cd ../
    exit 0
fi

OK=0
for src in "${MODELS[@]}"; do
    model=$(basename "${src}")
    cp "${src}" ./ || continue

    clean_pdb.py "${model}" B >> "${FOLDER1}.all.fasta"

    if "$FOLDX_BIN" -c AnalyseComplex --pdb="${model}" \
         --analyseComplexChains=A,B >> "${FOLDER1}.txt"; then
        OK=$((OK+1))
    else
        echo "WARNING: FoldX failed on ${model}." >&2
    fi

    rm -f "${model}"
done

echo "${FOLDER1}: FoldX completed ${OK}/${#MODELS[@]} model(s)."

# Pull the Total interaction energy per model, as "<model>.pdb<TAB><energy>".
#
# WHY THIS IS NOT THE ORIGINAL PIPELINE
# -------------------------------------
# It used to be a grep whose pattern contained the exact run of spaces and
# literal tabs FoldX puts after "Total          =", plus the alternative
# "Complex Analysis of .", relying on those two line kinds arriving in strict
# alternation so that `sed N` could glue each pair together.
#
# FoldX Pro names the model differently and in the other order:
#
#   old (foldx_20270131):  Total ... THEN  "Complex Analysis of ./X.pdb went fine"
#   Pro (foldxpro2026...): "1 models read: X.pdb"  THEN  Total ...
#
# so the name alternative never matched, grep emitted nothing but Total lines,
# and `sed N` glued TWO DIFFERENT MODELS' energies into one row. The result was
# a scores table of the form "<energy>\t<energy>" -- the selector then treated
# column 1 as a filename and went looking for "-3.pdbs/-3.47", and column 2 was
# some other model's energy, so every score in the table was misattributed to
# the wrong peptide while looking perfectly well-formed. Silent wrong numbers,
# not a crash.
#
# This version matches either name format, accepts them in either order, and
# keys on field position rather than on whitespace, so it works with both
# binaries and does not depend on FoldX's output alignment. If a future FoldX
# changes the wording again, this prints a warning instead of inventing rows.
if [ -s "${FOLDER1}.txt" ]; then
    awk '
      # ONE row per model, and the row is the INTERACTION energy.
      #
      # AnalyseComplex prints three Totals per model -- the whole complex,
      # group 0 alone, then the interaction -- and FoldX 5 prints BOTH name
      # markers ("N models read:" and "Complex Analysis of ... went fine").
      # Pairing a name with "the next Total" therefore emitted twice per
      # model: the ~+150 complex energy AND the binding term. Only the Total
      # that FOLLOWS "interaction between" is the binding energy.
      function flush(   ) {
          if (name == "") { ninter = 0; ival = ""; return }
          if (ninter == 1 && ival != "") {
              print name "\t" ival
          } else if (ninter == 0) {
              printf "WARNING: no \"interaction between\" section for %s; no score emitted.\n", name > "/dev/stderr"
              bad++
          } else {
              # Three chains grouped as A|B and A|C, say. Which pair is the
              # peptide is not something this parser can know, and a wrong
              # binding energy is worse than a missing one because it is
              # ranked on.
              printf "WARNING: %s has %d \"interaction between\" sections; refusing to guess which is the peptide. No score emitted.\n", name, ninter > "/dev/stderr"
              bad++
          }
          name = ""; ninter = 0; ival = ""
      }
      # FoldX Pro prints this; FoldX 5 prints it AND the "Complex Analysis"
      # line below, which is what caused the double emit.
      /^[0-9]+ models? read:/ { flush(); name = $NF; next }
      /Complex Analysis of/ {
          line = $0
          sub(/.*Complex Analysis of \.?\/?/, "", line)
          sub(/ *went fine.*/, "", line)
          if (name == "") name = line
          flush(); next
      }
      /^interaction between/ { inter = 1; ninter++; next }
      # Anchored so per-term "Total" lines elsewhere cannot match. Taken only
      # when an interaction header has just been seen.
      /^Total[ \t]*=/ { if (inter) { ival = $NF; inter = 0 } next }
      END { flush() }
    ' "${FOLDER1}.txt" > "${FOLDER1}.1.txt"

    if [ -s "${FOLDER1}.1.txt" ]; then
        # A row whose first column is not a .pdb name means the name pattern
        # stopped matching again. Fail loudly here rather than letting the
        # selector hunt for a PDB called "-3.47".
        if awk -F'\t' '$1 !~ /\.pdb$/ { exit 1 }' "${FOLDER1}.1.txt"; then :; else
            echo "ERROR: ${FOLDER1}.1.txt has non-PDB names in column 1 --" >&2
            echo "       FoldX output format changed; refusing to emit scores." >&2
            head -3 "${FOLDER1}.1.txt" >&2
            rm -f "${FOLDER1}.1.txt"
            cd ../
            exit 1
        fi
        awk 'NR == 1 || $2 < min {line = $0; min = $2} END {if (line != "") print line}' \
            "${FOLDER1}.1.txt" >> "${FOLDER1}.min.txt"
        cat "${FOLDER1}.1.txt" >> "../${FOLDER2}.all.txt"
    else
        echo "WARNING: ${FOLDER1}.txt held no parsable Total lines." >&2
    fi
else
    echo "WARNING: ${FOLDER1}.txt is missing or empty; no FoldX scores for this run." >&2
fi

rm -f ./*.fxout

cd ../
exit 0
