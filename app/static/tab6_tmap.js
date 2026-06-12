/**
 * Render logic for the Tree-Map (Minimum Spanning Tree)
 * Specifically handles the Master ID resolution to ensure correct file paths.
 */
export function renderMSTTree(selectedMetric) {
    const job_id = window.currentJobId; // e.g., "SPCRCK_ea2ea86d"
    const container = document.getElementById('tmap6-container');
    
    if (!job_id || !container) {
        console.warn("MST Rendering skipped: Missing JobID or Container.");
        return;
    }

    // --- FIX: Resolve Master ID (SPCRCK_ea2ea86d -> SPCRCK) ---
    // This matches the folder structure where your CSV and PDBs actually live.
    const master_id = job_id.split('_')[0]; 

    // Show loading state
    container.innerHTML = `
        <div style="display: flex; flex-direction: column; align-items: center; justify-content: center; height: 700px; color: #666; background: #fafafa; border-radius: 8px;">
            <div style="margin-bottom: 15px; font-weight: bold; font-size: 1.2rem;">Mapping Chemical Space...</div>
            <div style="font-size: 0.9rem;">Reading data from master folder: <strong>${master_id}</strong></div>
            <div style="margin-top: 10px; font-size: 0.8rem; color: #999;">Generating fingerprints & LSH Forest</div>
        </div>
    `;

    // We still pass the full job_id to the route, but the backend uses get_master_id()
    fetch(`/get_tmap_tree/${job_id}`)
        .then(response => response.json())
        .then(data => {
            if (!data.success) {
                container.innerHTML = `
                    <div style="padding:30px; text-align:center;">
                        <div style="color:#721c24; background:#f8d7da; border:1px solid #f5c6cb; padding:15px; border-radius:4px; display:inline-block;">
                            <strong>TMAP Data Not Ready</strong><br>
                            ${data.error}<br>
                            <small style="display:block; margin-top:10px;">Looking in master folder: ${master_id}</small>
                        </div>
                    </div>`;
                return;
            }

            // Small timeout to ensure the DOM is ready for WebGL (scattergl)
            setTimeout(() => {
                const { x, y, s, t, metadata } = data;

                // 1. Edge Trace (The Tree structure)
                const edgeX = [];
                const edgeY = [];
                for (let i = 0; i < s.length; i++) {
                    edgeX.push(x[s[i]], x[t[i]], null);
                    edgeY.push(y[s[i]], y[t[i]], null);
                }

                const edgeTrace = {
                    x: edgeX,
                    y: edgeY,
                    mode: 'lines',
                    line: { color: '#cccccc', width: 1 },
                    hoverinfo: 'none',
                    type: 'scattergl' 
                };

                // 2. Node Trace (The Peptides)
                const isScore = selectedMetric.toLowerCase().includes('score');
                const metricValues = metadata.map(d => parseFloat(d[selectedMetric]) || 0);

                const nodeTrace = {
                    x: x,
                    y: y,
                    mode: 'markers',
                    type: 'scattergl',
                    marker: {
                        size: 14,
                        color: metricValues,
                        colorscale: isScore ? 'Viridis' : 'Plasma',
                        reversescale: isScore, 
                        showscale: true,
                        colorbar: { 
                            title: selectedMetric.replace(/_/g, ' '), 
                            thickness: 20 
                        },
                        line: { width: 1.5, color: '#ffffff' }
                    },
                    // Store relative path for the click-to-viewer feature
                    customdata: metadata.map(d => d.pdb_relative_path),
                    text: metadata.map((d, i) => {
                        return `<b>Peptide:</b> ${d.pepSeq}<br>` +
                               `<b>${selectedMetric.replace(/_/g, ' ')}:</b> ${metricValues[i].toFixed(4)}<br>` +
                               `<i style="color:#aaa;">Click node to view 3D structure</i>`;
                    }),
                    hoverinfo: 'text'
                };

                const layout = {
                    title: {
                        text: `<b>Peptide Similarity Landscape (MST)</b><br><span style="font-size:12px; color: #666;">Structural Similarity based on MHFP Fingerprints</span>`,
                        font: { size: 18 }
                    },
                    hovermode: 'closest',
                    dragmode: 'pan', // Enable panning
                    xaxis: { visible: false },
                    yaxis: { visible: false },
                    margin: { l: 0, r: 0, b: 0, t: 80 },
                    height: 700,
                    template: 'plotly_white'
                };

                const config = {
                    responsive: true,
                    displaylogo: false,
                    scrollZoom: true, // Enable mouse wheel zoom
                    modeBarButtonsToRemove: ['select2d', 'lasso2d']
                };

                Plotly.newPlot('tmap6-container', [edgeTrace, nodeTrace], layout, config);

                // --- TAB SYNC: Click a node to open it in Tab 2 NGL Viewer ---
                container.on('plotly_click', function(clickData) {
                    const relativePath = clickData.points[0].customdata;
                    if (relativePath && window.loadPDBIntoNGL) {
                        // Find the Tab 2 button and click it
                        const tab2Btn = document.querySelector('button[onclick*="tab2"]');
                        if (tab2Btn) tab2Btn.click();
                        
                        // Load the structure using the same logic as the Results Table
                        window.loadPDBIntoNGL(relativePath);
                    }
                });

            }, 100);
        })
        .catch(err => {
            console.error("TMAP Loading Error:", err);
            container.innerHTML = `<div style="padding:20px; color:red;">Connection error. Could not retrieve TMAP coordinates.</div>`;
        });
}
