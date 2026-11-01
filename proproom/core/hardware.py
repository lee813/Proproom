"""Hardware tier detection (handover §4.2): CUDA -> Intel GPU -> CPU."""
from __future__ import annotations

import functools
import glob
import os

from ..sdk.errors import ProproomError


@functools.cache
def detect() -> dict:
    """The tier this process started on. Detected once: a GPU that disappears later is `gpu_lost()`, not a new tier."""
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


def embed_device() -> tuple[str, str]:
    """(device, dtype) for the embedder on this tier."""
    hw = detect()
    if hw["tier"] == "nvidia":
        return "cuda", hw["devices"][0]["dtype"]
    return "cpu", "fp32"


def gpu_lost(nodes: list[str] | None = None) -> list[str]:
    """NVIDIA device nodes this process can no longer open, although it started on the nvidia tier.

    A host `systemctl daemon-reload` drops a running container's device-cgroup rules: the nodes stay listed but
    opening them fails, CUDA reports no GPU, and work would silently move to the CPU. Opening the nodes is cheap
    enough to check before every unit of work."""
    if detect()["tier"] != "nvidia":
        return []
    if nodes is None:
        nodes = ["/dev/nvidiactl", *sorted(glob.glob("/dev/nvidia[0-9]*"))]
    blocked = []
    for node in nodes:
        try:
            os.close(os.open(node, os.O_RDWR))
        except OSError:
            blocked.append(node)
    return blocked


def require_gpu() -> None:
    """Refuse to run model work on the CPU behind the user's back when the GPU this process started with is gone."""
    if blocked := gpu_lost():
        raise ProproomError("gpu_lost", f"The GPU is no longer reachable ({', '.join(blocked)})",
                            hint="restart the container; propd exits on its own so Docker restarts it",
                            retryable=True)


def load_per_core() -> float:
    """/proc/loadavg inside the container is the host's load (handover §4.3)."""
    with open("/proc/loadavg") as f:
        return float(f.read().split()[0]) / (os.cpu_count() or 1)
