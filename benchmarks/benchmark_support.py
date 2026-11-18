"""Report helpers; RSS includes native allocations, caches and the interpreter."""
import math
import os
import platform
import statistics
import subprocess
import sys


def summary(values):
    values = sorted(values)
    return {"p50": statistics.median(values),
            "p95": values[math.ceil(len(values) * .95) - 1], "max": values[-1]}


def peak_rss_bytes():
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.PeakWorkingSetSize
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


def metadata(checkout):
    from holdem.p2p import ristretto
    commit = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    dirty = bool(subprocess.check_output(["git", "-C", str(checkout), "status", "--porcelain"], text=True).strip())
    return {
        "working_tree": "dirty" if dirty else "clean",
        "commit": commit, "python": platform.python_version(),
        "platform": platform.platform(), "processor": platform.processor(),
        "cpu_count": os.cpu_count(), "libsodium": ristretto.libsodium_version(),
        "quantile_method": "nearest rank: ceil(p * sample_count)",
        "memory_scope": "OS process-lifetime peak RSS, includes native allocations and setup; run one configuration per process",
        "warmup_scope": "first hand in this process recorded separately, then warm samples; excludes interpreter/import startup",
    }
