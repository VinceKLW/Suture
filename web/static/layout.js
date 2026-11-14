// Rearrangeable panel layout: resize with the gutters, drag a panel to move it,
// saved per browser.
//
//   SutureLayout.init(mainEl, defaults)
//   defaults = { cols: [{ f: <width weight>, panels: [{ id: <panel element id>, h: <height weight> }] }] }
//
// Dragging works like a phone home screen: the panel lifts off and follows the
// pointer, a placeholder holds the spot it would land in, and the other panels
// slide out of the way live (FLIP animations) as the pointer moves over them.
// Drop to keep the new arrangement, Esc to put everything back.
//
// Every child of `main` with a `data-panel` attribute is managed. Columns are flex
// boxes sized by weight, so the layout keeps its proportions when the window
// resizes. Below BREAK px wide the panels just stack in layout order. Vanilla JS,
// no build step.
(function () {
  const KEY = "suture.layout.v1";
  const MIN_W = 220, MIN_H = 120, MAX_COLS = 4, BREAK = 900, THRESHOLD = 6;
  const DWELL_MS = 130;     // pointer must rest on a new spot this long before the layout reflows
  const ANIM_MS = 260;      // slide duration; no new reflow starts while one is playing
  const LIFT_MAX = { w: 560, h: 520 };  // a big lifted panel shrinks toward the grab point to about this...
  const LIFT_MIN_SCALE = 0.6;           // ...but never below 60%, so it still reads as the panel
  const EASE = "cubic-bezier(.2,.7,.2,1)";
  const root = document.documentElement;
  const clone = (v) => JSON.parse(JSON.stringify(v));
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

  let main, defaults, panels, colEls = [];
  let state;          // committed layout (saved)
  let view;           // layout on screen: === state, or a preview while dragging
  let drag = null;    // the drag in progress, if any

  // ---------------------------------------------------------------- state
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
  const locate = (s, id) => { for (let ci = 0; ci < s.cols.length; ci++) { const pi = s.cols[ci].panels.findIndex((p) => p.id === id); if (pi >= 0) return { ci, pi }; } return null; };
  const title = (el) => el.dataset.title || el.querySelector("h2")?.textContent || el.id;
  const canNewColumn = (s, id) => s.cols.length < MAX_COLS || s.cols[locate(s, id).ci].panels.length === 1;

  // Move panel `id` next to panel target.id in layout `s` (mutates s).
  // zone: before | after (same column) or left | right (new column beside target's).
  function move(s, id, target) {
    const from = locate(s, id);
    if (!from || target.id === id) return false;
    const [item] = s.cols[from.ci].panels.splice(from.pi, 1);
    if (!s.cols[from.ci].panels.length) {           // column emptied: give its width to a neighbour
      const [gone] = s.cols.splice(from.ci, 1);
      (s.cols[from.ci] || s.cols[from.ci - 1]).f += gone.f;
    }
    const t = locate(s, target.id);
    const col = s.cols[t.ci];
    if (target.zone === "left" || target.zone === "right") {
      const f = col.f / 2;
      col.f -= f;
      s.cols.splice(t.ci + (target.zone === "right" ? 1 : 0), 0, { f, panels: [{ id, h: 1 }] });
    } else {
      item.h = col.panels.reduce((n, p) => n + p.h, 0) / col.panels.length;
      col.panels.splice(t.pi + (target.zone === "after" ? 1 : 0), 0, item);
    }
    return true;
  }

  // ---------------------------------------------------------------- render
  const nodeFor = (id) => (drag && drag.started && id === drag.id ? drag.placeholder : panels.get(id));

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
      const before = snapshot();
      pair[0][k] = pair[1][k] = total / 2;
      applySizes(); flip(before); save();
    });
    return g;
  }

  function render({ animate = false } = {}) {
    const before = animate ? snapshot() : null;
    const stack = stacked();
    root.classList.toggle("layout-on", !stack);
    root.classList.toggle("layout-stack", stack);
    const keep = new Map([...panels.values()].map((p) => [p, p.querySelector(".scroll")?.scrollTop || 0]));
    colEls = [];
    const frag = document.createDocumentFragment();
    view.cols.forEach((col, ci) => {
      if (ci && !stack) frag.append(gutter("v", ci));
      const el = document.createElement("div");
      el.className = "lcol";
      col.panels.forEach((p, pi) => {
        if (pi && !stack) el.append(gutter("h", ci, pi));
        el.append(nodeFor(p.id));
      });
      colEls.push(el);
      frag.append(el);
    });
    if (drag && drag.started) frag.append(drag.panel);   // lifted: position fixed, out of the flow
    main.replaceChildren(frag);
    for (const [p, top] of keep) { const sc = p.querySelector(".scroll"); if (sc) sc.scrollTop = top; }
    applySizes();
    if (before) flip(before);
    if (window.SutureGlass) SutureGlass.scan();  // panels moved: re-align the refracted photo
  }

  function applySizes() {
    const stack = stacked();
    view.cols.forEach((col, ci) => {
      colEls[ci].style.flex = stack ? "" : `${col.f} 1 0px`;
      col.panels.forEach((p) => { nodeFor(p.id).style.flex = stack ? "" : `${p.h} 1 0px`; });
    });
  }

  // ---------------------------------------------------------------- FLIP
  // Record where everything is, change the layout, then slide each panel from
  // its old spot to its new one. Edges that grow are revealed with a clip
  // instead of a scale, so text never stretches.
  function snapshot() {
    const m = new Map();
    for (const el of main.querySelectorAll(":scope > .lcol > [data-panel], :scope > .lcol > .drop-slot")) m.set(el, el.getBoundingClientRect());
    return m;
  }
  function flip(before) {
    if (reduced) return;
    for (const [el, a] of before) {
      if (!el.isConnected || (drag && el === drag.panel)) continue;
      const b = el.getBoundingClientRect();
      if (!b.width || !b.height) continue;
      const dx = a.left - b.left, dy = a.top - b.top;
      const cr = Math.max(0, b.width - a.width), cb = Math.max(0, b.height - a.height);
      if (Math.abs(dx) < 1 && Math.abs(dy) < 1 && cr < 1 && cb < 1) continue;
      el.getAnimations().forEach((an) => an.id === "flip" && an.cancel());
      const anim = el.animate([
        { transform: `translate(${dx}px, ${dy}px)`, clipPath: `inset(0 ${cr}px ${cb}px 0 round 12px)` },
        { transform: "translate(0, 0)", clipPath: "inset(0 0 0 0 round 12px)" },
      ], { duration: ANIM_MS, easing: EASE });
      anim.id = "flip";
      anim.onfinish = () => window.SutureGlass && SutureGlass.place(el);
    }
  }

  // ---------------------------------------------------------------- resize
  function startResize(e, g, dir, ci, pi) {
    if (e.button !== 0 || drag) return;
    e.preventDefault();
    g.setPointerCapture(e.pointerId);
    const [a, b] = dir === "v" ? [colEls[ci - 1], colEls[ci]] : [panels.get(state.cols[ci].panels[pi - 1].id), panels.get(state.cols[ci].panels[pi].id)];
    const [A, B] = dir === "v" ? [state.cols[ci - 1], state.cols[ci]] : [state.cols[ci].panels[pi - 1], state.cols[ci].panels[pi]];
    const k = dir === "v" ? "f" : "h", min = dir === "v" ? MIN_W : MIN_H;
    const size = (el) => (dir === "v" ? el.getBoundingClientRect().width : el.getBoundingClientRect().height);
    const sa = size(a), sb = size(b), total = A[k] + B[k], start = dir === "v" ? e.clientX : e.clientY;
    g.classList.add("active");
    root.classList.add("layout-resizing", dir === "v" ? "resizing-v" : "resizing-h");
    const onMove = (ev) => {
      const na = clamp(sa + (dir === "v" ? ev.clientX : ev.clientY) - start, min, sa + sb - min);
      A[k] = total * na / (sa + sb); B[k] = total - A[k];
      applySizes();
    };
    const up = () => {
      g.removeEventListener("pointermove", onMove); g.removeEventListener("pointerup", up); g.removeEventListener("pointercancel", up);
      g.classList.remove("active");
      root.classList.remove("layout-resizing", "resizing-v", "resizing-h");
      save();
    };
    g.addEventListener("pointermove", onMove); g.addEventListener("pointerup", up); g.addEventListener("pointercancel", up);
  }

  // ---------------------------------------------------------------- drag
  // Where would the panel go if dropped at (x, y)? Measured against what is on
  // screen now. Over the placeholder (or a gutter) means "stay where you are".
  function targetAt(x, y) {
    const inside = (r) => x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
    if (inside(drag.placeholder.getBoundingClientRect())) return null;
    for (const [pid, el] of panels) {
      if (pid === drag.id) continue;
      const r = el.getBoundingClientRect();
      if (!inside(r)) continue;
      const rx = (x - r.left) / r.width, ry = (y - r.top) / r.height;
      const side = rx < 0.2 ? "left" : rx > 0.8 ? "right" : null;
      if (side && canNewColumn(state, drag.id)) return { id: pid, zone: side };
      return { id: pid, zone: ry < 0.5 ? "before" : "after" };
    }
    return null;
  }

  function lift(ev) {
    const p = drag.panel, r = p.getBoundingClientRect();
    getSelection()?.removeAllRanges();  // a header drag may have started a text selection
    drag.started = true;
    drag.rect = r;
    drag.grab = { x: drag.sx - r.left, y: drag.sy - r.top };
    drag.scale = clamp(Math.min(LIFT_MAX.w / r.width, LIFT_MAX.h / r.height), LIFT_MIN_SCALE, 1);
    drag.placeholder = document.createElement("div");
    drag.placeholder.className = "drop-slot";
    drag.placeholder.innerHTML = `<span>${title(p).replace(/[&<>"']/g, "")}</span>`;
    root.classList.add("layout-dragging");
    const before = snapshot();
    render();                                   // placeholder takes the panel's slot
    Object.assign(p.style, { left: r.left + "px", top: r.top + "px", width: r.width + "px", height: r.height + "px",
                             transformOrigin: `${drag.grab.x}px ${drag.grab.y}px` });
    p.classList.add("lifted");
    p.getBoundingClientRect();                  // commit scale 1 so the shrink below eases via the CSS transition
    p.style.scale = String(drag.scale);
    flip(before);
    follow(ev);
  }

  function follow(ev) {
    drag.panel.style.translate = `${ev.clientX - drag.sx}px ${ev.clientY - drag.sy}px`;
    if (window.SutureGlass) SutureGlass.place(drag.panel);
  }

  function preview(target, key) {
    const next = clone(state);
    if (!move(next, drag.id, target)) return;
    view = next;
    drag.key = key;
    drag.busyUntil = performance.now() + ANIM_MS;
    render({ animate: true });
  }

  function onDragMove(ev) {
    if (!drag.started) {
      if (Math.hypot(ev.clientX - drag.sx, ev.clientY - drag.sy) < THRESHOLD) return;
      lift(ev);
    }
    follow(ev);
    const t = targetAt(ev.clientX, ev.clientY);
    const key = t ? `${t.id}:${t.zone}` : null;
    if (!t || key === drag.key) { clearTimeout(drag.timer); drag.pending = null; return; }
    if (key === drag.pending) return;
    drag.pending = key;
    clearTimeout(drag.timer);
    // Wait for the pointer to settle, and for the last slide to finish, so the layout never flickers.
    const wait = Math.max(DWELL_MS, drag.busyUntil - performance.now());
    drag.timer = setTimeout(() => { if (drag && drag.pending === key) { drag.pending = null; preview(t, key); } }, wait);
  }

  function endDrag(commit) {
    removeEventListener("pointermove", onDragMove);
    removeEventListener("pointerup", onUp);
    removeEventListener("pointercancel", onCancel);
    removeEventListener("keydown", onKey, true);
    const d = drag;
    if (!d) return;
    clearTimeout(d.timer);
    if (!d.started) { drag = null; return; }
    const p = d.panel, from = p.getBoundingClientRect();
    if (commit && d.key) state = view; else view = state;
    const before = snapshot();
    drag = null;
    root.classList.remove("layout-dragging");
    p.classList.remove("lifted");
    for (const k of ["left", "top", "width", "height", "translate", "scale", "transformOrigin"]) p.style[k] = "";
    render();
    flip(before);
    // Settle the lifted panel into its slot.
    const to = p.getBoundingClientRect();
    if (!reduced && to.width && to.height) {
      p.animate([
        { transformOrigin: "0 0", transform: `translate(${from.left - to.left}px, ${from.top - to.top}px) scale(${from.width / to.width}, ${from.height / to.height})` },
        { transformOrigin: "0 0", transform: "none" },
      ], { duration: ANIM_MS, easing: EASE }).onfinish = () => window.SutureGlass && SutureGlass.place(p);
    }
    if (commit && d.key) save();
  }
  const onUp = (ev) => {
    // A quick flick may be released before the reflow caught up: drop where the pointer is.
    if (drag && drag.started) {
      const t = targetAt(ev.clientX, ev.clientY), key = t && `${t.id}:${t.zone}`;
      if (t && key !== drag.key) { const next = clone(state); if (move(next, drag.id, t)) { view = next; drag.key = key; } }
    }
    endDrag(true);
  };
  const onCancel = () => endDrag(false);
  const onKey = (ev) => { if (ev.key === "Escape" && drag && drag.started) { ev.stopPropagation(); endDrag(false); } };

  function startDrag(e, panel) {
    if (e.button !== 0 || stacked() || drag) return;
    if (e.target.closest("a, button:not(.grip), input, select, textarea, label, [contenteditable]")) return;
    if (e.target.closest(".grip")) e.preventDefault();
    drag = { id: panel.id, panel, sx: e.clientX, sy: e.clientY, started: false, key: null, pending: null, timer: 0, busyUntil: 0 };
    addEventListener("pointermove", onDragMove);
    addEventListener("pointerup", onUp);
    addEventListener("pointercancel", onCancel);
    addEventListener("keydown", onKey, true);
  }

  // Keyboard: arrows on a focused grip move the panel (up/down within a column, left/right between columns).
  function keyMove(e, panel) {
    const at = locate(state, panel.id);
    if (!at || stacked() || drag) return;
    const col = state.cols[at.ci];
    let target = null;
    if (e.key === "ArrowUp" && at.pi > 0) target = { id: col.panels[at.pi - 1].id, zone: "before" };
    if (e.key === "ArrowDown" && at.pi < col.panels.length - 1) target = { id: col.panels[at.pi + 1].id, zone: "after" };
    if (e.key === "ArrowLeft" && at.ci > 0) target = { id: state.cols[at.ci - 1].panels.at(-1).id, zone: "after" };
    if (e.key === "ArrowRight" && at.ci < state.cols.length - 1) target = { id: state.cols[at.ci + 1].panels.at(-1).id, zone: "after" };
    if (!target) return;
    e.preventDefault();
    if (move(state, panel.id, target)) { view = state; render({ animate: true }); save(); panel.querySelector(".grip").focus(); }
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
      grip.addEventListener("keydown", (e) => keyMove(e, p));
      p.append(grip);
    }
    // Grips and panel headers are handles. Delegated, because some headers are re-rendered (the ledger header).
    main.addEventListener("pointerdown", (e) => {
      const p = e.target.closest(".grip, .panel-head, [data-drag-handle]")?.closest("[data-panel]");
      if (p && main.contains(p)) startDrag(e, p);
    });
    state = load();
    view = state;
    render();
    let wasStacked = stacked();
    addEventListener("resize", () => { if (stacked() !== wasStacked) { wasStacked = stacked(); render(); } });
  }

  function reset() {
    if (drag) return;
    state = clone(defaults);
    view = state;
    try { localStorage.removeItem(KEY); } catch (_) { /* ignore */ }
    render({ animate: true });
    main.dispatchEvent(new CustomEvent("layoutchange", { bubbles: true }));
  }

  window.SutureLayout = { init, reset, customized };
})();
