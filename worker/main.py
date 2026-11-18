"""
Worker entry point.

Loads the OCR models once, publishes a heartbeat for the admin system page, then serves the
'priority' (synchronous requests) and 'default' (asynchronous jobs) queues in this process so the
models stay in GPU memory between jobs.
"""
import json
import os
import socket
import threading
import time
from datetime import datetime, timezone

from redis import Redis
from rq import Queue
from rq.timeouts import TimerDeathPenalty
from rq.worker import SimpleWorker

from shared.config import settings
from shared.logger import setup_logger
from shared.queue import redis_conn

log = setup_logger("worker.main")

HEARTBEAT_KEY = "idp:worker:heartbeat"
HEARTBEAT_SECONDS = 20


def _gpu_info() -> dict:
    try:
        import paddle
        if paddle.device.is_compiled_with_cuda() and paddle.device.cuda.device_count() > 0:
            return {"device": "gpu", "gpu_name": paddle.device.cuda.get_device_name(0)}
    except Exception:
        pass
    return {"device": "cpu"}


def _heartbeat(info: dict) -> None:
    while True:
        try:
            payload = {**info, "last_seen": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            redis_conn.set(HEARTBEAT_KEY, json.dumps(payload), ex=HEARTBEAT_SECONDS * 3)
        except Exception as exc:
            log.debug("heartbeat failed: %s", exc)
        time.sleep(HEARTBEAT_SECONDS)


def _warm_up_ocr() -> None:
    """One inference at start-up so the first real job does not pay the GPU warm-up cost."""
    try:
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont
        from worker.ocr_instance import ocr_images
        img = Image.new("RGB", (900, 160), "white")
        ImageDraw.Draw(img).text((20, 50), "Warm up 0123456789", fill="black", font=ImageFont.load_default(size=40))
        started = time.perf_counter()
        ocr_images([np.ascontiguousarray(np.asarray(img)[:, :, ::-1])])
        log.info("OCR warm-up inference took %.2fs", time.perf_counter() - started)
    except Exception as exc:  # warm-up is an optimisation only
        log.warning("OCR warm-up skipped: %s", exc)


def preload() -> dict:
    info = {"hostname": socket.gethostname(), "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    mode = settings.preload_models.lower()
    if mode in ("ocr", "all"):
        started = time.perf_counter()
        from worker.ocr_instance import get_ocr_instance, ocr_info
        get_ocr_instance()
        _warm_up_ocr()
        info["ocr"] = ocr_info()
        info["ocr_load_seconds"] = round(time.perf_counter() - started, 1)
        log.info("OCR models loaded in %ss", info["ocr_load_seconds"])
    if mode == "all":
        started = time.perf_counter()
        from worker.engine_parse import get_pipeline
        get_pipeline()
        info["parse_load_seconds"] = round(time.perf_counter() - started, 1)
        log.info("Document parsing pipeline loaded in %ss", info["parse_load_seconds"])
    info.update(_gpu_info())
    return info


def main() -> None:
    log.info("Starting IDP worker (preload=%s)", settings.preload_models)
    info = preload()
    # Warm the render pool so the first scanned PDF does not pay the process start-up cost.
    try:
        from worker.render_pool import get_pool
        get_pool(settings.render_workers)
    except Exception as exc:
        log.warning("render pool could not be started: %s", exc)
    slots = max(1, settings.worker_concurrency)
    info["concurrency"] = slots
    threading.Thread(target=_heartbeat, args=(info,), daemon=True, name="heartbeat").start()

    # Most of a job's time is spent waiting on the language model, so one process runs several jobs at
    # once. All slots share the models already loaded in this process; GPU inference is serialised by a
    # lock in ocr_instance, so extra slots cost no GPU memory.
    for slot in range(1, slots):
        threading.Thread(target=_run_slot, args=(slot,), daemon=True, name=f"job-slot-{slot}").start()
    log.info("Serving the priority and default queues with %s concurrent job slot(s)", slots)
    _run_slot(0)


class ThreadSlotWorker(SimpleWorker):
    """RQ worker that can run outside the main thread: no signal handlers, timer-based job timeouts."""
    death_penalty_class = TimerDeathPenalty

    def _install_signal_handlers(self):
        pass


def _run_slot(slot: int) -> None:
    # Each slot has its own connection without a read timeout: RQ blocks on BLPOP for minutes and sets
    # an appropriate socket timeout itself when none is configured.
    conn = Redis(host=settings.redis_host, port=settings.redis_port, socket_connect_timeout=5,
                 socket_timeout=None, health_check_interval=30)
    queues = [Queue("priority", connection=conn), Queue("default", connection=conn)]
    worker_class = SimpleWorker if slot == 0 else ThreadSlotWorker
    while True:
        try:
            worker_class(queues, connection=conn).work(with_scheduler=False)
            if slot == 0:
                return  # the main slot exits on shutdown signals; the container then stops
        except Exception as exc:  # a slot must never silently disappear
            log.error("job slot %s stopped unexpectedly (%s); restarting it", slot, exc)
        time.sleep(2)


if __name__ == "__main__":
    main()
