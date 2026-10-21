import { app } from "../../scripts/app.js";
const PP_VERSION = 5;

// ---------------------------------------------------------------- helpers
const urlFor = (name) => {
  if (!name) return null;
  let sub = "", fn = name;
  const i = name.lastIndexOf("/");
  if (i >= 0) { sub = name.slice(0, i); fn = name.slice(i + 1); }
  return `/view?filename=${encodeURIComponent(fn)}&subfolder=${encodeURIComponent(sub)}&type=input`;
};
const widget = (node, name) => node?.widgets?.find((w) => w.name === name);
// value of an input: follow the link to the source node's widget, else use our own widget
function linkedSourceNode(node, inputName) {
  const slot = node.inputs?.find((i) => i.name === inputName);
  if (!slot || slot.link == null) return null;
  const link = node.graph?.links?.[slot.link] ?? node.graph?.links?.get?.(slot.link);
  if (!link) return null;
  return node.graph.getNodeById(link.origin_id);
}
function fileOf(node, inputName) {
  const src = linkedSourceNode(node, inputName);
  if (!src) return null;
  const w = widget(src, "file") || widget(src, "video") || widget(src, "image");
  return w ? w.value : null;
}
function numberOf(node, name) {
  const src = linkedSourceNode(node, name);
  if (src) { const w = src.widgets?.[0]; if (w && typeof w.value === "number") return w.value; }
  const w = widget(node, name);
  return w ? Number(w.value) : 0;
}
function setWidget(node, name, v) {
  const w = widget(node, name);
  if (w && w.value !== v) { w.value = v; w.callback?.(v); }
}

// ---------------------------------------------------------------- extension
app.registerExtension({
  name: "claude.PlacementPicker",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== "PlacementPicker") return;

    const onCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      onCreated?.apply(this, arguments);
      const node = this;
      const wrap = document.createElement("div");
      wrap.style.cssText = "display:flex;flex-direction:column;gap:4px;width:100%;height:100%;";
      const canvas = document.createElement("canvas");
      canvas.style.cssText = "width:100%;flex:1;min-height:120px;background:#111;border-radius:6px;cursor:move;touch-action:none;";
      const info = document.createElement("div");
      info.style.cssText = "font:12px sans-serif;color:#ddd;text-align:center;";
      info.textContent = "Connect background + overlay, then drag the box (corner = resize)";
      wrap.append(canvas, info);
      node.addDOMWidget("placement_preview", "preview", wrap, { serialize: false, hideOnZoom: false,
        getMinHeight: () => 220 });

      const bg = document.createElement("video"); bg.muted = true; bg.crossOrigin = "anonymous"; bg.preload = "auto";
      const ov = document.createElement("video"); ov.muted = true; ov.crossOrigin = "anonymous"; ov.loop = true; ov.preload = "auto";
      const ovImg = new Image();
      const st = { bgFile: null, ovFile: null, ovKind: null, time: -1, drag: null, playing: false };
      node._pp = st;

      const ovSize = () => {
        if (st.ovKind === "video" && ov.videoWidth) return [ov.videoWidth, ov.videoHeight];
        if (st.ovKind === "image" && ovImg.naturalWidth) return [ovImg.naturalWidth, ovImg.naturalHeight];
        return [16, 9];
      };
      const geom = () => {
        const vw = bg.videoWidth || 768, vh = bg.videoHeight || 448;
        const cw = canvas.clientWidth || 300, ch = canvas.clientHeight || 180;
        const s = Math.min(cw / vw, ch / vh);
        const ox = (cw - vw * s) / 2, oy = (ch - vh * s) / 2;
        const [ow, oh] = ovSize();
        const w = widget(node, "width").value, h = Math.round(w * oh / ow);
        return { vw, vh, cw, ch, s, ox, oy, x: widget(node, "x").value, y: widget(node, "y").value, w, h };
      };
      const draw = () => {
        const dpr = window.devicePixelRatio || 1;
        const cw = canvas.clientWidth || 300, ch = canvas.clientHeight || 180;
        if (canvas.width !== Math.round(cw * dpr)) canvas.width = Math.round(cw * dpr);
        if (canvas.height !== Math.round(ch * dpr)) canvas.height = Math.round(ch * dpr);
        const c = canvas.getContext("2d"); c.setTransform(dpr, 0, 0, dpr, 0, 0);
        c.fillStyle = "#111"; c.fillRect(0, 0, cw, ch);
        const g = geom();
        if (bg.readyState >= 2) c.drawImage(bg, g.ox, g.oy, g.vw * g.s, g.vh * g.s);
        else { c.fillStyle = "#333"; c.fillRect(g.ox, g.oy, g.vw * g.s, g.vh * g.s); }
        const bx = g.ox + g.x * g.s, by = g.oy + g.y * g.s, bw = g.w * g.s, bh = g.h * g.s;
        try {
          if (st.ovKind === "video" && ov.readyState >= 2) c.drawImage(ov, bx, by, bw, bh);
          else if (st.ovKind === "image" && ovImg.complete && ovImg.naturalWidth) c.drawImage(ovImg, bx, by, bw, bh);
          else { c.fillStyle = "rgba(80,140,255,0.35)"; c.fillRect(bx, by, bw, bh); }
        } catch (e) {}
        c.strokeStyle = "#ffd400"; c.lineWidth = 2; c.strokeRect(bx, by, bw, bh);
        c.fillStyle = "#ffd400"; c.fillRect(bx + bw - 9, by + bh - 9, 14, 14);
        const t0 = numberOf(node, "time"), hasEnd = !!linkedSourceNode(node, "end"), t1 = numberOf(node, "end");
        const letter = (node.title || "").trim().charAt(0);
        const when = hasEnd ? `visible ${t0.toFixed(1)} s → ${t1.toFixed(1)} s  (change in "${letter}: start" / "${letter}: end")` : `frame at ${t0.toFixed(1)} s`;
        info.textContent = `x ${g.x}  y ${g.y}  width ${g.w}  (height ${g.h})  ·  ${when}`;
      };
      node._ppDraw = draw;
      try { new ResizeObserver(() => draw()).observe(canvas); } catch (e) {}

      const refresh = () => {
        const bf = fileOf(node, "background");
        if (bf && bf !== st.bgFile) { st.bgFile = bf; bg.src = urlFor(bf); }
        const vf = fileOf(node, "overlay_video"), imf = fileOf(node, "overlay_image");
        if (vf) { if (vf !== st.ovFile) { st.ovFile = vf; st.ovKind = "video"; ov.src = urlFor(vf); ov.play().catch(() => {}); } }
        else if (imf) { if (imf !== st.ovFile) { st.ovFile = imf; st.ovKind = "image"; ovImg.src = urlFor(imf); } }
        const t = numberOf(node, "time");
        if (bg.readyState >= 1 && Math.abs(t - st.time) > 0.01) { st.time = t; try { bg.currentTime = Math.min(t, Math.max(0, (bg.duration || t) - 0.05)); } catch (e) {} }
        draw();
      };
      node._ppRefresh = refresh;
      bg.addEventListener("loadedmetadata", () => { st.time = -1; refresh(); });
      bg.addEventListener("seeked", draw);
      ovImg.addEventListener("load", draw);
      ov.addEventListener("loadeddata", draw);
      // keep the overlay clip playing in the preview
      const tick = () => { if (!node.graph) return; if (st.ovKind === "video" && !ov.paused) draw(); requestAnimationFrame(tick); };
      requestAnimationFrame(tick);
      setInterval(() => { if (node.graph) refresh(); }, 1000);

      // ------------------------------------------------ dragging
      const toVideo = (ev) => {
        // the node may be zoomed by the graph canvas (CSS transform): convert screen px -> canvas css px first
        const r = canvas.getBoundingClientRect(); const g = geom();
        const zx = r.width / (canvas.clientWidth || r.width), zy = r.height / (canvas.clientHeight || r.height);
        return { px: ((ev.clientX - r.left) / zx - g.ox) / g.s, py: ((ev.clientY - r.top) / zy - g.oy) / g.s, g, tol: 22 / (g.s * zx) };
      };
      for (const t of ["mousedown", "wheel", "contextmenu"]) canvas.addEventListener(t, (ev) => ev.stopPropagation());
      canvas.addEventListener("pointerdown", (ev) => {
        const { px, py, g, tol } = toVideo(ev);
        const nearCorner = Math.abs(px - (g.x + g.w)) < tol && Math.abs(py - (g.y + g.h)) < tol;
        const inside = px >= g.x && px <= g.x + g.w && py >= g.y && py <= g.y + g.h;
        if (!nearCorner && !inside) {        // click elsewhere: centre the box there
          setWidget(node, "x", Math.round(Math.max(0, Math.min(g.vw - g.w, px - g.w / 2))));
          setWidget(node, "y", Math.round(Math.max(0, Math.min(g.vh - g.h, py - g.h / 2))));
          draw();
        }
        st.drag = { mode: nearCorner ? "resize" : "move", px, py, x: widget(node, "x").value, y: widget(node, "y").value, w: widget(node, "width").value };
        st.startDrag(); ev.preventDefault(); ev.stopImmediatePropagation();
      });
      const onMove = (ev) => {
        if (!st.drag) return;
        const { px, py, g } = toVideo(ev);
        const d = st.drag;
        if (d.mode === "move") {
          setWidget(node, "x", Math.round(Math.max(0, Math.min(g.vw - g.w, d.x + px - d.px))));
          setWidget(node, "y", Math.round(Math.max(0, Math.min(g.vh - g.h, d.y + py - d.py))));
        } else {
          const [ow, oh] = ovSize();
          let w = Math.round(Math.max(16, d.w + px - d.px));
          w = Math.min(w, g.vw - d.x, Math.floor((g.vh - d.y) * ow / oh));
          setWidget(node, "width", w);
        }
        node._ppLast = { mode: d.mode, w: widget(node, "width").value, x: widget(node, "x").value, y: widget(node, "y").value };
        draw(); ev.preventDefault(); ev.stopImmediatePropagation();
      };
      const onUp = (ev) => {
        if (st.drag) { st.drag = null; node.graph?.setDirtyCanvas(true, true); ev.stopImmediatePropagation(); }
        window.removeEventListener("pointermove", onMove, true);
        window.removeEventListener("pointerup", onUp, true);
        window.removeEventListener("pointercancel", onUp, true);
      };
      st.startDrag = () => {   // window-level capture: nothing in the graph editor can swallow the drag
        window.addEventListener("pointermove", onMove, true);
        window.addEventListener("pointerup", onUp, true);
        window.addEventListener("pointercancel", onUp, true);
      };
      canvas.addEventListener("pointermove", (ev) => {   // hover cursor only
        if (st.drag) return;
        const { px, py, g, tol } = toVideo(ev);
        const nearCorner = Math.abs(px - (g.x + g.w)) < tol && Math.abs(py - (g.y + g.h)) < tol;
        canvas.style.cursor = nearCorner ? "nwse-resize" : "move";
      });

      for (const n of ["x", "y", "width", "time"]) {
        const w = widget(node, n); const cb = w?.callback;
        if (w) w.callback = function () { const r = cb?.apply(this, arguments); draw(); return r; };
      }
      const baseCompute = node.computeSize;
      node.computeSize = function () {
        const s = baseCompute.apply(this, arguments);
        if (app.canvas?.resizing_node === this) return s;            // user drags the corner: allow smaller
        return [Math.max(s[0], this.size?.[0] || 0, 420), Math.max(s[1], this.size?.[1] || 0, 440)];
      };
      if (!node.size || node.size[1] < 420) node.setSize([Math.max(node.size?.[0] || 0, 420), 440]);
      setTimeout(refresh, 300);
    };

    const onConn = nodeType.prototype.onConnectionsChange;
    nodeType.prototype.onConnectionsChange = function () {
      const r = onConn?.apply(this, arguments);
      setTimeout(() => this._ppRefresh?.(), 50);
      return r;
    };
  },
});
