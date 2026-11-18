import os
import shutil
import time

from shared.config import settings
from shared.logger import setup_logger

log = setup_logger("worker.storage")

LOCAL_OUTPUT_DIR = os.path.join(settings.data_dir, "output")


def save_file(source_path: str, job_id: str, filename: str) -> str:
    """Stores an output file under the job's own folder. Returns the local path or an s3:// URL."""
    if settings.storage_mode.upper() == "S3" and settings.s3_bucket_name:
        import boto3
        s3 = boto3.client("s3", aws_access_key_id=settings.s3_access_key,
                          aws_secret_access_key=settings.s3_secret_key)
        key = f"{job_id}/{filename}"
        s3.upload_file(source_path, settings.s3_bucket_name, key)
        return f"s3://{settings.s3_bucket_name}/{key}"
    folder = os.path.join(LOCAL_OUTPUT_DIR, job_id)
    os.makedirs(folder, exist_ok=True)
    destination = os.path.join(folder, filename)
    shutil.copy2(source_path, destination)
    return destination


def cleanup_old_files() -> None:
    """Deletes local outputs and stale uploads older than the retention period."""
    days = settings.file_retention_days
    if days <= 0:
        return
    cutoff = time.time() - days * 86400
    for root in (LOCAL_OUTPUT_DIR, os.path.join(settings.data_dir, "uploads")):
        if not os.path.isdir(root):
            continue
        for entry in os.scandir(root):
            try:
                if entry.stat().st_mtime >= cutoff:
                    continue
                if entry.is_dir():
                    shutil.rmtree(entry.path, ignore_errors=True)
                else:
                    os.remove(entry.path)
            except OSError as exc:
                log.error("Failed to delete expired item %s: %s", entry.path, exc)
