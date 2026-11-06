"""Background jobs for the UI: pipeline runs (subprocess, cancellable) and downloads (thread).

Video runs are queued and run one at a time (each one already uses the whole GPU / CPU / model quota);
downloads start right away and run in parallel."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..progress import PREFIX

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
STAGE_WEIGHTS = {"panels": 5, "read": 35, "script": 12, "voice": 13, "render": 35}
STAGE_ORDER = ["panels", "read", "script", "voice", "render"]


@dataclass
class Job:
    id: str
    kind: str  # "pipeline" | "download"
    project: str
    title: str
    status: str = "queued"  # queued | running | done | failed | cancelled
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    log: list[str] = field(default_factory=list)
    stages: dict[str, dict] = field(default_factory=dict)  # stage -> {done,total,msg}
    current: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    then: Callable[[], None] | None = None
    position: int | None = None  # place in the video queue while queued (1 = next)
    _start: Callable[[], None] | None = None
    _proc: subprocess.Popen | None = None
    _cancel: threading.Event = field(default_factory=threading.Event)

    def overall(self) -> float:
        if self.status == "done":
            return 1.0
        if self.kind == "download":
            st = self.stages.get("download") or {}
            return (st.get("done") or 0) / max(st.get("total") or 1, 1)
        total = sum(STAGE_WEIGHTS.values())
        got = 0.0
        for stage, w in STAGE_WEIGHTS.items():
            st = self.stages.get(stage)
            if not st:
                continue
            frac = (st.get("done") or 0) / st["total"] if st.get("total") else 0.0
            got += w * min(frac, 1.0)
        return got / total

    def view(self, since: int = 0) -> dict:
        return {
            "id": self.id, "kind": self.kind, "project": self.project, "title": self.title, "status": self.status,
            "created": self.created, "started": self.started, "finished": self.finished, "current": self.current,
            "stages": self.stages, "progress": round(self.overall(), 4), "result": self.result, "error": self.error,
            "log": self.log[since:], "log_size": len(self.log), "position": self.position,
        }  # fmt: skip


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._queue: list[Job] = []  # pipeline jobs waiting for their turn
        self._running: Job | None = None  # the pipeline job using the machine

    def list(self) -> list[dict]:
        return [j.view(len(j.log)) | {"log": []} for j in sorted(self.jobs.values(), key=lambda j: -j.created)]

    def get(self, job_id: str) -> Job:
        return self.jobs[job_id]

    def active_for(self, project: str) -> Job | None:
        return next((j for j in self.jobs.values() if j.project == project and j.status in ("queued", "running")), None)

    def _new(self, kind: str, project: str, title: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:10], kind=kind, project=project, title=title)
        with self._lock:
            self.jobs[job.id] = job
        return job

    # ------------------------------------------------------------- pipeline
    def run_pipeline(self, project: str, project_dir: Path, until: str, force: list[str], preview: bool, output: str | None,
                     quick: bool = False, chapters: str | None = None) -> Job:
        if self.active_for(project):
            raise RuntimeError("this project already has a video queued or running")
        title = "Quick preview" if quick else (f"Make video ({until})" if until != "render" else "Make video")
        if chapters:
            title += f" · ch {chapters}"
        job = self._new("pipeline", project, title)
        args = [sys.executable, "-m", "recaper", "run", str(project_dir), "--until", until]
        for f in force:
            args += ["--force", f]
        if preview:
            args.append("--preview")
        if quick:
            args.append("--quick")
        if chapters:
            args += ["--chapters", chapters]
        if output:
            args += ["--out", str(project_dir / output)]
        job._start = lambda: threading.Thread(target=self._run_subprocess, args=(job, args, project_dir), daemon=True).start()
        with self._lock:
            self._queue.append(job)
        self._pump()
        return job

    def _pump(self) -> None:
        """Start the next queued video run when none is running; keep queue positions current."""
        with self._lock:
            self._queue = [j for j in self._queue if j.status == "queued"]
            if self._running is not None and self._running.status not in ("queued", "running"):
                self._running = None
            if self._running is None and self._queue:
                job = self._running = self._queue.pop(0)
                job.status, job.position = "running", None
                assert job._start is not None
                job._start()
            for i, j in enumerate(self._queue, start=1):
                j.position = i

    def _run_subprocess(self, job: Job, args: list[str], cwd: Path) -> None:
        env = {**os.environ, "RECAPER_PROGRESS": "1", "PYTHONIOENCODING": "utf-8", "COLUMNS": "140", "PYTHONUNBUFFERED": "1"}
        job.started = time.time()
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        try:
            if job._cancel.is_set():  # stopped between leaving the queue and starting
                job.status = "cancelled"
                return
            proc = subprocess.Popen(
                args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=flags,
                start_new_session=os.name != "nt",
            )  # fmt: skip
            job._proc = proc
            assert proc.stdout is not None
            for line in proc.stdout:
                self._ingest(job, line.rstrip("\n"))
            code = proc.wait()
            if job._cancel.is_set():
                job.status = "cancelled"
            elif code == 0:
                job.status = "done"
            else:
                job.status = "failed"
                job.error = next((l for l in reversed(job.log) if l.strip() and not l.startswith(("│", "└", "┌"))), f"exit code {code}")
        except Exception as e:  # noqa: BLE001
            job.status, job.error = "failed", str(e)
        finally:
            job.finished = time.time()
            self._pump()

    def _ingest(self, job: Job, line: str) -> None:
        if line.startswith(PREFIX):
            try:
                ev = json.loads(line[len(PREFIX) :])
            except json.JSONDecodeError:
                return
            stage = ev.get("stage")
            if stage == "done":
                job.result["output"] = ev.get("output")
                job.result.setdefault("outputs", []).append(ev.get("output"))
                return
            job.current = stage
            job.stages[stage] = {"done": ev.get("done"), "total": ev.get("total"), "msg": ev.get("msg")}
            # stages before the current one are complete (they may have been fully cached)
            if stage in STAGE_ORDER:
                for prev in STAGE_ORDER[: STAGE_ORDER.index(stage)]:
                    st = job.stages.setdefault(prev, {"done": 1, "total": 1, "msg": "cached"})
                    if st.get("total"):
                        st["done"] = st["total"]
            return
        clean = ANSI.sub("", line)
        if clean.strip():
            job.log.append(clean)
            if len(job.log) > 5000:
                del job.log[:1000]

    # ------------------------------------------------------------- download
    def run_download(self, project: str, title: str, work: Callable[[Job], dict], then: Callable[[], None] | None = None) -> Job:
        job = self._new("download", project, title)
        job.then = then

        def runner() -> None:
            job.status, job.started = "running", time.time()
            try:
                job.result = work(job)
                if job._cancel.is_set():
                    job.status = "cancelled"
                else:
                    failed = job.result.get("failed") or []
                    job.status = "done" if not failed or job.result.get("downloaded") else "failed"
                    if failed:
                        job.log.append(f"{len(failed)} chapter(s) failed: " + "; ".join(f"ch {f['chapter']}: {f['error']}" for f in failed[:5]))
                        job.error = job.error or (None if job.status == "done" else job.log[-1])
            except Exception as e:  # noqa: BLE001
                job.status, job.error = "failed", str(e)
                job.log.append(f"error: {e}")
            finally:
                job.finished = time.time()
            if job.status == "done" and job.then:
                try:
                    job.then()
                except Exception as e:  # noqa: BLE001
                    job.log.append(f"could not start the video job: {e}")

        threading.Thread(target=runner, daemon=True).start()
        return job

    # --------------------------------------------------------------- cancel
    def cancel(self, job_id: str) -> None:
        job = self.jobs[job_id]
        job._cancel.set()
        proc = job._proc
        if proc and proc.poll() is None:
            if os.name == "nt":  # kill the whole tree (render/voice worker processes too)
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
            else:
                import signal

                os.killpg(proc.pid, signal.SIGTERM)
        if job.status == "queued":
            job.status, job.position, job.finished = "cancelled", None, time.time()
            self._pump()
