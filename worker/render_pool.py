"""
Parallel PDF page rendering.

Rendering is CPU bound and PDFium is not thread safe, so pages are rasterised in a small pool of
spawned processes. This module must stay free of paddle imports: it is imported by the children.
"""
import atexit
import multiprocessing
from concurrent.futures import Future, ProcessPoolExecutor
from typing import Optional

_POOL: Optional[ProcessPoolExecutor] = None
_POOL_SIZE = 0


def render_page(pdf_path: str, page_number: int, dpi: int) -> tuple:
    """Returns (page_number, width, height, rgb_bytes). Runs inside a pool process."""
    from worker.pdf_util import PDFIUM_LOCK, open_pdf, render_rgb
    with PDFIUM_LOCK:  # uncontended in pool processes; required when rendering in a job thread
        pdf = open_pdf(pdf_path)
        try:
            page = pdf[page_number]
            image = render_rgb(page, dpi)
            page.close()
        finally:
            pdf.close()
    return page_number, image.width, image.height, image.tobytes()


def _shutdown():
    global _POOL
    if _POOL is not None:
        _POOL.shutdown(wait=False, cancel_futures=True)
        _POOL = None


def get_pool(workers: int) -> Optional[ProcessPoolExecutor]:
    """Lazily creates the pool. Returns None when parallel rendering is disabled (workers <= 1)."""
    global _POOL, _POOL_SIZE
    if workers <= 1:
        return None
    if _POOL is None or _POOL_SIZE != workers:
        _shutdown()
        _POOL = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"))
        _POOL_SIZE = workers
        atexit.register(_shutdown)
    return _POOL


def submit_render(pdf_path: str, page_number: int, dpi: int, workers: int) -> Future:
    """Schedules a page render. Falls back to rendering in the calling thread if the pool is unavailable."""
    pool = get_pool(workers)
    if pool is not None:
        try:
            return pool.submit(render_page, pdf_path, page_number, dpi)
        except Exception:
            _shutdown()
    future: Future = Future()
    try:
        future.set_result(render_page(pdf_path, page_number, dpi))
    except Exception as exc:  # surfaced when the caller reads the result
        future.set_exception(exc)
    return future


def to_bgr_array(width: int, height: int, rgb_bytes: bytes):
    import numpy as np
    arr = np.frombuffer(rgb_bytes, dtype=np.uint8).reshape(height, width, 3)
    return np.ascontiguousarray(arr[:, :, ::-1])


def to_pil(width: int, height: int, rgb_bytes: bytes):
    from PIL import Image
    return Image.frombytes("RGB", (width, height), rgb_bytes)
