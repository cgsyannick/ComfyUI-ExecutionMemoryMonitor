# ComfyUI Execution Memory Monitor

Minimal global ComfyUI extension. No workflow node is required.

## What it shows

- A small pulsing green light on the node currently executing.
- A movable/collapsible memory monitor (position persisted in the browser).
- On Windows, when the native WDDM performance counters are available:
  - dedicated VRAM used on the GPU adapter selected from the ComfyUI process;
  - dedicated/shared GPU memory attributed to the ComfyUI Python process;
  - ComfyUI process GPU utilization (busiest WDDM engine).
- CUDA/accelerator-visible device memory as a separate diagnostic value.
- PyTorch tensor memory (`allocated`) and allocator cache (`reserved`).
- ComfyUI process RAM RSS/Working Set, rather than whole-system RAM usage.
- ComfyUI process CPU usage and physical disk read/write throughput.
- For each model managed by `comfy.model_management.current_loaded_models`:
  - model/loader name when provenance can be recovered;
  - active/idle state;
  - model-weight residency in VRAM;
  - model-weight residency/offload in host RAM;
  - animated V/R bars so DynamicVRAM transfers are visible.
- Core `LoraLoader` patches and their strengths when they pass through ComfyUI's standard loader.

## Install

Copy the folder `ComfyUI-ExecutionMemoryMonitor` into:

`ComfyUI/custom_nodes/`

Then restart ComfyUI and hard-refresh the browser page.

No `pip install` is required. `psutil` is already a ComfyUI dependency.

## Display semantics

- **VRAM**: on Windows, WDDM/PerfMon dedicated usage for the adapter associated with the ComfyUI process when available; otherwise the accelerator-runtime device value.
- **CUDA device**: device-wide usage visible through ComfyUI / `cudaMemGetInfo`. WDDM can legitimately report a different number.
- **Comfy GPU**: Windows GPU memory attributed specifically to the ComfyUI Python process, plus shared GPU memory when present.
- **Torch A/R**: `A` = tensors allocated by PyTorch; `R` = memory reserved by the PyTorch caching allocator.
- **RAM**: resident physical memory (RSS / Working Set) of the ComfyUI Python process. The bar uses installed RAM only as its capacity denominator; it does not display whole-system RAM consumption.
- **CPU / I/O**: process CPU usage and actual process disk read/write rate.

## Accuracy notes

The per-model V/R bars represent **model weights**, not all temporary inference allocations. Activations, latents, attention buffers, CUDA contexts and allocator caches are intentionally not assigned to individual models.

Windows WDDM and CUDA use different accounting layers. A discrepancy between the Windows **VRAM** line and **CUDA device** is therefore expected and useful diagnostic information rather than an error.

The WDDM counters are queried directly through Windows PDH (`pdh.dll`), with no pywin32/NVML dependency. If the counters are unavailable, the monitor automatically falls back to ComfyUI/PyTorch metrics.

For standard GPU->CPU offload, the per-model RAM line approximates weights managed/offloaded by ComfyUI. Memory-mapped or third-party loaders can use backing storage strategies that do not map one-for-one to process RSS.

Exact filenames are captured for ComfyUI core Checkpoint, UNET, CLIP, Dual/Triple CLIP and VAE loaders. Unsupported third-party loaders still appear using the underlying Python model class, but may not expose their original filename or LoRA provenance.
