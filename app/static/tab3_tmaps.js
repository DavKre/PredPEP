// static/tab3_tmaps.js
import { mergeData } from './plots_utils.js';
import { calculateGRAVY, calculateNetCharge, calculateAromaticity } from './pep_utils.js';

export function renderTSNEPlots(selectedMetric) {
    const data = window.globalData.rosetta; 
    if (!data || data.length === 0) return;

    // 1. Process data & calculate properties on the fly
    const plotData = data.map(d => ({
        ...d,
        GRAVY: calculateGRAVY(d.pepSeq),
        NetCharge: calculateNetCharge(d.pepSeq),
        Aromaticity: calculateAromaticity(d.pepSeq),
        // Ensure we have coordinates (using tSNE from CSV or placeholder)
        x: d.tSNE1 || 0, 
        y: d.tSNE2 || 0
    }));

    let traces = [];

    // --- CASE A: Top 10 Highlighting (FoldX or Rosetta) ---
    if (selectedMetric === 'FoldX' || selectedMetric === 'Rosetta') {
        const scoreKey = selectedMetric === 'FoldX' ? 'FoldX_Score' : 'Rosetta_Total_Score';
        
        const sorted = [...plotData].sort((a, b) => a[scoreKey] - b[scoreKey]);
        const top10Ids = new Set(sorted.slice(0, 10).map(p => p.pdbId));

        const others = plotData.filter(d => !top10Ids.has(d.pdbId));
        const top10 = plotData.filter(d => top10Ids.has(d.pdbId));

        traces.push({
            x: others.map(d => d.x), y: others.map(d => d.y),
            mode: 'markers', name: 'Others',
            marker: { color: 'rgba(200,200,200,0.4)', size: 6 }
        });

        traces.push({
            x: top10.map(d => d.x), y: top10.map(d => d.y),
            mode: 'markers', name: 'Top 10',
            marker: { color: '#22c55e', size: 12, line: { width: 2, color: '#14532d' } },
            text: top10.map(d => `Seq: ${d.pepSeq}<br>Score: ${d[scoreKey]}`),
            hoverinfo: 'text'
        });
    } 
    // --- CASE B: Heatmap for GRAVY, pI, NetCharge, Aromaticity ---
    else {
        traces.push({
            x: plotData.map(d => d.x),
            y: plotData.map(d => d.y),
            mode: 'markers',
            marker: {
                size: 8,
                color: plotData.map(d => d[selectedMetric]),
                colorscale: 'Viridis',
                showscale: true,
                colorbar: { title: selectedMetric }
            },
            text: plotData.map(d => `Seq: ${d.pepSeq}<br>${selectedMetric}: ${d[selectedMetric].toFixed(2)}`),
            hoverinfo: 'text'
        });
    }

    const layout = {
        title: `T-Map Cluster Analysis: ${selectedMetric}`,
        xaxis: { title: 't-SNE 1' },
        yaxis: { title: 't-SNE 2' },
        hovermode: 'closest',
        height: 600
    };

    Plotly.newPlot('tmap-foldx-best', traces, layout);
}
