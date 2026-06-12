// static/plots_utils.js

// --- 1. RE-EXPORTS ---
// These allow index.js to import everything from this one hub.
export { renderComparisonPlots } from './tab4_plots.js';
export { renderAdvancedPlots } from './tab5_analysis.js';

// Global variables to store parsed and processed data
window.globalData = {
    jobId: null,
    masterId: null,
    foldx: null, 
    rosetta: null,
    rosettaHeaders: []
};

/**
 * Helper to normalize PDB IDs for matching across different software outputs.
 */
export function normalizeId(id) {
    if (!id) return "";
    const clean = id.replace('.pdb', '').replace('_full', '');
    const parts = clean.split('_');
    return parts.length >= 2 ? `${parts[0]}_${parts[1]}` : clean;
}

/**
 * Parses FoldX (.all.txt) content.
 */
export function parseFoldXData(content) {
    if (!content) return [];
    const lines = content.trim().split('\n').filter(line => line.length > 0);
    return lines.map(line => {
        const parts = line.trim().split(/\s+/);
        if (parts.length >= 2) {
            return {
                pdbId: parts[0],
                score: parseFloat(parts[1]),
            };
        }
        return null;
    }).filter(item => item !== null);
}

/**
 * Parses Rosetta CSV content.
 */
export function parseRosettaData(content) {
    if (!content) return [];
    const lines = content.trim().split('\n').filter(line => line.length > 0);
    if (lines.length < 1) return [];

    const headers = lines[0].split(',').map(h => h.trim());
    window.globalData.rosettaHeaders = headers;

    return lines.slice(1).map(line => {
        const parts = line.split(',');
        const obj = {};
        headers.forEach((header, index) => {
            if (parts[index] !== undefined) {
                const value = parts[index].trim();
                obj[header] = isNaN(parseFloat(value)) ? value : parseFloat(value);
            }
        });
        return obj;
    });
}

/**
 * Merges FoldX and Rosetta data using normalized IDs.
 * THIS WAS MISSING IN YOUR NEW FILE
 */
export function mergeData(foldxData, rosettaData) {
    const rosettaIndex = {};
    rosettaData.forEach(r => {
        const rawId = r.pepID || r.pdbId || (r.pdb_relative_path ? r.pdb_relative_path.split('/').pop() : null);
        if (rawId) {
            rosettaIndex[normalizeId(rawId)] = r;
        }
    });

    return foldxData.map(f => {
        const normFoldxId = normalizeId(f.pdbId);
        const rosettaScores = rosettaIndex[normFoldxId];
        
        return {
            pdbId: f.pdbId,
            foldxScore: f.score,
            rosettaScores: rosettaScores || { total_score: 0, interface_score: 0 }
        };
    });
}

/**
 * Main function to load and process all data upon job completion.
 */
export function processAllData(jobId, rawData) {
    window.globalData.jobId = jobId;
    window.globalData.masterId = rawData.master_id;
    
    window.globalData.foldx = parseFoldXData(rawData.foldx_txt_content);
    
    if (rawData.rosetta_csv_content) {
        window.globalData.rosetta = parseRosettaData(rawData.rosetta_csv_content);
    } else {
        window.globalData.rosetta = [];
    }

    const masterDisp = document.getElementById('masterIdDisplay');
    if (masterDisp) masterDisp.textContent = window.globalData.masterId;
    
    if (typeof window.generateResultsTable === 'function') {
         window.generateResultsTable(window.globalData.foldx, window.globalData.rosetta, rawData.base_result_folder);
    }
}
