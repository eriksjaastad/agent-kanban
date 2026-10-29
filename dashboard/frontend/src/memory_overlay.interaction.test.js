// Regression tests for #7147 (follow-up: #7657) — dashboard/static/memory_overlay.js
// is a plain classic script, not an ES module (dashboard/templates/memory.html
// loads it with a bare <script src="..."> tag), and it isn't wired into any
// test suite. It has no `export`s, so we can't `import` the functions we need
// to exercise without turning it into a module — which would change how the
// real page loads it. Instead we read the file's source text and evaluate it
// as the body of a `new Function(...)`, then `return` the handful of
// internal identifiers (functions + accessors for the module-level `let`
// variables) the tests below need. This runs the exact committed source,
// unmodified, inside jsdom; it is not a copy or a reimplementation of it.
//
// Bug #7147 fixed: every call to loadOverlay() (initial load, min-mentions
// change, "Show more", cluster-expand click) called setupOverlayInteraction(),
// which called canvas.addEventListener(...) for mousemove/mousedown/mouseup/
// mouseleave/wheel with no guard — so N reloads left N stacked listener sets
// on the same <canvas>, and effects like wheel-zoom compounded per reload.
// The fix added the `overlayCanvas.dataset.interactionBound` guard (lines
// ~506-507) so the listeners are attached exactly once no matter how many
// times setupOverlayInteraction() is called on the same canvas element.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { describe, it, expect, beforeEach } from 'vitest';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const OVERLAY_SOURCE_PATH = path.resolve(__dirname, '../../static/memory_overlay.js');
const overlaySource = readFileSync(OVERLAY_SOURCE_PATH, 'utf8');

/**
 * Evaluate dashboard/static/memory_overlay.js's exact source as a function
 * body and return a handle onto the module-level state/functions the tests
 * need. No text is added to or removed from the real file; the harness only
 * appends a local `return { ... }` statement to the copy it evaluates.
 */
function loadOverlayModule() {
  const harness = `${overlaySource}
return {
  setCanvas: (c) => { overlayCanvas = c; overlayCtx = c.getContext && c.getContext('2d'); },
  setData: (d) => { overlayData = d; },
  getTransform: () => overlayTransform,
  setTransform: (t) => { overlayTransform = t; },
  setupOverlayInteraction,
  overlaySimulationSettled,
  overlayFitToScreen,
  resetAutoFitState: () => {
    overlayUserInteracted = false;
    overlayAutoFitHandled = false;
    overlaySimTickCount = 0;
  },
  isAutoFitHandled: () => overlayAutoFitHandled,
};`;
  // Loading a plain classic script's source as a function body is the
  // harness itself; see the module comment above for why.
  const factory = new Function('document', 'window', 'localStorage', 'd3', harness);
  return factory(document, window, window.localStorage, undefined);
}

function makeCanvas() {
  const canvas = document.createElement('canvas');
  canvas.width = 400;
  canvas.height = 300;
  document.body.appendChild(canvas);
  return canvas;
}

describe('memory_overlay.js interaction-listener-once guard (#7147)', () => {
  let overlay;
  let canvas;

  beforeEach(() => {
    overlay = loadOverlayModule();
    canvas = makeCanvas();
    overlay.setCanvas(canvas);
  });

  it('binds wheel/mouse handlers exactly once no matter how many times setup runs', () => {
    // loadOverlay() calls setupOverlayInteraction() on every (re)load —
    // simulate 3 reloads/re-opens of the same canvas element.
    overlay.setupOverlayInteraction();
    overlay.setupOverlayInteraction();
    overlay.setupOverlayInteraction();

    overlay.setTransform({ x: 0, y: 0, k: 1 });

    // A single wheel-up tick multiplies k by 1.05 exactly once if the
    // handler is bound once. If setupOverlayInteraction() had stacked 3
    // independent listener sets (the pre-#7147 bug), one dispatched event
    // would fire the handler 3 times and compound the zoom to 1.05^3.
    const wheelEvent = new window.WheelEvent('wheel', {
      deltaY: -1,
      clientX: 10,
      clientY: 10,
      bubbles: true,
      cancelable: true,
    });
    canvas.dispatchEvent(wheelEvent);

    const { k } = overlay.getTransform();
    expect(k).toBeCloseTo(1.05, 5);
    expect(k).not.toBeCloseTo(1.05 ** 3, 5);
  });

  it('marks the canvas as bound after the first setup call', () => {
    expect(canvas.dataset.interactionBound).toBeUndefined();
    overlay.setupOverlayInteraction();
    expect(canvas.dataset.interactionBound).toBe('1');
    // Re-running setup must not clear or change that marker.
    overlay.setupOverlayInteraction();
    expect(canvas.dataset.interactionBound).toBe('1');
  });
});

describe('memory_overlay.js auto-fit-on-settle guard (#7147)', () => {
  let overlay;
  let canvas;

  beforeEach(() => {
    overlay = loadOverlayModule();
    canvas = makeCanvas();
    overlay.setCanvas(canvas);
    overlay.setData({ nodes: [{ id: 1, x: 10, y: 10, size: 1 }], edges: [] });
    overlay.resetAutoFitState();
  });

  it('fits the view the first time the simulation settles', () => {
    overlay.setTransform({ x: 0, y: 0, k: 1 });
    overlay.overlaySimulationSettled();

    expect(overlay.isAutoFitHandled()).toBe(true);
    // overlayFitToScreen() recenters on the single node; k should no longer
    // be the untouched sentinel value of 1 with x/y at the origin.
    const fitted = overlay.getTransform();
    expect(fitted).not.toEqual({ x: 0, y: 0, k: 1 });
  });

  it('does not re-fit on a second settle event (one auto-fit per load)', () => {
    overlay.overlaySimulationSettled();
    const sentinel = { x: 12345, y: 12345, k: 12345 };
    overlay.setTransform(sentinel);

    overlay.overlaySimulationSettled();

    // Without the overlayAutoFitHandled guard, this second settle would call
    // overlayFitToScreen() again and overwrite the sentinel transform.
    expect(overlay.getTransform()).toEqual(sentinel);
  });
});
