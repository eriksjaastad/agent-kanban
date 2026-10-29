// Code Graph Overlay — Memory renderer
// Renders the knowledge graph from brain.db: typed nodes + weighted edges.
// Uses d3-force for layout, Canvas 2D for rendering.

let overlayCanvas, overlayCtx;
let overlayLoaded = false;
let overlayData = { nodes: [], edges: [] };
let overlaySimulation = null;
let overlayTransform = { x: 0, y: 0, k: 1 };
let overlayHovered = null;
let overlaySelected = null;
let overlayPanning = false;
let overlayPanStart = { x: 0, y: 0 };
let overlayPanMoved = false;
let overlayRafId = null;
let overlayShowLabels = false;
let overlayUserInteracted = false;
let overlayAutoFitHandled = false;
let overlaySimTickCount = 0;
const OVERLAY_FIT_TICK_FALLBACK = 600;

// Server-side caps for /api/memory/knowledge-graph. The user can double these via the
// "Show more" control; the choice is persisted in localStorage.
const OVERLAY_DEFAULT_MAX_NODES = 1500;
const OVERLAY_DEFAULT_MAX_EDGES = 5000;
const OVERLAY_MAX_NODES_CAP = 20000;
const OVERLAY_MAX_EDGES_CAP = 300000;
const OVERLAY_EDGE_STALL_THRESHOLD = 50000;
let overlayMaxNodes = getOverlayMaxNodes();
let overlayMaxEdges = getOverlayMaxEdges();

function getOverlayMaxNodes() {
    try {
        const saved = parseInt(localStorage.getItem('overlay-max-nodes'), 10);
        if (saved >= 50 && saved <= OVERLAY_MAX_NODES_CAP) return saved;
    } catch (_) { /* localStorage unavailable */ }
    return OVERLAY_DEFAULT_MAX_NODES;
}

function getOverlayMaxEdges() {
    try {
        const saved = parseInt(localStorage.getItem('overlay-max-edges'), 10);
        if (saved >= 100 && saved <= OVERLAY_MAX_EDGES_CAP) return saved;
    } catch (_) { /* localStorage unavailable */ }
    return OVERLAY_DEFAULT_MAX_EDGES;
}

function saveOverlayLimits() {
    try {
        localStorage.setItem('overlay-max-nodes', String(overlayMaxNodes));
        localStorage.setItem('overlay-max-edges', String(overlayMaxEdges));
    } catch (_) { /* localStorage unavailable */ }
}

// Knowledge-graph node types (the memory store's `graph_nodes` table), a
// separate palette from the thought types in memory.js. Everything here is
// prefixed overlay*: an unprefixed nodeColor() once replaced memory.js's and
// grayed out the Graph view. The overlay doesn't write #memory-stats.
const NODE_COLORS = {
    project:  '#58a6ff',  // blue
    person:   '#f97583',  // pink-red
    artifact: '#3fb950',  // green
    concept:  '#d2a8ff',  // purple
    task:     '#f0883e',  // orange
    decision: '#56d364',  // bright green
    tag:      '#8b949e',  // gray
};

const NODE_LABELS = {
    project: 'Projects',
    person: 'People',
    artifact: 'Files',
    concept: 'Concepts',
    task: 'Tasks',
    decision: 'Decisions',
    tag: 'Tags',
};

function overlayNodeColor(type) {
    return NODE_COLORS[type] || '#8b949e';
}

function overlayNodeRadius(node) {
    if (node.is_cluster) {
        return Math.min(6 + Math.sqrt(node.cluster_count) * 1.5, 20);
    }
    const s = node.size || 1;
    if (s > 100) return 12;
    if (s > 20) return 8;
    if (s > 5) return 5;
    return 3;
}

function getOverlayMinMentions() {
    const slider = document.getElementById('overlay-min-mentions');
    if (!slider) return 1;
    // Restore from localStorage on first call
    const saved = localStorage.getItem('overlay-min-mentions');
    if (saved && !slider.dataset.restored) {
        slider.value = saved;
        slider.dataset.restored = '1';
        const valEl = document.getElementById('overlay-min-mentions-val');
        if (valEl) valEl.textContent = saved;
    }
    return parseInt(slider.value, 10);
}

async function loadOverlay(forceReload = false, clusterMode = 'auto') {
    overlayCanvas = document.getElementById('overlay-canvas');
    if (!overlayCanvas) return;

    overlayCtx = overlayCanvas.getContext('2d');

    const container = document.getElementById('overlay-container');
    overlayCanvas.width = container.clientWidth || window.innerWidth;
    overlayCanvas.height = container.clientHeight || (window.innerHeight - 120);

    const status = document.getElementById('overlay-status');
    if (status) status.style.display = 'none';

    if (overlayLoaded && overlayData.nodes.length > 0 && !forceReload) {
        renderOverlay();
        return;
    }

    // Each fresh (re)load gets one auto-fit once the simulation settles.
    // Pans/zooms after this point set overlayUserInteracted, which cancels it.
    // Any interaction from a previous load must not block the refit.
    overlayUserInteracted = false;
    overlayAutoFitHandled = false;
    overlaySimTickCount = 0;
    if (overlaySimulation) overlaySimulation.stop();

    // Set up min-mentions slider listener (once)
    const slider = document.getElementById('overlay-min-mentions');
    if (slider && !slider.dataset.bound) {
        slider.dataset.bound = '1';
        slider.addEventListener('input', e => {
            document.getElementById('overlay-min-mentions-val').textContent = e.target.value;
        });
        slider.addEventListener('change', () => {
            localStorage.setItem('overlay-min-mentions', slider.value);
            overlayLoaded = false;
            if (overlaySimulation) overlaySimulation.stop();
            loadOverlay(true);
        });
    }

    // Fetch knowledge graph with min_mentions filter and server-side caps
    const minMentions = getOverlayMinMentions();
    try {
        // /api/memory/knowledge-graph: the knowledge graph (graph_nodes + graph_edges) in the configured memory store.
        const resp = await fetch(`/api/memory/knowledge-graph?min_mentions=${minMentions}&cluster=${clusterMode}&max_nodes=${overlayMaxNodes}&max_edges=${overlayMaxEdges}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        overlayData = await resp.json();
    } catch (e) {
        if (status) {
            status.style.display = 'block';
            document.getElementById('overlay-waiting').textContent = `Error loading knowledge graph: ${e.message}`;
        }
        return;
    }

    if (!overlayData.nodes || overlayData.nodes.length === 0) {
        if (status) {
            status.style.display = 'block';
            document.getElementById('overlay-waiting').textContent = 'Knowledge graph is empty — see the build status above, or use the manual rebuild command.';
        }
        return;
    }

    // Build node lookup for edge resolution
    const nodeMap = new Map();
    overlayData.nodes.forEach(n => nodeMap.set(n.id, n));

    // Filter edges to only those with valid source/target
    overlayData.edges = overlayData.edges.filter(e => nodeMap.has(e.source) && nodeMap.has(e.target));

    // Update overlay stats and Show more control
    updateOverlayStats();
    updateOverlayShowMore();

    // Run d3 force layout
    const w = overlayCanvas.width;
    const h = overlayCanvas.height;

    overlaySimulation = d3.forceSimulation(overlayData.nodes)
        .force('link', d3.forceLink(overlayData.edges)
            .id(d => d.id)
            .distance(d => d.weight > 2 ? 30 : 60)
            .strength(d => Math.min(d.weight * 0.1, 0.5))
        )
        .force('charge', d3.forceManyBody()
            .strength(d => -overlayNodeRadius(d) * 8)
            .distanceMax(300)
        )
        .force('center', d3.forceCenter(w / 2, h / 2))
        .force('collision', d3.forceCollide(d => overlayNodeRadius(d) + 2))
        .alphaDecay(0.02)
        .on('tick', () => {
            overlaySimTickCount += 1;
            renderOverlay();
            // Fallback for simulations that never fully settle (e.g. reheated
            // by a drag): fit once after N ticks even without an end event.
            if (!overlayAutoFitHandled && overlaySimTickCount >= OVERLAY_FIT_TICK_FALLBACK) {
                overlaySimulationSettled();
            }
        })
        .on('end', overlaySimulationSettled);

    setupOverlayInteraction();

    // Labels toggle
    const labelsToggle = document.getElementById('overlay-labels-toggle');
    if (labelsToggle && !labelsToggle.dataset.bound) {
        labelsToggle.dataset.bound = '1';
        labelsToggle.addEventListener('change', () => {
            overlayShowLabels = labelsToggle.checked;
            renderOverlay();
        });
    }

    // Reset view button — same fit logic the auto-fit uses on load.
    const resetBtn = document.getElementById('overlay-reset-view');
    if (resetBtn && !resetBtn.dataset.bound) {
        resetBtn.dataset.bound = '1';
        resetBtn.addEventListener('click', overlayFitToScreen);
    }

    // Show more: double the server-side caps and reload
    const showMoreBtn = document.getElementById('overlay-show-more');
    if (showMoreBtn && !showMoreBtn.dataset.bound) {
        showMoreBtn.dataset.bound = '1';
        showMoreBtn.addEventListener('click', () => {
            overlayMaxNodes = Math.min(overlayMaxNodes * 2, OVERLAY_MAX_NODES_CAP);
            overlayMaxEdges = Math.min(overlayMaxEdges * 2, OVERLAY_MAX_EDGES_CAP);
            saveOverlayLimits();
            overlayLoaded = false;
            if (overlaySimulation) overlaySimulation.stop();
            loadOverlay(true);
        });
    }

    overlayLoaded = true;
}

function updateOverlayShowMore() {
    const btn = document.getElementById('overlay-show-more');
    const warning = document.getElementById('overlay-show-more-warning');
    if (!btn) return;

    const truncated = !!(overlayData.stats && overlayData.stats.truncated);
    const canGrow = overlayMaxNodes < OVERLAY_MAX_NODES_CAP || overlayMaxEdges < OVERLAY_MAX_EDGES_CAP;
    const showBtn = truncated && canGrow;
    btn.classList.toggle('hidden', !showBtn);

    if (warning) {
        const nextEdges = Math.min(overlayMaxEdges * 2, OVERLAY_MAX_EDGES_CAP);
        warning.classList.toggle('hidden', !(showBtn && nextEdges > OVERLAY_EDGE_STALL_THRESHOLD));
    }
}

async function loadMemoryBuildStatus() {
    const el = document.getElementById('memory-build-status-text');
    if (!el) return;
    try {
        const resp = await fetch('/api/memory/build-status');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        el.textContent = formatBuildStatus(await resp.json());
    } catch (_) {
        el.textContent = 'Graph build status unavailable';
    }
}

function formatBuildStatus(data) {
    if (!data || data.state === 'unknown') return 'Graph build status unknown';
    const when = data.started_at ? new Date(data.started_at) : null;
    const dateStr = when && !isNaN(when)
        ? when.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })
        : 'unknown date';
    let text = data.state === 'failed' ? `Last graph build failed ${dateStr}` : `Graph last built ${dateStr}`;
    if (data.started_at && data.finished_at) {
        const seconds = (new Date(data.finished_at) - new Date(data.started_at)) / 1000;
        if (seconds > 0) text += ` (took ${formatBuildDuration(seconds)})`;
    }
    if (data.schedule) text += ` · scheduled ${data.schedule}`;
    if (data.state === 'failed' && data.exit_status != null) text += ` (exit ${data.exit_status})`;
    return text;
}

function formatBuildDuration(totalSeconds) {
    const minutes = Math.round(totalSeconds / 60);
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    const mins = minutes % 60;
    return `${hours}h ${mins}m`;
}

function updateOverlayStats() {
    const stats = overlayData.stats || {};
    const typeCounts = {};
    overlayData.nodes.forEach(n => {
        typeCounts[n.type] = (typeCounts[n.type] || 0) + 1;
    });

    // Populate sidebar legend
    const legendEl = document.getElementById('overlay-legend');
    if (legendEl) {
        legendEl.innerHTML = '<h4>Legend</h4>' + Object.entries(NODE_COLORS)
            .filter(([type]) => typeCounts[type])
            .map(([type, color]) => `<div class="legend-item"><span class="legend-color" style="background:${color}"></span>${NODE_LABELS[type] || type} (${typeCounts[type]})</div>`)
            .join('');
    }

    // Populate sidebar stats
    const statsEl = document.getElementById('overlay-stats');
    if (statsEl) {
        const shownNodes = stats.nodes_shown ?? stats.total_nodes ?? overlayData.nodes.length;
        const shownEdges = stats.edges_shown ?? stats.total_edges ?? overlayData.edges.length;
        const availNodes = stats.total_nodes_available ?? shownNodes;
        const availEdges = stats.total_edges_available ?? shownEdges;
        let statsHtml;
        if (stats.truncated) {
            statsHtml = `Showing <strong>${shownNodes.toLocaleString()}</strong> of ${availNodes.toLocaleString()} nodes · <strong>${shownEdges.toLocaleString()}</strong> of ${availEdges.toLocaleString()} strongest connections`;
        } else {
            statsHtml = `<strong>${shownNodes.toLocaleString()}</strong> nodes · <strong>${shownEdges.toLocaleString()}</strong> edges`;
        }
        if (stats.clustered) {
            const clusterCount = overlayData.nodes.filter(n => n.is_cluster).length;
            statsHtml += ` · <em>${clusterCount} clusters</em>`;
        }
        statsEl.innerHTML = statsHtml;
    }
}

function overlaySimulationSettled() {
    if (overlayAutoFitHandled) return;
    overlayAutoFitHandled = true;
    // The user took control of the view while the simulation was still
    // running — leave their pan/zoom alone instead of snapping the graph.
    if (overlayUserInteracted) return;
    overlayFitToScreen();
}

// Fit the current node positions (plus their radii) into the canvas viewport.
// Shared by the auto-fit-on-settle path and the "Fit to Screen" button.
function overlayFitToScreen() {
    if (!overlayCanvas || !overlayData.nodes || overlayData.nodes.length === 0) return;
    const w = overlayCanvas.width;
    const h = overlayCanvas.height;
    if (!w || !h) return;

    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of overlayData.nodes) {
        if (n.x == null || n.y == null) continue;
        const r = overlayNodeRadius(n);
        if (n.x - r < minX) minX = n.x - r;
        if (n.y - r < minY) minY = n.y - r;
        if (n.x + r > maxX) maxX = n.x + r;
        if (n.y + r > maxY) maxY = n.y + r;
    }
    if (minX === Infinity) return;

    const boundsWidth = maxX - minX;
    const boundsHeight = maxY - minY;

    if (boundsWidth === 0 && boundsHeight === 0) {
        // Degenerate layout (e.g. a single node): center it at full scale.
        overlayTransform = { x: w / 2 - minX, y: h / 2 - minY, k: 1 };
        renderOverlay();
        return;
    }

    // Same padding convention as memory_svg.js svgFitToScreen: leave 15% of
    // the viewport as a margin so the graph doesn't touch the edges.
    const padding = 0.85;
    let scale = Math.min(w / boundsWidth, h / boundsHeight) * padding;
    // Keep the fitted scale within the same range the wheel zoom allows.
    scale = Math.max(0.1, Math.min(5, scale));
    if (!isFinite(scale) || scale <= 0) scale = 1;

    const midX = (minX + maxX) / 2;
    const midY = (minY + maxY) / 2;
    overlayTransform = { x: w / 2 - scale * midX, y: h / 2 - scale * midY, k: scale };
    renderOverlay();
}

function renderOverlay() {
    if (!overlayCtx) return;
    const w = overlayCanvas.width;
    const h = overlayCanvas.height;
    const ctx = overlayCtx;
    const t = overlayTransform;

    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = '#0d1117';
    ctx.fillRect(0, 0, w, h);

    ctx.save();
    ctx.translate(t.x, t.y);
    ctx.scale(t.k, t.k);

    // Build highlight set on hover
    let highlightedIds = null;
    if (overlayHovered) {
        highlightedIds = new Set([overlayHovered.id]);
        overlayData.edges.forEach(e => {
            const sid = e.source.id ?? e.source;
            const tid = e.target.id ?? e.target;
            if (sid === overlayHovered.id) highlightedIds.add(tid);
            if (tid === overlayHovered.id) highlightedIds.add(sid);
        });
    }

    // Draw edges
    overlayData.edges.forEach(e => {
        const src = e.source;
        const tgt = e.target;
        if (src.x == null || tgt.x == null) return;

        const sid = src.id ?? src;
        const tid = tgt.id ?? tgt;
        const isHighlighted = overlayHovered &&
            (sid === overlayHovered.id || tid === overlayHovered.id);
        const dimmed = highlightedIds && !isHighlighted;

        ctx.beginPath();
        ctx.moveTo(src.x, src.y);
        ctx.lineTo(tgt.x, tgt.y);

        if (isHighlighted) {
            ctx.strokeStyle = 'rgba(78, 205, 196, 0.85)';
            ctx.lineWidth = 2;
        } else {
            ctx.strokeStyle = 'rgba(160, 160, 160, 0.4)';
            ctx.lineWidth = Math.min(e.weight * 0.5, 2);
        }
        ctx.globalAlpha = dimmed ? 0.08 : 1;
        ctx.stroke();
        ctx.globalAlpha = 1;
    });

    // Draw nodes
    overlayData.nodes.forEach(n => {
        if (n.x == null) return;
        const r = overlayNodeRadius(n);
        const color = overlayNodeColor(n.type);
        const isDimmed = highlightedIds && !highlightedIds.has(n.id);
        const isHovered = n === overlayHovered;

        ctx.globalAlpha = isDimmed ? 0.15 : 1;

        ctx.beginPath();
        ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
        ctx.fillStyle = n.is_cluster ? 'rgba(0,0,0,0.6)' : color;
        ctx.fill();

        if (n.is_cluster) {
            ctx.setLineDash([3, 3]);
            ctx.strokeStyle = color;
            ctx.lineWidth = 1.5;
            ctx.stroke();
            ctx.setLineDash([]);
        } else if (isHovered || n === overlaySelected) {
            ctx.strokeStyle = n === overlaySelected ? '#007bff' : '#fff';
            ctx.lineWidth = 2;
            ctx.stroke();
        }

        // Draw label
        if (overlayShowLabels && !isDimmed && r >= 5) {
            ctx.font = '10px system-ui';
            ctx.fillStyle = '#ccc';
            ctx.textAlign = 'center';
            ctx.globalAlpha = isDimmed ? 0.15 : 0.9;
            ctx.fillText(n.name, n.x, n.y + r + 12);
        }

        ctx.globalAlpha = 1;
    });

    ctx.restore();

    // Draw tooltip on hover
    if (overlayHovered) {
        const sx = overlayHovered.x * t.k + t.x;
        const sy = overlayHovered.y * t.k + t.y;
        const label = overlayHovered.is_cluster
            ? `${overlayHovered.name} — click to expand`
            : `${overlayHovered.name} (${overlayHovered.type}, ${overlayHovered.size} refs)`;
        ctx.font = '12px system-ui';
        const tw = ctx.measureText(label).width;
        ctx.fillStyle = 'rgba(0,0,0,0.8)';
        ctx.fillRect(sx - tw/2 - 6, sy - 30, tw + 12, 20);
        ctx.fillStyle = '#eee';
        ctx.textAlign = 'center';
        ctx.fillText(label, sx, sy - 16);
    }
}

function setupOverlayInteraction() {
    if (!overlayCanvas) return;

    // Bind the pan/zoom/hover handlers exactly once per canvas element.
    // loadOverlay() calls this after every fetch, including "Show more"
    // reloads, so without this guard each reload would stack an identical
    // set of listeners and pan speed would double per reload. The handlers
    // close over the module-level overlay* variables, which are reassigned
    // in place on reload, so they keep working across reloads.
    if (overlayCanvas.dataset.interactionBound) return;
    overlayCanvas.dataset.interactionBound = '1';

    overlayCanvas.addEventListener('mousemove', e => {
        const rect = overlayCanvas.getBoundingClientRect();
        const mx = (e.clientX - rect.left - overlayTransform.x) / overlayTransform.k;
        const my = (e.clientY - rect.top - overlayTransform.y) / overlayTransform.k;

        if (overlayPanning) {
            const dx = e.clientX - overlayPanStart.x;
            const dy = e.clientY - overlayPanStart.y;
            if (Math.abs(dx) > 3 || Math.abs(dy) > 3) overlayPanMoved = true;
            overlayTransform.x += dx;
            overlayTransform.y += dy;
            overlayPanStart = { x: e.clientX, y: e.clientY };
            if (overlayPanMoved) overlayUserInteracted = true;
            renderOverlay();
            return;
        }

        let found = null;
        for (const n of overlayData.nodes) {
            if (n.x == null) continue;
            const dx = mx - n.x;
            const dy = my - n.y;
            if (dx * dx + dy * dy < (overlayNodeRadius(n) + 4) ** 2) {
                found = n;
                break;
            }
        }
        if (found !== overlayHovered) {
            overlayHovered = found;
            overlayCanvas.style.cursor = found ? 'pointer' : 'default';
            renderOverlay();
        }
    });

    overlayCanvas.addEventListener('mousedown', e => {
        overlayPanning = true;
        overlayPanStart = { x: e.clientX, y: e.clientY };
        overlayCanvas.style.cursor = 'grabbing';
        overlayPanMoved = false;
    });

    overlayCanvas.addEventListener('mouseup', e => {
        const wasPanning = overlayPanMoved;
        overlayPanning = false;
        overlayCanvas.style.cursor = overlayHovered ? 'pointer' : 'default';

        // Click handler — only fire if we didn't drag
        if (!wasPanning && overlayHovered) {
            if (overlayHovered.is_cluster) {
                // Expand cluster: reload with clustering off
                overlayLoaded = false;
                if (overlaySimulation) overlaySimulation.stop();
                loadOverlay(true, 'off');
                return;
            }
            overlaySelected = overlayHovered;
            showOverlayDetail(overlaySelected);
            renderOverlay();
        } else if (!wasPanning && !overlayHovered) {
            closeOverlayDetail();
        }
    });

    overlayCanvas.addEventListener('mouseleave', () => {
        overlayPanning = false;
        overlayHovered = null;
        renderOverlay();
    });

    overlayCanvas.addEventListener('wheel', e => {
        e.preventDefault();
        overlayUserInteracted = true;
        const rect = overlayCanvas.getBoundingClientRect();
        const mx = e.clientX - rect.left;
        const my = e.clientY - rect.top;
        const delta = e.deltaY > 0 ? 0.95 : 1.05;
        const newK = Math.max(0.1, Math.min(5, overlayTransform.k * delta));

        overlayTransform.x = mx - (mx - overlayTransform.x) * (newK / overlayTransform.k);
        overlayTransform.y = my - (my - overlayTransform.y) * (newK / overlayTransform.k);
        overlayTransform.k = newK;
        renderOverlay();
    }, { passive: false });
}

function showOverlayDetail(node) {
    const panel = document.getElementById('overlay-detail-panel');
    const nameEl = document.getElementById('overlay-detail-name');
    const contentEl = document.getElementById('overlay-detail-content');
    if (!panel || !node) return;

    // Find connected nodes
    const connections = [];
    overlayData.edges.forEach(e => {
        const sid = e.source.id ?? e.source;
        const tid = e.target.id ?? e.target;
        if (sid === node.id) {
            const target = overlayData.nodes.find(n => n.id === tid);
            if (target) connections.push({ node: target, edge_type: e.edge_type || e.type || 'related', weight: e.weight || 1 });
        } else if (tid === node.id) {
            const source = overlayData.nodes.find(n => n.id === sid);
            if (source) connections.push({ node: source, edge_type: e.edge_type || e.type || 'related', weight: e.weight || 1 });
        }
    });

    // Sort by weight descending
    connections.sort((a, b) => b.weight - a.weight);

    // Group connections by type
    const byType = {};
    connections.forEach(c => {
        const type = c.node.type || 'other';
        if (!byType[type]) byType[type] = [];
        byType[type].push(c);
    });

    nameEl.textContent = node.name;
    nameEl.style.color = overlayNodeColor(node.type);

    let html = `<div class="overlay-detail-meta">`;
    if (node.is_cluster) {
        html += `<strong>Cluster:</strong> ${node.cluster_count} ${NODE_LABELS[node.type] || node.type} nodes<br>`;
        html += `<strong>Click to expand</strong>`;
    } else {
        html += `<strong>Type:</strong> ${NODE_LABELS[node.type] || node.type}<br>`;
        html += `<strong>References:</strong> ${node.size || 0}<br>`;
        html += `<strong>Connections:</strong> ${connections.length}`;
    }
    html += `</div>`;

    html += `<div class="overlay-detail-connections">`;
    for (const [type, conns] of Object.entries(byType)) {
        const label = NODE_LABELS[type] || type;
        const color = overlayNodeColor(type);
        html += `<h4 style="color:${color}">● ${label} (${conns.length})</h4><ul>`;
        conns.slice(0, 20).forEach(c => {
            html += `<li data-node-id="${c.node.id}">${c.node.name}<span class="conn-type">${c.edge_type} ×${c.weight}</span></li>`;
        });
        if (conns.length > 20) html += `<li style="color:#666">...and ${conns.length - 20} more</li>`;
        html += `</ul>`;
    }
    html += `</div>`;

    contentEl.innerHTML = html;
    panel.classList.remove('hidden');

    // Click on connection to navigate to that node
    contentEl.querySelectorAll('li[data-node-id]').forEach(li => {
        li.addEventListener('click', () => {
            const targetNode = overlayData.nodes.find(n => n.id === li.dataset.nodeId);
            if (targetNode) {
                overlaySelected = targetNode;
                showOverlayDetail(targetNode);
                renderOverlay();
            }
        });
    });
}

function closeOverlayDetail() {
    const panel = document.getElementById('overlay-detail-panel');
    if (panel) panel.classList.add('hidden');
    overlaySelected = null;
    renderOverlay();
}

// Populate the header build-status line on page load (memory.html only).
loadMemoryBuildStatus();
