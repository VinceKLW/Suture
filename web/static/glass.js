// Liquid glass for every `.glass` element on the page.
//
// Each panel shows a copy of the fixed background photo, aligned 1:1 with it and
// bent through an SVG displacement map computed from a squircle dome + Snell's law
// (n = 1.5), with a slight per-channel split for dispersion. A specular rim and a
// soft glare follow the pointer. Technique from julian-poke-portfolio, generalized
// to many panels (one filter + map per panel, rebuilt only when its size changes).
//
// Falls back to "lite" glass (backdrop blur + tint, no refraction) when the layout
// scrolls (narrow / short viewports) or the user prefers reduced transparency.
(function () {
  const BG = { url: "/static/night.jpg", w: 1774, h: 600, x: 0.5, y: 1.0 }; // matches .bg in suture.css
  const OVERSCAN = 24;   // .glass-refract inset, so the blur never pulls in empty edges
  const BEZEL = 26;      // rim band (px) where the bending happens
  const MAX_SHIFT = 42;  // strongest displacement at the very edge (px)
  const BLUR = 11;       // frosting: dense data sits on top, so the photo stays soft
  const isSafari = /^((?!chrome|android|crios|fxios|edg).)*safari/i.test(navigator.userAgent);
  const reducedTransparency = matchMedia("(prefers-reduced-transparency: reduce)").matches;
  const root = document.documentElement;

  const items = new Map(); // element -> state
  let rev = 0;

  function wantLite() {
    return reducedTransparency || innerWidth < 1100 || innerHeight < 640 || root.dataset.glass === "lite";
  }

  // surface tilt -> refracted bend; 0 in the flat centre, max at the rim
  function bend(x) {
    const slope = (1 - x) ** 3 / (1 - (1 - x) ** 4) ** 0.75;
    const ti = Math.atan(slope);
    const tt = Math.asin(Math.sin(ti) / 1.5);
    return Math.sin(ti - tt);
  }
  const BEND_MAX = bend(1e-6);

  function buildMap(W, H, radius) {
    const m = OVERSCAN, cw = W + 2 * m, ch = H + 2 * m;
    const canvas = document.createElement("canvas");
    canvas.width = cw; canvas.height = ch;
    const ctx = canvas.getContext("2d");
    const img = ctx.createImageData(cw, ch);
    const d = img.data;
    const hw = W / 2, hh = H / 2;
    for (let py = 0; py < ch; py++) {
      for (let px = 0; px < cw; px++) {
        const i = (py * cw + px) * 4;
        let r = 128, g = 128;
        const x = px + 0.5 - m - hw, y = py + 0.5 - m - hh;
        const qx = Math.abs(x) - (hw - radius), qy = Math.abs(y) - (hh - radius);
        const ox = Math.max(qx, 0), oy = Math.max(qy, 0);
        const sd = Math.hypot(ox, oy) + Math.min(Math.max(qx, qy), 0) - radius; // <0 inside
        const depth = -sd;
        if (depth > 0 && depth < BEZEL) {
          let nx, ny;
          if (qx > 0 && qy > 0) { const l = Math.hypot(qx, qy); nx = qx / l; ny = qy / l; }
          else if (qx > qy) { nx = 1; ny = 0; } else { nx = 0; ny = 1; }
          nx *= Math.sign(x) || 1; ny *= Math.sign(y) || 1;
          const s = bend(depth / BEZEL) / BEND_MAX;
          r = 128 - nx * s * 127;   // sample inward: the rim compresses the world behind it
          g = 128 - ny * s * 127;
        }
        d[i] = r; d[i + 1] = g; d[i + 2] = 128; d[i + 3] = 255;
      }
    }
    ctx.putImageData(img, 0, 0);
    return new Promise((res) => canvas.toBlob(res));
  }

  function decorate(el) {
    if (items.has(el)) return items.get(el);
    const refract = document.createElement("div");
    refract.className = "glass-refract"; refract.setAttribute("aria-hidden", "true");
    const tint = document.createElement("div");
    tint.className = "glass-tint"; tint.setAttribute("aria-hidden", "true");
    el.prepend(refract, tint);
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", "0"); svg.setAttribute("height", "0"); svg.setAttribute("aria-hidden", "true");
    svg.style.cssText = "position:absolute;width:0;height:0;pointer-events:none";
    document.body.appendChild(svg);
    const st = { el, refract, tint, svg, w: 0, h: 0, url: null };
    items.set(el, st);
    new ResizeObserver(() => schedule(st)).observe(el);
    return st;
  }

  // Keep the refracted copy aligned with the fixed, cover-sized background.
  function place(st) {
    const vw = innerWidth, vh = innerHeight;
    const s = Math.max(vw / BG.w, vh / BG.h);
    const cw = BG.w * s, ch = BG.h * s;
    const ox = (vw - cw) * BG.x, oy = (vh - ch) * BG.y;
    const r = st.el.getBoundingClientRect();
    st.refract.style.backgroundSize = `${cw}px ${ch}px`;
    st.refract.style.backgroundPosition = `${ox - (r.left - OVERSCAN)}px ${oy - (r.top - OVERSCAN)}px`;
  }

  async function rebuild(st) {
    if (root.classList.contains("glass-lite")) return;
    const W = Math.round(st.el.offsetWidth), H = Math.round(st.el.offsetHeight);
    if (!W || !H || (W === st.w && H === st.h)) return;
    st.w = W; st.h = H;
    const radius = parseFloat(getComputedStyle(st.el).borderTopLeftRadius) || 12;
    const blob = await buildMap(W, H, Math.min(radius, W / 2, H / 2));
    if (W !== st.w || H !== st.h) return; // superseded
    if (st.url) URL.revokeObjectURL(st.url);
    st.url = URL.createObjectURL(blob);  // blob:, not data: (WebKit won't load data: in feImage)
    const fw = W + 2 * OVERSCAN, fh = H + 2 * OVERSCAN, S = MAX_SHIFT * 2;
    const id = "liquid-glass-" + (++rev); // fresh id each rebuild, Safari caches by id
    const disp = (scale, out) =>
      `<feDisplacementMap in="soft" in2="map" scale="${scale}" xChannelSelector="R" yChannelSelector="G" result="${out}"/>`;
    const chain = isSafari ? disp(S, "out")
      : disp(S, "dr") + disp(S * 0.95, "dg") + disp(S * 0.9, "db") +
        `<feColorMatrix in="dr" type="matrix" values="1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0" result="r"/>
         <feColorMatrix in="dg" type="matrix" values="0 0 0 0 0 0 1 0 0 0 0 0 0 0 0 0 0 0 1 0" result="g"/>
         <feColorMatrix in="db" type="matrix" values="0 0 0 0 0 0 0 0 0 0 0 0 1 0 0 0 0 0 1 0" result="b"/>
         <feBlend in="r" in2="g" mode="screen" result="rg"/><feBlend in="rg" in2="b" mode="screen" result="out"/>`;
    st.svg.innerHTML =
      `<filter id="${id}" x="0" y="0" width="${fw}" height="${fh}" filterUnits="userSpaceOnUse" primitiveUnits="userSpaceOnUse" color-interpolation-filters="sRGB">
         <feImage href="${st.url}" x="0" y="0" width="${fw}" height="${fh}" preserveAspectRatio="none" result="map"/>
         <feGaussianBlur in="SourceGraphic" stdDeviation="${BLUR}" result="blurred"/>
         <feColorMatrix in="blurred" type="saturate" values="1.3" result="soft"/>
         ${chain}
       </filter>`;
    st.refract.style.filter = `url(#${id})`;
    place(st);
  }

  const queued = new Set();
  let frame = 0;
  // Line the panel up now (cheap: two style writes), and rebuild its displacement map
  // on the next frame. Every frame also re-places ALL panels, because one panel
  // growing moves its siblings without resizing them, and a moved panel would keep
  // showing the patch of photo that used to be behind it.
  function schedule(st) {
    place(st);
    queued.add(st);
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      for (const s of items.values()) place(s);
      for (const s of queued) rebuild(s);
      queued.clear();
    });
  }
  const placeAll = () => { for (const st of items.values()) place(st); };

  function applyMode() {
    const lite = wantLite();
    root.classList.toggle("glass-lite", lite);
    root.classList.toggle("glass-liquid", !lite);
    for (const st of items.values()) {
      if (lite) { st.refract.style.filter = ""; st.w = st.h = 0; } else schedule(st);
    }
  }

  function scan() { document.querySelectorAll(".glass").forEach((el) => schedule(decorate(el))); }

  // Rim light + glare follow the pointer.
  addEventListener("pointermove", (e) => {
    if (e.pointerType !== "mouse") return;
    for (const st of items.values()) {
      const r = st.el.getBoundingClientRect();
      const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      const a = Math.atan2(e.clientY - cy, e.clientX - cx) * 180 / Math.PI + 90;
      st.el.style.setProperty("--light-angle", `${a + 180}deg`);
      st.tint.style.setProperty("--glare-x", `${((e.clientX - r.left) / r.width) * 100}%`);
      st.tint.style.setProperty("--glare-y", `${((e.clientY - r.top) / r.height) * 100}%`);
    }
  }, { passive: true });

  addEventListener("resize", () => { applyMode(); for (const st of items.values()) schedule(st); });
  // Panels inside a scrolling container (the retro page's left column) move with it.
  addEventListener("scroll", placeAll, { capture: true, passive: true });
  // Frames are paused while the page is hidden (background tab, hidden pane): catch up on return.
  document.addEventListener("visibilitychange", () => { if (!document.hidden) for (const st of items.values()) schedule(st); });
  addEventListener("pageshow", placeAll);

  window.SutureGlass = { scan, applyMode };
  applyMode();
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", scan); else scan();
})();
