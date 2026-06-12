// static/tab2_results_table.js

/**
 * Initializes the NGL Viewer Stage.
 * Ensures the container has dimensions before initializing to prevent WebGL Framebuffer errors.
 */
window.initializeNGLViewer = () => {
    const container = document.getElementById('ngl-viewer');
    
    if (!container) {
        console.error("NGL Container 'ngl-viewer' not found in DOM.");
        return;
    }

    // FIX: Check if container has height/width. If it's 0 (hidden tab), retry when it's visible.
    if (container.offsetWidth === 0 || container.offsetHeight === 0) {
        console.warn("NGL Container has no size. NGL will initialize when the tab is clicked.");
        return;
    }

    if (!window.stage) { 
        window.stage = new NGL.Stage("ngl-viewer");
        window.stage.setParameters({ 
            backgroundColor: "white",
            clipNear: 0
        });
        
        // Add resize listener to the window
        window.addEventListener("resize", () => {
            if (window.stage) window.stage.handleResize();
        });
        
        console.log("NGL Viewer initialized with dimensions:", container.offsetWidth, "x", container.offsetHeight);
    }
};

/**
 * Loads a specific PDB file into the NGL Viewer using the deterministic relative path.
 */
window.loadPDBIntoNGL = async (relativePDBPath) => { 
    if (!relativePDBPath) return;

    // Use the secure streaming route
    const pdbUrl = `/stream_final_pdb/${relativePDBPath}`;
    const pdbId = relativePDBPath.split('/').pop();
    
    // Ensure stage is ready (and has dimensions)
    window.initializeNGLViewer();
    
    if (!window.stage) {
        console.error("NGL Stage could not be initialized. Is the container visible?");
        return;
    }
    
    window.stage.removeAllComponents();
    // Provide visual feedback inside the viewer
    const loadingMsg = document.createElement('div');
    loadingMsg.id = "ngl-loading-overlay";
    loadingMsg.style.cssText = "position:absolute; top:10px; left:10px; z-index:10; background:rgba(255,255,255,0.8); padding:5px; border-radius:4px;";
    loadingMsg.innerHTML = `Loading: <strong>${pdbId}</strong>...`;
    document.getElementById('ngl-viewer').appendChild(loadingMsg);

    try {
        const component = await window.stage.loadFile(pdbUrl, { ext: "pdb" });
        
        // Timeout to ensure NGL internal buffers are ready
        setTimeout(() => {
            component.addRepresentation("cartoon", { sele: "protein", color: "blue" });
            component.addRepresentation("licorice", { sele: "hetero or :B", color: "red" });
            component.addRepresentation("spacefill", { sele: "hetero or :B", color: "red", radius: 0.1 });

            window.stage.autoView();
            
            // Remove loading message
            const msg = document.getElementById('ngl-loading-overlay');
            if (msg) msg.remove();
            
            console.log(`NGL Component loaded and representations set for ${pdbId}`);
        }, 50);

    } catch (e) {
        console.error("Error loading PDB into NGL:", e);
        document.getElementById('ngl-viewer').innerHTML = `<div class="p-4 text-red-600">Failed to load PDB: ${pdbId}. <br>Error: ${e.message}</div>`;
    }
};

/**
 * Generates the interactive results table for Tab 2.
 */
window.generateResultsTable = (foldxData, rosettaData, resultBaseFolder) => {
    const tableBody = document.getElementById('resultsTable').querySelector('tbody');
    tableBody.innerHTML = ''; 
    
    if (!rosettaData || rosettaData.length === 0) {
        console.warn("No Rosetta/Merged data available to display in table.");
        return;
    }

    console.log(`DEBUG: Total Rosetta/Merged results (for table): ${rosettaData.length}`);
    
    // --- COLOR LOGIC: Find top 10 FoldX scores for highlighting ---
    // We assume rosettaData is already sorted, but we verify the top 10 values safely
    const foldxScores = rosettaData
        .map(r => parseFloat(r.FoldX_Score))
        .filter(s => !isNaN(s))
        .sort((a, b) => a - b);
    
    const top10Threshold = foldxScores.length >= 10 ? foldxScores[9] : foldxScores[foldxScores.length - 1];

    let topRelativePath = null;

    rosettaData.forEach((r, index) => {
        const pdbId = r.pdbId || 'Unknown'; 
        const pepSeq = r.pepSeq || 'N/A';
        const relativePDBPath = r.pdb_relative_path;

        if (!relativePDBPath) {
             console.error(`ERROR: No pdb_relative_path found for ${pdbId}. Skipping row.`);
             return;
        }

        if (index === 0) topRelativePath = relativePDBPath;
        
        // Parse scores
        const fScoreNum = parseFloat(r.FoldX_Score);
        const foldxScore = !isNaN(fScoreNum) ? fScoreNum.toFixed(2) : 'N/A';

        const rScoreNum = parseFloat(r.Rosetta_Total_Score);
        const rosettaTotalScore = !isNaN(rScoreNum) ? rScoreNum.toFixed(2) : 'N/A';
        
        const row = tableBody.insertRow();
        
        // --- HIGHLIGHTING LOGIC ---
        // If the score is within the top 10 (lowest values), color the row light green
        if (!isNaN(fScoreNum) && fScoreNum <= top10Threshold) {
            row.style.backgroundColor = "#dcfce7"; // Tailwind green-100 / Light green
        }
        
        // Column 1: Sequence/Link
        const cell1 = row.insertCell();
        const link = document.createElement('a');
        link.href = "#";
        link.className = "text-blue-600 hover:underline font-mono";
        link.textContent = pepSeq !== 'N/A' ? pepSeq : pdbId.replace('.pdb', '');
        link.onclick = (e) => {
            e.preventDefault();
            window.loadPDBIntoNGL(relativePDBPath);
        };
        cell1.appendChild(link);
        
        // Column 2: PDB ID
        row.insertCell().textContent = pdbId;
        
        // Column 3: FoldX Score
        const cellFoldX = row.insertCell();
        cellFoldX.textContent = foldxScore;
        cellFoldX.className = "font-semibold";
        
        // Column 4: Rosetta Total Score
        row.insertCell().textContent = rosettaTotalScore;

        if (index < 3) { 
            console.log(`DEBUG Row ${index + 1}: PDB: ${pdbId}, URL: /stream_final_pdb/${relativePDBPath}`);
        }
    });

    // Initial load for the top hit
    if (topRelativePath) {
        // Use a small delay to ensure the Tab 2 is becoming visible/rendered
        setTimeout(() => window.loadPDBIntoNGL(topRelativePath), 100);
    }
};
