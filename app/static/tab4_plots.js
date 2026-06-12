// static/tab4_plots.js
import { mergeData } from './plots_utils.js';

/**
 * Renders the plots for Tab 4 (Score Comparison).
 */
export function renderComparisonPlots() {
    const mergedData = mergeData(window.globalData.foldx, window.globalData.rosetta).filter(d => 
        !isNaN(d.foldxScore) && !isNaN(d.rosettaScores.total_score)
    );

    if (mergedData.length === 0) {
        document.getElementById('score-comparison-scatter').innerHTML = '<p>Insufficient data to compare FoldX and Rosetta scores.</p>';
        document.getElementById('foldx-distribution-plot').innerHTML = '<p>Insufficient data to plot FoldX distribution.</p>';
        return;
    }
    
    // --- Plot 1: Score Comparison Scatter Plot ---
    const scatterTrace = {
        x: mergedData.map(d => d.foldxScore),
        y: mergedData.map(d => d.rosettaScores.total_score),
        mode: 'markers',
        type: 'scatter',
        name: 'Peptide Scores',
        marker: { size: 8, color: '#3182bd' },
        text: mergedData.map(d => `PDB ID: ${d.pdbId}<br>FoldX: ${d.foldxScore.toFixed(2)}<br>Rosetta: ${d.rosettaScores.total_score.toFixed(2)}`),
        hoverinfo: 'text'
    };

    const scatterLayout = {
        title: 'FoldX vs. Rosetta Total Score',
        xaxis: { title: 'FoldX ΔΔG (kcal/mol)' },
        yaxis: { title: 'Rosetta Total Score' },
        height: 500,
        margin: { t: 50, b: 50, l: 50, r: 50 }
    };

    Plotly.newPlot('score-comparison-scatter', [scatterTrace], scatterLayout);

    // --- Plot 2: FoldX Distribution Plot ---
    const histogramTrace = {
        x: mergedData.map(d => d.foldxScore),
        type: 'histogram',
        marker: { color: '#e6550d' },
        name: 'FoldX ΔΔG'
    };

    const histogramLayout = {
        title: 'FoldX ΔΔG Distribution',
        xaxis: { title: 'FoldX ΔΔG (kcal/mol)' },
        yaxis: { title: 'Count' },
        height: 500,
        margin: { t: 50, b: 50, l: 50, r: 50 }
    };

    Plotly.newPlot('foldx-distribution-plot', [histogramTrace], histogramLayout);
}
