from __future__ import annotations

import functools
import logging
import os
import re
import time
from typing import Any

import psutil

from aiohttp import web

import comfy.model_management as mm
from server import PromptServer

WEB_DIRECTORY = "./web"
NODE_CLASS_MAPPINGS = {}

_LOG = logging.getLogger("ExecutionMemoryMonitor")
_PATCHED = False
_GPU_TYPES = {"cuda", "xpu", "mps", "npu", "mlu", "privateuseone"}


_PROCESS = psutil.Process(os.getpid())
try:
    _PROCESS.cpu_percent(None)  # prime non-blocking CPU sampling
except Exception:
    pass
_IO_LAST = None


class _WindowsGpuPerf:
    """Lightweight Windows PDH probe used to mirror Task Manager/WDDM counters.

    It intentionally has no external dependency (no pynvml/pywin32). If the
    counters are unavailable the extension simply falls back to CUDA/PyTorch.
    """

    PDH_FMT_DOUBLE = 0x00000200
    PDH_FMT_LARGE = 0x00000400

    def __init__(self):
        self.ok = False
        self.query = None
        self.counters = {}
        self._last_sample_time = 0.0
        self._last_sample = None
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            self.ctypes = ctypes
            self.wintypes = wintypes
            self.pdh = ctypes.WinDLL("pdh.dll")
            self.pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
            self.pdh.PdhOpenQueryW.restype = wintypes.DWORD
            self.pdh.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
            self.pdh.PdhAddEnglishCounterW.restype = wintypes.DWORD
            self.pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
            self.pdh.PdhCollectQueryData.restype = wintypes.DWORD
            self.pdh.PdhGetFormattedCounterArrayW.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
            self.pdh.PdhGetFormattedCounterArrayW.restype = wintypes.DWORD
            self.pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
            self.pdh.PdhCloseQuery.restype = wintypes.DWORD

            class _LargeValue(ctypes.Structure):
                _fields_ = [("CStatus", wintypes.DWORD), ("value", ctypes.c_longlong)]

            class _DoubleValue(ctypes.Structure):
                _fields_ = [("CStatus", wintypes.DWORD), ("value", ctypes.c_double)]

            class _LargeItem(ctypes.Structure):
                _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", _LargeValue)]

            class _DoubleItem(ctypes.Structure):
                _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", _DoubleValue)]

            self._LargeItem = _LargeItem
            self._DoubleItem = _DoubleItem

            q = ctypes.c_void_p()
            if self.pdh.PdhOpenQueryW(None, 0, ctypes.byref(q)) != 0:
                return
            self.query = q

            paths = {
                "adapter_dedicated": r"\GPU Adapter Memory(*)\Dedicated Usage",
                "adapter_shared": r"\GPU Adapter Memory(*)\Shared Usage",
                "process_dedicated": r"\GPU Process Memory(*)\Dedicated Usage",
                "process_shared": r"\GPU Process Memory(*)\Shared Usage",
                "engine_util": r"\GPU Engine(*)\Utilization Percentage",
            }
            for key, path in paths.items():
                h = ctypes.c_void_p()
                if self.pdh.PdhAddEnglishCounterW(self.query, path, 0, ctypes.byref(h)) == 0:
                    self.counters[key] = h
            if not self.counters:
                self.close()
                return
            self.pdh.PdhCollectQueryData(self.query)
            self.ok = True
        except Exception as exc:
            _LOG.debug("Windows GPU counters unavailable: %s", exc)
            self.close()

    def close(self):
        try:
            if self.query is not None and getattr(self, "pdh", None) is not None:
                self.pdh.PdhCloseQuery(self.query)
        except Exception:
            pass
        self.query = None
        self.ok = False

    def _array(self, key: str, fmt: int, item_type):
        h = self.counters.get(key)
        if not h:
            return []
        ctypes = self.ctypes
        wintypes = self.wintypes
        size = wintypes.DWORD(0)
        count = wintypes.DWORD(0)
        # First call returns the required byte count for wildcard counters.
        self.pdh.PdhGetFormattedCounterArrayW(h, fmt, ctypes.byref(size), ctypes.byref(count), None)
        if not size.value or not count.value:
            return []
        buf = ctypes.create_string_buffer(size.value)
        rc = self.pdh.PdhGetFormattedCounterArrayW(h, fmt, ctypes.byref(size), ctypes.byref(count), ctypes.cast(buf, ctypes.c_void_p))
        if rc != 0:
            return []
        items = ctypes.cast(buf, ctypes.POINTER(item_type))
        out = []
        for i in range(count.value):
            item = items[i]
            try:
                if int(item.FmtValue.CStatus) in (0, 1):
                    out.append((str(item.szName or ""), item.FmtValue.value))
            except Exception:
                continue
        return out

    @staticmethod
    def _adapter_key(name: str) -> str | None:
        m = re.search(r"(luid_[^_]+_[^_]+_phys_\d+)", name.lower())
        return m.group(1) if m else None

    def sample(self, pid: int) -> dict | None:
        if not self.ok:
            return None
        now = time.monotonic()
        if self._last_sample is not None and now - self._last_sample_time < 0.5:
            return self._last_sample
        try:
            if self.pdh.PdhCollectQueryData(self.query) != 0:
                return self._last_sample
            ad = self._array("adapter_dedicated", self.PDH_FMT_LARGE, self._LargeItem)
            ash = self._array("adapter_shared", self.PDH_FMT_LARGE, self._LargeItem)
            pd = self._array("process_dedicated", self.PDH_FMT_LARGE, self._LargeItem)
            psh = self._array("process_shared", self.PDH_FMT_LARGE, self._LargeItem)
            eu = self._array("engine_util", self.PDH_FMT_DOUBLE, self._DoubleItem)

            needle = f"pid_{pid}_"
            proc_ded = [(n, max(0, int(v))) for n, v in pd if needle in n.lower()]
            proc_sh = [(n, max(0, int(v))) for n, v in psh if needle in n.lower()]
            proc_util = [max(0.0, float(v)) for n, v in eu if needle in n.lower()]

            proc_by_adapter = {}
            for n, v in proc_ded:
                k = self._adapter_key(n)
                if k:
                    proc_by_adapter[k] = proc_by_adapter.get(k, 0) + v

            adapter_ded = {}
            for n, v in ad:
                k = self._adapter_key(n) or n.lower()
                adapter_ded[k] = adapter_ded.get(k, 0) + max(0, int(v))
            adapter_sh = {}
            for n, v in ash:
                k = self._adapter_key(n) or n.lower()
                adapter_sh[k] = adapter_sh.get(k, 0) + max(0, int(v))

            selected = None
            if proc_by_adapter:
                selected = max(proc_by_adapter, key=proc_by_adapter.get)
            elif adapter_ded:
                selected = max(adapter_ded, key=adapter_ded.get)

            if selected is not None and selected not in adapter_ded and adapter_ded:
                selected = max(adapter_ded, key=adapter_ded.get)

            sample = {
                "source": "windows_pdh",
                "adapter": selected,
                "bytes_dedicated_used": adapter_ded.get(selected) if selected else None,
                "bytes_shared_used": adapter_sh.get(selected) if selected else None,
                "process_bytes_dedicated": sum(v for _, v in proc_ded),
                "process_bytes_shared": sum(v for _, v in proc_sh),
                # Task Manager effectively surfaces the busiest engine for its single GPU % column.
                "process_gpu_percent": min(100.0, max(proc_util)) if proc_util else None,
            }
            self._last_sample_time = now
            self._last_sample = sample
            return sample
        except Exception as exc:
            _LOG.debug("Windows GPU PDH sample failed: %s", exc)
            return None


_WINDOWS_GPU = _WindowsGpuPerf()


def _device_type(device: Any) -> str:
    return str(getattr(device, "type", device or "unknown")).lower()


def _human_device(device: Any) -> str:
    try:
        return str(device)
    except Exception:
        return "unknown"


def _patcher_from(obj: Any):
    if obj is None:
        return None
    if hasattr(obj, "model_size") and hasattr(obj, "loaded_size"):
        return obj
    patcher = getattr(obj, "patcher", None)
    if patcher is not None and hasattr(patcher, "model_size"):
        return patcher
    return None


def _set_meta(obj: Any, *, label: str | None = None, role: str | None = None, lora: dict | None = None):
    patcher = _patcher_from(obj)
    if patcher is None:
        return
    try:
        if label:
            setattr(patcher, "_emm_label", label)
        if role:
            setattr(patcher, "_emm_role", role)
        if lora:
            current = list(getattr(patcher, "_emm_loras", []))
            key = (lora.get("name"), lora.get("strength_model"), lora.get("strength_clip"))
            if key not in {(x.get("name"), x.get("strength_model"), x.get("strength_clip")) for x in current}:
                current.append(dict(lora))
            setattr(patcher, "_emm_loras", current)
    except Exception:
        pass


def _copy_meta(src: Any, dst: Any):
    src_p = _patcher_from(src)
    dst_p = _patcher_from(dst)
    if src_p is None or dst_p is None:
        return
    for attr in ("_emm_label", "_emm_role", "_emm_loras"):
        if hasattr(src_p, attr):
            try:
                value = getattr(src_p, attr)
                setattr(dst_p, attr, list(value) if attr == "_emm_loras" else value)
            except Exception:
                pass


def _wrap_loader(cls, method_name: str, callback):
    original = getattr(cls, method_name, None)
    if original is None or getattr(original, "_emm_wrapped", False):
        return

    @functools.wraps(original)
    def wrapped(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        try:
            callback(args, kwargs, result)
        except Exception as exc:
            _LOG.debug("Metadata hook failed for %s.%s: %s", cls.__name__, method_name, exc)
        return result

    wrapped._emm_wrapped = True
    setattr(cls, method_name, wrapped)


def _install_provenance_hooks():
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    try:
        import nodes
    except Exception as exc:
        _LOG.warning("Could not import core nodes for provenance labels: %s", exc)
        return

    if hasattr(nodes, "UNETLoader"):
        def cb_unet(args, kwargs, out):
            name = kwargs.get("unet_name", args[0] if args else None)
            if out:
                _set_meta(out[0], label=str(name), role="diffusion")
        _wrap_loader(nodes.UNETLoader, "load_unet", cb_unet)

    if hasattr(nodes, "CLIPLoader"):
        def cb_clip(args, kwargs, out):
            name = kwargs.get("clip_name", args[0] if args else None)
            if out:
                _set_meta(out[0], label=str(name), role="text_encoder")
        _wrap_loader(nodes.CLIPLoader, "load_clip", cb_clip)

    if hasattr(nodes, "DualCLIPLoader"):
        def cb_dual(args, kwargs, out):
            n1 = kwargs.get("clip_name1", args[0] if len(args) > 0 else None)
            n2 = kwargs.get("clip_name2", args[1] if len(args) > 1 else None)
            if out:
                _set_meta(out[0], label=f"{n1} + {n2}", role="text_encoder")
        _wrap_loader(nodes.DualCLIPLoader, "load_clip", cb_dual)

    if hasattr(nodes, "TripleCLIPLoader"):
        def cb_triple(args, kwargs, out):
            names = [kwargs.get(f"clip_name{i}", args[i-1] if len(args) >= i else None) for i in (1, 2, 3)]
            if out:
                _set_meta(out[0], label=" + ".join(str(x) for x in names if x), role="text_encoder")
        _wrap_loader(nodes.TripleCLIPLoader, "load_clip", cb_triple)

    if hasattr(nodes, "VAELoader"):
        def cb_vae(args, kwargs, out):
            name = kwargs.get("vae_name", args[0] if args else None)
            if out:
                _set_meta(out[0], label=str(name), role="vae")
        _wrap_loader(nodes.VAELoader, "load_vae", cb_vae)

    if hasattr(nodes, "CheckpointLoaderSimple"):
        def cb_ckpt(args, kwargs, out):
            name = kwargs.get("ckpt_name", args[0] if args else None)
            if not out:
                return
            if len(out) > 0:
                _set_meta(out[0], label=str(name), role="diffusion")
            if len(out) > 1:
                _set_meta(out[1], label=f"{name} · CLIP", role="text_encoder")
            if len(out) > 2:
                _set_meta(out[2], label=f"{name} · VAE", role="vae")
        _wrap_loader(nodes.CheckpointLoaderSimple, "load_checkpoint", cb_ckpt)

    if hasattr(nodes, "LoraLoader"):
        original = getattr(nodes.LoraLoader, "load_lora", None)
        if original is not None and not getattr(original, "_emm_wrapped", False):
            @functools.wraps(original)
            def wrapped_lora(self, model, clip, lora_name, strength_model, strength_clip):
                result = original(self, model, clip, lora_name, strength_model, strength_clip)
                try:
                    lora = {
                        "name": str(lora_name),
                        "strength_model": float(strength_model),
                        "strength_clip": float(strength_clip),
                    }
                    if result and len(result) > 0 and result[0] is not None:
                        _copy_meta(model, result[0])
                        _set_meta(result[0], lora=lora)
                    if result and len(result) > 1 and result[1] is not None:
                        _copy_meta(clip, result[1])
                        _set_meta(result[1], lora=lora)
                except Exception as exc:
                    _LOG.debug("LoRA metadata hook failed: %s", exc)
                return result
            wrapped_lora._emm_wrapped = True
            nodes.LoraLoader.load_lora = wrapped_lora

    _LOG.info("Execution Memory Monitor provenance hooks installed")


def _fallback_role(patcher: Any, model_obj: Any) -> str:
    try:
        if getattr(patcher, "is_clip", False):
            return "text_encoder"
    except Exception:
        pass
    name = type(model_obj).__name__.lower()
    if "vae" in name or "autoencoder" in name:
        return "vae"
    if "clip" in name or "t5" in name or "qwen" in name and "vl" in name:
        return "text_encoder"
    return "model"


def _model_row(loaded: Any, index: int) -> dict:
    patcher = loaded.model
    if patcher is None:
        raise RuntimeError("dead model")
    model_obj = getattr(patcher, "model", None)

    total = max(0, int(loaded.model_memory()))
    resident = max(0, min(total, int(loaded.model_loaded_memory())))
    offloaded = max(0, total - resident)

    load_device = getattr(patcher, "load_device", getattr(loaded, "device", None))
    offload_device = getattr(patcher, "offload_device", None)
    load_type = _device_type(load_device)
    offload_type = _device_type(offload_device)

    vram = resident if load_type in _GPU_TYPES else 0
    ram = resident if load_type == "cpu" else 0
    if offload_type == "cpu":
        ram += offloaded

    label = getattr(patcher, "_emm_label", None)
    if not label:
        label = type(model_obj).__name__ if model_obj is not None else type(patcher).__name__

    role = getattr(patcher, "_emm_role", None) or _fallback_role(patcher, model_obj)
    loras = list(getattr(patcher, "_emm_loras", []))

    return {
        "id": f"{id(patcher):x}",
        "index": index,
        "name": str(label),
        "class_name": type(model_obj).__name__ if model_obj is not None else type(patcher).__name__,
        "role": role,
        "active": bool(getattr(loaded, "currently_used", False)),
        "dynamic": bool(getattr(patcher, "is_dynamic", lambda: False)()),
        "load_device": _human_device(load_device),
        "offload_device": _human_device(offload_device),
        "bytes_total": total,
        "bytes_resident": resident,
        "bytes_offloaded": offloaded,
        "bytes_vram_weights": vram,
        "bytes_ram_weights": ram,
        "loras": loras,
    }


def _device_rows() -> list[dict]:
    rows = []
    try:
        devices = mm.get_all_torch_devices()
        primary = mm.get_torch_device()
        if primary in devices:
            devices = [primary] + [d for d in devices if d != primary]
        else:
            devices = [primary] + list(devices)
    except Exception:
        devices = [mm.get_torch_device()]

    seen = set()
    for device in devices:
        key = str(device)
        if key in seen:
            continue
        seen.add(key)
        try:
            total, torch_total = mm.get_total_memory(device, torch_total_too=True)
            free, torch_free = mm.get_free_memory(device, torch_free_too=True)
            row = {
                "name": mm.get_torch_device_name(device),
                "device": str(device),
                "type": _device_type(device),
                # Device-wide memory visible to the accelerator runtime.
                "bytes_total": int(total),
                "bytes_free": int(free),
                "bytes_used": max(0, int(total - free)),
                # ComfyUI's allocator-reserved figures.
                "bytes_torch_total": int(torch_total),
                "bytes_torch_free": int(torch_free),
            }
            if _device_type(device) == "cuda":
                try:
                    stats = mm.torch.cuda.memory_stats(device)
                    row.update({
                        "bytes_torch_allocated": int(stats.get("allocated_bytes.all.current", 0)),
                        "bytes_torch_reserved": int(stats.get("reserved_bytes.all.current", 0)),
                        "bytes_torch_active": int(stats.get("active_bytes.all.current", 0)),
                        "bytes_torch_peak_allocated": int(stats.get("allocated_bytes.all.peak", 0)),
                    })
                except Exception:
                    pass
            rows.append(row)
        except Exception as exc:
            rows.append({"name": key, "device": key, "type": _device_type(device), "error": str(exc)})
    return rows


def _process_stats() -> dict:
    global _IO_LAST
    out = {"pid": os.getpid()}
    try:
        out["bytes_system_ram_total"] = int(psutil.virtual_memory().total)
    except Exception:
        pass
    try:
        mem = _PROCESS.memory_info()
        out["bytes_rss"] = int(mem.rss)
        out["bytes_vms"] = int(mem.vms)
        if hasattr(mem, "private"):
            out["bytes_private"] = int(mem.private)
        if hasattr(mem, "peak_wset"):
            out["bytes_peak_rss"] = int(mem.peak_wset)
    except Exception:
        pass
    try:
        out["cpu_percent"] = float(_PROCESS.cpu_percent(None))
        out["threads"] = int(_PROCESS.num_threads())
    except Exception:
        pass
    try:
        io = _PROCESS.io_counters()
        now = time.monotonic()
        current = (now, int(io.read_bytes), int(io.write_bytes))
        if _IO_LAST is not None:
            dt = max(1e-3, now - _IO_LAST[0])
            out["read_bytes_per_sec"] = max(0.0, (current[1] - _IO_LAST[1]) / dt)
            out["write_bytes_per_sec"] = max(0.0, (current[2] - _IO_LAST[2]) / dt)
        else:
            out["read_bytes_per_sec"] = 0.0
            out["write_bytes_per_sec"] = 0.0
        out["read_bytes_total"] = current[1]
        out["write_bytes_total"] = current[2]
        _IO_LAST = current
    except Exception:
        pass
    return out


def _snapshot() -> dict:
    process = _process_stats()
    windows_gpu = _WINDOWS_GPU.sample(os.getpid()) if _WINDOWS_GPU.ok else None

    models = []
    # Snapshot the references first. current_loaded_models can change while ComfyUI
    # offloads models, so every row is independently guarded.
    for idx, loaded in enumerate(list(mm.current_loaded_models)):
        try:
            if loaded is None or loaded.is_dead():
                continue
            models.append(_model_row(loaded, idx))
        except Exception as exc:
            _LOG.debug("Skipping model during concurrent memory update: %s", exc)

    unique_loras = []
    seen = set()
    for row in models:
        for lora in row.get("loras", []):
            key = (lora.get("name"), lora.get("strength_model"), lora.get("strength_clip"))
            if key not in seen:
                seen.add(key)
                unique_loras.append(lora)

    return {
        "process": process,
        "windows_gpu": windows_gpu,
        "devices": _device_rows(),
        "models": models,
        "loras": unique_loras,
    }


@PromptServer.instance.routes.get("/execution-monitor/state")
async def execution_monitor_state(request):
    try:
        return web.json_response(_snapshot())
    except Exception as exc:
        _LOG.exception("Could not build memory snapshot")
        return web.json_response({"error": str(exc)}, status=500)


_install_provenance_hooks()

__all__ = ["NODE_CLASS_MAPPINGS", "WEB_DIRECTORY"]
