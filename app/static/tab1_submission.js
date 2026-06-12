// static/tab1_submission.js

/**
 * Count unique chain B residues in a PDB text (CA atoms only).
 * Dedup key = chain + resSeq + iCode. In a multi-model NMR file each residue's
 * CA appears once per MODEL, but (resSeq, iCode) is stable across models, so
 * the Set collapses the duplicates automatically — no explicit MODEL/ENDMDL
 * tracking is needed as long as model numbering is consistent (standard).
 * Includes HETATM so modified residues (MSE, SEP, D-amino acids, etc.) count.
 */
function countChainBResidues(pdbText) {
    if (!pdbText) return 0;
    const seen = new Set();
    for (const line of pdbText.split('\n')) {
        if (line.length < 27) continue;
        const rec = line.slice(0, 6);
        if (!rec.startsWith('ATOM') && !rec.startsWith('HETATM')) continue;
        if (line.slice(12, 16).trim() !== 'CA') continue;
        if (line[21] !== 'B') continue;
        seen.add(line.slice(22, 27)); // resSeq (22-25) + iCode (26)
    }
    return seen.size;
}

/**
 * Divisors of n within [min, max], ascending. Used to suggest cpus values
 * that cleanly partition the per-residue work into equal waves.
 */
function divisorsInRange(n, min, max) {
    const divs = [];
    const upper = Math.min(n, max);
    for (let i = min; i <= upper; i++) {
        if (n % i === 0) divs.push(i);
    }
    return divs;
}

/**
 * Update the cpus hint span based on detected peptide length and current cpus.
 * Reads window.peptideLength + window.peptideDivisors (set by the file-change
 * handler) and the #cpus input.
 */
window.updateCpusHint = function() {
    const hintEl = document.getElementById('cpus-hint');
    if (!hintEl) return;
    const length = window.peptideLength;
    const cpus = parseInt(document.getElementById('cpus').value, 10) || 8;

    if (length === undefined) {
        hintEl.textContent = 'Upload a PDB to see recommended core count.';
        hintEl.style.color = '#666';
        hintEl.style.fontStyle = 'normal';
    } else if (length === 0) {
        hintEl.textContent = 'Could not detect peptide length — your cpus setting will be honored.';
        hintEl.style.color = '#999';
        hintEl.style.fontStyle = 'italic';
    } else if (cpus > length) {
        hintEl.textContent = `Peptide length: ${length} residues. Only ${length} cores will be used; use cpus=${length} for a single efficient wave.`;
        hintEl.style.color = '#d4a017';
        hintEl.style.fontStyle = 'normal';
    } else if (length % cpus !== 0) {
        const divisors = window.peptideDivisors || [];
        const below = divisors.filter(d => d < cpus).slice(-1);
        const atOrAbove = divisors.filter(d => d >= cpus).slice(0, 1);
        const suggestions = [...below, ...atOrAbove];
        let suggestion = '';
        if (suggestions.length === 1) suggestion = ` Nearest efficient value: ${suggestions[0]}.`;
        else if (suggestions.length === 2) suggestion = ` Nearest efficient values: ${suggestions[0]} or ${suggestions[1]}.`;
        hintEl.textContent = `Peptide length: ${length} residues. With ${cpus} cores, jobs run in waves of ${cpus} followed by a final wave of ${length % cpus}, which underuses cores.${suggestion}`;
        hintEl.style.color = '#777';
        hintEl.style.fontStyle = 'normal';
    } else {
        const waves = length / cpus;
        const waveText = waves === 1 ? '1 wave' : `${waves} waves`;
        hintEl.textContent = `Peptide length: ${length} residues. ${waveText} of ${cpus} cores — efficient.`;
        hintEl.style.color = '#666';
        hintEl.style.fontStyle = 'normal';
    }
};

/**
 * 1. NGL VIEWER INITIALIZATION
 */
window.initializeNGLTab1Viewer = function() {
    const fileInput = document.getElementById('file1');
    const viewerDiv = document.getElementById('pdb-viewer');

    if (!fileInput || !viewerDiv) return;

    if (!window.stageTab1) {
        window.stageTab1 = new NGL.Stage("pdb-viewer", { backgroundColor: "white" });
    }

    fileInput.addEventListener('change', function(e) {
        const file = e.target.files[0];
        if (!file) return;

        const reader = new FileReader();
        reader.onload = function(event) {
            const pdbText = event.target.result;
            const blob = new Blob([pdbText], { type: 'text/plain' });
            window.stageTab1.removeAllComponents();
            window.stageTab1.loadFile(blob, { ext: "pdb" }).then(function(component) {
                component.addRepresentation("cartoon", { color: "blue" });
                component.addRepresentation("ball+stick", { sele: "ligand" });
                window.stageTab1.autoView();
            });

            const dataDisplay = document.getElementById('pdb-data');
            if (dataDisplay) {
                dataDisplay.textContent = `File loaded: ${file.name} (${(file.size / 1024).toFixed(1)} KB)`;
            }

            // Detect chain B peptide length and cache divisors for the cpus hint.
            window.peptideLength = countChainBResidues(pdbText);
            window.peptideDivisors = divisorsInRange(window.peptideLength, 2, 32);
            window.updateCpusHint();
        };
        reader.readAsText(file);
    });
};

/**
 * 2. FORM SUBMISSION HANDLER
 */
document.getElementById('uploadForm').addEventListener('submit', async function(e) {
    e.preventDefault();

    if (window.statusInterval) {
        clearInterval(window.statusInterval);
    }

    const fileInput = document.getElementById('file1');
    const proteinInput = document.getElementById('protein_symbol');
    const userInput = document.getElementById('user_name');

    if (!fileInput.files.length) {
        document.getElementById('message').textContent = "Please select a file.";
        return;
    }

    if (!proteinInput.value || !userInput.value) {
        document.getElementById('message').textContent = "Protein Symbol and User Name are required.";
        return;
    }

    const formData = new FormData();
    formData.append('file1', fileInput.files[0]);
    formData.append('cpus', document.getElementById('cpus').value);
    formData.append('protein_symbol', proteinInput.value);
    formData.append('user_name', userInput.value);

    // UI Feedback
    document.getElementById('message').textContent = "Uploading PDB to server...";
    document.getElementById('downloadLink').innerHTML = "";
    document.getElementById('loading').style.display = 'block';

    // Disable ALL result tabs (including Tab 6)
    ['tab2Button', 'tab3Button', 'tab4Button', 'tab5Button', 'tab6Button'].forEach(id => {
        const btn = document.getElementById(id);
        if (btn) btn.disabled = true;
    });

    try {
        const response = await fetch('/upload', {
            method: 'POST',
            body: formData
        });

        const data = await response.json();

        if (response.ok && data.success) {
            // Use the actual job_id returned by predPEP.py
            const jobId = data.job_id;
            window.currentJobId = jobId;

            document.getElementById('message').textContent = "Upload successful. Job ID: " + jobId;

            // Start polling every 5 seconds (10s is a bit slow for feedback)
            // pollStatus is defined in index.js
            window.statusInterval = setInterval(() => window.pollStatus(jobId), 5000);

        } else {
            clearInterval(window.statusInterval);
            document.getElementById('loading').style.display = 'none';
            document.getElementById('message').textContent = "Submission Error: " + (data.error || "Unknown server error.");
        }

    } catch (error) {
        console.error("Submission Error:", error);
        clearInterval(window.statusInterval);
        document.getElementById('loading').style.display = 'none';
        document.getElementById('message').textContent = "An error occurred during submission.";
    }
});

// Initialize cpus hint on first load (shows "Upload a PDB..." until a file is picked).
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => window.updateCpusHint && window.updateCpusHint());
} else {
    window.updateCpusHint && window.updateCpusHint();
}
