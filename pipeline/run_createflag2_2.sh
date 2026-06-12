#!/bin/bash

# This template should be used when -d is 0 i.e. PDB having experimentally verified interaction

INFILE1=$1
OUTFILE=$2
PDBFILE=$3
ID=$4

#Define starting residue number if it is not 1
declare -i x=1
declare -i j=0

f1=$( cat ${INFILE1}.txt )
for (( i=0; i<${#f1}; i++ )); do
   j=$((j+1))
   echo "-ignore_unrecognized_res" > ${OUTFILE}_${j}.txt
   echo "-restore_pre_talaris_2013_behavior" >> ${OUTFILE}_${j}.txt
   echo "-score::weights pre_talaris_2013_standard.wts" >> ${OUTFILE}_${j}.txt
   echo "-pepspec::soft_wts soft_rep.wts" >> ${OUTFILE}_${j}.txt
   echo "-pepspec::interface_cutoff 3.0" >> ${OUTFILE}_${j}.txt
   echo "-pepspec::clash_cutoff 4.0" >> ${OUTFILE}_${j}.txt
   #echo "-pepspec::clash_cutoff 5.0" >> ${OUTFILE}_${j}.txt
   echo "-pepspec::n_peptides 10" >> ${OUTFILE}_${j}.txt
   echo "-pepspec::n_build_loop 100" >> ${OUTFILE}_${j}.txt
   echo "-pepspec:use_input_bb true" >> ${OUTFILE}_${j}.txt
   echo "-pepspec:gen_pep_bb_sequential true" >> ${OUTFILE}_${j}.txt
   echo "-pepspec::diversify_pep_seqs true" >> ${OUTFILE}_${j}.txt

   # When the experimentally verified interaction is avail then that pdb could be used instead of 
   # pdb of anchor docking having best score

   echo "-s inputPDB/${PDBFILE}.mod.pdb" >> ${OUTFILE}_${j}.txt
   echo "-pepspec:rmsd_analysis true" >> ${OUTFILE}_${j}.txt
   echo "-ex1" >> ${OUTFILE}_${j}.txt
   echo "-ex2" >> ${OUTFILE}_${j}.txt
   echo "-extrachi_cutoff 0" >> ${OUTFILE}_${j}.txt
   echo "-pepspec::pep_chain B" >> ${OUTFILE}_${j}.txt
   echo "-pepspec::pep_anchor ${x}" >> ${OUTFILE}_${j}.txt
   echo "-pepspec::n_prepend 0" >> ${OUTFILE}_${j}.txt
   echo "-pepspec::n_append 0" >> ${OUTFILE}_${j}.txt
   echo "-pepspec:use_input_bb true" >> ${OUTFILE}_${j}.txt
   echo "-o ${ID}${f1:$i:1}${x}" >> ${OUTFILE}_${j}.txt
   #echo "${i} ${x} ${j}"
   x=$((x+1))
done
