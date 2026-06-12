#!/bin/bash

FOLDER=$1
MFOLDER=$2


#echo "running run_catFiles.sh"
echo "${MFOLDER}/"

cd ${MFOLDER}/

echo "${FOLDER}"

STRIPPED_FOLDER="${FOLDER%%_*}"

declare -i y=1

current_folder=$(pwd)

echo "The script is running in the folder: $current_folder"

#declare -a array=$( find . -maxdepth 1 -name "${FOLDER}*" -type d )

declare -a array=($( ls -d ${STRIPPED_FOLDER}*/))

if [ -f "${STRIPPED_FOLDER}.all.txt" ] ; then
    cat ${STRIPPED_FOLDER}.all.txt >> ${STRIPPED_FOLDER}.old.tab
    rm  ${STRIPPED_FOLDER}.all.txt
    #awk -i inplace '!seen[$0]++' ${STRIPPED_FOLDER}.old.tab
    awk '!seen[$0]++' ${STRIPPED_FOLDER}.old.tab > ${STRIPPED_FOLDER}.tmp && mv ${STRIPPED_FOLDER}.tmp ${STRIPPED_FOLDER}.old.tab
    cat ${STRIPPED_FOLDER}.old.tab |sort -nk 2 > ${STRIPPED_FOLDER}.old.txt
fi

for i in "${array[@]}"
do
  x=$(echo $i |sed 's/\.\///1' |sed 's/\///1')
  echo $x
  cat ${x}/${STRIPPED_FOLDER}*.all.tab >> ${STRIPPED_FOLDER}.all.tab
done

cat ${STRIPPED_FOLDER}.all.tab |sort -nk 2 > ${STRIPPED_FOLDER}.all.txt
rm ${STRIPPED_FOLDER}.all.tab
