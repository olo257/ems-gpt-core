"""Verified daily logical backup of the EMS schema to local and OMV storage."""
from __future__ import annotations

import gzip
import hashlib
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class BackupAdapters:
    options: dict
    local_now: Callable
    record_event: Callable
    log: object


def _safe_directory(raw: str, roots: tuple[str, ...]) -> Path:
    path = Path(raw).resolve()
    if not any(path == Path(root) or Path(root) in path.parents for root in roots):
        raise ValueError("backup directory outside allowed storage")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rotate(directory: Path, prefix: str, daily: int, weekly: int, monthly: int) -> None:
    files = sorted(directory.glob(f"{prefix}_*.sql.gz"), reverse=True)
    keep = set(files[:daily])
    weeks, months = set(), set()
    for path in files:
        match = re.search(r"_(\d{4}-\d{2}-\d{2})_", path.name)
        if not match:
            continue
        stamp = date.fromisoformat(match.group(1))
        week = stamp.isocalendar()[:2]
        month = (stamp.year, stamp.month)
        if len(weeks) < weekly and week not in weeks:
            weeks.add(week); keep.add(path)
        if len(months) < monthly and month not in months:
            months.add(month); keep.add(path)
    for path in files:
        if path not in keep:
            path.unlink(missing_ok=True)
            path.with_suffix(path.suffix + ".sha256").unlink(missing_ok=True)


def build_backup_service(a: BackupAdapters):
    last_attempt_day = None
    last_result = {"status": "DISABLED"}
    worker = None

    def execute_backup(clock) -> None:
        nonlocal last_result
        try:
            local_dir = _safe_directory(str(a.options["backup_local_directory"]), ("/backup",))
            omv_dir = _safe_directory(str(a.options["backup_omv_directory"]), ("/media",))
            if local_dir == omv_dir:
                raise ValueError("local and OMV directories must differ")
            schema = str(a.options["db_name"])
            if not re.fullmatch(r"[A-Za-z0-9_]+", schema):
                raise ValueError("invalid database name")
            filename = f"{schema}_{clock.strftime('%Y-%m-%d_%H-%M-%S')}.sql.gz"
            temporary = local_dir / (filename + ".tmp")
            local_file = local_dir / filename
            environment = os.environ.copy()
            environment["MYSQL_PWD"] = str(a.options.get("db_password") or "")
            command = ["mariadb-dump", "--single-transaction", "--quick", "--skip-lock-tables",
                       "--skip-routines", "--skip-events", "--skip-triggers",
                       "--default-character-set=utf8mb4", "-h", str(a.options["db_host"]),
                       "-P", str(a.options["db_port"]), "-u", str(a.options["db_user"]), schema]
            with gzip.open(temporary, "wb", compresslevel=6) as output:
                process = subprocess.run(command, stdout=output, stderr=subprocess.PIPE, env=environment, timeout=3600)
            if process.returncode:
                raise RuntimeError("mariadb-dump failed")
            if temporary.stat().st_size < int(a.options.get("backup_min_size_bytes", 1048576)):
                raise RuntimeError("backup file below configured minimum size")
            os.replace(temporary, local_file)
            omv_file = omv_dir / filename
            shutil.copy2(local_file, omv_file)
            local_hash, omv_hash = _sha256(local_file), _sha256(omv_file)
            if a.options.get("backup_sha256_enabled", True) and local_hash != omv_hash:
                raise RuntimeError("local and OMV SHA-256 differ")
            for path in (local_file, omv_file):
                path.with_suffix(path.suffix + ".sha256").write_text(local_hash + "  " + path.name + "\n", encoding="ascii")
            daily = int(a.options.get("backup_daily_retention", 14))
            weekly = int(a.options.get("backup_weekly_retention", 8))
            monthly = int(a.options.get("backup_monthly_retention", 12))
            _rotate(local_dir, schema, daily, weekly, monthly)
            _rotate(omv_dir, schema, daily, weekly, monthly)
            result = {"status": "COMPLETED", "file": filename, "size_bytes": local_file.stat().st_size,
                      "sha256": local_hash, "local": True, "omv": True}
            a.record_event("database_backup_completed", "backup", result)
            last_result = result
        except Exception as exc:
            try:
                temporary.unlink(missing_ok=True)
            except UnboundLocalError:
                pass
            result = {"status": "ERROR", "error": str(exc)}
            a.log.exception("database backup failed")
            a.record_event("database_backup_failed", "backup", result, "ERROR")
            last_result = result

    def maintain_backup() -> dict:
        nonlocal last_attempt_day, last_result, worker
        if not a.options.get("backup_enabled"):
            last_result = {"status": "DISABLED"}
            return dict(last_result)
        clock = a.local_now()
        if worker is not None and worker.is_alive():
            return {"status": "RUNNING"}
        if clock.strftime("%H:%M") < str(a.options.get("backup_time", "02:20")):
            return {"status": "WAITING"}
        if last_attempt_day == clock.date():
            return dict(last_result)
        try:
            local_dir = _safe_directory(str(a.options["backup_local_directory"]), ("/backup",))
            schema = str(a.options["db_name"])
            existing = sorted(local_dir.glob(f"{schema}_{clock.strftime('%Y-%m-%d')}_*.sql.gz"), reverse=True)
            if existing:
                last_attempt_day = clock.date()
                last_result = {"status": "ALREADY_COMPLETED", "file": existing[0].name,
                               "size_bytes": existing[0].stat().st_size}
                return dict(last_result)
        except Exception:
            # The worker records a durable, redacted failure event.
            pass
        last_attempt_day = clock.date()
        last_result = {"status": "RUNNING", "started_at": clock.isoformat()}
        worker = threading.Thread(target=execute_backup, args=(clock,), daemon=True, name="ems-gpt-backup")
        worker.start()
        return dict(last_result)

    return maintain_backup
