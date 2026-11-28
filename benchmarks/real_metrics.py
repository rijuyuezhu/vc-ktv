"""Low-frequency Web and GPU observations during real party preparation."""

import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def distribution(values):
    ordered = sorted(values)
    if not ordered:
        return {}
    return {
        "p50": round(ordered[(len(ordered) - 1) // 2], 2),
        "p95": round(ordered[int((len(ordered) - 1) * 0.95)], 2),
        "max": round(ordered[-1], 2),
    }


class RealMetrics:
    def __init__(self, clients, interval=2):
        self.clients = clients
        self.interval = interval
        self.stop_signal = threading.Event()
        self.samples = []
        self.web = []
        self.errors = []
        self.phase = "idle"
        self.started = time.monotonic()
        self.nvidia = shutil.which("nvidia-smi")
        if not self.nvidia and Path("/usr/lib/wsl/lib/nvidia-smi").is_file():
            self.nvidia = "/usr/lib/wsl/lib/nvidia-smi"
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop_signal.set()
        self.thread.join()

    @staticmethod
    def read(client, path):
        start = time.perf_counter()
        try:
            result = client.get(path, timeout=10)
            result.raise_for_status()
            assert isinstance(result.json()["queue"], list)
            return (time.perf_counter() - start) * 1000, None
        except Exception as error:
            return (time.perf_counter() - start) * 1000, type(error).__name__

    def gpu_sample(self):
        if not self.nvidia:
            return {}
        fields = (
            "utilization.gpu",
            "temperature.gpu",
            "power.draw",
            "memory.used",
            "memory.total",
        )
        try:
            result = subprocess.run(
                [
                    self.nvidia,
                    "--query-gpu=" + ",".join(fields),
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            output = {}
            for name, value in zip(fields, result.stdout.splitlines()[0].split(",")):
                try:
                    output[name] = float(value.strip())
                except ValueError:
                    pass
            return output
        except (OSError, subprocess.SubprocessError, IndexError):
            return {}

    def loop(self):
        with ThreadPoolExecutor(max_workers=64) as pool:
            while not self.stop_signal.is_set():
                phase = self.phase
                start = time.monotonic()
                # Take a snapshot: new users may join between polling rounds.
                jobs = [
                    pool.submit(self.read, client, path)
                    for client, path in list(self.clients)
                ]
                self.samples.append(
                    {
                        "t": round(start - self.started, 2),
                        "phase": phase,
                        **self.gpu_sample(),
                    }
                )
                for job in jobs:
                    elapsed, error = job.result()
                    self.web.append((phase, elapsed))
                    if error:
                        self.errors.append((phase, error))
                self.stop_signal.wait(
                    max(0, self.interval - (time.monotonic() - start))
                )

    def summary(self):
        phases = sorted({s["phase"] for s in self.samples} | {p for p, _ in self.web})
        return {
            "sample_interval_seconds": self.interval,
            "phases": {
                phase: {
                    "web_requests": sum(p == phase for p, _ in self.web),
                    "web_latency_ms": distribution(
                        [v for p, v in self.web if p == phase]
                    ),
                    "web_errors": sum(p == phase for p, _ in self.errors),
                    "gpu": {
                        key: distribution(
                            [
                                s[key]
                                for s in self.samples
                                if s["phase"] == phase and key in s
                            ]
                        )
                        for key in (
                            "utilization.gpu",
                            "temperature.gpu",
                            "power.draw",
                            "memory.used",
                        )
                    },
                }
                for phase in phases
            },
            "gpu_samples": self.samples,
            "web_errors": self.errors,
        }
