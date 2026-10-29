// D3.js Project Graph Visualization

let width, height;
let svg, container, simulation;
let graphData = { nodes: [], edges: [] };
let filteredNodes = [], filteredEdges = [];
let projectColors = {};

const COLORS = {
    python: '#3572A5',
    typescript: '#2b7489',
    javascript: '#f1e05a',
    markdown: '#083fa1',
    go: '#00ADD8',
    swift: '#F05138',
    config: '#6e6e6e',
    other: '#999999'
};

document.addEventListener('DOMContentLoaded', () => {
    const svgElement = document.getElementById('graph-svg');
    width = svgElement.clientWidth || window.innerWidth - 560; // Approximate if not yet rendered
    height = svgElement.clientHeight || window.innerHeight - 80;
    console.log("Viewport dimensions:", { width, height });

    svg = d3.select('#graph-svg');
    
    console.log("SVG initialized:", svg.node());

    container = svg.append('g');

    // Setup zoom
    const zoom = d3.zoom()
        .scaleExtent([0.01, 20])
        .on('zoom', (event) => {
            container.attr('transform', event.transform);
        });

    svg.call(zoom);

    // Initial load
    loadGraph();

    // Resize listener
    window.addEventListener('resize', () => {
        width = svgElement.clientWidth;
        height = svgElement.clientHeight;
        if (simulation) {
            simulation.force('center', d3.forceCenter(width / 2, height / 2));
            simulation.force('x', d3.forceX(width / 2).strength(0.2));
            simulation.force('y', d3.forceY(height / 2).strength(0.2));
            simulation.alpha(0.3).restart();
        }
    });

    // Event listeners
    document.getElementById('project-filter').addEventListener('change', applyFilters);
    document.getElementById('orphan-toggle').addEventListener('change', applyFilters);
    document.getElementById('names-toggle').addEventListener('change', () => {
        const showHubNames = document.getElementById('names-toggle').checked;
        container.selectAll('.node text').style('display', d => (showHubNames && d.size > 20) ? 'block' : 'none');
    });
    document.getElementById('min-connections').addEventListener('input', (e) => {
        document.getElementById('min-connections-val').textContent = e.target.value;
        applyFilters();
    });
    document.getElementById('reset-view').addEventListener('click', () => {
        fitToScreen();
    });
});

async function loadGraph() {
    console.log("Loading graph data...");
    showLoading(true);
    try {
        const response = await fetch('/api/graph');
        graphData = await response.json();
        console.log("Graph data received:", {
            nodes: graphData.nodes.length,
            edges: graphData.edges.length,
            stats: graphData.stats
        });
        
        if (graphData.nodes.length === 0) {
            console.warn("No nodes found in graph data!");
        }

        const PROJECTS = [...new Set(graphData.nodes.map(n => n.project))].sort();
        
        // Create project colors mapping
        projectColors = {};
        const projectPalette = [
            '#FF6B6B', '#4ECDC4', '#45B7D1', '#96CEB4', '#FFEEAD', 
            '#D4A5A5', '#9B59B6', '#3498DB', '#E67E22', '#2ECC71',
            '#F1C40F', '#E74C3C', '#1ABC9C', '#34495E', '#95A5A6'
        ];
        PROJECTS.forEach((p, i) => {
            projectColors[p] = projectPalette[i % projectPalette.length];
        });

        const select = document.getElementById('project-filter');
        PROJECTS.forEach(p => {
            const opt = document.createElement('option');
            opt.value = p;
            opt.textContent = p;
            select.appendChild(opt);
        });

        // Populate type filters
        const types = [...new Set(graphData.nodes.map(n => n.type))].sort();
        const typeContainer = document.getElementById('type-filters');
        types.forEach(t => {
            const label = document.createElement('label');
            label.innerHTML = `<input type="checkbox" class="type-filter" value="${t}" checked> ${t}`;
            label.querySelector('input').addEventListener('change', applyFilters);
            typeContainer.appendChild(label);
        });

        applyFilters();
    } catch (err) {
        console.error('Failed to load graph:', err);
        alert('Failed to load graph data. Check if graph.json exists.');
    }
    showLoading(false);
}

function applyFilters() {
    console.log("Applying filters...");
    const selectedProject = document.getElementById('project-filter').value;
    const showOrphans = document.getElementById('orphan-toggle').checked;
    const minConnections = parseInt(document.getElementById('min-connections').value);
    const selectedTypes = Array.from(document.querySelectorAll('.type-filter:checked')).map(cb => cb.value);

    console.log("Filter criteria:", {
        selectedProject,
        showOrphans,
        minConnections,
        selectedTypes
    });

    // Filter nodes
    filteredNodes = graphData.nodes.filter(n => {
        if (selectedProject && n.project !== selectedProject) return false;
        if (!showOrphans && n.is_orphan) return false;
        if (n.size < minConnections) return false;
        if (!selectedTypes.includes(n.type)) return false;
        return true;
    });

    const nodeIds = new Set(filteredNodes.map(n => n.id));

    // Filter edges
    filteredEdges = graphData.edges.filter(e => {
        return nodeIds.has(e.source) && nodeIds.has(e.target);
    });

    console.log("Filtered data:", {
        nodes: filteredNodes.length,
        edges: filteredEdges.length
    });

    updateStats();
    renderGraph();
}

function updateStats() {
    const statsDiv = document.getElementById('graph-stats');
    statsDiv.innerHTML = `
        Nodes: ${filteredNodes.length} / ${graphData.nodes.length}<br>
        Edges: ${filteredEdges.length} / ${graphData.edges.length}
    `;

    // Update legend
    const legendItems = document.getElementById('legend-items');
    legendItems.innerHTML = '';
    const visibleProjects = [...new Set(filteredNodes.map(n => n.project))].sort();
    visibleProjects.forEach(p => {
        const item = document.createElement('div');
        item.className = 'legend-item';
        item.innerHTML = `
            <span class="legend-color" style="background: ${projectColors[p] || '#999'}"></span>
            <span class="legend-label">${p}</span>
        `;
        legendItems.appendChild(item);
    });
}

function renderGraph() {
    try {
        const svgElement = document.getElementById('graph-svg');
        width = svgElement.clientWidth;
        height = svgElement.clientHeight;
        
        console.log("Starting renderGraph with", filteredNodes.length, "nodes and", filteredEdges.length, "edges. Viewport:", width, "x", height);
        // Clear previous
        container.selectAll('*').remove();

        if (simulation) {
            console.log("Stopping previous simulation");
            simulation.stop();
        }

        if (filteredNodes.length === 0) {
            console.warn("No nodes to render!");
            return;
        }

        // Create deep copies of nodes and edges for D3 simulation
        // D3 modifies the objects passed to it (adds x, y, vx, vy)
        const d3Nodes = filteredNodes.map(d => ({...d}));
        const d3Edges = filteredEdges.map(d => ({...d}));

        simulation = d3.forceSimulation(d3Nodes)
            .force('link', d3.forceLink(d3Edges).id(d => d.id).distance(100))
            .force('charge', d3.forceManyBody().strength(-100)) // Stronger repulsion for clarity
            .force('center', d3.forceCenter(width / 2, height / 2))
            .force('x', d3.forceX(width / 2).strength(0.2)) // 2x stronger center pull
            .force('y', d3.forceY(height / 2).strength(0.2)) // 2x stronger center pull
            .force('collision', d3.forceCollide().radius(d => getRadius(d) + 2));

        console.log("Simulation initialized");

        // Set initial positions to center to prevent explosion
        d3Nodes.forEach(n => {
            n.x = width / 2 + (Math.random() - 0.5) * 100;
            n.y = height / 2 + (Math.random() - 0.5) * 100;
        });

        // Links
        const link = container.append('g')
            .selectAll('path')
            .data(d3Edges)
            .enter().append('path')
            .attr('class', d => `link link-${d.type}`)
            .attr('stroke', '#ccc') // Lighter color for better visibility against dark background
            .attr('stroke-width', 1.5) // Slightly thicker lines
            .attr('stroke-opacity', 0.8); // More opaque

        // Nodes
        const node = container.append('g')
            .selectAll('.node')
            .data(d3Nodes)
            .enter().append('g')
            .attr('class', 'node')
            .call(d3.drag()
                .on('start', dragstarted)
                .on('drag', dragged)
                .on('end', dragended))
            .on('click', (event, d) => showDetails(d))
            .on('mouseover', (event, d) => highlightNode(d, true))
            .on('mouseout', (event, d) => highlightNode(d, false));

        node.append('circle')
            .attr('r', d => getRadius(d))
            .attr('fill', d => projectColors[d.project] || COLORS.other);

        node.append('text')
            .attr('dx', d => getRadius(d) + 2)
            .attr('dy', '.35em')
            .text(d => d.name)
            .style('display', d => {
                const showHubNames = document.getElementById('names-toggle').checked;
                // Only show if toggle is on and node is a major hub (size > 20)
                return (showHubNames && d.size > 20) ? 'block' : 'none';
            });

        simulation.on('tick', () => {
            link.attr('d', d => {
                const dx = d.target.x - d.source.x,
                    dy = d.target.y - d.source.y,
                    dr = Math.sqrt(dx * dx + dy * dy);
                return `M${d.source.x},${d.source.y}A${dr},${dr} 0 0,1 ${d.target.x},${d.target.y}`;
            });

            node.attr('transform', d => `translate(${d.x},${d.y})`);
        });

        // Auto-fit after simulation stabilizes
        simulation.on('end', () => {
            console.log("Simulation stabilized, fitting to screen...");
            fitToScreen();
        });

        console.log("Render logic completed");
    } catch (err) {
        console.error("Error in renderGraph:", err);
    }
}

function fitToScreen() {
    if (filteredNodes.length === 0) return;

    const bounds = container.node().getBBox();
    const parent = svg.node();
    const fullWidth = parent.clientWidth;
    const fullHeight = parent.clientHeight;
    const width = bounds.width;
    const height = bounds.height;
    const midX = bounds.x + width / 2;
    const midY = bounds.y + height / 2;

    if (width === 0 || height === 0) return;

    const scale = 0.8 / Math.max(width / fullWidth, height / fullHeight);
    const translate = [fullWidth / 2 - scale * midX, fullHeight / 2 - scale * midY];

    svg.transition().duration(750).call(
        d3.zoom().transform,
        d3.zoomIdentity.translate(translate[0], translate[1]).scale(scale)
    );
}

function getRadius(d) {
    return 5 + Math.sqrt(d.size || 0) * 2;
}

function showDetails(node) {
    console.log("Showing details for:", node.name);
    const panel = document.getElementById('node-details');
    const placeholder = document.getElementById('details-placeholder');
    const info = document.getElementById('details-info');
    const actions = document.getElementById('details-actions');

    panel.classList.remove('hidden');
    if (placeholder) placeholder.style.display = 'none';
    if (info) info.classList.remove('hidden');
    if (actions) actions.classList.remove('hidden');

    document.getElementById('details-name').textContent = node.name;
    document.getElementById('details-path').textContent = node.path;
    document.getElementById('details-project').textContent = node.project;
    document.getElementById('details-type').textContent = node.type;
    document.getElementById('details-connections').textContent = node.size;

    // Show outgoing links
    const linksList = document.getElementById('links-list');
    linksList.innerHTML = '';
    const outgoing = graphData.edges.filter(e => e.source === node.id || e.source.id === node.id);
    outgoing.forEach(e => {
        const li = document.createElement('li');
        const targetId = typeof e.target === 'object' ? e.target.id : e.target;
        li.textContent = `→ ${targetId} (${e.type})`;
        linksList.appendChild(li);
    });

    // Update view button
    const viewBtn = document.getElementById('view-file-btn');
    if (node.type === 'markdown') {
        // Find if it belongs to a project we can link to
        const projectId = node.project.toLowerCase().replace(/ /g, '-');
        viewBtn.href = `/todo/${projectId}`; // Defaulting to TODO viewer for markdown
        viewBtn.style.display = 'inline-block';
    } else {
        viewBtn.style.display = 'none';
    }
}

function closeDetails() {
    document.getElementById('node-details').classList.add('hidden');
}

function highlightNode(d, active) {
    const nodeSelection = container.selectAll('.node');
    const linkSelection = container.selectAll('.link');

    if (active) {
        nodeSelection.style('opacity', n => (n === d || isConnected(d, n)) ? 1 : 0.1);
        linkSelection.style('opacity', l => (l.source === d || l.target === d || l.source.id === d.id || l.target.id === d.id) ? 1 : 0.05);
        // Only show names for the highlighted cluster
        nodeSelection.select('text').style('display', n => (n === d || isConnected(d, n)) ? 'block' : 'none');
    } else {
        const showHubNames = document.getElementById('names-toggle').checked;
        nodeSelection.style('opacity', 1);
        linkSelection.style('opacity', 0.8);
        // Return to hub-only names if toggle is on, or none if off
        nodeSelection.select('text').style('display', n => (showHubNames && n.size > 20) ? 'block' : 'none');
    }
}

function isConnected(a, b) {
    return filteredEdges.some(e => 
        (e.source === a && e.target === b) || 
        (e.source === b && e.target === a) ||
        (e.source.id === a.id && e.target.id === b.id) ||
        (e.source.id === b.id && e.target.id === a.id)
    );
}

function dragstarted(event, d) {
    if (!event.active) simulation.alphaTarget(0.3).restart();
    d.fx = d.x;
    d.fy = d.y;
}

function dragged(event, d) {
    d.fx = event.x;
    d.fy = event.y;
}

function dragended(event, d) {
    if (!event.active) simulation.alphaTarget(0);
    d.fx = null;
    d.fy = null;
}

function showLoading(show) {
    document.getElementById('loading-overlay').style.display = show ? 'flex' : 'none';
}

async function rebuildGraph() {
    const btn = document.querySelector('.btn-refresh');
    const originalText = btn ? btn.textContent : '';
    if (btn) {
        btn.disabled = true;
        btn.textContent = '⏳ Rebuilding...';
    }
    showLoading(true);
    ptToast('Rebuild started…', 'info');
    try {
        const response = await fetch('/api/refresh', { method: 'POST' });
        const data = await response.json();
        if (!response.ok || !data.job_id) {
            throw new Error(data.message || `HTTP ${response.status}`);
        }
        const job = await ptPollRebuild(data.job_id);
        if (job.status !== 'done') {
            throw new Error(job.error || 'rebuild failed');
        }
        await loadGraph();
        ptToast('Graph rebuilt ✓', 'success');
    } catch (err) {
        console.error('Rebuild failed:', err);
        ptToast(`Rebuild failed: ${err.message}`, 'error');
    } finally {
        showLoading(false);
        if (btn) {
            btn.disabled = false;
            btn.textContent = originalText;
        }
    }
}
