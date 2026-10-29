// Memory List View Logic

let listState = {
    query: '',
    thoughtType: '',
    limit: 50,
    offset: 0,
    total: 0
};

document.addEventListener('DOMContentLoaded', () => {
    // Only init if we are on the memory page
    if (!document.getElementById('memory-list-view')) return;

    // Event listeners
    document.getElementById('memory-search').addEventListener('input', debounce((e) => {
        listState.query = e.target.value;
        listState.offset = 0;
        loadThoughts();
    }, 300));

    document.getElementById('list-type-filter').addEventListener('change', (e) => {
        listState.thoughtType = e.target.value;
        listState.offset = 0;
        loadThoughts();
    });

    document.getElementById('prev-page').addEventListener('click', () => {
        if (listState.offset > 0) {
            listState.offset = Math.max(0, listState.offset - listState.limit);
            loadThoughts();
        }
    });

    document.getElementById('next-page').addEventListener('click', () => {
        if (listState.offset + listState.limit < listState.total) {
            listState.offset += listState.limit;
            loadThoughts();
        }
    });
});

async function loadThoughts() {
    const params = new URLSearchParams({
        query: listState.query,
        thought_type: listState.thoughtType,
        limit: listState.limit,
        offset: listState.offset
    });

    try {
        const response = await fetch(`/api/thoughts?${params}`);
        const data = await response.json();

        if (data.error) {
            console.error('Error loading thoughts:', data.error);
            return;
        }

        listState.total = data.total;
        renderTable(data.thoughts);
        updatePagination();
    } catch (err) {
        console.error('Failed to fetch thoughts:', err);
    }
}

function renderTable(thoughts) {
    const tbody = document.getElementById('thoughts-body');
    tbody.innerHTML = '';

    if (!thoughts || thoughts.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4" class="empty-state">No thoughts found matching your criteria.</td></tr>';
        return;
    }

    thoughts.forEach(t => {
        const tr = document.createElement('tr');
        tr.onclick = () => showInGraph(t.id);
        
        // Format date
        let dateStr = t.created_at;
        try {
            const date = new Date(t.created_at.replace(' ', 'T'));
            dateStr = date.toLocaleDateString();
        } catch(e) {}

        tr.innerHTML = `
            <td class="col-type"><span class="badge badge-${t.type}">${t.type}</span></td>
            <td class="col-content">${escapeHtml(t.content).substring(0, 150)}${t.content.length > 150 ? '...' : ''}</td>
            <td class="col-project">${escapeHtml(t.project || '')}</td>
            <td class="col-date">${dateStr}</td>
        `;
        tbody.appendChild(tr);
    });

    document.getElementById('list-stats').textContent = `Showing ${thoughts.length} of ${listState.total} thoughts`;
}

function updatePagination() {
    const page = Math.floor(listState.offset / listState.limit) + 1;
    const totalPages = Math.ceil(listState.total / listState.limit) || 1;
    
    document.getElementById('page-info').textContent = `Page ${page} of ${totalPages}`;
    document.getElementById('prev-page').disabled = listState.offset === 0;
    document.getElementById('next-page').disabled = listState.offset + listState.limit >= listState.total;
}

function switchView(view) {
    const graphWrapper = document.getElementById('memory-graph-wrapper');
    const graphView = document.getElementById('memory-viewport');
    const svgView = document.getElementById('memory-svg-view');
    const listView = document.getElementById('memory-list-view');
    const heatmapView = document.getElementById('memory-heatmap-view');
    const overlayView = document.getElementById('memory-overlay-view');
    const graphTab = document.getElementById('tab-graph');
    const svgTab = document.getElementById('tab-svg');
    const listTab = document.getElementById('tab-list');
    const heatmapTab = document.getElementById('tab-heatmap');
    const overlayTab = document.getElementById('tab-overlay');

    // Hide all views and deactivate all tabs
    [graphView, svgView, listView, heatmapView, overlayView].forEach(v => v && v.classList.add('hidden'));
    [graphTab, svgTab, listTab, heatmapTab, overlayTab].forEach(t => t && t.classList.remove('active'));

    if (view === 'graph') {
        graphWrapper && (graphWrapper.style.display = 'flex');
        graphView.classList.remove('hidden');
        graphTab && graphTab.classList.add('active');
    } else if (view === 'svg') {
        graphWrapper && (graphWrapper.style.display = 'flex');
        svgView.classList.remove('hidden');
        svgTab && svgTab.classList.add('active');
        if (typeof loadSvgGraph === 'function') loadSvgGraph();
    } else if (view === 'overlay') {
        graphWrapper && (graphWrapper.style.display = 'none');
        overlayView.classList.remove('hidden');
        overlayTab && overlayTab.classList.add('active');
        if (typeof loadOverlay === 'function') loadOverlay();
    } else if (view === 'heatmap') {
        graphWrapper && (graphWrapper.style.display = 'none');
        heatmapView.classList.remove('hidden');
        heatmapTab && heatmapTab.classList.add('active');
        if (typeof loadHeatmap === 'function') loadHeatmap();
    } else {
        graphWrapper && (graphWrapper.style.display = 'none');
        listView.classList.remove('hidden');
        listTab && listTab.classList.add('active');
        loadThoughts();
    }
}

function refreshData() {
    const svgView = document.getElementById('memory-svg-view');
    if (svgView && !svgView.classList.contains('hidden')) {
        if (typeof refreshSvgGraph === 'function') refreshSvgGraph();
    } else if (!document.getElementById('memory-viewport').classList.contains('hidden')) {
        if (typeof refreshMemoryGraph === 'function') refreshMemoryGraph();
    } else {
        loadThoughts();
    }
}

function showInGraph(id) {
    switchView('graph');
    // We need to wait a moment for the graph to render/be visible
    setTimeout(() => {
        const node = memoryData.nodes.find(n => n.id === String(id));
        if (node) {
            showDetails(node);
            highlightNode(node, true);
        }
    }, 500);
}

function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}
