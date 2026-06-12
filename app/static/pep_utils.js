// static/pep_utils.js

// Kyte-Doolittle Hydrophobicity Scale
const KD_SCALE = {
    'A': 1.8, 'R': -4.5, 'N': -3.5, 'D': -3.5, 'C': 2.5, 'Q': -3.5, 'E': -3.5,
    'G': -0.4, 'H': -3.2, 'I': 4.5, 'L': 3.8, 'K': -3.9, 'M': 1.9, 'F': 2.8,
    'P': -1.6, 'S': -0.8, 'T': -0.7, 'W': -0.9, 'Y': -1.3, 'V': 4.2
};

// Simplified pKa values for pI/Charge calculation (pH 7.4)
const PKAS = {
    'N_TERM': 9.6, 'C_TERM': 2.3,
    'D': 3.9, 'E': 4.2, 'H': 6.0, 'C': 8.3, 'Y': 10.1, 'K': 10.5, 'R': 12.5
};

export function calculateGRAVY(seq) {
    if (!seq) return 0;
    const scores = seq.split('').map(aa => KD_SCALE[aa.toUpperCase()] || 0);
    return scores.reduce((a, b) => a + b, 0) / seq.length;
}

export function calculateNetCharge(seq, pH = 7.4) {
    if (!seq) return 0;
    let charge = 0;
    charge += 1 / (1 + Math.pow(10, pH - PKAS.N_TERM));
    charge -= 1 / (1 + Math.pow(10, PKAS.C_TERM - pH));
    for (let aa of seq.toUpperCase()) {
        if (['D', 'E', 'C', 'Y'].includes(aa)) charge -= 1 / (1 + Math.pow(10, PKAS[aa] - pH));
        else if (['K', 'R', 'H'].includes(aa)) charge += 1 / (1 + Math.pow(10, pH - PKAS[aa]));
    }
    return charge;
}

export function calculateAromaticity(seq) {
    if (!seq) return 0;
    const aromatic = ['F', 'W', 'Y'];
    const count = seq.split('').filter(aa => aromatic.includes(aa.toUpperCase())).length;
    return count / seq.length;
}
