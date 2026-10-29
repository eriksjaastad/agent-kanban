// Memory Heatmap View
// Fetches /api/memory/heatmap lazily on first activation.
// Renders a D3 date × type density grid.

let heatmapLoaded = false;
let heatmapData = null;

const HEATMAP_COLORS = {
    observation:  '#4ECDC4',
    decision:     '#FF6B6B',
    idea:         '#FFD93D',
    question:     '#A8E6CF',
    conversation: '#B39DDB',
    fact:         '#80CBC4',
    project:      '#FFB74D',
    insight:      '#AED581',
    error:        '#EF9A9A',
    default:      '#888888'
};

function heatmapColor(type) {
    return HEATMAP_COLORS[type] || HEATMAP_COLORS.default;
}

async function loadHeatmap() {
    if (heatmapLoaded) {
        renderHeatmap();
        return;
    }
    const container = document.getElementById('heatmap-container');
    container.innerHTML = '<p style="color:#666;padding:40px;text-align:center">Loading heatmap…</p>';

    try {
        // /api/memory/heatmap: per-day counts by thought type from the memory
        // store's `thoughts` table. A failed request (a broken local config is
        // a 500 with `detail`) or an unreadable body is shown and not cached, so
        // the next visit tries again; only an error body may fail to parse.
        const res = await fetch('/api/memory/heatmap');
        const data = res.ok ? await res.json() : await res.json().catch(() => ({}));
        const error = res.ok ? data.error : (data.detail || data.error || `HTTP ${res.status}`);
        if (error) {
            const p = document.createElement('p');
            p.style.cssText = 'color:#FF6B6B;padding:40px;text-align:center';
            p.textContent = `Error: ${error}`;
            container.replaceChildren(p);
            return;
        }
        heatmapData = data.rows || [];
        heatmapLoaded = true;
        renderHeatmap();
    } catch (err) {
        container.innerHTML = `<p style="color:#FF6B6B;padding:40px;text-align:center">Failed to load heatmap data.</p>`;
        console.error('Heatmap load error:', err);
    }
}

function renderHeatmap() {
    const container = document.getElementById('heatmap-container');
    if (!heatmapData || heatmapData.length === 0) {
        container.innerHTML = '<p style="color:#666;padding:40px;text-align:center">No heatmap data available.</p>';
        return;
    }

    const margin = { top: 60, right: 30, bottom: 20, left: 140 };
    const containerW = container.clientWidth || 900;
    const containerH = container.clientHeight || 560;
    const svgW = containerW;
    const svgH = containerH;
    const innerW = svgW - margin.left - margin.right;
    const innerH = svgH - margin.top - margin.bottom;

    // Parse data
    const dates = [...new Set(heatmapData.map(d => d.date))].sort();
    const types = [...new Set(heatmapData.map(d => d.type))].sort();

    // Build lookup: date+type → count
    const lookup = {};
    heatmapData.forEach(d => {
        lookup[`${d.date}||${d.type}`] = d.count;
    });
    const maxCount = Math.max(...heatmapData.map(d => d.count), 1);

    // Clear and recreate SVG
    container.innerHTML = '';
    const svg = d3.select('#heatmap-container')
        .append('svg')
        .attr('width', svgW)
        .attr('height', svgH)
        .style('background', '#0a0a0a');

    const g = svg.append('g')
        .attr('transform', `translate(${margin.left},${margin.top})`);

    // Scales
    const xScale = d3.scaleBand()
        .domain(dates)
        .range([0, innerW])
        .padding(0.05);

    const yScale = d3.scaleBand()
        .domain(types)
        .range([0, innerH])
        .padding(0.08);

    const cellW = xScale.bandwidth();
    const cellH = yScale.bandwidth();

    // Title
    svg.append('text')
        .attr('x', svgW / 2)
        .attr('y', 28)
        .attr('text-anchor', 'middle')
        .attr('fill', '#eee')
        .attr('font-size', '15px')
        .attr('font-family', 'monospace')
        .text('Thought Density Over Time (date × type)');

    // Y axis — type labels
    g.append('g')
        .call(
            d3.axisLeft(yScale)
                .tickSize(0)
        )
        .call(ax => ax.select('.domain').remove())
        .call(ax => ax.selectAll('text')
            .attr('fill', d => heatmapColor(d))
            .attr('font-size', '12px')
            .attr('font-family', 'monospace')
            .attr('text-anchor', 'end')
            .attr('dx', '-8px')
        );

    // X axis — dates (show every Nth label to avoid crowding)
    const showEvery = Math.ceil(dates.length / Math.floor(innerW / 60));
    g.append('g')
        .attr('transform', `translate(0,${-8})`)
        .call(
            d3.axisTop(xScale)
                .tickValues(dates.filter((_, i) => i % showEvery === 0))
                .tickSize(0)
        )
        .call(ax => ax.select('.domain').remove())
        .call(ax => ax.selectAll('text')
            .attr('fill', '#666')
            .attr('font-size', '10px')
            .attr('font-family', 'monospace')
            .attr('transform', 'rotate(-45)')
            .attr('text-anchor', 'start')
        );

    // Tooltip
    const tooltip = d3.select('#heatmap-container')
        .append('div')
        .attr('class', 'heatmap-tooltip')
        .style('opacity', 0);

    // Cells
    types.forEach(type => {
        dates.forEach(date => {
            const count = lookup[`${date}||${type}`] || 0;
            if (count === 0) return;
            const alpha = 0.15 + 0.85 * (count / maxCount);
            const color = heatmapColor(type);

            g.append('rect')
                .attr('class', 'heatmap-cell')
                .attr('x', xScale(date))
                .attr('y', yScale(type))
                .attr('width', cellW)
                .attr('height', cellH)
                .attr('rx', 2)
                .attr('fill', color)
                .attr('opacity', alpha)
                .on('mouseover', function(event) {
                    d3.select(this).attr('stroke', '#fff').attr('stroke-width', 1.5);
                    tooltip
                        .style('opacity', 1)
                        .html(`<strong>${date}</strong><br>${type}: <strong>${count}</strong>`);
                })
                .on('mousemove', function(event) {
                    const [mx, my] = d3.pointer(event, container);
                    tooltip
                        .style('left', (mx + 14) + 'px')
                        .style('top', (my - 28) + 'px');
                })
                .on('mouseout', function() {
                    d3.select(this).attr('stroke', 'none');
                    tooltip.style('opacity', 0);
                });
        });
    });
}
