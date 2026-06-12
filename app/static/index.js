// static/index.js
import { processAllData, renderComparisonPlots, renderAdvancedPlots } from './plots_utils.js';

window.currentJobId = null;
window.statusInterval = null;
window.stage = null; 
window.stageTab1 = null; 

document.addEventListener('DOMContentLoaded', async () => {
    
    // Expanded to 6 buttons and views
    const tabButtons = [
        document.getElementById('tab1Button'),
        document.getElementById('tab2Button'),
        document.getElementById('tab4Button'),
        document.getElementById('tab5Button'),
        document.getElementById('tab7Button')
    ];
    const tabViews = [
        document.getElementById('tab1-view'),
        document.getElementById('tab2-view'),
        document.getElementById('tab4-view'),
        document.getElementById('tab5-view'),
        document.getElementById('tab7-view')
    ];

    window.switchTab = (targetIndex) => {
        if (window.stopJobsPolling) window.stopJobsPolling();
        tabViews.forEach((view, index) => {
            if (index === targetIndex) {
                tabButtons[index].classList.add('active');
                view.classList.remove('hidden');
                
                if (index === 0 && window.stageTab1) {
                    setTimeout(() => window.stageTab1.handleResize(), 50);
                }
                if (index === 1) {
                    if (typeof window.initializeNGLViewer === 'function') {
                        window.initializeNGLViewer();
                        if (window.stage) setTimeout(() => window.stage.handleResize(), 50);
                    }
                }
                if (index > 1) {
                    window.handlePlotRendering(index);
                }
                if (index === 4 && window.startJobsPolling) window.startJobsPolling();
            } else {
                tabButtons[index].classList.remove('active');
                view.classList.add('hidden');
            }
        });
    };

    tabButtons.forEach((button, index) => {
        button.addEventListener('click', () => {
            if (button && !button.disabled) window.switchTab(index);
        });
    });

    window.switchTab(0);
    
    const tryInitTab1 = () => {
        if (typeof window.initializeNGLTab1Viewer === 'function') {
            window.initializeNGLTab1Viewer();
            console.log("Tab 1 NGL Viewer Initialized.");
        } else {
            setTimeout(tryInitTab1, 100);
        }
    };
    tryInitTab1();
    
    window.handlePlotRendering = (tabIndex) => {
        if (!window.globalData || !window.globalData.foldx) return;

        switch (tabIndex) {
            case 2:
                renderComparisonPlots();
                break;
            case 3:
                renderAdvancedPlots();
                break;
        }
    };
    
});

async function fetchAndLoadResults(jobId) {
    try {
        const response = await fetch(`/results_data/${jobId}`);
        const rawData = await response.json();
        if (rawData.success) {
            processAllData(jobId, rawData);
            // Enable all 6 buttons (2-6)
            ['tab2Button', 'tab4Button', 'tab5Button'].forEach(id => {
                const b = document.getElementById(id);
                if (b) b.disabled = false;
            });
            window.switchTab(1);
        }
    } catch (error) { console.error('Results load error:', error); }
}

window.pollStatus = async (jobId) => {
    try {
        const response = await fetch(`/status/${jobId}`);
        const data = await response.json();
        document.getElementById('message').textContent = data.message || `Status: ${data.status}`;
        
        if (data.status === 'Complete') {
            clearInterval(window.statusInterval);
            document.getElementById('loading').style.display = 'none';
            
            // --- ADDED THIS SECTION / DIESEN ABSCHNITT HINZUGEFUEGT ---
            if (data.download_url) {
                const dlContainer = document.getElementById('downloadLink');
                dlContainer.innerHTML = `
                    <div class="alert alert-success mt-3">
                        <strong>Job Complete!</strong> 
                        <a href="${data.download_url}" class="btn btn-primary btn-sm ml-2" download>
                            Download Results (.zip)
                        </a>
                    </div>`;
            }
            // ---------------------------------------------------------

            await fetchAndLoadResults(jobId);
        }
    } catch (e) { console.error(e); }
};
