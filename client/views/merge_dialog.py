"""Dialogo 'Richiedi Merge': mostra i file trovati nella cartella locale del progetto (stessa estensione del Master,
oppure tutti) e permette di sceglierne uno."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QVBoxLayout, QWidget)

from client.localfiles import Candidate, find_merge_candidates, looks_like_kind, norm_ext, pick_default
from client.theme.tokens import T
from client.theme.widgets import icon
from common.filekinds import L5K, L5X, file_dialog_filters
from client.views.common import full_time, heading, item, make_table, muted


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


class MergeRequestDialog(QDialog):
    def __init__(self, folder: "str | Path", working_file: str, base_rev: int, project_name: str = "",
                 parent: Optional[QWidget] = None, scan: Callable[..., list[Candidate]] = find_merge_candidates,
                 ext: str = ".L5X", kind: str = L5X):
        super().__init__(parent)
        self.folder, self.working_file, self._scan = Path(folder), working_file, scan
        self.ext, self.kind = norm_ext(ext), kind
        self._cands: list[Candidate] = []
        self.setWindowTitle("Richiedi Merge")
        self.setMinimumSize(760, 520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 16)
        lay.setSpacing(10)
        lay.addWidget(heading("Quale file vuoi unire al Master?", 2))
        self.lbl_folder = QLabel(f"Cartella locale: <b>{self.folder}</b> &nbsp;·&nbsp; base: rev {base_rev}"
                                 + (f" &nbsp;·&nbsp; {project_name}" if project_name else ""))
        self.lbl_folder.setTextFormat(Qt.RichText)
        self.lbl_folder.setWordWrap(True)
        lay.addWidget(self.lbl_folder)
        self.lbl_hint = muted(f"Sono elencati i file {self.ext.upper()} della cartella (il Master scaricato e le sue copie "
                              "sono esclusi). Salva il lavoro in questa cartella e premi «Aggiorna»; per altri tipi di "
                              "file spunta «Mostra tutti i file».")
        lay.addWidget(self.lbl_hint)

        self.table = make_table(["File", "Modificato", "Dimensione", "Stato"], stretch_col=0)
        self.table.setColumnWidth(1, 150)
        self.table.setColumnWidth(2, 90)
        self.table.setColumnWidth(3, 190)
        lay.addWidget(self.table, 1)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.RichText)
        lay.addWidget(self.info)

        row = QHBoxLayout()
        self.btn_rescan = QPushButton("  Aggiorna")
        self.btn_rescan.setIcon(icon("refresh", T["TEXT"], 18))
        self.btn_other = QPushButton("  Altro file...")
        self.btn_other.setIcon(icon("folder", T["TEXT"], 18))
        row.addWidget(self.btn_rescan)
        row.addWidget(self.btn_other)
        self.chk_all = QCheckBox("Mostra tutti i file")
        self.chk_all.setToolTip("Elenca qualsiasi file della cartella, non solo quelli con l'estensione del Master")
        row.addWidget(self.chk_all)
        row.addStretch(1)
        lay.addLayout(row)

        lay.addWidget(QLabel("Descrizione delle modifiche"))
        self.message = QLineEdit()
        self.message.setPlaceholderText("es. Aggiunta logica del nastro N3")
        lay.addWidget(self.message)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_btn = self.buttons.button(QDialogButtonBox.Ok)
        self.ok_btn.setText("Invia richiesta")
        self.ok_btn.setProperty("variant", "primary")
        self.buttons.button(QDialogButtonBox.Cancel).setText("Annulla")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)

        self.btn_rescan.clicked.connect(self.rescan)
        self.btn_other.clicked.connect(self.browse_other)
        self.chk_all.toggled.connect(lambda _on: self.rescan())
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.itemDoubleClicked.connect(lambda _i: self.ok_btn.isEnabled() and self.accept())
        self.rescan()

    # ------------------------------------------------------------------ API
    def candidates(self) -> list[Candidate]:
        return list(self._cands)

    def selected(self) -> Optional[Candidate]:
        r = self.table.currentRow()
        return self._cands[r] if 0 <= r < len(self._cands) else None

    def selected_path(self) -> Optional[Path]:
        c = self.selected()
        return c.path if c else None

    def message_text(self) -> str:
        return self.message.text().strip()

    def select_path(self, path: "str | Path") -> bool:
        for r, c in enumerate(self._cands):
            if c.path == Path(path):
                self.table.selectRow(r)
                return True
        return False

    # ------------------------------------------------------------------ interno
    def rescan(self, keep: Optional[Path] = None) -> None:
        keep = keep if isinstance(keep, Path) else self.selected_path()
        extras = [c for c in self._cands if c.path.parent != self.folder and not str(c.path).startswith(str(self.folder))]
        self._cands = self._scan(self.folder, self.working_file, ext=self.ext, kind=self.kind,
                                 all_files=self.chk_all.isChecked()) + [e for e in extras if e.path.exists()]
        self._fill()
        if not (keep and self.select_path(keep)):
            d = pick_default(self._cands)
            if d:
                self.select_path(d.path)
        self._selection_changed()

    def _fill(self) -> None:
        self.table.setRowCount(len(self._cands))
        for r, c in enumerate(self._cands):
            try:
                rel = str(c.path.relative_to(self.folder))
            except ValueError:
                rel = str(c.path)
            self.table.setItem(r, 0, item(rel + ("  (file del checkout)" if c.is_working else ""), bold=c.modified and c.looks_ok))
            self.table.setItem(r, 1, item(full_time(datetime.fromtimestamp(c.mtime).astimezone())))
            self.table.setItem(r, 2, item(human_size(c.size)))
            if not c.looks_ok:
                st, col = f"Non sembra un file {self._kind_name()}", T["RED"]
            elif c.unchanged:
                st, col = "Identico al Master", T["MUTED"]
            elif c.modified:
                st, col = "Modificato", T["GREEN"]
            else:
                st, col = "Da verificare", T["ORANGE"]
            self.table.setItem(r, 3, item(st, col))

    def _selection_changed(self) -> None:
        c = self.selected()
        self.ok_btn.setEnabled(c is not None and c.looks_ok)
        if not self._cands:
            self.info.setText(f"<span style='color:{T['ORANGE']}'>Nessun file {self.ext.upper()} trovato in questa cartella."
                              "</span> Salva il tuo progetto qui, spunta «Mostra tutti i file» oppure usa «Altro file...».")
        elif c is None:
            self.info.setText("")
        elif not c.looks_ok:
            self.info.setText(f"<span style='color:{T['RED']}'>Il file non e' un {self._kind_name()} valido: il Master "
                              f"del progetto e' di tipo {self.ext.upper()}.</span>")
        elif c.unchanged:
            self.info.setText(f"<span style='color:{T['ORANGE']}'>Il file e' identico al Master scaricato: "
                              "non ci sono modifiche da unire.</span>")
        else:
            self.info.setText("")

    def _kind_name(self) -> str:
        return {L5X: "L5X (export XML di Studio 5000)", L5K: "L5K (export ASCII Rockwell)"}.get(self.kind, "del tipo del Master")

    def browse_other(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Seleziona il file da unire", str(self.folder),
                                              file_dialog_filters(self.ext, self.kind))
        if path:
            self.add_external(Path(path))

    def add_external(self, p: Path) -> None:
        """Aggiunge alla lista un file scelto a mano (anche fuori dalla cartella locale) e lo seleziona."""
        if not self.select_path(p):
            st = p.stat()
            self._cands.insert(0, Candidate(p, st.st_size, st.st_mtime, modified=True,
                                            looks_ok=looks_like_kind(p, self.kind)))
            self._fill()
            self.select_path(p)
        self._selection_changed()
