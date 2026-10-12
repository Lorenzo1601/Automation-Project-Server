"""Esecuzione di chiamate bloccanti (HTTP) fuori dal thread della GUI."""
from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(object)


class _Job(QRunnable):
    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self.fn, self.signals = fn, _Signals()

    def run(self) -> None:
        try:
            res = self.fn()
        except Exception as exc:                                  # noqa: BLE001 - riportata alla GUI
            self.signals.failed.emit(exc)
        else:
            self.signals.done.emit(res)


_live: set = set()


def run_async(fn: Callable[[], Any], on_done: Optional[Callable[[Any], None]] = None,
              on_error: Optional[Callable[[Exception], None]] = None) -> None:
    """Esegue ``fn`` nel threadpool; i callback tornano nel thread della GUI."""
    job = _Job(fn)
    _live.add(job)                                                # evita la garbage collection dei segnali

    def finish(cb, arg):
        _live.discard(job)
        if cb:
            cb(arg)
    job.signals.done.connect(lambda r: finish(on_done, r))
    job.signals.failed.connect(lambda e: finish(on_error, e))
    QThreadPool.globalInstance().start(job)
