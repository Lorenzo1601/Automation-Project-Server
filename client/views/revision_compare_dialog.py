"""Confronto di sola lettura tra due revisioni del Master: stesso visualizzatore delle modifiche dell'approvazione."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from client.gui_approval import KIND_IT, ChangeBrowser
from client.theme.tokens import T
from client.views.common import chip, full_time, heading, muted
from common.schemas import ChangeOut, MasterInfo
from common.schemas_revisions import CompareOut

FILTERS = [("Tutti i tipi", None), ("Aggiunti", "added"), ("Modificati", "modified"), ("Eliminati", "deleted")]


class CompareBrowser(ChangeBrowser):
    """``ChangeBrowser`` dell'approvazione, con i titoli adattati al confronto tra due revisioni (un solo gruppo)."""

    def __init__(self, from_rev: int, to_rev: int) -> None:
        super().__init__()
        self.from_rev, self.to_rev = from_rev, to_rev
        self.tree.setHeaderLabels(["Elemento", "Tipo", ""])
        self.tree.setColumnHidden(2, True)                       # l'origine (Proposta/Master) qui non ha senso

    def show_changes(self, changes: list[ChangeOut]) -> None:
        self.set_changes(f"rev {self.from_rev} → rev {self.to_rev}", changes, [])
        top, second = self.tree.topLevelItem(0), self.tree.topLevelItem(1)
        top.setText(0, f"Differenze da rev {self.from_rev} a rev {self.to_rev} ({len(changes)})")
        second.setHidden(True)

    def _selected(self, cur: Optional[QTreeWidgetItem], _prev=None) -> None:     # noqa: ANN001
        ch: Optional[ChangeOut] = cur.data(0, Qt.UserRole) if cur else None
        if ch is None:
            return
        self.path.setText(ch.path)
        self.diff.set_diff(ch.base_text, ch.new_text, f"rev {self.from_rev}", f"rev {self.to_rev}")


def _rev_line(m: MasterInfo) -> str:
    return (f"rev {m.revision}" + (f" «{m.label}»" if m.label else "") + f" — {m.updated_by}, "
            f"{full_time(m.updated_at)}" + (f" — {m.message}" if m.message else ""))


class RevisionCompareDialog(QDialog):
    """Mostra le differenze ``rev X → rev Y`` con conteggi, filtro per tipo ed esportazione del riepilogo (CSV/testo)."""

    def __init__(self, result: CompareOut, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.result = result
        self.a, self.b = result.from_info.revision, result.to_info.revision
        self.setWindowTitle(f"Confronto revisioni {self.a} → {self.b}")
        self.resize(1180, 700)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        self.lbl_title = heading(f"rev {self.a} → rev {self.b}", 2)
        lay.addWidget(self.lbl_title)
        self.lbl_from = muted("Da: " + _rev_line(result.from_info))
        self.lbl_to = muted("A: " + _rev_line(result.to_info))
        lay.addWidget(self.lbl_from)
        lay.addWidget(self.lbl_to)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.chips = QHBoxLayout()
        self.chips.setSpacing(6)
        row.addLayout(self.chips)
        row.addStretch(1)
        row.addWidget(QLabel("Mostra:"))
        self.filter = QComboBox()
        for text, key in FILTERS:
            self.filter.addItem(text, key)
        row.addWidget(self.filter)
        self.btn_export = QPushButton("Esporta riepilogo...")
        row.addWidget(self.btn_export)
        lay.addLayout(row)

        self.browser = CompareBrowser(self.a, self.b)
        lay.addWidget(self.browser, 1)
        self.lbl_empty = muted("Nessuna differenza tra le due revisioni.")
        self.lbl_empty.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.lbl_empty)
        self.lbl_warn = muted("  ".join(result.warnings))
        self.lbl_warn.setVisible(bool(result.warnings))
        lay.addWidget(self.lbl_warn)
        foot = QHBoxLayout()
        foot.addStretch(1)
        close = QPushButton("Chiudi")
        foot.addWidget(close)
        lay.addLayout(foot)

        for text, n, color in ((f"{len(result.changes)} differenze", len(result.changes), None),
                               (f"{result.n_added} aggiunte", result.n_added, T["GREEN"]),
                               (f"{result.n_modified} modificate", result.n_modified, T["ORANGE"]),
                               (f"{result.n_deleted} eliminate", result.n_deleted, T["RED"])):
            self.chips.addWidget(chip(text, color if n else None))
        self.filter.currentIndexChanged.connect(lambda _i: self._refresh())
        self.btn_export.clicked.connect(lambda: self.export_summary())
        close.clicked.connect(self.accept)
        self._refresh()

    # ------------------------------------------------------------------ dati
    def visible_changes(self) -> list[ChangeOut]:
        kind = self.filter.currentData()
        return [c for c in self.result.changes if kind is None or c.kind == kind]

    def set_filter(self, kind: Optional[str]) -> None:
        self.filter.setCurrentIndex(max(0, self.filter.findData(kind)))

    def _refresh(self) -> None:
        ch = self.visible_changes()
        self.browser.show_changes(ch)
        self.browser.setVisible(bool(ch))
        self.lbl_empty.setVisible(not ch)
        self.lbl_empty.setText("Nessuna differenza tra le due revisioni." if not self.result.changes
                               else "Nessuna differenza di questo tipo.")
        self.btn_export.setEnabled(bool(self.result.changes))

    # ------------------------------------------------------------------ esportazione
    def summary_rows(self) -> list[list[str]]:
        return [[c.path, KIND_IT[c.kind], c.title, c.label] for c in self.result.changes]

    def export_summary(self, path: Optional[str] = None) -> Optional[str]:
        """Salva il riepilogo delle differenze: ``.csv`` (UTF-8 per Excel, separatore ;) oppure testo. ``path`` salta il dialogo."""
        if not isinstance(path, str):
            path, _ = QFileDialog.getSaveFileName(self, "Esporta riepilogo", f"confronto_rev{self.a}_rev{self.b}.csv",
                                                  "CSV (*.csv);;Testo (*.txt)")
        if not path:
            return None
        rows = self.summary_rows()
        if Path(path).suffix.lower() == ".csv":
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["Percorso", "Tipo", "Titolo", "Elemento"])
                w.writerows(rows)
        else:
            r = self.result
            lines = [f"Confronto rev {self.a} → rev {self.b}", "Da: " + _rev_line(r.from_info), "A: " + _rev_line(r.to_info),
                     f"{len(r.changes)} differenze: {r.n_added} aggiunte, {r.n_modified} modificate, "
                     f"{r.n_deleted} eliminate", ""]
            lines += [f"[{k}] {p}" for p, k, _t, _l in rows]
            Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path
