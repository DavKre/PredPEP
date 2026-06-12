#!/bin/bash

export PATH="/usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408/main/tools/protein_tools/scripts:$PATH"

FOLDER1=$1
FOLDER2=$2


#echo "I am inside the run_foldX2.sh"

#echo "Going to ${FOLDER2}"
#cd ${FOLDER2}

#echo "The current directory is: $(pwd)"

#chmod 775 foldX_ana

cd foldX_ana/

#echo "The current directory is: $(pwd)"
#echo "The Folder 1 is:${FOLDER1}"
#echo "The Folder 2 is:${FOLDER2}"

#for i in {1..10}
#echo "runing for loop of run_foldX2.sh"
for ((i=1; i<=10; i++)); do
   #echo "${i}"
   if [[ -f "../${FOLDER1}.pdbs/${FOLDER1}_${i}.pdb" ]]; then

       cp ../${FOLDER1}.pdbs/${FOLDER1}_${i}.pdb ./

       clean_pdb.py ${FOLDER1}_${i}.pdb B >> ${FOLDER1}.all.fasta

       foldx_20270131 -c AnalyseComplex --pdb="${FOLDER1}_${i}.pdb" --analyseComplexChains=A,B >> ${FOLDER1}.txt

       rm ${FOLDER1}_${i}.pdb
   fi
done

#echo "${FOLDER1}.txt"
grep "Total          = 				  \|Complex Analysis of ." ${FOLDER1}.txt |sed 's/Complex Analysis of \.\///1' |sed 's/went fine//1' |sed 'N;s/\n/ /1' |awk 'NR%2==0' |sed 's/Total          =//g' |awk -F' ' '{print $2 "\t" $1}' > ${FOLDER1}.1.txt
cat ${FOLDER1}.1.txt |awk 'NR == 1 || $2 < min {line = $0; min = $2}END{print line}' >> ${FOLDER1}.min.txt
cat ${FOLDER1}.1.txt >> ../${FOLDER2}.all.txt
rm *.fxout

cd ../
