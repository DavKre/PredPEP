#!/bin/bash


FOLDER=$1
PDB_BASE=$2
INFILE=$3

declare -i x=1
declare -i j=0

#cd ${FOLDER}

#echo "I am inside teh run_foldX.sh"
echo "The current directory is: $(pwd)"

res=$( cat ${INFILE}.txt )

#echo "runing for loop of run_foldx.sh"
for (( i=0; i<${#res}; i++ )); do

   j=$((j+1))
   #echo "${j}"
   #echo "The Folder is: ${FOLDER}${res:$i:1}${x}"
   #echo "The folder is: ${FOLDER}"
   run_foldX2.sh ${PDB_BASE}${res:$i:1}${x} ${PDB_BASE}
   x=$((x+1))
   #echo "${x}"
done

cat ${PDB_BASE}.all.txt |sort -n -k 2 > ${PDB_BASE}.all.tab
