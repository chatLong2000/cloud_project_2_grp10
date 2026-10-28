"""
CloudWatch Logs handler for the Scenario Analyzer.

Buffers log records in memory and streams them asynchronously in batches to Amazon
CloudWatch Logs using a background daemon thread, ensuring logging never blocks
the analysis loop. Automatically creates the log group, sets the retention policy
(365 days / 1 year), and creates the log stream.
"""

import atexit
import logging
import queue
import sys
import threading
import time
import uuid
from typing import Any

from botocore.exceptions import ClientError

log = logging.getLogger("analyser.cloudwatch")

MAX_BATCH_COUNT = 100
MAX_BATCH_BYTES = 524288  # 512 KB
FLUSH_INTERVAL_S = 3.0


class CloudWatchLogsHandler(logging.Handler):
    """
    Asynchronous logging handler that streams records to Amazon CloudWatch Logs.
    """

    def __init__(
        self,
        log_group_name: str,
        log_stream_name: str | None = None,
        region: str = "us-east-1",
        retention_days: int = 365,
        client: Any = None,
    ) -> None:
        super().__init__()
        self.log_group_name = log_group_name
        self.region = region
        self.retention_days = retention_days
        if not log_stream_name:
            log_stream_name = f"analyser-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.log_stream_name = log_stream_name
        self._client = client
        self._queue: queue.Queue = queue.Queue(maxsize=10000)
        self._stop_event = threading.Event()
        self._initialized = False
        self._init_lock = threading.Lock()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="cloudwatch-log-worker",
            daemon=True,
        )
        self._worker.start()
        atexit.register(self.close)

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("logs", region_name=self.region)
        return self._client

    def _ensure_group_and_stream(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            client = self._get_client()
            try:
                client.create_log_group(logGroupName=self.log_group_name)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ResourceAlreadyExistsException":
                    raise

            if self.retention_days:
                try:
                    client.put_retention_policy(
                        logGroupName=self.log_group_name,
                        retentionInDays=self.retention_days,
                    )
                except ClientError as exc:
                    print(f"[CloudWatchLogsHandler] Warning: could not set retention policy: {exc}", file=sys.stderr)

            try:
                client.create_log_stream(
                    logGroupName=self.log_group_name,
                    logStreamName=self.log_stream_name,
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ResourceAlreadyExistsException":
                    raise

            self._initialized = True

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            timestamp = int(record.created * 1000)
            item = {"timestamp": timestamp, "message": msg}
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                pass
        except Exception:
            self.handleError(record)

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            self._flush_batch(timeout=FLUSH_INTERVAL_S)

    def _flush_batch(self, timeout: float = 0.0) -> None:
        batch: list[dict[str, Any]] = []
        batch_bytes = 0
        try:
            if timeout > 0:
                item = self._queue.get(timeout=timeout)
            else:
                item = self._queue.get_nowait()
            batch.append(item)
            batch_bytes += len(item["message"].encode("utf-8", errors="replace")) + 26
            while len(batch) < MAX_BATCH_COUNT and batch_bytes < MAX_BATCH_BYTES:
                try:
                    item = self._queue.get_nowait()
                    batch.append(item)
                    batch_bytes += len(item["message"].encode("utf-8", errors="replace")) + 26
                except queue.Empty:
                    break
        except queue.Empty:
            return

        if not batch:
            return

        try:
            self._ensure_group_and_stream()
            client = self._get_client()
            batch.sort(key=lambda x: x["timestamp"])
            client.put_log_events(
                logGroupName=self.log_group_name,
                logStreamName=self.log_stream_name,
                logEvents=batch,
            )
        except Exception as exc:
            print(
                f"[CloudWatchLogsHandler Error] Failed to send {len(batch)} logs to CloudWatch: {exc}",
                file=sys.stderr,
            )
        finally:
            for _ in batch:
                self._queue.task_done()

    def close(self) -> None:
        self._stop_event.set()
        while not self._queue.empty():
            self._flush_batch(timeout=0.0)
        super().close()
