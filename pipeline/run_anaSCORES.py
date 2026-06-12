#!/home/spacepep/miniforge3/envs/predPEP/bin/python

import pandas as pd
import numpy as np
from sklearn.preprocessing import StandardScaler
from sklearn.decomposition import PCA
from sklearn.manifold import TSNE
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D # Required for 3D plot
import seaborn as sns
import os
import warnings 

# --- Configuration ---
INPUT_CSV = "processed_peptide_scores_clean.csv"
OUTPUT_NUMERIC_CSV = "numerical_scores_for_analysis_final.csv"

# The most important Rosetta score terms for binding and structure quality
IMPORTANT_SCORE_COLUMNS = [
    'binding_score',
    'interface_score',
    'total_score',
    'fa_atr',       # Attractive Van der Waals
    'fa_rep',       # Repulsive Van der Waals (steric clashes)
    'fa_sol',       # Solvation/Desolvation
    'fa_dun',       # Side-chain rotamer quality/strain
    'hbond_sc',     # Side-chain hydrogen bonding
    'pro_close',    # Proline closure term
    'ref'           # Reference energy (inherent stability)
]
# ---------------------

def prepare_data_for_plotting(input_file, output_file, selected_cols):
    """
    Loads the processed data, selects key columns, scales the data,
    and performs PCA/t-SNE for dimension reduction.
    """
    if not os.path.exists(input_file):
        print(f"❌ Input file not found: {input_file}. Please run the score extraction script first.")
        return None

    print(f"📂 Loading data from {input_file}...")
    df = pd.read_csv(input_file)

    # 1. Select the relevant numerical columns (using .copy() to avoid SettingWithCopyWarning)
    score_cols_present = [col for col in selected_cols if col in df.columns]
    
    if not score_cols_present:
        print("❌ None of the specified important score columns were found in the CSV.")
        return None

    df_numeric = df.loc[:, score_cols_present].copy()
    metadata_df = df[['pepID', 'pepSeq']].copy()

    # --- Robust NaN and Constant Column Handling ---
    df_numeric.dropna(axis=1, how='all', inplace=True)
    
    # DROP CONSTANT COLUMNS (std=0 causes division-by-zero during scaling)
    constant_cols = df_numeric.columns[df_numeric.std() == 0]
    if not constant_cols.empty:
        print(f"⚠️ Dropping constant columns (StdDev=0): {', '.join(constant_cols.tolist())}")
        df_numeric.drop(columns=constant_cols, inplace=True)
        
    score_cols = df_numeric.columns.tolist()
    
    # Fill remaining NaNs with the mean of the column (Imputation)
    df_numeric.fillna(df_numeric.mean(), inplace=True)

    # Drop any rows that still contain NaN
    df_numeric.dropna(axis=0, inplace=True)
    
    if df_numeric.empty:
        print("❌ Data frame is empty after cleaning. Cannot proceed.")
        return None

    print(f"🔢 Selected {len(score_cols)} numerical features: {', '.join(score_cols)}")

    # 3. Standardize the data (Mean=0, StdDev=1)
    scaler = StandardScaler()
    
    # --- WARNING SUPPRESSION START ---
    # Suppress non-critical runtime warnings often seen during scaling large datasets
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=RuntimeWarning)
        warnings.filterwarnings("ignore", "overflow encountered in square")
        warnings.filterwarnings("ignore", "invalid value encountered in subtract")
        
        scaled_data = scaler.fit_transform(df_numeric)
    # --- WARNING SUPPRESSION END ---

    # --- CRITICAL FIX: Final Cleanup of Scaled Data ---
    # Convert any remaining NaN or Inf created by scaling to 0.0 or system boundary values
    scaled_data = np.nan_to_num(scaled_data, nan=0.0, posinf=np.finfo(np.float64).max, neginf=np.finfo(np.float64).min)
    # --- CRITICAL FIX END ---
    
    df_scaled = pd.DataFrame(scaled_data, columns=score_cols, index=df_numeric.index) 
    
    # Re-align metadata with cleaned, scaled data 
    metadata_df = metadata_df.loc[df_numeric.index].reset_index(drop=True)
    df_scaled.reset_index(drop=True, inplace=True)

    df_final = pd.concat([metadata_df, df_scaled], axis=1)

    # --- Dimension Reduction (for clustering/visualization) ---
    print("🧠 Performing Dimension Reduction (PCA and t-SNE)...")
    
    # A. Principal Component Analysis (PCA) for 2D/3D plots
    pca = PCA(n_components=3)
    principal_components = pca.fit_transform(scaled_data) 
    
    df_final['PCA1'] = principal_components[:, 0]
    df_final['PCA2'] = principal_components[:, 1]
    df_final['PCA3'] = principal_components[:, 2] 

    print(f"PCA explained variance ratio (first 3 components): {pca.explained_variance_ratio_.sum():.2f}")
    
    # B. t-Distributed Stochastic Neighbor Embedding (t-SNE) for clustering visualization
    # FIX: Changed 'n_iter' to 'max_iter' for compatibility with newer scikit-learn versions
    tsne = TSNE(n_components=2, random_state=42, perplexity=30, max_iter=1000) 
    tsne_results = tsne.fit_transform(scaled_data)
    
    df_final['tSNE1'] = tsne_results[:, 0]
    df_final['tSNE2'] = tsne_results[:, 1]
    
    # 4. Save the final analysis-ready CSV
    df_final.to_csv(output_file, index=False)
    print(f"💾 Analysis-ready data saved to **{output_file}**")
    
    return df_final

# ----------------------------------------------------------------------
## PLOTTING FUNCTIONS
# ----------------------------------------------------------------------

def visualize_data(df_analysis):
    """
    Creates basic 2D plots for quick visualization of the dimension reduction results (PCA and t-SNE).
    """
    if df_analysis is None:
        return

    # Create a simplified sequence identifier (e.g., the base peptide name)
    df_analysis['BasePeptide'] = df_analysis['pepID'].apply(lambda x: x.split('_')[0])

    plt.figure(figsize=(15, 6))

    ## 2D Scatter Plot: PCA
    plt.subplot(1, 2, 1)
    # Visual cue: Use a scatter plot to show clustering of peptide scores in the reduced space
    sns.scatterplot(
        x='PCA1', y='PCA2', 
        hue='BasePeptide', 
        data=df_analysis, 
        legend='full',
        palette='tab10'
    )
    plt.title(f'PCA Plot (Scores Normalized)')
    plt.xlabel('Principal Component 1')
    plt.ylabel('Principal Component 2')
    
    ## 2D Scatter Plot: t-SNE
    plt.subplot(1, 2, 2)
    sns.scatterplot(
        x='tSNE1', y='tSNE2', 
        hue='BasePeptide', 
        data=df_analysis, 
        legend=False, 
        palette='tab10'
    )
    plt.title('t-SNE Plot (Clustering of Scores)')
    plt.xlabel('t-SNE Component 1')
    plt.ylabel('t-SNE Component 2')

    plt.tight_layout()
    plt.show()
    # 

def visualize_3d_pca(df_analysis):
    """
    Generates an interactive 3D scatter plot of the first three Principal Components.
    """
    if df_analysis is None:
        return
    
    print("\n🚀 Generating 3D PCA plot...")
    
    df_analysis['BasePeptide'] = df_analysis['pepID'].apply(lambda x: x.split('_')[0])
    
    fig = plt.figure(figsize=(10, 8))
    ax = fig.add_subplot(111, projection='3d')
    
    unique_peptides = df_analysis['BasePeptide'].unique()
    
    colors = plt.cm.get_cmap('tab10', len(unique_peptides))
    
    for i, peptide in enumerate(unique_peptides):
        subset = df_analysis[df_analysis['BasePeptide'] == peptide]
        # Visual cue: Use a 3D scatter plot to show score distribution
        ax.scatter(
            subset['PCA1'], subset['PCA2'], subset['PCA3'], 
            c=[colors(i)], label=peptide, s=50
        )

    ax.set_xlabel('Principal Component 1')
    ax.set_ylabel('Principal Component 2')
    ax.set_zlabel('Principal Component 3')
    ax.set_title('3D PCA of Peptide Scores')
    ax.legend(title='Base Peptide', loc='upper right', bbox_to_anchor=(1.25, 1))

    plt.show()
    # 

# ----------------------------------------------------------------------
## MAIN EXECUTION BLOCK
# ----------------------------------------------------------------------

if __name__ == "__main__":
    
    df_analysis = prepare_data_for_plotting(INPUT_CSV, OUTPUT_NUMERIC_CSV, IMPORTANT_SCORE_COLUMNS)
    
    if df_analysis is not None:
        visualize_data(df_analysis)
        visualize_3d_pca(df_analysis)
        print("\n✅ Analysis complete. The plots should now be displayed!")
