// Memory Graph — Classic D3+SVG Renderer
// Original organic force physics with curved SVG arc edges.
// Loaded lazily when the Classic tab is first activated.

(function () {
    'use strict';

    // ─── State ───────────────────────────────────────────────────────────────────

    let svgEl, svgContainer, svgSimulation;
    let svgWidth, svgHeight;
    let svgMemoryData = { nodes: [], edges: [] };
    let svgInitialized = false;
    let svgDataLoaded = false;

    // ─── Color maps (match Canvas renderer) ──────────────────────────────────────
    // Keep in step with TYPE_COLORS in memory.js: a type in neither renders gray.

    const SVG_TYPE_COLORS = {
        observation:      '#4ECDC4',
        decision:         '#FF6B6B',
        idea:             '#FFD93D',
        question:         '#A8E6CF',
        conversation:     '#B39DDB',
        meeting_debrief:  '#F48FB1',
        fact:             '#80CBC4',
        project:          '#FFB74D',
        insight:          '#AED581',
        error:            '#EF9A9A',
        person:           '#F97583',
        person_note:      '#F97583',
        tool:             '#56D364',
        preference:       '#79B8FF',
        loop_state:       '#CE93D8',
        dream:            '#E1BEE7',
        default:          '#8b949e'
    };

    function svgNodeColor(type) {
        return SVG_TYPE_COLORS[type] || SVG_TYPE_COLORS.default;
    }

    const SVG_MACHINE_COLORS = {
        'openclaw':  '#FF9800',
        'macbook':   '#42A5F5',
    };

    function svgMachineColor(machine) {
        if (!machine) return null;
        const lower = machine.toLowerCase();
        if (lower.includes('mac-mini') || lower.includes('openclaw') || lower.includes('mini')) return SVG_MACHINE_COLORS['openclaw'];
        if (lower.includes('macbook')) return SVG_MACHINE_COLORS['macbook'];
        return '#666666';
    }

    // ─── Public entry point ──────────────────────────────────────────────────────

    window.loadSvgGraph = function () {
        if (!svgInitialized) {
            initSvg();
            svgInitialized = true;
        }
        if (!svgDataLoaded) {
            fetchSvgData();
        }
    };

    // ─── Init ────────────────────────────────────────────────────────────────────

    function initSvg() {
        svgEl = document.getElementById('memory-svg');
        if (!svgEl) {
            console.warn('SVG: #memory-svg element not found in DOM');
            return;
        }

        svgWidth = svgEl.clientWidth || window.innerWidth - 280;
        svgHeight = svgEl.clientHeight || window.innerHeight - 80;
        console.log('SVG init dimensions:', svgWidth, 'x', svgHeight);

        const d3Svg = d3.select('#memory-svg');
        svgContainer = d3Svg.append('g');

        // Manual zoom + pan (bypasses d3.zoom which crashes with
        // "s.invert is not a function" on CSS-sized hidden-then-shown SVGs)
        var svgTransform = { x: 0, y: 0, k: 1 };
        var isPanning = false, panStart = { x: 0, y: 0 };

        function applySvgTransform() {
            svgContainer.attr('transform',
                'translate(' + svgTransform.x + ',' + svgTransform.y + ') scale(' + svgTransform.k + ')');
        }

        svgEl.addEventListener('wheel', function (e) {
            e.preventDefault();
            var rect = svgEl.getBoundingClientRect();
            var mx = e.clientX - rect.left;
            var my = e.clientY - rect.top;
            var delta = -e.deltaY * 0.001;
            var factor = Math.exp(delta * 2.5);
            var newK = Math.max(0.01, Math.min(20, svgTransform.k * factor));
            // Zoom towards cursor
            svgTransform.x = mx - (mx - svgTransform.x) * (newK / svgTransform.k);
            svgTransform.y = my - (my - svgTransform.y) * (newK / svgTransform.k);
            svgTransform.k = newK;
            applySvgTransform();
        }, { passive: false });

        svgEl.addEventListener('mousedown', function (e) {
            isPanning = true;
            panStart.x = e.clientX - svgTransform.x;
            panStart.y = e.clientY - svgTransform.y;
            svgEl.style.cursor = 'grabbing';
        });

        window.addEventListener('mousemove', function (e) {
            if (!isPanning) return;
            svgTransform.x = e.clientX - panStart.x;
            svgTransform.y = e.clientY - panStart.y;
            applySvgTransform();
        });

        window.addEventListener('mouseup', function () {
            if (isPanning) {
                isPanning = false;
                svgEl.style.cursor = 'grab';
            }
        });

        // Store transform reference for fitToScreen
        svgEl.__svgTransform = svgTransform;
        svgEl.__applySvgTransform = applySvgTransform;

        // Resize handler
        window.addEventListener('resize', () => {
            svgWidth = svgEl.clientWidth || window.innerWidth - 280;
            svgHeight = svgEl.clientHeight || window.innerHeight - 80;
            if (svgSimulation) {
                svgSimulation.force('center', d3.forceCenter(svgWidth / 2, svgHeight / 2));
                svgSimulation.alpha(0.3).restart();
            }
        });
    }

    // ─── Data fetching ───────────────────────────────────────────────────────────

    async function fetchSvgData() {
        showSvgLoading(true);
        try {
            // Same sources as memory.js: /api/memory-graph and /api/memory/types.
            const [graphRes, typesRes] = await Promise.all([
                fetch('/api/memory-graph'),
                fetch('/api/memory/types')
            ]);

            svgMemoryData = await graphRes.json();

            // 404/503 bodies carry `error`; a 500 (such as a broken local config) carries `detail`.
            const loadError = svgMemoryData.error || svgMemoryData.detail;
            if (loadError) {
                alert('SVG Graph Error: ' + loadError);
                showSvgLoading(false);
                return;
            }

            // Preserve raw size
            svgMemoryData.nodes.forEach(n => { n.rawSize = n.size || 0; });

            svgDataLoaded = true;
            applySvgFilters();
        } catch (err) {
            console.error('SVG graph load error:', err);
            alert('Failed to load SVG memory graph');
        }
        showSvgLoading(false);
    }

    // ─── Filtering (client-side, same logic as Canvas) ───────────────────────────

    function applySvgFilters() {
        if (!svgDataLoaded) {
            console.warn('SVG: Data not loaded yet, skipping filter apply');
            return;
        }

        const typeVal = document.getElementById('type-filter').value || '';
        const machineVal = document.getElementById('machine-filter').value || '';
        const rawMinSim = parseInt(document.getElementById('min-similarity').value);
        const minSim = (isNaN(rawMinSim) ? 30 : rawMinSim) / 100;
        const rawMaxEdges = parseInt(document.getElementById('max-edges').value);
        const maxEdges = isNaN(rawMaxEdges) ? 10 : rawMaxEdges;

        // Filter nodes
        let filteredNodes = svgMemoryData.nodes;
        if (typeVal) {
            filteredNodes = filteredNodes.filter(n => n.type === typeVal);
        }
        if (machineVal) {
            filteredNodes = filteredNodes.filter(n => {
                const lower = (n.source_machine || '').toLowerCase();
                if (machineVal === 'macbook') return lower.includes('macbook');
                if (machineVal === 'openclaw') return lower.includes('mac-mini') || lower.includes('mini');
                return true;
            });
        }

        const nodeIds = new Set(filteredNodes.map(n => n.id));

        // Filter edges by similarity, both endpoints present, and max-edges cap
        const edgeCounts = {};
        const filteredEdges = [];
        const sortedEdges = [...svgMemoryData.edges]
            .filter(e => {
                const src = typeof e.source === 'object' ? e.source.id : e.source;
                const tgt = typeof e.target === 'object' ? e.target.id : e.target;
                return nodeIds.has(src) && nodeIds.has(tgt) && e.similarity >= minSim;
            })
            .sort((a, b) => b.similarity - a.similarity);

        sortedEdges.forEach(e => {
            const src = typeof e.source === 'object' ? e.source.id : e.source;
            const tgt = typeof e.target === 'object' ? e.target.id : e.target;
            edgeCounts[src] = edgeCounts[src] || 0;
            edgeCounts[tgt] = edgeCounts[tgt] || 0;
            if (edgeCounts[src] < maxEdges && edgeCounts[tgt] < maxEdges) {
                filteredEdges.push(e);
                edgeCounts[src]++;
                edgeCounts[tgt]++;
            }
        });

        // Update node sizes based on raw degree in visible edge set
        const rawDegree = {};
        sortedEdges.forEach(e => {
            const src = typeof e.source === 'object' ? e.source.id : e.source;
            const tgt = typeof e.target === 'object' ? e.target.id : e.target;
            rawDegree[src] = (rawDegree[src] || 0) + 1;
            rawDegree[tgt] = (rawDegree[tgt] || 0) + 1;
        });
        filteredNodes.forEach(n => { n.size = rawDegree[n.id] || 0; });

        renderSvgGraph(filteredNodes, filteredEdges);
    }

    // Expose for external filter change events
    window.applySvgFilters = applySvgFilters;

    // Expose fit-to-screen for the sidebar button
    window.svgFitToScreen = svgFitToScreen;

    // ─── SVG Rendering ───────────────────────────────────────────────────────────

    function renderSvgGraph(nodes, edges) {
        if (!svgContainer) {
            console.warn('SVG: Container not initialized, cannot render');
            return;
        }

        // Clear previous
        svgContainer.selectAll('*').remove();

        if (!nodes || nodes.length === 0) {
            console.warn('SVG: No thoughts to display');
            return;
        }

        // Deep copy for D3 force (it mutates)
        const d3Nodes = nodes.map(d => ({ ...d }));
        const d3Edges = edges.map(d => ({ ...d }));

        // Initial positions
        d3Nodes.forEach(n => {
            if (!n.x) {
                n.x = svgWidth / 2 + (Math.random() - 0.5) * 200;
                n.y = svgHeight / 2 + (Math.random() - 0.5) * 200;
            }
        });

        // Original force physics: charge -500, link distance 200, collision 50, NO distanceMax
        svgSimulation = d3.forceSimulation(d3Nodes)
            .force('link', d3.forceLink(d3Edges).id(d => d.id).distance(200))
            .force('charge', d3.forceManyBody().strength(-500))
            .force('center', d3.forceCenter(svgWidth / 2, svgHeight / 2))
            .force('collision', d3.forceCollide().radius(50));

        // Draw edges as curved arcs
        const link = svgContainer.append('g')
            .attr('class', 'svg-links')
            .selectAll('path')
            .data(d3Edges)
            .enter().append('path')
            .attr('class', 'svg-link')
            .attr('fill', 'none')
            .attr('stroke', 'rgba(120,120,120,0.35)')
            .attr('stroke-width', d => Math.max(0.5, d.similarity * 3));

        // Draw nodes
        const node = svgContainer.append('g')
            .attr('class', 'svg-nodes')
            .selectAll('g')
            .data(d3Nodes)
            .enter().append('g')
            .attr('class', 'svg-node')
            .call(d3.drag()
                .on('start', dragstarted)
                .on('drag', dragged)
                .on('end', dragended))
            .on('click', (event, d) => svgShowDetails(d))
            .on('mouseover', (event, d) => svgHighlight(d, true))
            .on('mouseout', (event, d) => svgHighlight(d, false));

        // Machine ring (outer circle)
        node.append('circle')
            .attr('class', 'svg-machine-ring')
            .attr('r', d => Math.max(20, 10 + d.size * 2) + 3)
            .attr('fill', 'none')
            .attr('stroke', d => svgMachineColor(d.source_machine) || 'transparent')
            .attr('stroke-width', 2.5);

        // Main node circle
        node.append('circle')
            .attr('class', 'svg-node-circle')
            .attr('r', d => Math.max(20, 10 + d.size * 2))
            .attr('fill', d => svgNodeColor(d.type))
            .attr('stroke', 'rgba(255,255,255,0.4)')
            .attr('stroke-width', 1);

        // Labels (hidden by default, controlled by labels-toggle)
        const showLabels = document.getElementById('labels-toggle') &&
                           document.getElementById('labels-toggle').checked;
        node.append('text')
            .attr('dx', d => Math.max(20, 10 + d.size * 2) + 5)
            .attr('dy', 4)
            .attr('fill', 'rgba(255,255,255,0.9)')
            .attr('font-size', '11px')
            .attr('font-family', 'monospace')
            .text(d => d.content.substring(0, 50))
            .style('display', showLabels ? 'block' : 'none')
            .style('pointer-events', 'none');

        // Tick — curved arc edges: M x1,y1 A dr,dr 0 0,1 x2,y2
        svgSimulation.on('tick', () => {
            link.attr('d', d => {
                const dx = d.target.x - d.source.x;
                const dy = d.target.y - d.source.y;
                const dr = Math.sqrt(dx * dx + dy * dy) * 1.2;
                return `M ${d.source.x},${d.source.y} A ${dr},${dr} 0 0,1 ${d.target.x},${d.target.y}`;
            });

            node.attr('transform', d => `translate(${d.x},${d.y})`);
        });

        // Auto-fit when simulation cools
        svgSimulation.on('end', () => {
            svgFitToScreen();
        });

    }

    // ─── Fit to screen ───────────────────────────────────────────────────────────

    function svgFitToScreen() {
        if (!svgContainer || !svgEl) return;
        const bounds = svgContainer.node().getBBox();
        if (bounds.width === 0 || bounds.height === 0) return;

        const fullWidth = svgWidth;
        const fullHeight = svgHeight;
        const midX = bounds.x + bounds.width / 2;
        const midY = bounds.y + bounds.height / 2;
        const scale = 0.85 / Math.max(bounds.width / fullWidth, bounds.height / fullHeight);
        const tx = fullWidth / 2 - scale * midX;
        const ty = fullHeight / 2 - scale * midY;

        var t = svgEl.__svgTransform;
        var apply = svgEl.__applySvgTransform;
        if (t && apply) {
            t.x = tx; t.y = ty; t.k = scale;
            apply();
        }
    }

    // ─── Hover highlight ─────────────────────────────────────────────────────────

    function svgHighlight(thought, active) {
        if (active) {
            const connectedIds = new Set([thought.id]);
            svgContainer.selectAll('.svg-link').each(function (e) {
                const srcId = e.source.id || e.source;
                const tgtId = e.target.id || e.target;
                if (srcId === thought.id) connectedIds.add(tgtId);
                if (tgtId === thought.id) connectedIds.add(srcId);
            });

            svgContainer.selectAll('.svg-node').style('opacity', d => connectedIds.has(d.id) ? 1 : 0.15);
            svgContainer.selectAll('.svg-link')
                .style('opacity', e => {
                    const srcId = e.source.id || e.source;
                    const tgtId = e.target.id || e.target;
                    return (srcId === thought.id || tgtId === thought.id) ? 1 : 0.25;
                })
                .attr('stroke', e => {
                    const srcId = e.source.id || e.source;
                    const tgtId = e.target.id || e.target;
                    return (srcId === thought.id || tgtId === thought.id)
                        ? 'rgba(78, 205, 196, 0.85)' : 'rgba(160,160,160,0.55)';
                });
        } else {
            svgContainer.selectAll('.svg-node').style('opacity', 1);
            svgContainer.selectAll('.svg-link')
                .style('opacity', 1)
                .attr('stroke', 'rgba(120,120,120,0.35)');
        }
    }

    // ─── Click to inspect (reuses Canvas details panel) ──────────────────────────

    function svgShowDetails(thought) {
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

        // Related thoughts from visible edges
        const relatedList = document.getElementById('related-list');
        relatedList.innerHTML = '';

        svgContainer.selectAll('.svg-link').each(function (edge) {
            const srcId = edge.source.id || edge.source;
            const tgtId = edge.target.id || edge.target;
            if (srcId !== thought.id && tgtId !== thought.id) return;
            const relatedId = srcId === thought.id ? tgtId : srcId;
            const relatedNode = svgContainer.selectAll('.svg-node').data().find(n => n.id === relatedId);
            if (!relatedNode) return;
            const li = document.createElement('li');
            li.textContent = `${relatedNode.content.substring(0, 60)}... (${(edge.similarity * 100).toFixed(0)}%)`;
            li.onclick = () => svgShowDetails(relatedNode);
            relatedList.appendChild(li);
        });
    }

    // ─── Drag behavior ───────────────────────────────────────────────────────────

    function dragstarted(event, d) {
        if (!event.active) svgSimulation.alphaTarget(0.3).restart();
        d.fx = d.x;
        d.fy = d.y;
    }

    function dragged(event, d) {
        d.fx = event.x;
        d.fy = event.y;
    }

    function dragended(event, d) {
        if (!event.active) svgSimulation.alphaTarget(0);
        d.fx = null;
        d.fy = null;
    }

    // ─── Loading ─────────────────────────────────────────────────────────────────

    function showSvgLoading(show) {
        const overlay = document.getElementById('loading-overlay');
        if (overlay) overlay.style.display = show ? 'flex' : 'none';
    }

    // ─── Labels toggle listener ──────────────────────────────────────────────────

    document.addEventListener('DOMContentLoaded', () => {
        const toggle = document.getElementById('labels-toggle');
        if (toggle) {
            toggle.addEventListener('change', () => {
                if (!svgContainer) return;
                const show = toggle.checked;
                svgContainer.selectAll('.svg-node text').style('display', show ? 'block' : 'none');
            });
        }
    });

    // ─── Refresh support ─────────────────────────────────────────────────────────

    window.refreshSvgGraph = function () {
        svgDataLoaded = false;
        fetchSvgData();
    };

})();
