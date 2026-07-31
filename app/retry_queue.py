from __future__ import annotations
"""
ripple/app/retry_queue.py

Webhook Retry Queue — resilience for when GitHub/GitLab API calls fail.

Handles:
- GitHub/GitLab API timeouts (network issues)
- Rate limit responses (429 Too Many Requests)
- 5xx server errors (temporary outages)
- Failed PR/MR creation (retry with exponential backoff)

Architecture:
- In-memory queue with background processing
- Exponential backoff: 5s, 15s, 45s, 135s (3x multiplier)
- Max 4 retries per job (then dead-letter)
- Dead-letter queue for manual inspection
- Thread-safe for FastAPI concurrent requests
"""

import time
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional, Any
from enum import Enum


class JobStatus(Enum):
    PENDING = "pending"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD_LETTERED = "dead_lettered"


@dataclass
class RetryJob:
    """A job in the retry queue."""
    id: str
    description: str
    payload: dict
    callback_name: str  # name of the function to retry
    created_at: float = field(default_factory=time.time)
    attempts: int = 0
    max_attempts: int = 4
    next_retry_at: float = 0.0
    last_error: str = ""
    status: JobStatus = JobStatus.PENDING


class RetryQueue:
    """
    In-memory retry queue with exponential backoff.
    
    Usage:
        queue = RetryQueue()
        queue.enqueue("create-pr", {"repo": "org/repo", ...}, callback_name="create_pr")
        
        # Background worker processes retries
        queue.start_worker()
    """
    
    def __init__(
        self,
        base_delay: float = 5.0,
        multiplier: float = 3.0,
        max_attempts: int = 4,
    ):
        self.base_delay = base_delay
        self.multiplier = multiplier
        self.max_attempts = max_attempts
        self._queue: deque[RetryJob] = deque()
        self._dead_letter: list[RetryJob] = []
        self._completed: list[RetryJob] = []
        self._callbacks: dict[str, Callable] = {}
        self._lock = threading.Lock()
        self._worker_running = False
    
    def register_callback(self, name: str, fn: Callable):
        """Register a callable that can be retried."""
        self._callbacks[name] = fn
    
    def enqueue(self, description: str, payload: dict, callback_name: str) -> str:
        """Add a job to the retry queue. Returns job ID."""
        import uuid
        job_id = str(uuid.uuid4())[:8]
        
        job = RetryJob(
            id=job_id,
            description=description,
            payload=payload,
            callback_name=callback_name,
            max_attempts=self.max_attempts,
            next_retry_at=time.time() + self.base_delay,
        )
        
        with self._lock:
            self._queue.append(job)
        
        return job_id
    
    def process_ready_jobs(self) -> list[dict]:
        """Process all jobs that are ready for retry. Returns results."""
        now = time.time()
        results = []
        
        with self._lock:
            ready_jobs = [j for j in self._queue if j.next_retry_at <= now]
        
        for job in ready_jobs:
            result = self._execute_job(job)
            results.append(result)
        
        return results
    
    def _execute_job(self, job: RetryJob) -> dict:
        """Execute a single retry job."""
        job.attempts += 1
        job.status = JobStatus.RETRYING
        
        callback = self._callbacks.get(job.callback_name)
        if not callback:
            job.status = JobStatus.FAILED
            job.last_error = f"No callback registered for '{job.callback_name}'"
            self._move_to_dead_letter(job)
            return {"job_id": job.id, "status": "failed", "error": job.last_error}
        
        try:
            result = callback(job.payload)
            job.status = JobStatus.SUCCEEDED
            with self._lock:
                self._queue.remove(job)
                self._completed.append(job)
            return {"job_id": job.id, "status": "succeeded", "result": result}
        
        except Exception as e:
            job.last_error = str(e)
            
            if job.attempts >= job.max_attempts:
                self._move_to_dead_letter(job)
                return {"job_id": job.id, "status": "dead_lettered", "error": str(e), "attempts": job.attempts}
            
            # Schedule next retry with exponential backoff
            delay = self.base_delay * (self.multiplier ** (job.attempts - 1))
            job.next_retry_at = time.time() + delay
            job.status = JobStatus.PENDING
            
            return {"job_id": job.id, "status": "retrying", "next_retry_in": f"{delay:.0f}s", "attempts": job.attempts}
    
    def _move_to_dead_letter(self, job: RetryJob):
        """Move a failed job to the dead letter queue."""
        job.status = JobStatus.DEAD_LETTERED
        with self._lock:
            if job in self._queue:
                self._queue.remove(job)
            self._dead_letter.append(job)
    
    def stats(self) -> dict:
        """Get queue statistics."""
        with self._lock:
            return {
                "pending": len([j for j in self._queue if j.status == JobStatus.PENDING]),
                "retrying": len([j for j in self._queue if j.status == JobStatus.RETRYING]),
                "completed": len(self._completed),
                "dead_lettered": len(self._dead_letter),
                "total_processed": len(self._completed) + len(self._dead_letter),
            }
    
    def dead_letter_jobs(self) -> list[dict]:
        """Get all dead-lettered jobs for inspection."""
        with self._lock:
            return [
                {
                    "id": j.id,
                    "description": j.description,
                    "attempts": j.attempts,
                    "last_error": j.last_error,
                    "created_at": j.created_at,
                }
                for j in self._dead_letter
            ]
    
    def retry_dead_letter(self, job_id: str) -> bool:
        """Move a dead-lettered job back to the queue for retry."""
        with self._lock:
            for job in self._dead_letter:
                if job.id == job_id:
                    self._dead_letter.remove(job)
                    job.attempts = 0
                    job.status = JobStatus.PENDING
                    job.next_retry_at = time.time() + self.base_delay
                    self._queue.append(job)
                    return True
        return False
    
    def clear(self):
        """Clear all queues."""
        with self._lock:
            self._queue.clear()
            self._dead_letter.clear()
            self._completed.clear()


def should_retry(status_code: int) -> bool:
    """Determine if an HTTP error should trigger a retry."""
    # Retry on: rate limit, server errors, gateway timeout
    return status_code in (429, 500, 502, 503, 504)


def should_retry_error(error: Exception) -> bool:
    """Determine if an exception should trigger a retry."""
    error_str = str(error).lower()
    retryable = ["timeout", "connection", "reset", "broken pipe", "429", "503"]
    return any(r in error_str for r in retryable)


# Singleton instance
_retry_queue = RetryQueue()


def get_retry_queue() -> RetryQueue:
    """Get the global retry queue instance."""
    return _retry_queue
