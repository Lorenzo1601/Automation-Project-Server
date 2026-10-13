"""«Notifiche»: posta in arrivo persistente (richieste di merge, esiti, branch aggiornati, menzioni)."""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget

from client.api_client import ApiClient, ApiError
from client.theme.tokens import T
from client.views.common import full_time, heading, item, make_table, muted
from client.workers import run_async
from common.schemas_inbox import NotificationOut

KIND_LABELS = {"merge_request": "Richiesta di merge", "merge_rejected": "Richiesta rifiutata",
               "merge_merged": "Richiesta unita", "branch_updated": "Branch aggiornato", "mention": "Menzione"}
KIND_COLORS = {"merge_request": T["ORANGE"], "merge_rejected": T["RED"], "merge_merged": T["GREEN"],
               "branch_updated": T["ACCENT"], "mention": T["ACCENT_ALT"]}


class NotificationsDialog(QDialog):
    open_requested = Signal(object)          # NotificationOut: la finestra principale apre richiesta / branch collegati
    unread_changed = Signal(int)

    def __init__(self, api: ApiClient, run: Callable = run_async, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.api, self._run = api, run
        self._rows: list[NotificationOut] = []
        self.setWindowTitle("Notifiche")
        self.resize(940, 460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(10)
        lay.addWidget(heading("Notifiche", 2))
        self.lbl = muted("")
        lay.addWidget(self.lbl)
        self.table = make_table(["", "Quando", "Tipo", "Messaggio", "Progetto"], stretch_col=3)
        self.table.setColumnWidth(0, 22)
        self.table.setColumnWidth(1, 150)
        self.table.setColumnWidth(2, 150)
        self.table.setColumnWidth(4, 160)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.chk_unread = QCheckBox("Solo non lette")
        self.btn_open = QPushButton("Apri")
        self.btn_open.setProperty("variant", "primary")
        self.btn_read = QPushButton("Segna come letta")
        self.btn_all = QPushButton("Segna tutte come lette")
        self.btn_reload = QPushButton("Aggiorna")
        self.btn_close = QPushButton("Chiudi")
        for w in (self.chk_unread, self.btn_open, self.btn_read, self.btn_all, self.btn_reload):
            row.addWidget(w)
        row.addStretch(1)
        row.addWidget(self.btn_close)
        lay.addLayout(row)
        self.chk_unread.toggled.connect(lambda _on: self.reload())
        self.btn_open.clicked.connect(lambda _c=False: self.open_selected())
        self.btn_read.clicked.connect(lambda _c=False: self.mark_selected())
        self.btn_all.clicked.connect(lambda _c=False: self.mark_all())
        self.btn_reload.clicked.connect(lambda _c=False: self.reload())
        self.btn_close.clicked.connect(self.accept)
        self.table.itemSelectionChanged.connect(self._sel)
        self.table.cellDoubleClicked.connect(lambda *_: self.open_selected())
        self._sel()
        self.reload()

    def _err(self, exc: Exception) -> None:
        QMessageBox.warning(self, "Errore", exc.detail if isinstance(exc, ApiError) else str(exc))

    def reload(self) -> None:
        only = self.chk_unread.isChecked()
        self._run(lambda: self.api.notifications(only), self.set_rows, self._err)

    def set_rows(self, rows: list[NotificationOut]) -> None:
        self._rows = rows
        t = self.table
        t.setRowCount(len(rows))
        for r, n in enumerate(rows):
            dim = T["MUTED"] if n.read else None
            t.setItem(r, 0, item("" if n.read else "●", T["ACCENT"], bold=True))
            try:
                when = full_time(datetime.fromisoformat(n.ts).astimezone())
            except ValueError:
                when = n.ts
            t.setItem(r, 1, item(when, dim))
            t.setItem(r, 2, item(KIND_LABELS.get(n.kind, n.kind), None if n.read else KIND_COLORS.get(n.kind), bold=not n.read))
            t.setItem(r, 3, item(n.message + (f"  [branch {n.branch}]" if n.branch else ""), dim, bold=not n.read))
            t.setItem(r, 4, item(n.project_name, dim))
        unread = sum(1 for n in rows if not n.read)
        self.lbl.setText(f"{len(rows)} notifiche" + (f" · {unread} non lette" if unread else ""))
        self._sel()

    def selected(self) -> Optional[NotificationOut]:
        r = self.table.currentRow()
        return self._rows[r] if 0 <= r < len(self._rows) else None

    def _sel(self) -> None:
        n = self.selected()
        self.btn_open.setEnabled(n is not None and bool(n.ref or n.branch))
        self.btn_read.setEnabled(n is not None and not n.read)
        self.btn_all.setEnabled(any(not x.read for x in self._rows))

    def _marked(self, unread: int) -> None:
        self.unread_changed.emit(unread)
        self.reload()

    def mark_selected(self) -> None:
        n = self.selected()
        if n is None or n.read:
            return
        self._run(lambda: self.api.mark_notifications_read([n.id]), self._marked, self._err)

    def mark_all(self) -> None:
        self._run(lambda: self.api.mark_notifications_read(None), self._marked, self._err)

    def open_selected(self) -> None:
        n = self.selected()
        if n is None or not (n.ref or n.branch):
            return
        if not n.read:
            self._run(lambda: self.api.mark_notifications_read([n.id]), lambda u: self.unread_changed.emit(u), self._err)
        self.accept()
        self.open_requested.emit(n)
