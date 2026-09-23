"""Process heartbeat independent of an individual processing job's lease."""
import json
import os
import threading
import time
from pathlib import Path
from django.conf import settings
import psutil


class WorkerHeartbeat:
    def __init__(self):
        self.path = Path(os.getenv("BUDGET_RUNTIME_ROOT", settings.BASE_DIR / ".runtime")) / "worker-heartbeat.json"
        self.event = threading.Event()
        self.process = psutil.Process()
        self.state = "IDLE"
        self.job_id = None

    def write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps({"pid": os.getpid(), "created": self.process.create_time(),
            "heartbeat_at": time.time(), "state": self.state, "job_id": self.job_id}), encoding="utf-8")
        temporary.replace(self.path)

    def _run(self):
        while not self.event.wait(2):
            self.write()

    def __enter__(self):
        self.write()
        self.thread = threading.Thread(target=self._run, name="budget-worker-heartbeat", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.event.set()
        self.thread.join(timeout=3)
        try:
            if json.loads(self.path.read_text())["pid"] == os.getpid():
                self.path.unlink()
        except (OSError, ValueError, KeyError):
            pass


def read_worker_status(runtime_root=None):
    """Public-safe status, validated against the actual process identity and TTL."""
    root = Path(runtime_root or os.getenv("BUDGET_RUNTIME_ROOT", settings.BASE_DIR / ".runtime"))
    try:
        data = json.loads((root / "worker-heartbeat.json").read_text(encoding="utf-8"))
        process = psutil.Process(data["pid"])
        healthy = (process.is_running() and process.status() != psutil.STATUS_ZOMBIE
                   and process.create_time() == data["created"]
                   and 0 <= time.time() - data["heartbeat_at"] < 10)
        return {"healthy": healthy, "state": data.get("state", "UNKNOWN") if healthy else "UNAVAILABLE",
                "heartbeat_at": data["heartbeat_at"]}
    except (OSError, ValueError, TypeError, KeyError, psutil.Error):
        return {"healthy": False, "state": "UNAVAILABLE", "heartbeat_at": None}
