// Memory Graph — Canvas Renderer
// Uses d3-force for physics, Canvas 2D for rendering (handles 2000+ nodes at 60fps).
// All filtering is client-side from a single full-data fetch.
// Shares the page's global scope with the other memory_*.js renderers (see
// memory.html): this file owns the unprefixed names (nodeColor, nodeRadius,
// drawFrame), and every other renderer must prefix its own. On 2026-04-05 the
// overlay's nodeColor() replaced this one and every node rendered gray.

// ─── State ───────────────────────────────────────────────────────────────────

let canvas, ctx, simulation;
let width, height;

// Full dataset fetched once from /api/memory-graph
let memoryData = { nodes: [], edges: [] };

// Filtered view (updated in-place on filter change, no re-fetch)
let visibleNodes = [];
let visibleEdges = [];

// Current filter settings
let currentFilters = {
    thoughtType: '',
    machine: '',
    minSimilarity: 0.3,
    maxEdges: 10
};

// Zoom / pan transform
let transform = { x: 0, y: 0, k: 1 };
// Target transform for animated fit-to-screen
let fitTarget = null;
let fitFrame = 0;
const FIT_FRAMES = 30;

// Interaction state
let hoveredNode = null;
let selectedNode = null;
let isPanning = false;
let panStart = { x: 0, y: 0 };

// rAF handle
let rafId = null;

// ─── Color map ───────────────────────────────────────────────────────────────
// Thought types from the memory store's `thoughts` table. Keep in step with
// SVG_TYPE_COLORS in memory_svg.js: a type in neither map renders gray.

const TYPE_COLORS = {
    observation:      '#4ECDC4',   // teal
    decision:         '#FF6B6B',   // coral red
    idea:             '#FFD93D',   // yellow
    question:         '#A8E6CF',   // mint green
    conversation:     '#B39DDB',   // lavender purple
    meeting_debrief:  '#F48FB1',   // pink
    fact:             '#80CBC4',   // soft teal
    project:          '#FFB74D',   // orange
    insight:          '#AED581',   // lime green
    error:            '#EF9A9A',   // light red
    person:           '#F97583',   // salmon
    person_note:      '#F97583',   // salmon (same as person)
    tool:             '#56D364',   // bright green
    preference:       '#79B8FF',   // light blue
    loop_state:       '#CE93D8',   // medium purple
    dream:            '#E1BEE7',   // light purple
    default:          '#8b949e'    // gray
};

function nodeColor(type) {
    return TYPE_COLORS[type] || TYPE_COLORS.default;
}

// ─── Machine color map (ring around nodes) ──────────────────────────────────

const MACHINE_COLORS = {
    'openclaw':  '#FF9800',    // OpenClaw (Mac Mini) — orange
    'macbook':   '#42A5F5',    // MacBook — blue
};

function machineColor(machine) {
    if (!machine) return null;
    const lower = machine.toLowerCase();
    if (lower.includes('mac-mini') || lower.includes('openclaw') || lower.includes('mini')) return MACHINE_COLORS['openclaw'];
    if (lower.includes('macbook')) return MACHINE_COLORS['macbook'];
    return '#666666';  // Unknown machine — gray
}

// ─── Node sizing ─────────────────────────────────────────────────────────────

function nodeRadius(node) {
    // Original SVG formula — linear, uncapped. Hubs grow big.
    const raw = node.rawSize || 0;
    return Math.max(20, 10 + raw * 2);
}

// ─── Init ────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    canvas = document.getElementById('memory-canvas');
    ctx = canvas.getContext('2d');

    resizeCanvas();
    window.addEventListener('resize', () => {
        resizeCanvas();
        if (simulation) {
            simulation.force('center', d3.forceCenter(width / 2, height / 2));
        }
    });

    // Controls
    document.getElementById('type-filter').addEventListener('change', applyFilters);
    document.getElementById('machine-filter').addEventListener('change', applyFilters);
    document.getElementById('min-similarity').addEventListener('input', e => {
        const val = parseInt(e.target.value) / 100;
        document.getElementById('min-similarity-val').textContent = val.toFixed(2);
        currentFilters.minSimilarity = val;
        applyFilters();
    });
    document.getElementById('max-edges').addEventListener('input', e => {
        document.getElementById('max-edges-val').textContent = e.target.value;
        currentFilters.maxEdges = parseInt(e.target.value);
        applyFilters();
    });
    document.getElementById('labels-toggle').addEventListener('change', () => {
        if (rafId === null) startLoop();
    });
    document.getElementById('reset-view').addEventListener('click', () => {
        const svgView = document.getElementById('memory-svg-view');
        if (svgView && !svgView.classList.contains('hidden') && typeof svgFitToScreen === 'function') {
            svgFitToScreen();
        } else {
            fitToScreen();
        }
    });

    // Canvas mouse events
    canvas.addEventListener('mousemove', onMouseMove);
    canvas.addEventListener('mousedown', onMouseDown);
    canvas.addEventListener('mouseup', onMouseUp);
    canvas.addEventListener('mouseleave', onMouseLeave);
    canvas.addEventListener('wheel', onWheel, { passive: false });
    canvas.addEventListener('click', onClick);

    // Load everything
    loadGraph();
});

// Scales the canvas buffer by devicePixelRatio; without it, retina screens
// (DPR 2) render blurry and washed out. drawFrame applies the same scale.
function resizeCanvas() {
    const dpr = window.devicePixelRatio || 1;
    width = canvas.clientWidth;
    height = canvas.clientHeight;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    // Store DPR so drawFrame can use it
    canvas._dpr = dpr;
}

// ─── Data loading ─────────────────────────────────────────────────────────────

async function loadGraph() {
    showLoading(true);
    try {
        const t0 = performance.now();

        // /api/memory-graph (thoughts + embedding similarity) and /api/memory/types
        // (distinct thought types for the legend), both from the configured memory store.
        const [graphRes, typesRes] = await Promise.all([
            fetch('/api/memory-graph'),
            fetch('/api/memory/types')
        ]);

        memoryData = await graphRes.json();
        const typesData = await typesRes.json();

        // 404/503 bodies carry `error`; a 500 (such as a broken local config) carries `detail`.
        const loadError = memoryData.error || memoryData.detail;
        if (loadError) {
            alert(`Error: ${loadError}`);
            showLoading(false);
            return;
        }

        // Preserve the backend-computed raw degree on each node so filters
        // can overwrite node.size for display without losing the true connectivity.
        memoryData.nodes.forEach(n => { n.rawSize = n.size || 0; });

        // Populate filter dropdowns dynamically
        populateTypeDropdowns(typesData.types || []);
        populateMachineDropdown();
        // Build dynamic legend
        buildLegend(typesData.types || []);

        // Apply current filters to get visible subset
        applyFiltersInternal();
        updateStats();
        buildSimulation();

        console.log(`Graph loaded: ${memoryData.nodes.length} nodes, ${memoryData.edges.length} edges in ${(performance.now() - t0).toFixed(0)}ms`);
    } catch (err) {
        console.error('Error loading memory graph:', err);
        alert('Failed to load memory graph');
    }
    showLoading(false);
}

function populateTypeDropdowns(types) {
    ['type-filter', 'list-type-filter'].forEach(id => {
        const sel = document.getElementById(id);
        if (!sel) return;
        // Remove all except the first "All Types" option
        while (sel.options.length > 1) sel.remove(1);
        types.forEach(t => {
            const opt = document.createElement('option');
            opt.value = t;
            opt.textContent = t.charAt(0).toUpperCase() + t.slice(1);
            sel.appendChild(opt);
        });
    });
}

function populateMachineDropdown() {
    const sel = document.getElementById('machine-filter');
    if (!sel) return;
    while (sel.options.length > 1) sel.remove(1);
    // Count nodes per machine for the label
    const macbook = memoryData.nodes.filter(n => (n.source_machine || '').toLowerCase().includes('macbook')).length;
    const mini = memoryData.nodes.filter(n => {
        const m = (n.source_machine || '').toLowerCase();
        return m.includes('mac-mini') || m.includes('mini');
    }).length;
    if (macbook > 0) {
        const opt = document.createElement('option');
        opt.value = 'macbook';
        opt.textContent = `MacBook (${macbook})`;
        sel.appendChild(opt);
    }
    if (mini > 0) {
        const opt = document.createElement('option');
        opt.value = 'openclaw';
        opt.textContent = `OpenClaw (${mini})`;
        sel.appendChild(opt);
    }
}

function buildLegend(types) {
    const legend = document.getElementById('memory-legend');
    legend.innerHTML = '<h4>Type</h4>';
    types.forEach(t => {
        const color = nodeColor(t);
        const item = document.createElement('div');
        item.className = 'legend-item';
        item.innerHTML = `<span class="legend-color" style="background:${color}"></span><span>${t.charAt(0).toUpperCase() + t.slice(1)}</span>`;
        legend.appendChild(item);
    });

    // Machine legend
    const machineHeader = document.createElement('h4');
    machineHeader.textContent = 'Machine';
    machineHeader.style.marginTop = '12px';
    legend.appendChild(machineHeader);

    [
        { name: 'OpenClaw', color: MACHINE_COLORS['openclaw'] },
        { name: 'MacBook', color: MACHINE_COLORS['macbook'] },
    ].forEach(m => {
        const item = document.createElement('div');
        item.className = 'legend-item';
        item.innerHTML = `<span class="legend-color" style="background:transparent;border:3px solid ${m.color};box-sizing:border-box"></span><span>${m.name}</span>`;
        legend.appendChild(item);
    });
}

// ─── Filtering (client-side, instant) ─────────────────────────────────────────

// Track recomputing state for visual feedback
let isRecomputing = false;

function applyFilters() {
    currentFilters.thoughtType = document.getElementById('type-filter').value;
    currentFilters.machine = document.getElementById('machine-filter').value;

    // If SVG view is active, apply SVG filters instead
    const svgView = document.getElementById('memory-svg-view');
    if (svgView && !svgView.classList.contains('hidden')) {
        if (typeof applySvgFilters === 'function') applySvgFilters();
        return;
    }

    // Dim canvas to signal recomputation
    isRecomputing = true;
    canvas.style.opacity = '0.35';
    canvas.style.transition = 'opacity 0.15s';

    // Defer to next frame so the dim renders before JS blocks
    requestAnimationFrame(() => {
        applyFiltersInternal();
        updateStats();
        buildSimulation();

        // Handle empty results — restore opacity immediately
        if (visibleNodes.length === 0) {
            canvas.style.opacity = '1';
            isRecomputing = false;
            ctx.clearRect(0, 0, width, height);
            return;
        }

        // Restore opacity on first simulation tick
        simulation.on('tick.filterFeedback', () => {
            canvas.style.opacity = '1';
            isRecomputing = false;
            simulation.on('tick.filterFeedback', null);
        });

        // Safety timeout in case tick never fires
        setTimeout(() => {
            if (isRecomputing) {
                canvas.style.opacity = '1';
                isRecomputing = false;
            }
        }, 300);
    });
}

function applyFiltersInternal() {
    // Type + machine filter
    let filteredNodes = memoryData.nodes;
    if (currentFilters.thoughtType) {
        filteredNodes = filteredNodes.filter(n => n.type === currentFilters.thoughtType);
    }
    if (currentFilters.machine) {
        filteredNodes = filteredNodes.filter(n => {
            const lower = (n.source_machine || '').toLowerCase();
            if (currentFilters.machine === 'macbook') return lower.includes('macbook');
            if (currentFilters.machine === 'openclaw') return lower.includes('mac-mini') || lower.includes('mini');
            return true;
        });
    }

    const nodeIds = new Set(filteredNodes.map(n => n.id));

    // Similarity + max-edges filter on edges
    const edgeCounts = {};
    const filteredEdges = [];

    // Sort by similarity descending so we pick the strongest edges first
    const sortedEdges = [...memoryData.edges]
        .filter(e => {
            const src = typeof e.source === 'object' ? e.source.id : e.source;
            const tgt = typeof e.target === 'object' ? e.target.id : e.target;
            return nodeIds.has(src) && nodeIds.has(tgt) && e.similarity >= currentFilters.minSimilarity;
        })
        .sort((a, b) => b.similarity - a.similarity);

    sortedEdges.forEach(e => {
        const src = typeof e.source === 'object' ? e.source.id : e.source;
        const tgt = typeof e.target === 'object' ? e.target.id : e.target;
        edgeCounts[src] = (edgeCounts[src] || 0);
        edgeCounts[tgt] = (edgeCounts[tgt] || 0);
        if (edgeCounts[src] < currentFilters.maxEdges && edgeCounts[tgt] < currentFilters.maxEdges) {
            filteredEdges.push(e);
            edgeCounts[src]++;
            edgeCounts[tgt]++;
        }
    });

    // Node SIZE uses raw degree (all valid edges before display cap)
    // so highly-connected hubs look bigger even when we only draw 10 of their edges.
    const rawDegree = {};
    sortedEdges.forEach(e => {
        const src = typeof e.source === 'object' ? e.source.id : e.source;
        const tgt = typeof e.target === 'object' ? e.target.id : e.target;
        rawDegree[src] = (rawDegree[src] || 0) + 1;
        rawDegree[tgt] = (rawDegree[tgt] || 0) + 1;
    });
    filteredNodes.forEach(n => {
        n.size = rawDegree[n.id] || 0;
        // rawSize stays as the backend degree — never overwritten
    });

    visibleNodes = filteredNodes;
    visibleEdges = filteredEdges;
}

function updateStats() {
    const stats = memoryData.stats || {};
    document.getElementById('memory-stats').innerHTML = `
        <div><strong>Shown:</strong> ${visibleNodes.length} of ${stats.total_in_db || stats.total_thoughts || 0} total</div>
        <div><strong>Edges:</strong> ${visibleEdges.length}</div>
        <div><strong>Avg conn:</strong> ${stats.avg_connections || 0}</div>
    `;
}

// ─── D3-force simulation ──────────────────────────────────────────────────────

function buildSimulation() {
    if (simulation) {
        simulation.stop();
    }
    if (rafId !== null) {
        cancelAnimationFrame(rafId);
        rafId = null;
    }

    if (!visibleNodes.length) return;

    // Spread initial positions if nodes lack them
    visibleNodes.forEach(n => {
        if (!n.x) {
            n.x = width / 2 + (Math.random() - 0.5) * Math.min(width, height) * 0.9;
            n.y = height / 2 + (Math.random() - 0.5) * Math.min(width, height) * 0.9;
        }
    });

    // Physics tuned to match old SVG version's organic layout:
    // Strong repulsion with NO distance cap creates tendrils and chains.
    // Large fixed collision radius prevents blob packing.
    simulation = d3.forceSimulation(visibleNodes)
        .force('link', d3.forceLink(visibleEdges).id(d => d.id).distance(200))
        .force('charge', d3.forceManyBody().strength(-500))
        .force('center', d3.forceCenter(width / 2, height / 2))
        .force('collision', d3.forceCollide().radius(d => Math.max(30, nodeRadius(d) + 4)));

    startLoop();

    // Auto-fit once simulation cools
    simulation.on('end', () => {
        scheduleFit();
    });
}

function startLoop() {
    if (rafId !== null) return;
    function loop() {
        drawFrame();
        rafId = requestAnimationFrame(loop);
    }
    rafId = requestAnimationFrame(loop);
}

// ─── Canvas rendering ─────────────────────────────────────────────────────────

function drawFrame() {
    // Animated fit interpolation
    if (fitTarget && fitFrame < FIT_FRAMES) {
        const t = fitFrame / FIT_FRAMES;
        const ease = t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t; // ease in-out
        transform.x = transform.x + (fitTarget.x - transform.x) * ease * 0.15;
        transform.y = transform.y + (fitTarget.y - transform.y) * ease * 0.15;
        transform.k = transform.k + (fitTarget.k - transform.k) * ease * 0.15;
        fitFrame++;
        if (fitFrame >= FIT_FRAMES) fitTarget = null;
    }

    const dpr = canvas._dpr || 1;

    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    if (!visibleNodes.length) return;

    ctx.save();
    // Apply DPR scaling, then pan/zoom transform
    ctx.setTransform(
        dpr * transform.k, 0,
        0, dpr * transform.k,
        dpr * transform.x, dpr * transform.y
    );

    // Draw edges — subtle so node colors dominate (matches SVG Classic style)
    const highlightedIds = hoveredNode ? getConnectedIds(hoveredNode) : null;

    visibleEdges.forEach(e => {
        const src = e.source;
        const tgt = e.target;
        if (!src || !tgt || src.x == null) return;

        const isHighlighted = highlightedIds &&
            (highlightedIds.has(src.id) && highlightedIds.has(tgt.id));
        const dimmed = highlightedIds && !isHighlighted;

        ctx.beginPath();
        ctx.moveTo(src.x, src.y);
        ctx.lineTo(tgt.x, tgt.y);
        // 0.35 is the ceiling: with 4,000+ edges anything more opaque turns the graph into a gray mesh.
        ctx.strokeStyle = isHighlighted ? 'rgba(78, 205, 196, 0.85)' : 'rgba(120,120,120,0.35)';
        ctx.lineWidth = isHighlighted ? 2 : Math.max(0.5, e.similarity * 2);
        ctx.globalAlpha = dimmed ? 0.1 : 1;
        ctx.stroke();
        ctx.globalAlpha = 1;
    });

    // Draw nodes
    const showLabels = document.getElementById('labels-toggle') &&
                       document.getElementById('labels-toggle').checked &&
                       transform.k > 0.35;

    visibleNodes.forEach(n => {
        if (n.x == null) return;
        const r = nodeRadius(n);
        const color = nodeColor(n.type);
        const isHovered = n === hoveredNode;
        const isSelected = n === selectedNode;
        const isDimmed = highlightedIds && !highlightedIds.has(n.id);

        ctx.globalAlpha = isDimmed ? 0.15 : 1;

        // Colored glow behind every node so type colors pop at any zoom
        ctx.shadowColor = color;
        ctx.shadowBlur = isHovered || isSelected ? 18 : 6;

        // Circle fill
        ctx.beginPath();
        ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();

        // Reset shadow before stroke
        ctx.shadowBlur = 0;

        // Stroke — only prominent on hover/select
        if (isHovered || isSelected) {
            ctx.strokeStyle = '#fff';
            ctx.lineWidth = isSelected ? 2.5 : 2;
            ctx.stroke();
        }

        // Machine ring (second visual channel)
        const mColor = machineColor(n.source_machine);
        if (mColor) {
            ctx.beginPath();
            ctx.arc(n.x, n.y, r + 2, 0, Math.PI * 2);
            ctx.strokeStyle = mColor;
            ctx.lineWidth = 2.5;
            ctx.stroke();
        }

        ctx.globalAlpha = 1;

        // Labels
        if (showLabels) {
            ctx.fillStyle = 'rgba(255,255,255,0.9)';
            ctx.font = `${Math.max(9, 11 / transform.k)}px monospace`;
            ctx.shadowColor = '#000';
            ctx.shadowBlur = 3;
            ctx.fillText(n.content.substring(0, 35), n.x + r + 4, n.y + 4);
            ctx.shadowBlur = 0;
        }
    });

    ctx.restore();
}

// ─── Hit detection ────────────────────────────────────────────────────────────

function canvasToWorld(cx, cy) {
    return {
        x: (cx - transform.x) / transform.k,
        y: (cy - transform.y) / transform.k
    };
}

function hitTest(cx, cy) {
    const { x, y } = canvasToWorld(cx, cy);
    // Test in reverse order so topmost visually is hit first
    for (let i = visibleNodes.length - 1; i >= 0; i--) {
        const n = visibleNodes[i];
        if (n.x == null) continue;
        const r = nodeRadius(n);
        const dx = x - n.x;
        const dy = y - n.y;
        if (dx * dx + dy * dy <= r * r) return n;
    }
    return null;
}

function getConnectedIds(node) {
    const ids = new Set([node.id]);
    visibleEdges.forEach(e => {
        const src = typeof e.source === 'object' ? e.source.id : e.source;
        const tgt = typeof e.target === 'object' ? e.target.id : e.target;
        if (src === node.id) ids.add(tgt);
        if (tgt === node.id) ids.add(src);
    });
    return ids;
}

// ─── Mouse events ─────────────────────────────────────────────────────────────

function getCanvasPos(e) {
    const rect = canvas.getBoundingClientRect();
    return { cx: e.clientX - rect.left, cy: e.clientY - rect.top };
}

function onMouseMove(e) {
    const { cx, cy } = getCanvasPos(e);
    if (isPanning) {
        transform.x += e.movementX;
        transform.y += e.movementY;
        return;
    }
    const hit = hitTest(cx, cy);
    if (hit !== hoveredNode) {
        hoveredNode = hit;
        canvas.style.cursor = hit ? 'pointer' : 'grab';
    }
}

function onMouseDown(e) {
    if (e.button !== 0) return;
    const { cx, cy } = getCanvasPos(e);
    const hit = hitTest(cx, cy);
    if (!hit) {
        isPanning = true;
        panStart = { x: cx, y: cy };
        canvas.style.cursor = 'grabbing';
    }
}

function onMouseUp(e) {
    isPanning = false;
    const { cx, cy } = getCanvasPos(e);
    canvas.style.cursor = hitTest(cx, cy) ? 'pointer' : 'grab';
}

function onMouseLeave() {
    hoveredNode = null;
    isPanning = false;
}

function onClick(e) {
    const { cx, cy } = getCanvasPos(e);
    const hit = hitTest(cx, cy);
    if (hit) {
        selectedNode = hit;
        showDetails(hit);
    } else {
        selectedNode = null;
        closeDetails();
    }
}

function onWheel(e) {
    e.preventDefault();
    const { cx, cy } = getCanvasPos(e);
    const delta = -e.deltaY * 0.001;
    const factor = Math.exp(delta * 2.5);
    const newK = Math.max(0.01, Math.min(15, transform.k * factor));
    // Zoom towards cursor position
    transform.x = cx - (cx - transform.x) * (newK / transform.k);
    transform.y = cy - (cy - transform.y) * (newK / transform.k);
    transform.k = newK;
}

// ─── Fit to screen ────────────────────────────────────────────────────────────

function scheduleFit() {
    if (!visibleNodes.length) return;
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    visibleNodes.forEach(n => {
        if (n.x == null) return;
        minX = Math.min(minX, n.x);
        maxX = Math.max(maxX, n.x);
        minY = Math.min(minY, n.y);
        maxY = Math.max(maxY, n.y);
    });
    if (!isFinite(minX)) return;

    const pad = 60;
    const gw = maxX - minX || 1;
    const gh = maxY - minY || 1;
    const k = Math.min((width - pad * 2) / gw, (height - pad * 2) / gh);
    const midX = (minX + maxX) / 2;
    const midY = (minY + maxY) / 2;

    fitTarget = {
        x: width / 2 - k * midX,
        y: height / 2 - k * midY,
        k
    };
    fitFrame = 0;
}

function fitToScreen() {
    // Instant version for the button — use scheduleFit which animates
    if (simulation && simulation.alpha() > 0.01) {
        simulation.stop();
    }
    scheduleFit();
}

// ─── Details panel ─────────────────────────────────────────────────────────────

function showDetails(thought) {
    const panel = document.getElementById('thought-details');
    panel.classList.remove('hidden');
    document.getElementById('details-placeholder').style.display = 'none';
    document.getElementById('details-info').classList.remove('hidden');

    document.getElementById('details-type').textContent =
        thought.type.charAt(0).toUpperCase() + thought.type.slice(1);
    document.getElementById('details-content').textContent = thought.content;

    const parts = thought.created_at && thought.created_at.match(/(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})/);
    if (parts) {
        const [_, year, month, day, hour, min, sec] = parts;
        const date = new Date(year, month - 1, day, hour, min, sec);
        document.getElementById('details-created').textContent = date.toLocaleString();
    } else {
        document.getElementById('details-created').textContent = thought.created_at || '';
    }

    document.getElementById('details-project').textContent = thought.project || 'N/A';
    document.getElementById('details-connections').textContent = thought.size;
    const machineEl = document.getElementById('details-machine');
    if (machineEl) machineEl.textContent = thought.source_machine || 'Unknown';

    const relatedEdges = visibleEdges.filter(e => {
        const src = typeof e.source === 'object' ? e.source.id : e.source;
        const tgt = typeof e.target === 'object' ? e.target.id : e.target;
        return src === thought.id || tgt === thought.id;
    });

    const relatedList = document.getElementById('related-list');
    relatedList.innerHTML = '';
    relatedEdges.slice(0, 8).forEach(edge => {
        const src = typeof edge.source === 'object' ? edge.source.id : edge.source;
        const tgt = typeof edge.target === 'object' ? edge.target.id : edge.target;
        const relatedId = src === thought.id ? tgt : src;
        const relatedNode = visibleNodes.find(n => n.id === relatedId);
        if (!relatedNode) return;
        const li = document.createElement('li');
        li.textContent = `${relatedNode.content.substring(0, 60)}... (${(edge.similarity * 100).toFixed(0)}%)`;
        li.onclick = () => {
            selectedNode = relatedNode;
            showDetails(relatedNode);
        };
        relatedList.appendChild(li);
    });
}

function closeDetails() {
    document.getElementById('thought-details').classList.add('hidden');
    document.getElementById('details-placeholder').style.display = '';
    document.getElementById('details-info').classList.add('hidden');
    selectedNode = null;
}

// ─── Loading overlay ──────────────────────────────────────────────────────────

function showLoading(show) {
    document.getElementById('loading-overlay').style.display = show ? 'flex' : 'none';
}

// ─── Public API (called by memory_list.js and memory.html) ───────────────────

function refreshMemoryGraph() {
    loadGraph();
}
