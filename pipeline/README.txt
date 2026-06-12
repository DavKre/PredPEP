This pipeline offers two possibilities

1.   If there are no experimentaly verified interactions between target and a protein/peptide that could be used as template, then needs to run anchor docking first and use outputs of anchor docking for peptide design
using the flag -d 1

2.   If there are experimentaly verified interactions between target and a protein/peptide that could be used as template, then run peptide design using step using the flag -d 0 

bash run_pepSpec4.sh -f1 test_pipe1 -f2 test_pipe2 -s pep -m homol -r res -p CD19 -i CD19 -f flags -d 1


In the following scripts set the starting residue number of the peptide if it is not 1 in the starting PDB

1.  run_createflag1.sh
declare -i x=n

2.  run_createflag2_1.sh
declare -i x=n

3.  run_createflag2_2.sh
declare -i x=n

4.  run_editPDB.sh
resid=n-1

5.  inputPDB/run_createH.sh
declare -i x=n
