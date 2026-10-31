"""Hardware tier detection (handover §4.2): CUDA -> Intel GPU -> CPU."""
from __future__ import annotations

import glob
import os


def detect() -> dict:
    info: dict = {"tier": "cpu", "devices": [], "cores": os.cpu_count()}
    try:
        import torch  # noqa: PLC0415 - optional, present in the CUDA image

        if torch.cuda.is_available():
            for i in range(torch.cuda.device_count()):
                p = torch.cuda.get_device_properties(i)
                info["devices"].append({
                    "name": p.name,
                    "sm": f"{p.major}{p.minor}",
                    "vram_mb": p.total_memory // 2**20,
                    "dtype": "bf16" if p.major >= 8 else "fp16",
                })
            if any(int(d["sm"]) >= 75 for d in info["devices"]):
                info["tier"] = "nvidia"
                return info
    except Exception as e:  # noqa: BLE001
        info["cuda_error"] = repr(e)

    if glob.glob("/dev/dri/renderD*"):
        info["tier"] = "intel"
        info["devices"].append({"render_nodes": sorted(glob.glob("/dev/dri/renderD*"))})
    return info


def load_per_core() -> float:
    """/proc/loadavg inside the container is the host's load (handover §4.3)."""
    with open("/proc/loadavg") as f:
        return float(f.read().split()[0]) / (os.cpu_count() or 1)
