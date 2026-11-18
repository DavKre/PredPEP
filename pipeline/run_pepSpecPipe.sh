#!/bin/bash

export PATH="/usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408/main/tools/protein_tools/scripts:$PATH"
export PATH="/usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408/main/source/bin:$PATH"

PDBPATH=$1
OUTPATH=$2
N=$3

PDB_BASE=$(basename "$PDBPATH" .pdb)
echo "${PDB_BASE}"
LOG_FILE="${OUTPATH}/${PDB_BASE}.log"      # sequential parts
PEPSPEC_LOG="${OUTPATH}/${PDB_BASE}_runs.log"   # parallel part
echo "${LOG_FILE}"
echo "${PEPSPEC_LOG}"

# Navigate to the job's dedicated output directory
mkdir -p "${OUTPATH}"
cd "${OUTPATH}" || exit 1

# Clear previous logs/output for a clean run
rm -f "${LOG_FILE}" "${PEPSPEC_LOG}"

echo "cp ${PDBPATH} ${PDB_BASE}.pdb"
cp "${PDBPATH}" "${PDB_BASE}.pdb"

echo "${N}" > numberofcoresSelected.txt

mkdir -p foldX_ana
mkdir -p inputPDB

# ----------------------------------------------------------------------------
# Chain preparation.
#
# Chains A and B used to be literals here: clean_pdb.py was called on A, then on
# B, and the two outputs concatenated. That hardcoding is why a peptide made of
# two segments cannot be designed -- each segment needs a turn as the peptide
# with the other as part of the receptor, which this step could not express.
#
# The defaults reproduce the old behaviour EXACTLY (receptor A, peptide B), so
# an ordinary job is unchanged. The per-segment split sets these per run.
#
# Order matters and is deliberate: receptor chains first, peptide last, so the
# peptide occupies the highest pose numbers. run_createflags.py derives pose
# numbering by reading this same .mod.pdb, so the two always agree -- but the
# .disulf file it writes is in pose numbers, and reordering the concatenation
# between runs would silently point those at different residues.
# ----------------------------------------------------------------------------
# Where the chains come from, in order of precedence:
#
#   1. the job's own design_spec.json   (segment.peptide_chain / receptor_chains)
#   2. PREDPEP_PEP_CHAIN / PREDPEP_RECEPTOR_CHAINS in the environment
#   3. B as the peptide, A as the receptor -- the historical behaviour
#
# The spec wins over the environment deliberately. The environment variables are
# container-wide, so on a node running several jobs they cannot describe more
# than one of them; a per-segment sub-job must be able to say "my receptor is A
# and C" without that leaking into every other job on the box. The env vars
# remain useful as a default for jobs whose spec says nothing, and for testing.
DESIGN_SPEC="${DESIGN_SPEC:-../design_spec.json}"
SPEC_PEP=''
SPEC_REC=''
if [ -f "${DESIGN_SPEC}" ]; then
    _chains=$(python3 -I -c '
import json, sys
try:
    s = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit(0)
seg = s.get("segment") or {}
pep = seg.get("peptide_chain") or ""
rec = seg.get("receptor_chains") or []
if isinstance(rec, str):
    rec = rec.split()
print(str(pep).strip())
print(" ".join(str(c).strip() for c in rec))
' "${DESIGN_SPEC}" 2>/dev/null)
    SPEC_PEP=$(printf '%s\n' "${_chains}" | sed -n '1p')
    SPEC_REC=$(printf '%s\n' "${_chains}" | sed -n '2p')
fi

PEP_CHAIN="${SPEC_PEP:-${PREDPEP_PEP_CHAIN:-B}}"
RECEPTOR_CHAINS="${SPEC_REC:-${PREDPEP_RECEPTOR_CHAINS:-A}}"
if [ -n "${SPEC_PEP}${SPEC_REC}" ]; then
    echo "chains from ${DESIGN_SPEC}"
fi

case "${PEP_CHAIN}" in
    [A-Za-z0-9]) ;;
    *) echo "ERROR: PREDPEP_PEP_CHAIN must be a single chain ID, got '${PEP_CHAIN}'." >&2
       exit 1 ;;
esac
for ch in ${RECEPTOR_CHAINS}; do
    case "${ch}" in
        [A-Za-z0-9]) ;;
        *) echo "ERROR: PREDPEP_RECEPTOR_CHAINS must be single chain IDs separated by spaces, got '${RECEPTOR_CHAINS}'." >&2
           exit 1 ;;
    esac
    if [ "${ch}" = "${PEP_CHAIN}" ]; then
        echo "ERROR: chain ${ch} is listed as both the peptide and the receptor." >&2
        exit 1
    fi
done
echo "peptide chain: ${PEP_CHAIN}   receptor chain(s): ${RECEPTOR_CHAINS}"

# A chain present in the input but claimed by neither role would simply be left
# out of the .mod.pdb, and the peptide would then be designed against an
# incomplete receptor -- with a full set of scores at the end and nothing to say
# a whole chain was missing. That is the failure shape this pipeline keeps
# producing, so it is fatal by default.
#
# Only ATOM records are counted. Ligands, ions and waters arrive as HETATM and
# are dropped by clean_pdb.py regardless, so counting them would fail jobs that
# are perfectly fine.
present=$(awk '/^ATOM/ {print substr($0,22,1)}' "${PDB_BASE}.pdb" | sort -u | tr -d '\n ')
claimed=$(printf '%s %s' "${PEP_CHAIN}" "${RECEPTOR_CHAINS}" | tr -d ' ')
unclaimed=''
i=1
while [ "$i" -le "${#present}" ]; do
    ch=$(printf '%s' "${present}" | cut -c"$i")
    case "${claimed}" in
        *"${ch}"*) ;;
        *) unclaimed="${unclaimed}${ch}" ;;
    esac
    i=$((i+1))
done
if [ -n "${unclaimed}" ]; then
    if [ "${PREDPEP_IGNORE_EXTRA_CHAINS:-0}" = '1' ]; then
        echo "WARNING: chain(s) '${unclaimed}' are in the input but are neither" >&2
        echo "         the peptide nor in the receptor list, and are being" >&2
        echo "         DROPPED because PREDPEP_IGNORE_EXTRA_CHAINS=1." >&2
    else
        echo "ERROR: chain(s) '${unclaimed}' are present in ${PDB_BASE}.pdb but" >&2
        echo "       are neither the peptide (${PEP_CHAIN}) nor in the receptor" >&2
        echo "       list (${RECEPTOR_CHAINS}). They would be silently left out" >&2
        echo "       of the input complex, docking the peptide against an" >&2
        echo "       incomplete receptor. Add them to the receptor list, or set" >&2
        echo "       PREDPEP_IGNORE_EXTRA_CHAINS=1 to drop them deliberately." >&2
        exit 1
    fi
fi

for ch in ${RECEPTOR_CHAINS}; do
    clean_pdb.py "${PDB_BASE}.pdb" "${ch}"
    # A missing receptor chain used to go unnoticed: clean_pdb.py writes nothing,
    # the concatenation below silently omits it, and pepspec docks the peptide
    # against a partial -- or empty -- receptor. Scores come out either way.
    if [ ! -s "${PDB_BASE}_${ch}.pdb" ]; then
        echo "ERROR: clean_pdb.py produced no output for receptor chain ${ch}." >&2
        echo "       The input PDB has no usable chain ${ch}; refusing to dock" >&2
        echo "       the peptide against an incomplete receptor." >&2
        exit 1
    fi
done

clean_pdb.py "${PDB_BASE}.pdb" "${PEP_CHAIN}" | tee length.txt
if [ ! -s "${PDB_BASE}_${PEP_CHAIN}.pdb" ]; then
    echo "ERROR: clean_pdb.py produced no output for peptide chain ${PEP_CHAIN}." >&2
    exit 1
fi
if [ ! -s "${PDB_BASE}_${PEP_CHAIN}.fasta" ]; then
    echo "ERROR: no FASTA for peptide chain ${PEP_CHAIN}; cannot build pepSeq.txt." >&2
    exit 1
fi

sed '/^>/d' "${PDB_BASE}_${PEP_CHAIN}.fasta" > pepSeq.txt

: > inputPDB/"${PDB_BASE}.mod.pdb"
for ch in ${RECEPTOR_CHAINS}; do
    cat "${PDB_BASE}_${ch}.pdb" >> inputPDB/"${PDB_BASE}.mod.pdb"
done
cat "${PDB_BASE}_${PEP_CHAIN}.pdb" >> inputPDB/"${PDB_BASE}.mod.pdb"

# ----------------------------------------------------------------------------
# Flag generation.
#
# predPEP.py writes the submission's design spec to the JOB directory, which is
# the parent of this iteration directory (OUTPATH = <job dir>/<job>_iterN_xxxx).
# So the spec is at ../design_spec.json and run_iteMAN.py needs no new argument.
#
# If no spec exists -- an older job, or a submission that sent none -- the
# generator runs without one and reproduces run_createflag2_2.sh's output
# exactly, so behaviour is unchanged.
# ----------------------------------------------------------------------------
DESIGN_SPEC="${DESIGN_SPEC:-../design_spec.json}"
if [ -f "${DESIGN_SPEC}" ]; then
    echo "Using design spec: ${DESIGN_SPEC}"
    SPEC_ARG=(--spec "${DESIGN_SPEC}")
else
    echo "No design spec found at ${DESIGN_SPEC}; using pipeline defaults."
    SPEC_ARG=()
fi

run_createflags.py pepSeq flags2 "${PDB_BASE}" "${PDB_BASE}" \
    --input-pdb "inputPDB/${PDB_BASE}.mod.pdb" \
    --pep-chain "${PEP_CHAIN}" \
    "${SPEC_ARG[@]}" 2>&1 | tee -a "${LOG_FILE}"

# run_createflags.py exits 2 when the spec asks for constraints pepspec cannot
# honour. Stop rather than silently running a job with different settings than
# were requested.
GEN_STATUS=${PIPESTATUS[0]}
if [ "${GEN_STATUS}" -ne 0 ]; then
    echo "FATAL: run_createflags.py exited ${GEN_STATUS}; no pepspec runs started." >&2
    exit "${GEN_STATUS}"
fi

if ! ls flags2_*.txt >/dev/null 2>&1; then
    echo "FATAL: no flag files were generated." >&2
    exit 1
fi

# ----------------------------------------------------------------------------
# pepspec runs: one per generated flag file, N at a time.
#
# Driven by the files that EXIST rather than counting 1..peptide_length: the
# spec may select a sparse set of anchors, in which case the old loop would
# reference flag files that were never written.
# ----------------------------------------------------------------------------
# pepspec crashes intermittently -- a stochastic out-of-range access in
# core/conformation/Conformation.hh:514, seen at roughly 1 run in 9 on a
# 23-residue peptide. It is not caused by any flag (verified: the same anchor
# with and without rmsd_analysis both completed cleanly at low sampling), and it
# costs models within a run rather than the whole run. Rather than hide it, each
# run's exit status is recorded so the loss is a number in the results.
RUN_STATUS_FILE="${OUTPATH}/runs_status.json"
declare -a WAVE_PIDS=() WAVE_FILES=()
N_CORES=$N
TOTAL=0; FAILED=0
: > "${OUTPATH}/.runs_status.tmp"

drain_wave() {
    local i st
    for i in "${!WAVE_PIDS[@]}"; do
        wait "${WAVE_PIDS[$i]}"; st=$?
        TOTAL=$((TOTAL+1))
        [ "$st" -ne 0 ] && FAILED=$((FAILED+1))
        printf '{"flag_file":"%s","exit_code":%d}\n' "${WAVE_FILES[$i]}" "$st" \
            >> "${OUTPATH}/.runs_status.tmp"
        if [ "$st" -ne 0 ]; then
            echo "WARNING: ${WAVE_FILES[$i]} exited ${st} (models from this run may be missing)."
        fi
    done
    WAVE_PIDS=(); WAVE_FILES=()
}

for flagfile in flags2_*.txt; do
    pepspec.static.linuxgccrelease @"${flagfile}" \
      -database /usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408/main/database \
      >> "${OUTPATH}/${PDB_BASE}.log" 2>&1 &
    WAVE_PIDS+=("$!"); WAVE_FILES+=("${flagfile}")
    # Wait for the whole wave before starting the next, matching the original
    # core-budget behaviour, but collecting each run's status first.
    [ "${#WAVE_PIDS[@]}" -ge "${N_CORES}" ] && drain_wave
done
drain_wave

{
    printf '{\n  "iteration_dir": "%s",\n' "$(basename "${OUTPATH}")"
    printf '  "runs_total": %d,\n  "runs_failed": %d,\n' "${TOTAL}" "${FAILED}"
    printf '  "spec_files": %d,\n' "$(ls -1 *.spec 2>/dev/null | wc -l)"
    printf '  "crash_log_present": %s,\n' \
        "$([ -f ROSETTA_CRASH.log ] && echo true || echo false)"
    printf '  "runs": [\n'
    paste -sd, - < "${OUTPATH}/.runs_status.tmp" | sed 's/},{/},\n    {/g; s/^/    /'
    printf '\n  ]\n}\n'
} > "${RUN_STATUS_FILE}"
rm -f "${OUTPATH}/.runs_status.tmp"

echo "All pepspec runs finished (${TOTAL} run(s), ${FAILED} failed)."

# OUTPATH, not the never-defined OUTPUT_DIR. run_foldX.sh currently ignores its
# first argument (its cd is commented out), so this was silently passing an
# empty string.
run_foldX.sh "${OUTPATH}" "${PDB_BASE}" pepSeq

# run_iteMAN.py handles aggregation and the final zip after all iterations.
exit 0
