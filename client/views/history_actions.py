"""Collega ``HistoryDialog`` alle chiamate al server (confronto, etichette, blocco): una sola implementazione per il client
(storico del progetto aperto) e per la console del server (storico di un progetto scelto, ``pid`` esplicito)."""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QObject

from client.api_client import ApiClient
from client.views.history_dialog import HistoryDialog
from client.views.revision_compare_dialog import RevisionCompareDialog


class HistoryActions(QObject):
    """``pid=None``: progetto condiviso dell'ApiClient; altrimenti le varianti ``*_for(pid)``."""

    def __init__(self, dialog: HistoryDialog, api: ApiClient, run: Callable, pid: Optional[int] = None,
                 on_status: Optional[Callable[[str], None]] = None, on_error: Optional[Callable[[Exception], None]] = None):
        super().__init__(dialog)
        self.dialog, self.api, self._run, self.pid = dialog, api, run, pid
        self._status = on_status or (lambda _m: None)
        self._error = on_error or (lambda _e: None)
        self.compare_dialog: Optional[RevisionCompareDialog] = None         # ultimo confronto aperto (utile ai test)
        dialog.compare_requested.connect(self.compare)
        dialog.label_requested.connect(self.set_label)
        dialog.lock_requested.connect(self.set_locked)
        dialog.actions = self

    # ------------------------------------------------------------------ confronto
    def compare(self, a: int, b: int) -> None:
        api, pid = self.api, self.pid

        def show(res) -> None:
            dlg = self.compare_dialog = RevisionCompareDialog(res, self.dialog)
            dlg.exec()
        self._run(lambda: api.compare_revisions(a, b) if pid is None else api.compare_revisions_for(pid, a, b),
                  show, self._error)

    # ------------------------------------------------------------------ etichetta / blocco
    def _refresh(self) -> None:
        api, pid = self.api, self.pid

        def fill(h) -> None:
            try:
                self.dialog.set_history(h, keep_selection=True)
            except RuntimeError:                                            # dialogo gia' chiuso
                pass
        self._run(lambda: api.history() if pid is None else api.history_for(pid), fill, self._error)

    def _patch(self, rev: int, msg: str, **kw) -> None:
        api, pid = self.api, self.pid

        def done(_m) -> None:
            self._status(msg)
            self._refresh()
        self._run(lambda: api.patch_revision(rev, **kw) if pid is None else api.patch_revision_for(pid, rev, **kw),
                  done, self._error)

    def set_label(self, rev: int, label: str) -> None:
        self._patch(rev, f"Rev {rev}: etichetta «{label}»" if label else f"Rev {rev}: etichetta rimossa", label=label)

    def set_locked(self, rev: int, locked: bool) -> None:
        self._patch(rev, f"Rev {rev} bloccata (non verrà mai eliminata dal limite di revisioni)" if locked
                    else f"Rev {rev} sbloccata", locked=locked)
