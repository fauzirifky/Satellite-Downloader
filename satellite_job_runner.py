from __future__ import annotations

import argparse
import json
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

try:
    from .export_satellite_support import initialize_earth_engine
    from .satellite_general_workbook import (
        SatelliteWorkbookConfig,
        build_satellite_workbook_resumable,
        workbook_config_from_payload,
    )
except ImportError:
    from export_satellite_support import initialize_earth_engine
    from satellite_general_workbook import (
        SatelliteWorkbookConfig,
        build_satellite_workbook_resumable,
        workbook_config_from_payload,
    )


JOB_CONFIG_NAME = "job_config.json"
JOB_STATUS_NAME = "job_status.json"
JOB_LOG_NAME = "job.log"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def update_status(job_dir: Path, **fields: Any) -> Dict[str, Any]:
    status_path = job_dir / JOB_STATUS_NAME
    status = read_json(status_path) if status_path.exists() else {}
    status.update(fields)
    write_json(status_path, status)
    return status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Background job runner for satellite workbook generation.")
    parser.add_argument("--job-dir", required=True, help="Folder job yang berisi job_config.json.")
    return parser.parse_args()


def load_job(job_dir: Path) -> tuple[Dict[str, Any], SatelliteWorkbookConfig]:
    config_path = job_dir / JOB_CONFIG_NAME
    if not config_path.exists():
        raise FileNotFoundError(f"File job config tidak ditemukan: {config_path}")
    payload = read_json(config_path)
    config = workbook_config_from_payload(payload["config"])
    return payload, config


def run_job(job_dir: Path) -> None:
    payload, config = load_job(job_dir)
    project = str(payload.get("gee_project", "")).strip()
    cache_root = Path(payload["cache_root"])
    status_path = job_dir / JOB_STATUS_NAME
    if not status_path.exists():
        update_status(
            job_dir,
            job_id=job_dir.name,
            status="queued",
            created_at=payload.get("created_at", now_iso()),
        )

    update_status(job_dir, status="running", started_at=now_iso(), error="", compiled_workbook_path="")
    initialize_earth_engine(project=project or None, authenticate=False)

    def progress_callback(step: int, total: int, label: str, status: str) -> None:
        update_status(
            job_dir,
            status="running",
            progress_step=step,
            progress_total=total,
            progress_label=label,
            progress_status=status,
            updated_at=now_iso(),
        )

    frames, meta, manifest = build_satellite_workbook_resumable(
        config=config,
        cache_root=cache_root,
        progress_callback=progress_callback,
    )
    del frames
    del meta
    compiled_path = str(manifest.get("compiled_workbook_path", ""))
    update_status(
        job_dir,
        status="completed",
        completed_at=now_iso(),
        compiled_workbook_path=compiled_path,
        manifest_path=str((Path(manifest["run_dir"]) / "manifest.json")) if manifest.get("run_dir") else "",
    )


def main() -> int:
    args = parse_args()
    job_dir = Path(args.job_dir).resolve()
    job_dir.mkdir(parents=True, exist_ok=True)
    log_path = job_dir / JOB_LOG_NAME
    try:
        run_job(job_dir)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{now_iso()}] Job selesai.\n")
        return 0
    except Exception:
        error_text = traceback.format_exc()
        update_status(
            job_dir,
            status="failed",
            completed_at=now_iso(),
            error=error_text,
        )
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{now_iso()}] Job gagal.\n{error_text}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
