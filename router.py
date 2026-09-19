import threading
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel

from main import TransferCancelled, run_transfer


app = FastAPI(
    title="Baidu Drive Transfer API",
    version="0.1.0",
)


class CreateTransferRequest(BaseModel):
    baidu_url: str
    google_folder_url: str


class TransferJob(BaseModel):
    job_id: str
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None

    total_files: int = 0
    total_bytes: int = 0

    baidu_downloaded_bytes: int = 0
    baidu_completed_files: int = 0
    baidu_percent: float = 0
    baidu_speed: float = 0

    google_uploaded_bytes: int = 0
    google_completed_files: int = 0
    google_percent: float = 0
    google_speed: float = 0


jobs: dict[str, TransferJob] = {}
cancel_events: dict[str, threading.Event] = {}
active_job_id: str | None = None
job_lock = threading.Lock()
        

def run_transfer_worker(
    job_id: str,
    baidu_url: str,
    google_folder_url: str,
) -> None:
    global active_job_id

    with job_lock:
        job = jobs[job_id]
        job.status = "running"
        cancel_event = cancel_events[job_id]
        job.started_at = datetime.now(timezone.utc)

    def update_progress(progress) -> None:
        with job_lock:
            job = jobs[job_id]

            job.total_files = progress.total_files
            job.total_bytes = progress.total_bytes

            job.baidu_downloaded_bytes = (
                progress.baidu_downloaded_bytes
            )
            job.baidu_completed_files = (
                progress.baidu_completed_files
            )
            job.baidu_percent = progress.baidu_percent
            job.baidu_speed = progress.baidu_speed

            job.google_uploaded_bytes = (
                progress.google_uploaded_bytes
            )
            job.google_completed_files = (
                progress.google_completed_files
            )
            job.google_percent = progress.google_percent
            job.google_speed = progress.google_speed
    
    try:
        run_transfer(
            baidu_url=baidu_url,
            google_folder_url=google_folder_url,
            mode="all",
            progress_callback=update_progress,
            cancel_event=cancel_event,
        )

        with job_lock:
            job = jobs[job_id]
            job.status = "completed"
            job.finished_at = datetime.now(timezone.utc)
            
    except TransferCancelled:
        with job_lock:
            job = jobs[job_id]
            job.status = "cancelled"
            job.error = None
            job.finished_at = datetime.now(timezone.utc)

    except Exception as exc:
        with job_lock:
            job = jobs[job_id]
            job.status = "failed"
            job.error = str(exc)
            job.finished_at = datetime.now(timezone.utc)

    finally:
        with job_lock:
            if active_job_id == job_id:
                active_job_id = None
                
                
@app.post(
    "/transfers",
    status_code=status.HTTP_202_ACCEPTED,
)
def create_transfer(payload: CreateTransferRequest):
    global active_job_id

    with job_lock:
        if active_job_id is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A transfer is already running.",
            )

        job_id = str(uuid.uuid4())

        job = TransferJob(
            job_id=job_id,
            status="pending",
            created_at=datetime.now(timezone.utc),
        )

        jobs[job_id] = job
        # 每個 job 都建立自己的 cancel event
        cancel_events[job_id] = threading.Event()
        active_job_id = job_id

    thread = threading.Thread(
        target=run_transfer_worker,
        args=(
            job_id,
            payload.baidu_url,
            payload.google_folder_url,
        ),
        daemon=True,
    )
    thread.start()

    return {
        "job_id": job_id,
        "status": "pending",
    }
    
    
@app.get("/transfers/active")
def get_active_transfer():
    with job_lock:
        if active_job_id is None:
            return {
                "active": False,
            }

        job = jobs[active_job_id]

        return {
            "active": True,
            "job": job,
        }
        
        
@app.get("/transfers/{job_id}")
def get_transfer(job_id: str):
    with job_lock:
        job = jobs.get(job_id)

        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Transfer job not found.",
            )

        return job
    
    
@app.post("/transfers/{job_id}/cancel")
def cancel_transfer(job_id: str):
    with job_lock:
        job = jobs.get(job_id)

        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Transfer job not found.",
            )

        if job.status not in ("pending", "running"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Transfer job is already {job.status}.",
            )

        cancel_event = cancel_events[job_id]
        cancel_event.set()

        return {
            "job_id": job_id,
            "status": job.status,
            "cancel_requested": True,
        }
        
        
    