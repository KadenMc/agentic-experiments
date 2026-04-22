"""Local-only tracker adapter. Always available.

Writes a JSONL event log into the job's workspace:
``<workspace>/tracker_log/events.jsonl``. Good for offline runs, test
fixtures, and as the default before a user opts in to a remote backend.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aexp.schema import iso_utc_now
from aexp.trackers.base import (
    RunHandle,
    RunRecord,
    TrackerAdapter,
)


class NoopAdapter(TrackerAdapter):
    """Write tracker events to a local JSONL file; never reach out to a network."""

    name = "noop"

    def __init__(self, log_root: Path | None = None) -> None:
        """Parameters
        ----------
        log_root : Path | None
            If given, JSONL files go under ``<log_root>/<run_id>.jsonl``
            instead of the job workspace. Handy for tests that don't have
            a real signac workspace yet. Default: write into the job
            workspace discovered via ``handle.extra["workspace"]``.
        """
        self.log_root = Path(log_root) if log_root is not None else None

    # -----------------------------------------------------------------------
    # TrackerAdapter interface
    # -----------------------------------------------------------------------

    def init_run(
        self,
        *,
        project: str,
        group: str,
        tags: list[str],
        config: dict[str, Any],
        notes: str | None = None,
        offline: bool = False,
        workspace: str | None = None,
    ) -> RunHandle:
        run_id = uuid.uuid4().hex[:12]
        # Prefer the workspace the caller passed in (bind_tracker does);
        # fall back to signac-based resolution for direct test use.
        resolved_workspace: Path | None = None
        if workspace:
            resolved_workspace = Path(workspace)
        else:
            resolved_workspace = self._resolve_workspace(config.get("job_id"))
        log_dir = self._log_dir(resolved_workspace, run_id)
        log_dir.mkdir(parents=True, exist_ok=True)
        handle = RunHandle(
            id=run_id,
            backend=self.name,
            url=None,
            project=project,
            group=group,
            extra={
                "workspace": str(resolved_workspace) if resolved_workspace else "",
                "log_dir": str(log_dir),
            },
        )
        self._write_event(
            handle,
            {
                "event": "init_run",
                "project": project,
                "group": group,
                "tags": tags,
                "config": config,
                "notes": notes,
                "offline": offline,
            },
        )
        return handle

    def log(self, handle: RunHandle, metrics: dict[str, Any]) -> None:
        self._write_event(handle, {"event": "log", "metrics": metrics})

    def log_artifact(self, handle: RunHandle, name: str, path: Path) -> None:
        self._write_event(
            handle,
            {
                "event": "log_artifact",
                "name": name,
                "path": str(path),
                "size_bytes": path.stat().st_size if path.exists() else None,
            },
        )

    def finish(self, handle: RunHandle, *, exit_code: int = 0) -> None:
        self._write_event(handle, {"event": "finish", "exit_code": exit_code})

    def list_runs(self, *, project: str, group_prefix: str) -> list[RunRecord]:
        """Walk the log root for matching runs.

        Only meaningful when the adapter was constructed with ``log_root``;
        otherwise we don't know where to look without a per-job workspace.
        """
        if self.log_root is None:
            return []
        records: list[RunRecord] = []
        for run_dir in self.log_root.iterdir():
            if not run_dir.is_dir():
                continue
            summary = self._summarize_run(run_dir, project=project, group_prefix=group_prefix)
            if summary is not None:
                records.append(summary)
        return records

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    def _resolve_workspace(self, job_id: str | None) -> Path | None:
        """Best-effort: if we have a job id, find its workspace on disk.

        Only meaningful when ``log_root`` isn't set — then we write into
        the job's own workspace.
        """
        if self.log_root is not None or not job_id:
            return None
        # Walk up from cwd looking for a ``.runs/workspace/<job_id>/`` dir.
        from aexp.utils.paths import (
            find_repo_root,
            resolve_run_store_path,
        )

        try:
            root = find_repo_root()
            workspace = resolve_run_store_path(root) / "workspace" / job_id
        except Exception:
            return None
        return workspace if workspace.is_dir() else None

    def _log_dir(self, workspace: Path | None, run_id: str) -> Path:
        """Where the JSONL for this run lives."""
        if self.log_root is not None:
            return self.log_root / run_id
        if workspace is not None:
            return workspace / "tracker_log" / run_id
        # Last resort: write into a tmp under cwd.
        return Path.cwd() / ".aex_noop_tracker" / run_id

    def _events_path(self, handle: RunHandle) -> Path:
        return Path(handle.extra["log_dir"]) / "events.jsonl"

    def _write_event(self, handle: RunHandle, payload: dict[str, Any]) -> None:
        record = {"timestamp": iso_utc_now(), **payload}
        path = self._events_path(handle)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(record) + "\n")

    def _summarize_run(
        self, run_dir: Path, *, project: str, group_prefix: str
    ) -> RunRecord | None:
        events = run_dir / "events.jsonl"
        if not events.is_file():
            return None
        try:
            lines = events.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        init_payload: dict[str, Any] = {}
        finish_payload: dict[str, Any] | None = None
        metrics: dict[str, Any] = {}
        created_at: datetime | None = None
        for raw in lines:
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            ts = record.get("timestamp")
            if ts and created_at is None:
                try:
                    created_at = datetime.fromisoformat(ts.rstrip("Z")).replace(
                        tzinfo=UTC
                    )
                except ValueError:
                    pass
            ev = record.get("event")
            if ev == "init_run":
                init_payload = record
            elif ev == "finish":
                finish_payload = record
            elif ev == "log":
                metrics.update(record.get("metrics") or {})
        run_project = init_payload.get("project") or ""
        run_group = init_payload.get("group") or ""
        if project and run_project != project:
            return None
        if group_prefix and not run_group.startswith(group_prefix):
            return None
        state = "complete" if finish_payload else "running"
        return RunRecord(
            id=run_dir.name,
            group=run_group,
            tags=tuple(init_payload.get("tags") or []),
            state=state,
            summary=metrics,
            created_at=created_at,
        )


__all__ = ["NoopAdapter"]
