"""W&B tracker adapter — optional, behind ``pip install agentic-experiments[wandb]``.

``wandb`` is imported lazily inside methods so a plain install never pulls
it in at package load. If ``wandb`` is missing, constructing a
:class:`WandbAdapter` raises :class:`TrackerInitError`.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aexp.trackers.base import (
    RunHandle,
    RunRecord,
    TrackerAdapter,
    TrackerInitError,
)


class WandbAdapter(TrackerAdapter):
    """Thin wrapper over ``wandb.init`` / ``wandb.log`` / ``wandb.finish``."""

    name = "wandb"

    def __init__(self, *, entity: str | None = None) -> None:
        """Parameters
        ----------
        entity : str | None
            Optional W&B entity (team / user). If ``None``, uses whatever
            ``wandb.init`` resolves from env / config.
        """
        self._entity = entity
        self._wandb = self._import_wandb()

    # -----------------------------------------------------------------------
    # Lazy import
    # -----------------------------------------------------------------------

    @staticmethod
    def _import_wandb():
        try:
            import wandb  # type: ignore[import-not-found]
        except ImportError as exc:
            raise TrackerInitError(
                "wandb is not installed; install agentic-experiments[wandb] to use WandbAdapter"
            ) from exc
        return wandb

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
        init_kwargs: dict[str, Any] = {
            "project": project,
            "group": group,
            "tags": tags,
            "config": config,
            "notes": notes,
            "reinit": True,
        }
        if self._entity is not None:
            init_kwargs["entity"] = self._entity
        if offline:
            init_kwargs["mode"] = "offline"
        if workspace is not None:
            # Co-locate wandb's local state (offline run dir, cache, logs)
            # with the signac job workspace, so each run is self-contained.
            # Produces ``<workspace>/wandb/offline-run-*/`` in offline mode.
            wandb_dir = Path(workspace)
            wandb_dir.mkdir(parents=True, exist_ok=True)
            init_kwargs["dir"] = str(wandb_dir)

        try:
            run = self._wandb.init(**init_kwargs)
        except Exception as exc:
            raise TrackerInitError(f"wandb.init failed: {exc}") from exc

        url = getattr(run, "url", None)
        run_id = getattr(run, "id", None) or ""
        return RunHandle(
            id=run_id,
            backend=self.name,
            url=url,
            project=project,
            group=group,
            extra={"run_object": run, "workspace": workspace or ""},
        )

    def log(self, handle: RunHandle, metrics: dict[str, Any]) -> None:
        run = handle.extra.get("run_object")
        if run is None:
            # Fallback to module-level wandb (single active run).
            self._wandb.log(metrics)
            return
        run.log(metrics)

    def log_artifact(self, handle: RunHandle, name: str, path: Path) -> None:
        run = handle.extra.get("run_object")
        artifact = self._wandb.Artifact(name, type="file")
        artifact.add_file(str(path))
        if run is None:
            self._wandb.log_artifact(artifact)
            return
        run.log_artifact(artifact)

    def finish(self, handle: RunHandle, *, exit_code: int = 0) -> None:
        run = handle.extra.get("run_object")
        if run is None:
            self._wandb.finish(exit_code=exit_code)
            return
        run.finish(exit_code=exit_code)

    def list_runs(self, *, project: str, group_prefix: str) -> list[RunRecord]:
        try:
            api = self._wandb.Api()
        except Exception as exc:
            raise TrackerInitError(f"wandb.Api() failed: {exc}") from exc

        entity = self._entity
        path = f"{entity}/{project}" if entity else project
        try:
            runs = api.runs(path, filters={"group": {"$regex": f"^{group_prefix}"}})
        except Exception as exc:
            raise TrackerInitError(f"wandb Api.runs failed: {exc}") from exc

        records: list[RunRecord] = []
        for run in runs:
            created = None
            raw_created = getattr(run, "created_at", None) or getattr(run, "createdAt", None)
            if raw_created:
                try:
                    created = datetime.fromisoformat(str(raw_created).replace("Z", "+00:00"))
                except Exception:
                    created = None
            records.append(
                RunRecord(
                    id=getattr(run, "id", ""),
                    group=getattr(run, "group", "") or "",
                    tags=tuple(getattr(run, "tags", []) or []),
                    state=str(getattr(run, "state", "") or ""),
                    summary=dict(getattr(run, "summary", {}) or {}),
                    created_at=created.astimezone(UTC) if created else None,
                )
            )
        return records


@dataclass(frozen=True)
class OfflineSyncResult:
    """Outcome of a single ``wandb sync`` invocation."""

    path: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def find_offline_runs(run_store: str | Path) -> list[Path]:
    """Return every ``offline-run-*`` directory produced by wandb inside ``run_store``.

    Walks ``<run_store>/workspace/<job_id>/wandb/`` and returns any matching
    offline run dirs. Works for the tightened layout (offline data lives under
    each signac workspace) and also catches runs written to a flat
    ``<run_store>/wandb/`` if a caller bypassed ``bind_tracker``.
    """
    root = Path(run_store)
    matches: list[Path] = []
    for candidate in root.rglob("offline-run-*"):
        if candidate.is_dir():
            matches.append(candidate)
    return sorted(matches)


def sync_offline_runs(
    run_store: str | Path,
    *,
    dry_run: bool = False,
) -> list[OfflineSyncResult]:
    """Invoke ``wandb sync`` on every offline run beneath ``run_store``.

    Parameters
    ----------
    run_store : str | Path
        Signac project directory (typically ``<repo>/.runs``).
    dry_run : bool
        If ``True``, report what would be synced without calling ``wandb``.
    """
    runs = find_offline_runs(run_store)
    results: list[OfflineSyncResult] = []
    for run_dir in runs:
        if dry_run:
            results.append(OfflineSyncResult(str(run_dir), 0, f"(dry-run) {run_dir}", ""))
            continue
        try:
            # stdin=DEVNULL so wandb never reads from the MCP JSON-RPC pipe
            # when sync_offline is invoked as an MCP tool.
            proc = subprocess.run(
                [sys.executable, "-m", "wandb", "sync", str(run_dir)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=600,
            )
        except FileNotFoundError as exc:
            results.append(
                OfflineSyncResult(str(run_dir), 127, "", f"wandb not found: {exc}")
            )
            continue
        except subprocess.TimeoutExpired as exc:
            results.append(
                OfflineSyncResult(str(run_dir), 124, "", f"timeout: {exc}")
            )
            continue
        results.append(
            OfflineSyncResult(
                path=str(run_dir),
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )
        )
    return results


__all__ = [
    "OfflineSyncResult",
    "WandbAdapter",
    "find_offline_runs",
    "sync_offline_runs",
]
