import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const EXT = "cg.execution-memory-monitor";
const POLL_RUNNING_MS = 300;
const POLL_IDLE_MS = 1200;
let overlay = null;
let collapsed = localStorage.getItem(`${EXT}.collapsed`) === "1";
let timer = null;
let running = false;
let animationFrame = null;
const POSITION_KEY = `${EXT}.position`;

function gb(bytes) {
  if (!Number.isFinite(bytes)) return "—";
  return `${(bytes / 1073741824).toFixed(bytes >= 10 * 1073741824 ? 1 : 2)}G`;
}

function rate(bytesPerSec) {
  if (!Number.isFinite(bytesPerSec)) return "—";
  const abs = Math.abs(bytesPerSec);
  if (abs >= 1073741824) return `${(bytesPerSec / 1073741824).toFixed(1)}G/s`;
  if (abs >= 1048576) return `${(bytesPerSec / 1048576).toFixed(1)}M/s`;
  if (abs >= 1024) return `${(bytesPerSec / 1024).toFixed(0)}K/s`;
  return `${Math.round(bytesPerSec)}B/s`;
}

function pct(part, total) {
  if (!Number.isFinite(part) || !Number.isFinite(total) || total <= 0) return 0;
  return Math.max(0, Math.min(100, (part / total) * 100));
}

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function shortName(name, max = 38) {
  const s = String(name ?? "unknown");
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

function ensureStyles() {
  if (document.getElementById("emm-style")) return;
  const style = document.createElement("style");
  style.id = "emm-style";
  style.textContent = `
    #emm-overlay{position:fixed;top:58px;right:14px;z-index:10020;width:300px;max-height:70vh;overflow:hidden;
      color:var(--fg-color,#ddd);background:color-mix(in srgb,var(--comfy-menu-bg,#202020) 94%,transparent);
      border:1px solid color-mix(in srgb,var(--border-color,#777) 60%,transparent);border-radius:8px;
      box-shadow:0 6px 18px rgba(0,0,0,.28);font:11px/1.25 ui-monospace,SFMono-Regular,Consolas,monospace;
      backdrop-filter:blur(4px);user-select:none}
    #emm-overlay .emm-head{display:flex;align-items:center;justify-content:space-between;padding:7px 9px;cursor:move;
      font-weight:650;letter-spacing:.04em;border-bottom:1px solid rgba(127,127,127,.22)}
    #emm-overlay .emm-head small{font-weight:400;opacity:.65;letter-spacing:0}
    #emm-overlay .emm-body{padding:7px 9px 9px;max-height:calc(70vh - 34px);overflow:auto}
    #emm-overlay.emm-collapsed{width:auto;min-width:154px}
    #emm-overlay.emm-collapsed .emm-body{display:none}
    #emm-overlay.emm-collapsed .emm-head{border-bottom:0}
    #emm-overlay .emm-total{display:grid;grid-template-columns:48px 1fr auto;gap:6px;align-items:center;margin:2px 0 5px}
    #emm-overlay .emm-statline{display:flex;gap:8px;justify-content:space-between;align-items:center;opacity:.76;margin:2px 0 5px;white-space:nowrap}
    #emm-overlay .emm-statline span{overflow:hidden;text-overflow:ellipsis}
    #emm-overlay .emm-model{padding:6px 0;border-top:1px solid rgba(127,127,127,.14)}
    #emm-overlay .emm-model:first-of-type{border-top:0}
    #emm-overlay .emm-name{display:flex;gap:6px;align-items:center;margin-bottom:4px;min-width:0}
    #emm-overlay .emm-dot{width:7px;height:7px;border-radius:50%;background:#777;box-shadow:0 0 0 1px rgba(255,255,255,.1) inset;flex:0 0 auto}
    #emm-overlay .emm-dot.active{background:#63df79;box-shadow:0 0 7px rgba(99,223,121,.85)}
    #emm-overlay .emm-role{opacity:.48;margin-left:auto;font-size:9px;text-transform:uppercase}
    #emm-overlay .emm-line{display:grid;grid-template-columns:12px 1fr 48px;gap:5px;align-items:center;margin:2px 0}
    #emm-overlay .emm-bar{height:5px;border-radius:4px;background:rgba(127,127,127,.22);overflow:hidden}
    #emm-overlay .emm-fill{height:100%;width:0;border-radius:4px;transition:width .28s ease}
    #emm-overlay .emm-fill.vram{background:#68a8ff}
    #emm-overlay .emm-fill.ram{background:#d7a85a}
    #emm-overlay .emm-number{text-align:right;opacity:.82}
    #emm-overlay .emm-loras{border-top:1px solid rgba(127,127,127,.18);margin-top:6px;padding-top:6px}
    #emm-overlay .emm-lora{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;opacity:.82;margin-top:2px}
    #emm-overlay .emm-muted{opacity:.55;padding:5px 0}
    #emm-overlay .emm-error{color:#ff7777;padding:5px 0}
  `;
  document.head.appendChild(style);
}

function clampOverlayPosition() {
  if (!overlay?.isConnected || !overlay.style.left) return;
  const rect = overlay.getBoundingClientRect();
  const margin = 6;
  const maxLeft = Math.max(margin, window.innerWidth - rect.width - margin);
  const maxTop = Math.max(margin, window.innerHeight - rect.height - margin);
  const left = Math.max(margin, Math.min(parseFloat(overlay.style.left) || rect.left, maxLeft));
  const top = Math.max(margin, Math.min(parseFloat(overlay.style.top) || rect.top, maxTop));
  overlay.style.left = `${left}px`;
  overlay.style.top = `${top}px`;
}

function saveOverlayPosition() {
  if (!overlay?.style.left) return;
  localStorage.setItem(POSITION_KEY, JSON.stringify({
    left: parseFloat(overlay.style.left) || 0,
    top: parseFloat(overlay.style.top) || 0,
  }));
}

function restoreOverlayPosition() {
  try {
    const saved = JSON.parse(localStorage.getItem(POSITION_KEY) || "null");
    if (!saved || !Number.isFinite(saved.left) || !Number.isFinite(saved.top)) return;
    overlay.style.right = "auto";
    overlay.style.left = `${saved.left}px`;
    overlay.style.top = `${saved.top}px`;
    requestAnimationFrame(clampOverlayPosition);
  } catch (_) {}
}

function toggleCollapsed() {
  collapsed = !collapsed;
  localStorage.setItem(`${EXT}.collapsed`, collapsed ? "1" : "0");
  overlay.classList.toggle("emm-collapsed", collapsed);
  requestAnimationFrame(() => {
    clampOverlayPosition();
    saveOverlayPosition();
  });
}

function enableOverlayDrag(head) {
  let drag = null;

  head.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    const rect = overlay.getBoundingClientRect();
    drag = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      left: rect.left,
      top: rect.top,
      moved: false,
    };
    overlay.style.right = "auto";
    overlay.style.left = `${rect.left}px`;
    overlay.style.top = `${rect.top}px`;
    head.setPointerCapture?.(event.pointerId);
    event.preventDefault();
  });

  head.addEventListener("pointermove", (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;
    if (!drag.moved && Math.hypot(dx, dy) >= 3) drag.moved = true;
    if (!drag.moved) return;

    const margin = 6;
    const rect = overlay.getBoundingClientRect();
    const maxLeft = Math.max(margin, window.innerWidth - rect.width - margin);
    const maxTop = Math.max(margin, window.innerHeight - rect.height - margin);
    const left = Math.max(margin, Math.min(drag.left + dx, maxLeft));
    const top = Math.max(margin, Math.min(drag.top + dy, maxTop));
    overlay.style.left = `${left}px`;
    overlay.style.top = `${top}px`;
  });

  const finish = (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    const wasMoved = drag.moved;
    try { head.releasePointerCapture?.(event.pointerId); } catch (_) {}
    drag = null;
    if (wasMoved) saveOverlayPosition();
    else toggleCollapsed();
  };

  head.addEventListener("pointerup", finish);
  head.addEventListener("pointercancel", (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    try { head.releasePointerCapture?.(event.pointerId); } catch (_) {}
    drag = null;
  });
}

function ensureOverlay() {
  if (overlay?.isConnected) return overlay;
  ensureStyles();
  overlay = document.createElement("div");
  overlay.id = "emm-overlay";
  overlay.classList.toggle("emm-collapsed", collapsed);
  overlay.innerHTML = `<div class="emm-head"><span>MEMORY</span><small>loading…</small></div><div class="emm-body"></div>`;
  document.body.appendChild(overlay);
  enableOverlayDrag(overlay.querySelector(".emm-head"));
  restoreOverlayPosition();
  window.addEventListener("resize", () => {
    clampOverlayPosition();
    saveOverlayPosition();
  });
  return overlay;
}

function render(data) {
  const el = ensureOverlay();
  const headSmall = el.querySelector(".emm-head small");
  const body = el.querySelector(".emm-body");

  if (!data || data.error) {
    headSmall.textContent = "error";
    body.innerHTML = `<div class="emm-error">${esc(data?.error || "No data")}</div>`;
    return;
  }

  const gpu = (data.devices || []).find(d => d.type !== "cpu" && !d.error) || (data.devices || []).find(d => !d.error);
  const proc = data.process || {};
  const win = data.windows_gpu || null;
  const osVramUsed = Number.isFinite(win?.bytes_dedicated_used) ? win.bytes_dedicated_used : null;
  const displayVramUsed = osVramUsed ?? gpu?.bytes_used;
  headSmall.textContent = gpu && Number.isFinite(displayVramUsed) ? `${gb(displayVramUsed)} / ${gb(gpu.bytes_total)}` : "CPU";

  let html = "";
  if (gpu) {
    const source = osVramUsed != null ? "Windows WDDM dedicated GPU memory" : "Accelerator runtime / cudaMemGetInfo";
    html += `<div class="emm-total" title="${esc(source)}"><span>VRAM</span><div class="emm-bar"><div class="emm-fill vram" style="width:${pct(displayVramUsed,gpu.bytes_total)}%"></div></div><span class="emm-number">${gb(displayVramUsed)}/${gb(gpu.bytes_total)}</span></div>`;

    if (osVramUsed != null && Number.isFinite(gpu.bytes_used)) {
      html += `<div class="emm-statline" title="CUDA-visible device usage. This can differ from Windows WDDM accounting."><span>CUDA device</span><b>${gb(gpu.bytes_used)}</b></div>`;
    }
    if (Number.isFinite(win?.process_bytes_dedicated)) {
      const shared = Number.isFinite(win?.process_bytes_shared) && win.process_bytes_shared > 0 ? ` + ${gb(win.process_bytes_shared)} shared` : "";
      const util = Number.isFinite(win?.process_gpu_percent) ? ` · ${win.process_gpu_percent.toFixed(0)}% GPU` : "";
      html += `<div class="emm-statline" title="Windows WDDM memory attributed to the ComfyUI Python process"><span>Comfy GPU</span><b>${gb(win.process_bytes_dedicated)}${shared}${util}</b></div>`;
    }
    if (Number.isFinite(gpu.bytes_torch_allocated) || Number.isFinite(gpu.bytes_torch_reserved)) {
      const alloc = gpu.bytes_torch_allocated || 0;
      const reserved = gpu.bytes_torch_reserved || 0;
      html += `<div class="emm-total" title="PyTorch allocated tensors / caching allocator reserved memory"><span>Torch</span><div class="emm-bar"><div class="emm-fill vram" style="width:${pct(reserved,gpu.bytes_total)}%"></div></div><span class="emm-number">A ${gb(alloc)} · R ${gb(reserved)}</span></div>`;
    }
  }

  if (Number.isFinite(proc.bytes_rss)) {
    const totalRam = proc.bytes_system_ram_total || 0;
    const privateText = Number.isFinite(proc.bytes_private) ? ` · private ${gb(proc.bytes_private)}` : "";
    html += `<div class="emm-total" title="ComfyUI Python process resident working set${esc(privateText)}"><span>RAM</span><div class="emm-bar"><div class="emm-fill ram" style="width:${pct(proc.bytes_rss,totalRam)}%"></div></div><span class="emm-number">${gb(proc.bytes_rss)}</span></div>`;
  }
  if (Number.isFinite(proc.cpu_percent) || Number.isFinite(proc.read_bytes_per_sec) || Number.isFinite(proc.write_bytes_per_sec)) {
    const cpu = Number.isFinite(proc.cpu_percent) ? `${proc.cpu_percent.toFixed(0)}% CPU` : "CPU —";
    html += `<div class="emm-statline" title="ComfyUI process CPU usage and physical disk I/O rate"><span>${cpu}</span><b>↓${rate(proc.read_bytes_per_sec)} ↑${rate(proc.write_bytes_per_sec)}</b></div>`;
  }

  const models = data.models || [];
  if (!models.length) {
    html += `<div class="emm-muted">No ComfyUI-managed model resident.</div>`;
  } else {
    for (const m of models) {
      const total = Math.max(1, m.bytes_total || 0);
      const title = `${m.name}\n${m.class_name}\nload: ${m.load_device}\noffload: ${m.offload_device}`;
      html += `<div class="emm-model" title="${esc(title)}">`;
      html += `<div class="emm-name"><span class="emm-dot ${m.active ? "active" : ""}"></span><span>${esc(shortName(m.name))}</span><span class="emm-role">${esc(m.role)}</span></div>`;
      html += `<div class="emm-line"><span>V</span><div class="emm-bar"><div class="emm-fill vram" style="width:${pct(m.bytes_vram_weights,total)}%"></div></div><span class="emm-number">${gb(m.bytes_vram_weights)}</span></div>`;
      html += `<div class="emm-line"><span>R</span><div class="emm-bar"><div class="emm-fill ram" style="width:${pct(m.bytes_ram_weights,total)}%"></div></div><span class="emm-number">${gb(m.bytes_ram_weights)}</span></div>`;
      html += `</div>`;
    }
  }

  const loras = data.loras || [];
  if (loras.length) {
    html += `<div class="emm-loras"><div style="opacity:.55">LoRA patches</div>`;
    for (const l of loras) {
      const sm = Number(l.strength_model);
      const sc = Number(l.strength_clip);
      const strengths = Number.isFinite(sm) || Number.isFinite(sc) ? ` · M:${Number.isFinite(sm) ? sm.toFixed(2) : "—"} C:${Number.isFinite(sc) ? sc.toFixed(2) : "—"}` : "";
      html += `<div class="emm-lora" title="${esc(l.name)}">✓ ${esc(shortName(l.name, 34))}${strengths}</div>`;
    }
    html += `</div>`;
  }

  body.innerHTML = html;
}

async function poll() {
  try {
    const response = await api.fetchApi("/execution-monitor/state", { cache: "no-store" });
    render(await response.json());
  } catch (err) {
    render({ error: err?.message || String(err) });
  } finally {
    clearTimeout(timer);
    timer = setTimeout(poll, running ? POLL_RUNNING_MS : POLL_IDLE_MS);
  }
}

function startCanvasPulse() {
  if (animationFrame != null) return;
  const tick = () => {
    if (!running) {
      animationFrame = null;
      return;
    }
    try { app.canvas?.setDirty?.(true, false); } catch (_) {}
    animationFrame = requestAnimationFrame(tick);
  };
  animationFrame = requestAnimationFrame(tick);
}

function updateRunning() {
  running = app.runningNodeId != null;
  if (running) startCanvasPulse();
}

app.registerExtension({
  name: EXT,

  async setup() {
    ensureOverlay();
    api.addEventListener("executing", () => {
      updateRunning();
      clearTimeout(timer);
      timer = setTimeout(poll, 0);
    });
    api.addEventListener("execution_start", () => {
      running = true;
      startCanvasPulse();
    });
    for (const eventName of ["execution_success", "execution_error", "execution_interrupted"]) {
      api.addEventListener(eventName, () => {
        running = false;
        clearTimeout(timer);
        timer = setTimeout(poll, 0);
      });
    }
    poll();
  },

  async beforeRegisterNodeDef(nodeType) {
    const previous = nodeType.prototype.onDrawForeground;
    nodeType.prototype.onDrawForeground = function(ctx) {
      const result = previous?.apply(this, arguments);
      try {
        const activeId = app.runningNodeId;
        if (activeId != null && String(activeId) === String(this.id)) {
          const t = performance.now() / 350;
          const pulse = 0.65 + 0.35 * (0.5 + 0.5 * Math.sin(t));
          ctx.save();
          ctx.beginPath();
          ctx.arc(this.size[0] - 11, 11, 4.5 + pulse, 0, Math.PI * 2);
          ctx.fillStyle = `rgba(100, 235, 125, ${0.72 + pulse * 0.2})`;
          ctx.shadowColor = "rgba(100,235,125,.95)";
          ctx.shadowBlur = 7 + pulse * 4;
          ctx.fill();
          ctx.restore();
        }
      } catch (_) {}
      return result;
    };
  },
});
