// static/tab5_analysis.js

/**
 * Renders the plots for Tab 5 (Advanced Analysis - PCA and Heatmaps).
 */
export function renderAdvancedPlots() {
    const rosettaData = window.globalData.rosetta;
    
    // --- Plot 1: PCA Plot ---
    const pcaData = rosettaData.filter(d => d.PC1 && d.PC2); 
    
    if (pcaData.length > 0) {
        const pcaTrace = {
            x: pcaData.map(d => d.PC1),
            y: pcaData.map(d => d.PC2),
            mode: 'markers',
            type: 'scatter',
            name: 'Peptides',
            marker: { size: 8, color: '#6a0dad' },
            text: pcaData.map(d => `PDB: ${d.pepID}<br>PC1: ${d.PC1.toFixed(2)}<br>PC2: ${d.PC2.toFixed(2)}`),
            hoverinfo: 'text'
        };

        const pcaLayout = {
            title: 'Principal Component Analysis (PCA)',
            xaxis: { title: 'Principal Component 1' },
            yaxis: { title: 'Principal Component 2' },
            height: 500,
            margin: { t: 50, b: 50, l: 50, r: 50 }
        };

        Plotly.newPlot('pca-plot', [pcaTrace], pcaLayout);
    } else {
        document.getElementById('pca-plot').innerHTML = '<p>PCA data (PC1, PC2 columns) not found in Rosetta CSV.</p>';
    }

    // --- Plot 2: Feature Correlation Heatmap ---
    document.getElementById('correlation-heatmap').innerHTML = '<p>Correlation Heatmap functionality is complex and relies on server-side correlation matrix calculation. Placeholder for now.</p>';
}
