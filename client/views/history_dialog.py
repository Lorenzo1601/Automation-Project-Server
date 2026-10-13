"""Storico revisioni del Master: download (tutti) e ripristino/rollback (solo Admin)."""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel,
                               QMessageBox, QPushButton, QVBoxLayout, QWidget)

from client.localfiles import norm_ext
from client.theme.tokens import T
from client.theme.widgets import icon
from client.views.common import full_time, heading, item, make_table, muted
from common.filekinds import DEFAULT_NAME, rev_filename
from common.roles import Role
from common.schemas import MasterInfo
from common.schemas_revisions import LABEL_MAX, LABEL_PRESETS

PURGED_LABEL = "eliminata (limite revisioni)"
MIN_REASON = 5                                   # caratteri minimi del motivo di un rollback


def revision_save_name(name: Optional[str], revision: int, ext: Optional[str]) -> str:
    """Nome proposto per salvare una revisione: ``<nome>_revN<ext>`` (stesso criterio dei file del checkout)."""
    return rev_filename(name or DEFAULT_NAME, revision, norm_ext(ext))


def revision_save_filter(ext: Optional[str]) -> str:
    return f"File del progetto (*{norm_ext(ext)});;Tutti i file (*)"


class HistoryDialog(QDialog):
    """Revisioni del Master: download (tutti) e ripristino (solo Admin).

    Le revisioni ``purged`` (file eliminato dal server per il limite di revisioni) restano elencate, in grigio, ma non
    si possono scaricare ne' ripristinare. ``max_revisions`` (opzionale) alimenta la nota in fondo.
    """

    download_requested = Signal(int)
    restore_requested = Signal(int)
    compare_requested = Signal(int, int)        # (rev di partenza, rev di arrivo): la piu' vecchia per prima
    label_requested = Signal(int, str)          # (rev, etichetta; "" = toglie l'etichetta) - Lead/Admin
    lock_requested = Signal(int, bool)          # (rev, True = blocca / False = sblocca; sbloccare: solo Admin)

    def __init__(self, history: list[MasterInfo], role: Role, parent: Optional[QWidget] = None,
                 max_revisions: Optional[int] = None):
        super().__init__(parent)
        self.setWindowTitle("Storico revisioni del Master")
        self.resize(1000, 500)
        self._rows: list[MasterInfo] = []
        self._max_revisions = max_revisions
        self._role = role
        # hook sostituibili nei test (nessun dialogo modale)
        self.ask_label: Callable[[int, str], Optional[str]] = self._ask_label
        self.confirm_lock: Callable[[int, bool], bool] = self._confirm_lock
        self.pick_pair: Callable[[], Optional[tuple[int, int]]] = self._pick_pair
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.addWidget(heading("Storico revisioni", 2))
        self.table = make_table(["Rev", "Data", "Autore", "Messaggio", "Stato", "Etichetta"], stretch_col=3)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)          # Ctrl+clic: due righe da confrontare
        self.table.setColumnWidth(4, 190)
        self.table.setColumnWidth(5, 170)
        lay.addWidget(self.table, 1)
        self.lbl_note = muted("")
        lay.addWidget(self.lbl_note)
        btns = QHBoxLayout()
        self.btn_dl = QPushButton("  Scarica revisione")
        self.btn_dl.setIcon(icon("download", T["TEXT"], 18))
        self.btn_restore = QPushButton("Ripristina come nuova revisione...")
        self.btn_restore.setProperty("variant", "warning")
        self.btn_restore.setVisible(role >= Role.ADMIN)
        self.btn_compare = QPushButton("Confronta...")
        self.btn_compare.setToolTip("Mostra le differenze tra due revisioni conservate: seleziona due righe (Ctrl+clic) "
                                    "oppure premi senza selezione per sceglierle")
        self.btn_label = QPushButton("Etichetta...")
        self.btn_label.setToolTip("Dai un nome alla revisione (es. FAT, SAT, Consegna cliente)")
        self.btn_label.setVisible(role >= Role.LEAD)
        self.btn_lock = QPushButton("Blocca revisione")
        self.btn_lock.setIcon(icon("lock", T["TEXT"], 16))
        self.btn_lock.setVisible(role >= Role.LEAD)
        close = QPushButton("Chiudi")
        btns.addWidget(self.btn_dl)
        btns.addWidget(self.btn_compare)
        btns.addWidget(self.btn_label)
        btns.addWidget(self.btn_lock)
        btns.addWidget(self.btn_restore)
        btns.addStretch(1)
        btns.addWidget(close)
        lay.addLayout(btns)
        self.btn_dl.clicked.connect(lambda: self._emit(self.download_requested))
        self.btn_restore.clicked.connect(lambda: self._emit(self.restore_requested))
        self.btn_compare.clicked.connect(self._compare)
        self.btn_label.clicked.connect(self._label)
        self.btn_lock.clicked.connect(self._toggle_lock)
        close.clicked.connect(self.accept)
        self.table.itemSelectionChanged.connect(self._sel_changed)
        self.set_history(history, max_revisions)

    # ------------------------------------------------------------------ dati
    def set_history(self, history: list[MasterInfo], max_revisions: Optional[int] = None,
                    keep_selection: bool = False) -> None:
        """(Ri)popola la tabella; la selezione di default e' la revisione piu' recente ancora conservata
        (``keep_selection=True``: resta sulla revisione selezionata, se c'e' ancora)."""
        if max_revisions is not None:
            self._max_revisions = max_revisions
        keep = self.selected_revision() if keep_selection else None
        self._rows = sorted(history, key=lambda m: -m.revision)
        t = self.table
        t.blockSignals(True)
        t.clearContents()
        t.setRowCount(len(self._rows))
        for r, m in enumerate(self._rows):
            if m.purged:
                grey = T["MUTED"]
                t.setItem(r, 0, item(f"rev {m.revision}", grey, data=m.revision))
                t.setItem(r, 1, item(full_time(m.updated_at), grey))
                t.setItem(r, 2, item(m.updated_by, grey))
                t.setItem(r, 3, item(m.message, grey))
                t.setItem(r, 4, item(PURGED_LABEL, grey))
                t.setItem(r, 5, item(m.label, grey))
                for c in range(6):
                    cell = t.item(r, c)
                    cell.setToolTip("Il file di questa revisione non è più sul server: restano solo i dati dello "
                                    "storico, non si può scaricare né ripristinare.")
            else:
                t.setItem(r, 0, item(f"rev {m.revision}", T["ACCENT"], data=m.revision, bold=True))
                t.setItem(r, 1, item(full_time(m.updated_at)))
                t.setItem(r, 2, item(m.updated_by))
                t.setItem(r, 3, item(m.message))
                t.setItem(r, 4, item("bloccata" if m.locked else "disponibile", T["ORANGE"] if m.locked else T["GREEN"]))
                t.setItem(r, 5, item(m.label, T["TEXT"], bold=bool(m.label)))
                if m.locked:
                    t.item(r, 0).setIcon(icon("lock", T["ORANGE"], 16))
                    t.item(r, 0).setToolTip("Revisione bloccata: il limite di revisioni non la elimina mai"
                                            + (f" (da {m.locked_by})" if m.locked_by else ""))
                if m.label and m.labeled_by:
                    t.item(r, 5).setToolTip(f"Etichetta di {m.labeled_by}")
        t.blockSignals(False)
        first = next((i for i, m in enumerate(self._rows) if not m.purged), 0 if self._rows else -1)
        if keep is not None:
            first = next((i for i, m in enumerate(self._rows) if m.revision == keep), first)
        if first >= 0:
            t.selectRow(first)
        self._update_note()
        self._sel_changed()

    def _update_note(self) -> None:
        purged = sum(1 for m in self._rows if m.purged)
        parts = []
        locked = sum(1 for m in self._rows if m.locked and not m.purged)
        if self._max_revisions:
            kept = sum(1 for m in self._rows if not m.purged and not m.locked)
            extra = (f" ({kept} conservate + {locked} bloccate: le bloccate non contano nel limite e non vengono mai "
                     "eliminate)") if locked else ""
            parts.append(f"Il server conserva le ultime {self._max_revisions} revisioni{extra}.")
        elif locked:
            parts.append(f"{locked} revisioni bloccate (mai eliminate dal limite di revisioni).")
        if purged:
            parts.append("1 revisione più vecchia è stata eliminata dal server" if purged == 1 else
                         f"{purged} revisioni più vecchie sono state eliminate dal server")
            parts[-1] += ": nello storico restano solo i dati."
        self.lbl_note.setText(" ".join(parts))
        self.lbl_note.setVisible(bool(parts))

    # ------------------------------------------------------------------ selezione
    def selected_info(self) -> Optional[MasterInfo]:
        r = self.table.currentRow()
        return self._rows[r] if 0 <= r < len(self._rows) else None

    def selected_revision(self) -> Optional[int]:
        m = self.selected_info()
        return m.revision if m is not None else None

    def _sel_changed(self) -> None:
        m = self.selected_info()
        purged = m is not None and m.purged
        head = max((x.revision for x in self._rows), default=None)
        self.btn_dl.setEnabled(m is not None and not purged)
        self.btn_dl.setToolTip("Il file di questa revisione non è più sul server" if purged else "")
        self.btn_restore.setEnabled(m is not None and not purged and m.revision != head)
        self.btn_restore.setToolTip("Il file di questa revisione non è più sul server" if purged else
                                    "È già la revisione corrente" if (m is not None and m.revision == head) else "")
        kept = [x for x in self._rows if not x.purged]
        self.btn_compare.setEnabled(len(kept) >= 2)
        self.btn_label.setEnabled(m is not None and not purged)
        self.btn_label.setToolTip("Il file di questa revisione non è più sul server" if purged else
                                  "Dai un nome alla revisione (es. FAT, SAT, Consegna cliente)")
        locked = m is not None and m.locked and not purged
        self.btn_lock.setText("Sblocca" if locked else "Blocca revisione")
        self.btn_lock.setEnabled(m is not None and not purged and not (locked and self._role < Role.ADMIN))
        self.btn_lock.setToolTip(
            "Il file di questa revisione non è più sul server" if purged else
            "Solo un Project Administrator può sbloccare una revisione" if (locked and self._role < Role.ADMIN) else
            "Sblocca: il limite di revisioni potrà di nuovo eliminarla" if locked else
            "Blocca: il limite di revisioni non eliminerà mai questa revisione (non conta nel limite)")

    def _emit(self, sig: Signal) -> None:
        m = self.selected_info()
        if m is not None and not m.purged:
            sig.emit(m.revision)

    # ------------------------------------------------------------------ confronto / etichette / blocco
    def selected_pair(self) -> Optional[tuple[int, int]]:
        """Le due revisioni conservate selezionate nella tabella (la piu' vecchia per prima), se sono esattamente due."""
        revs = sorted({self._rows[i.row()].revision for i in self.table.selectionModel().selectedRows()
                       if 0 <= i.row() < len(self._rows) and not self._rows[i.row()].purged})
        return (revs[0], revs[1]) if len(revs) == 2 else None

    def _compare(self) -> None:
        pair = self.selected_pair() or self.pick_pair()
        if pair is not None:
            self.compare_requested.emit(*pair)

    def _pick_pair(self) -> Optional[tuple[int, int]]:
        """Due menu a tendina con le sole revisioni conservate (le eliminate non sono selezionabili)."""
        kept = [m for m in self._rows if not m.purged]
        if len(kept) < 2:
            return None
        dlg = QDialog(self)
        dlg.setWindowTitle("Confronta revisioni")
        form = QFormLayout(dlg)
        boxes = []
        for text, default in (("Da (precedente):", kept[1]), ("A (successiva):", kept[0])):
            cb = QComboBox()
            for m in reversed(kept):
                cb.addItem(f"rev {m.revision}" + (f"  ·  {m.label}" if m.label else ""), m.revision)
            cb.setCurrentIndex(cb.findData(default.revision))
            form.addRow(text, cb)
            boxes.append(cb)
        row = QHBoxLayout()
        ok, cancel = QPushButton("Confronta"), QPushButton("Annulla")
        ok.setProperty("variant", "primary")
        row.addStretch(1)
        row.addWidget(cancel)
        row.addWidget(ok)
        form.addRow(row)
        ok.clicked.connect(dlg.accept)
        cancel.clicked.connect(dlg.reject)
        if dlg.exec() != QDialog.Accepted:
            return None
        a, b = boxes[0].currentData(), boxes[1].currentData()
        if a == b:
            QMessageBox.warning(self, "Confronta revisioni", "Scegli due revisioni diverse.")
            return None
        return (min(a, b), max(a, b))

    def _ask_label(self, revision: int, current: str) -> Optional[str]:
        items = [""] + [p for p in LABEL_PRESETS]
        if current and current not in items:
            items.append(current)
        text, ok = QInputDialog.getItem(self, "Etichetta della revisione",
                                        f"Etichetta per la rev {revision} (scegli un suggerimento o scrivila; "
                                        f"vuota = nessuna, massimo {LABEL_MAX} caratteri):",
                                        items, items.index(current) if current in items else 0, True)
        return text.strip()[:LABEL_MAX] if ok else None

    def _label(self) -> None:
        m = self.selected_info()
        if m is None or m.purged:
            return
        text = self.ask_label(m.revision, m.label)
        if text is not None and text != m.label:
            self.label_requested.emit(m.revision, text)

    def _confirm_lock(self, revision: int, lock: bool) -> bool:
        if not lock:
            return True
        return QMessageBox.question(
            self, "Blocca revisione",
            f"Bloccare la rev {revision}?\n\n• il limite di revisioni non la eliminerà mai (non conta nel limite);\n"
            "• potrà sbloccarla solo un Project Administrator.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def _toggle_lock(self) -> None:
        m = self.selected_info()
        if m is None or m.purged:
            return
        lock = not m.locked
        if (not lock and self._role < Role.ADMIN) or not self.confirm_lock(m.revision, lock):
            return
        self.lock_requested.emit(m.revision, lock)
