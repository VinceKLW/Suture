// Resizable panel layout: drag the gutters between panels, saved per browser.
//
//   SutureLayout.init(mainEl, defaults)
//   defaults = { cols: [{ f: <width weight>, panels: [{ id: <panel element id>, h: <height weight> }] }] }
//
// Every child of `main` with a `data-panel` attribute is managed. Columns are flex
// boxes sized by weight, so the layout keeps its proportions when the window
// resizes. Double-click a gutter to even out the two sides. Below BREAK px wide
// the panels just stack in layout order. Vanilla JS, no build step.
(function () {
  const KEY = "suture.layout.v1";
  const MIN_W = 220, MIN_H = 120, BREAK = 900;
  const root = document.documentElement;
  const clone = (v) => JSON.parse(JSON.stringify(v));
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  let main, defaults, state, panels, colEls = [];

  // A saved layout is only used if it has the same columns and panels as the
  // defaults (sizes may differ), so adding or moving a panel in the HTML never
  // strands someone on an old layout.
  function sameShape(a, b) {
    try {
      return a.cols.length === b.cols.length && a.cols.every((c, ci) => c.f > 0 && c.panels.length === b.cols[ci].panels.length
        && c.panels.every((p, pi) => p.h > 0 && p.id === b.cols[ci].panels[pi].id));
    } catch (_) { return false; }
  }
  function load() {
    try { const s = JSON.parse(localStorage.getItem(KEY)); if (sameShape(s, defaults)) return s; } catch (_) { /* storage blocked */ }
    return clone(defaults);
  }
  function save() {
    try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (_) { /* private window */ }
    main.dispatchEvent(new CustomEvent("layoutchange", { bubbles: true }));
  }
  const customized = () => JSON.stringify(state) !== JSON.stringify(defaults);
  const stacked = () => innerWidth < BREAK;

  // ---------------------------------------------------------------- render
  function gutter(dir, ci, pi) {
    const g = document.createElement("div");
    g.className = `gutter ${dir}`;
    g.setAttribute("role", "separator");
    g.setAttribute("aria-orientation", dir === "v" ? "vertical" : "horizontal");
    g.title = "Drag to resize · double-click to even out";
    g.addEventListener("pointerdown", (e) => startResize(e, g, dir, ci, pi));
    g.addEventListener("dblclick", () => {
      const [A, B] = pair(dir, ci, pi), k = dir === "v" ? "f" : "h", total = A[k] + B[k];
      A[k] = B[k] = total / 2;
      applySizes(); save();
    });
    return g;
  }
  const pair = (dir, ci, pi) => (dir === "v" ? [state.cols[ci - 1], state.cols[ci]] : [state.cols[ci].panels[pi - 1], state.cols[ci].panels[pi]]);

  function render() {
    const stack = stacked();
    root.classList.toggle("layout-on", !stack);
    root.classList.toggle("layout-stack", stack);
    const keep = new Map([...panels.values()].map((p) => [p, p.querySelector(".scroll")?.scrollTop || 0]));
    colEls = [];
    const frag = document.createDocumentFragment();
    state.cols.forEach((col, ci) => {
      if (ci && !stack) frag.append(gutter("v", ci));
      const el = document.createElement("div");
      el.className = "lcol";
      col.panels.forEach((p, pi) => {
        if (pi && !stack) el.append(gutter("h", ci, pi));
        el.append(panels.get(p.id));
      });
      colEls.push(el);
      frag.append(el);
    });
    main.replaceChildren(frag);
    for (const [p, top] of keep) { const sc = p.querySelector(".scroll"); if (sc) sc.scrollTop = top; }
    applySizes();
    if (window.SutureGlass) SutureGlass.scan();  // panels moved: re-align the refracted photo
  }

  function applySizes() {
    const stack = stacked();
    state.cols.forEach((col, ci) => {
      colEls[ci].style.flex = stack ? "" : `${col.f} 1 0px`;
      col.panels.forEach((p) => { panels.get(p.id).style.flex = stack ? "" : `${p.h} 1 0px`; });
    });
  }

  // ---------------------------------------------------------------- resize
  function startResize(e, g, dir, ci, pi) {
    if (e.button !== 0) return;
    e.preventDefault();
    g.setPointerCapture(e.pointerId);
    const [a, b] = dir === "v" ? [colEls[ci - 1], colEls[ci]] : [panels.get(state.cols[ci].panels[pi - 1].id), panels.get(state.cols[ci].panels[pi].id)];
    const [A, B] = pair(dir, ci, pi);
    const k = dir === "v" ? "f" : "h", min = dir === "v" ? MIN_W : MIN_H;
    const size = (el) => (dir === "v" ? el.getBoundingClientRect().width : el.getBoundingClientRect().height);
    const sa = size(a), sb = size(b), total = A[k] + B[k], start = dir === "v" ? e.clientX : e.clientY;
    g.classList.add("active");
    root.classList.add(dir === "v" ? "resizing-v" : "resizing-h");
    const onMove = (ev) => {
      const na = clamp(sa + (dir === "v" ? ev.clientX : ev.clientY) - start, min, sa + sb - min);
      A[k] = total * na / (sa + sb); B[k] = total - A[k];
      applySizes();
    };
    const up = () => {
      g.removeEventListener("pointermove", onMove); g.removeEventListener("pointerup", up); g.removeEventListener("pointercancel", up);
      g.classList.remove("active");
      root.classList.remove("resizing-v", "resizing-h");
      save();
    };
    g.addEventListener("pointermove", onMove); g.addEventListener("pointerup", up); g.addEventListener("pointercancel", up);
  }

  // ---------------------------------------------------------------- init
  function init(mainEl, layoutDefaults) {
    main = mainEl;
    defaults = clone(layoutDefaults);
    panels = new Map([...main.querySelectorAll("[data-panel]")].map((p) => [p.id, p]));
    state = load();
    render();
    let wasStacked = stacked();
    addEventListener("resize", () => { if (stacked() !== wasStacked) { wasStacked = stacked(); render(); } });
  }

  function reset() {
    state = clone(defaults);
    try { localStorage.removeItem(KEY); } catch (_) { /* ignore */ }
    render();
    main.dispatchEvent(new CustomEvent("layoutchange", { bubbles: true }));
  }

  window.SutureLayout = { init, reset, customized };
})();
