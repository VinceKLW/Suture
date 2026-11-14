// Rearrangeable panel layout: resize with the gutters, drag a panel by its grip
// (or its header) to move it, saved per browser.
//
//   SutureLayout.init(mainEl, defaults)
//   defaults = { cols: [{ f: <width weight>, panels: [{ id: <panel element id>, h: <height weight> }] }] }
//
// Every child of `main` with a `data-panel` attribute is managed. Columns are flex
// boxes sized by weight, so the layout keeps its proportions when the window
// resizes. Below BREAK px wide the panels just stack in layout order. Vanilla JS,
// no build step. Pointer events throughout (HTML5 drag-and-drop can't be styled).
(function () {
  const KEY = "suture.layout.v1";
  const MIN_W = 220, MIN_H = 120, MAX_COLS = 4, BREAK = 900, THRESHOLD = 6;
  const root = document.documentElement;
  const clone = (v) => JSON.parse(JSON.stringify(v));
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  let main, defaults, state, panels, colEls = [];

  function valid(s, ids) {
    try {
      const seen = s.cols.flatMap((c) => c.panels.map((p) => p.id));
      return s.cols.length >= 1 && s.cols.length <= MAX_COLS && s.cols.every((c) => c.f > 0 && c.panels.length && c.panels.every((p) => p.h > 0))
        && seen.length === ids.length && ids.every((id) => seen.includes(id));
    } catch (_) { return false; }
  }
  function load() {
    try { const s = JSON.parse(localStorage.getItem(KEY)); if (valid(s, [...panels.keys()])) return s; } catch (_) { /* storage blocked */ }
    return clone(defaults);
  }
  function save() {
    try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (_) { /* private window */ }
    main.dispatchEvent(new CustomEvent("layoutchange", { bubbles: true }));
  }
  const customized = () => JSON.stringify(state) !== JSON.stringify(defaults);
  const stacked = () => innerWidth < BREAK;
  const locate = (id) => { for (let ci = 0; ci < state.cols.length; ci++) { const pi = state.cols[ci].panels.findIndex((p) => p.id === id); if (pi >= 0) return { ci, pi }; } return null; };
  const title = (el) => el.dataset.title || el.querySelector("h2")?.textContent || el.id;

  // ---------------------------------------------------------------- render
  function gutter(dir, ci, pi) {
    const g = document.createElement("div");
    g.className = `gutter ${dir}`;
    g.setAttribute("role", "separator");
    g.setAttribute("aria-orientation", dir === "v" ? "vertical" : "horizontal");
    g.title = "Drag to resize · double-click to even out";
    g.addEventListener("pointerdown", (e) => startResize(e, g, dir, ci, pi));
    g.addEventListener("dblclick", () => {
      const pair = dir === "v" ? [state.cols[ci - 1], state.cols[ci]] : [state.cols[ci].panels[pi - 1], state.cols[ci].panels[pi]];
      const k = dir === "v" ? "f" : "h", total = pair[0][k] + pair[1][k];
      pair[0][k] = pair[1][k] = total / 2;
      applySizes(); save();
    });
    return g;
  }

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
    const [A, B] = dir === "v" ? [state.cols[ci - 1], state.cols[ci]] : [state.cols[ci].panels[pi - 1], state.cols[ci].panels[pi]];
    const k = dir === "v" ? "f" : "h", min = dir === "v" ? MIN_W : MIN_H;
    const size = (el) => (dir === "v" ? el.getBoundingClientRect().width : el.getBoundingClientRect().height);
    const sa = size(a), sb = size(b), total = A[k] + B[k], start = dir === "v" ? e.clientX : e.clientY;
    g.classList.add("active");
    root.classList.add("layout-resizing", dir === "v" ? "resizing-v" : "resizing-h");
    const move = (ev) => {
      const na = clamp(sa + (dir === "v" ? ev.clientX : ev.clientY) - start, min, sa + sb - min);
      A[k] = total * na / (sa + sb); B[k] = total - A[k];
      applySizes();
    };
    const up = () => {
      g.removeEventListener("pointermove", move); g.removeEventListener("pointerup", up); g.removeEventListener("pointercancel", up);
      g.classList.remove("active");
      root.classList.remove("layout-resizing", "resizing-v", "resizing-h");
      save();
    };
    g.addEventListener("pointermove", move); g.addEventListener("pointerup", up); g.addEventListener("pointercancel", up);
  }

  // ---------------------------------------------------------------- move
  function move(id, target) {
    const from = locate(id);
    if (!from || target.id === id) return false;
    const [item] = state.cols[from.ci].panels.splice(from.pi, 1);
    if (!state.cols[from.ci].panels.length) {           // column emptied: give its width to a neighbour
      const [gone] = state.cols.splice(from.ci, 1);
      (state.cols[from.ci] || state.cols[from.ci - 1]).f += gone.f;
    }
    const t = locate(target.id);
    const col = state.cols[t.ci];
    if (target.zone === "left" || target.zone === "right") {
      const f = col.f / 2;
      col.f -= f;
      state.cols.splice(t.ci + (target.zone === "right" ? 1 : 0), 0, { f, panels: [{ id, h: 1 }] });
    } else {
      item.h = col.panels.reduce((n, p) => n + p.h, 0) / col.panels.length;
      col.panels.splice(t.pi + (target.zone === "after" ? 1 : 0), 0, item);
    }
    return true;
  }

  const canNewColumn = (id) => state.cols.length < MAX_COLS || state.cols[locate(id).ci].panels.length === 1;

  function targetAt(x, y, id) {
    for (const [pid, el] of panels) {
      if (pid === id) continue;
      const r = el.getBoundingClientRect();
      if (x < r.left || x > r.right || y < r.top || y > r.bottom) continue;
      const rx = (x - r.left) / r.width, ry = (y - r.top) / r.height;
      const side = rx < 0.22 ? "left" : rx > 0.78 ? "right" : null;
      if (side && canNewColumn(id)) {
        const w = Math.min(r.width * 0.5, 160);
        return { id: pid, zone: side, rect: { left: side === "left" ? r.left : r.right - w, top: r.top, width: w, height: r.height }, label: "New column" };
      }
      const zone = ry < 0.5 ? "before" : "after";
      return { id: pid, zone, rect: { left: r.left, top: zone === "before" ? r.top : r.top + r.height / 2, width: r.width, height: r.height / 2 },
               label: zone === "before" ? `Above ${title(el)}` : `Below ${title(el)}` };
    }
    return null;
  }

  function startDrag(e, panel) {
    if (e.button !== 0 || stacked()) return;
    if (e.target.closest("a, button:not(.grip), input, select, textarea, label, [contenteditable]")) return;
    const sx = e.clientX, sy = e.clientY, src = e.currentTarget;
    let dragging = false, target = null, ghost, hint;
    src.setPointerCapture(e.pointerId);
    const onMove = (ev) => {
      if (!dragging) {
        if (Math.hypot(ev.clientX - sx, ev.clientY - sy) < THRESHOLD) return;
        dragging = true;
        getSelection()?.removeAllRanges();  // a header drag may have started a text selection
        panel.classList.add("dragging-panel");
        root.classList.add("layout-dragging");
        ghost = document.createElement("div"); ghost.className = "drag-ghost"; ghost.textContent = title(panel);
        hint = document.createElement("div"); hint.className = "drop-hint"; hint.hidden = true;
        document.body.append(hint, ghost);
      }
      ghost.style.transform = `translate(${ev.clientX + 14}px, ${ev.clientY + 14}px)`;
      target = targetAt(ev.clientX, ev.clientY, panel.id);
      hint.hidden = !target;
      if (target) {
        Object.assign(hint.style, { left: target.rect.left + "px", top: target.rect.top + "px", width: target.rect.width + "px", height: target.rect.height + "px" });
        hint.dataset.label = target.label;
      }
    };
    const finish = (commit) => {
      src.removeEventListener("pointermove", onMove); src.removeEventListener("pointerup", onUp);
      src.removeEventListener("pointercancel", onCancel); removeEventListener("keydown", onKey, true);
      if (!dragging) return;
      panel.classList.remove("dragging-panel");
      root.classList.remove("layout-dragging");
      ghost.remove(); hint.remove();
      if (commit && target && move(panel.id, target)) { render(); save(); }
    };
    const onUp = () => finish(true), onCancel = () => finish(false);
    const onKey = (ev) => { if (ev.key === "Escape" && dragging) { ev.stopPropagation(); finish(false); } };
    src.addEventListener("pointermove", onMove); src.addEventListener("pointerup", onUp); src.addEventListener("pointercancel", onCancel);
    addEventListener("keydown", onKey, true);
  }

  // Keyboard: arrows on a focused grip move the panel (up/down within a column, left/right between columns).
  function keyMove(e, panel) {
    const at = locate(panel.id);
    if (!at || stacked()) return;
    const col = state.cols[at.ci];
    let target = null;
    if (e.key === "ArrowUp" && at.pi > 0) target = { id: col.panels[at.pi - 1].id, zone: "before" };
    if (e.key === "ArrowDown" && at.pi < col.panels.length - 1) target = { id: col.panels[at.pi + 1].id, zone: "after" };
    if (e.key === "ArrowLeft" && at.ci > 0) target = { id: state.cols[at.ci - 1].panels.at(-1).id, zone: "after" };
    if (e.key === "ArrowRight" && at.ci < state.cols.length - 1) target = { id: state.cols[at.ci + 1].panels.at(-1).id, zone: "after" };
    if (!target) return;
    e.preventDefault();
    if (move(panel.id, target)) { render(); save(); panel.querySelector(".grip").focus(); }
  }

  // ---------------------------------------------------------------- init
  function init(mainEl, layoutDefaults) {
    main = mainEl;
    defaults = clone(layoutDefaults);
    panels = new Map([...main.querySelectorAll("[data-panel]")].map((p) => [p.id, p]));
    for (const p of panels.values()) {
      const grip = document.createElement("button");
      grip.type = "button"; grip.className = "grip";
      grip.setAttribute("aria-label", `Move ${title(p)}: drag, or use the arrow keys`);
      grip.title = "Drag to move";
      grip.addEventListener("pointerdown", (e) => startDrag(e, p));
      grip.addEventListener("keydown", (e) => keyMove(e, p));
      p.append(grip);
    }
    // Panel headers are handles too. Delegated, because some are re-rendered (the ledger header).
    main.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".grip")) return;
      const p = e.target.closest(".panel-head, [data-drag-handle]")?.closest("[data-panel]");
      if (p && main.contains(p)) startDrag(e, p);
    });
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
