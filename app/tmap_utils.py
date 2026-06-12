#!/home/spacepep/miniforge3/envs/predPEP/bin/python

import tmap as tm
from mhfp.encoder import MHFPEncoder
from rdkit import Chem
import numpy as np

def generate_tmap_coordinates(sequences):
    """
    Generates TMAP coordinates for peptide sequences.
    Converts: Sequence -> RDKit Mol -> MHFP Fingerprint -> TMAP Layout.
    Works for dynamic dataset sizes (e.g., 481+ rows).
    """
    if not sequences:
        return [], [], [], [], []

    # 1024 permutations is robust for datasets from 10 to 100,000 rows
    perm = 1024
    enc = MHFPEncoder(n_permutations=perm, seed=42)
    
    fps = []
    valid_indices = []

    print(f"DEBUG: TMAP processing {len(sequences)} sequences from CSV.")

    for i, seq in enumerate(sequences):
        # 1. Cleaning: Ensure no whitespace and standard uppercase
        clean_seq = "".join(str(seq).split()).strip().upper()
        if not clean_seq or clean_seq in ['NAN', 'NONE', 'NULL']:
            continue
            
        # 2. RDKit Mol Creation
        mol = Chem.MolFromSequence(clean_seq)
        if mol is None:
            mol = Chem.MolFromFASTA(f">pep\n{clean_seq}")

        if mol is None:
            continue
            
        try:
            # 3. MHFP Encoding
            raw_fp = enc.encode_mol(mol)
            
            # 4. Bitmasking: Ensure 32-bit unsigned integers for TMAP compatibility
            # We convert to list to satisfy the C++ binding requirements found in testing
            fp_list = [int(h & 0xFFFFFFFF) for h in raw_fp]
            
            fps.append(tm.VectorUint(fp_list))
            valid_indices.append(i)
        except Exception as e:
            print(f"DEBUG: Hashing error at index {i} ({clean_seq}): {e}")
            continue
    
    if len(fps) < 2:
        print(f"DEBUG: TMAP failed. Only {len(fps)} valid molecules found.")
        return [], [], [], [], []
    
    try:
        # 5. Build LSH Forest
        lf = tm.LSHForest(perm, 64)
        lf.batch_add(fps)
        lf.index()
        
        # 6. Layout Configuration
        cfg = tm.LayoutConfiguration()
        cfg.node_size = 1/25
        cfg.k = min(len(fps) - 1, 30)
        cfg.sl_extra_scaling_steps = 5
        cfg.sl_scaling_type = tm.ScalingType.RelativeToVisible
        
        # 7. Generate MST Coordinates
        # FIX: Added 'g' to capture the 5th return value to prevent ValueError
        # FIX: 'g' hinzugefuegt, um den 5. Rueckgabewert zu erfassen und ValueError zu verhindern
        x, y, s, t, g = tm.layout_from_lsh_forest(lf, cfg)
        
        print(f"DEBUG: TMAP successful. Processed {len(valid_indices)} sequences.")
        return list(x), list(y), list(s), list(t), valid_indices
        
    except Exception as e:
        print(f"DEBUG: TMAP Layout Internal Error: {e}")
        return [], [], [], [], []
