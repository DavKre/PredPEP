#!/bin/bash

export PATH="/usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408/main/tools/protein_tools/scripts:$PATH"
export PATH="/usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408/main/source/bin:$PATH"

PDBPATH=$1
PDB_BASE=$(basename "$PDBPATH" .pdb)
OUTPATH=$2
N=$3

# Derived variables
PDB_BASE=$(basename "$PDBPATH" .pdb)
echo ${PDB_BASE}
LOG_FILE="${OUTPATH}/${PDB_BASE}.log" # This will log the sequential parts
echo ${LOG_FILE}
PEPSPEC_LOG="${OUTPATH}/${PDB_BASE}_runs.log" # This will log the parallel part
echo ${PEPSPEC_LOG}

# Navigate to the job's dedicated output directory
mkdir -p "${OUTPATH}"
cd "${OUTPATH}"

# Clear previous logs/output for a clean run
rm -f "${LOG_FILE}" "${PEPSPEC_LOG}"

# Redirect stdout/stderr of the main script to a log file for sequential steps
# Use the file descriptor trick for the log, but simpler than before
#exec > >(tee -a "${PDB_BASE}.log") 2>&1

echo "cp ${PDBPATH} ${PDB_BASE}.pdb"

cp "${PDBPATH}" "${PDB_BASE}.pdb"

echo ${N} > numberofcoresSelected.txt

mkdir -p foldX_ana
mkdir -p inputPDB

#echo "extracting chain A"
#echo "clean_pdb.py ${PDB_BASE}.pdb A"

clean_pdb.py "${PDB_BASE}.pdb" A

#echo "extracting chain B"
#echo "clean_pdb.py ${PDB_BASE}.pdb B |tee length.txt"

clean_pdb.py "${PDB_BASE}.pdb" B |tee length.txt

sed -n '2p' length.txt |awk -F' ' '{print $3}' > length2.txt

x=$(cat length2.txt)

echo "${x}"

sed '/^>/d' "${PDB_BASE}_B.fasta" > pepSeq.txt

cat "${PDB_BASE}_A.pdb" "${PDB_BASE}_B.pdb" > inputPDB/"${PDB_BASE}.mod.pdb"

#echo "run_createflag2_2.sh pepSeq flags2 ${PDB_BASE} ${PDB_BASE}"

run_createflag2_2.sh pepSeq flags2 "${PDB_BASE}" "${PDB_BASE}"


k=0
N_CORES=$N

for ((i=1; i<=${x}; i++)); do
    # Wait for a slot to open if N_CORES are already running
    ((k=k%N_CORES));
    ((k++==0)) && wait

    # Run the job in the background, redirecting STDOUT and STDERR to a log file
    pepspec.static.linuxgccrelease @flags2_${i}.txt -database /usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408/main/database >> "${OUTPATH}/${PDB_BASE}.log" 2>&1 &
done

# Wait for all background jobs to finish
wait

echo "All pepspec runs finished."


#echo "run_foldX.sh ${OUTPUT_DIR} ${PDB_BASE} pepSeq"
run_foldX.sh "${OUTPUT_DIR}" "${PDB_BASE}" pepSeq

# The Python manager (run_iteMAN.py) will now handle aggregation
# and the final zip after all iterations are complete.

# To  ensure the script exits cleanly so the Python manager continues.
# Exit the shell
exit 0
