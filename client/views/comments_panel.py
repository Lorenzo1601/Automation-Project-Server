"""Pannello «Discussione» di una richiesta di merge: commenti a thread, risposte, modifica/eliminazione, menzioni.

Usato da ``ApprovalDialog`` (scheda «Discussione»). Regole (le stesse del server, che le applica comunque):
  * scrivono Developer, Lead e Admin; i Viewer leggono soltanto;
  * un commento si modifica solo se e' tuo ed entro 15 minuti; si elimina se e' tuo o se sei Admin (resta un
    segnaposto «[commento eliminato]» per non spezzare il thread);
  * ``@username`` valido (esiste ed ha accesso al progetto) e' evidenziato; se sei tu, in modo piu' marcato.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from client.api_client import ApiClient, ApiError
from client.theme.tokens import T
from client.theme.widgets import Avatar, EmptyState
from client.views.common import chip, drop_widget, full_time, human_time, role_chip, tint
from client.workers import run_async
from common.roles import Role
from common.schemas_comments import DELETED_TEXT, EDIT_WINDOW_S, MAX_COMMENT_LEN, MENTION_RE, CommentOut

NO_ANCHOR = "(nessun riferimento)"
INDENT = 26                      # rientro per livello di risposta (massimo 4 livelli)


def comment_html(text: str, valid: list[str], me: str) -> str:
    """Testo del commento -> HTML sicuro: escape, a capo, ``@menzioni`` valide colorate (la mia con fondo evidenziato)."""
    ok = {m.lower() for m in valid}

    def sub(m: re.Match) -> str:
        name = m.group(1).rstrip(".-")
        tail = m.group(1)[len(name):]
        if name.lower() not in ok:
            return html.escape(m.group(0))
        mine = name.lower() == me.lower()
        style = (f"color:{T['TEXT_ON_ACCENT']}; background-color:{T['ACCENT']}; font-weight:700;" if mine
                 else f"color:{T['ACCENT']}; font-weight:600;")
        return f'<span style="{style}">&nbsp;@{html.escape(name)}&nbsp;</span>' + html.escape(tail)

    out, last = [], 0
    for m in MENTION_RE.finditer(text):
        out.append(html.escape(text[last:m.start()]))
        out.append(sub(m))
        last = m.end()
    out.append(html.escape(text[last:]))
    return "".join(out).replace("\n", "<br>")


class _Editor(QPlainTextEdit):
    """Riquadro di scrittura: Ctrl+Invio invia."""
    submit = Signal()

    def keyPressEvent(self, e: QKeyEvent) -> None:               # noqa: N802
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and e.modifiers() & Qt.ControlModifier:
            self.submit.emit()
            return
        super().keyPressEvent(e)


class CommentItem(QFrame):
    """Un commento: avatar, autore + ruolo, orario relativo, ancora, testo e pulsanti."""
    reply = Signal(int)
    edit = Signal(int)
    delete = Signal(int)

    def __init__(self, c: CommentOut, me: str, can_reply: bool, can_edit: bool, can_delete: bool,
                 anchor_label: str = "", depth: int = 0, parent_author: str = ""):
        super().__init__()
        self.comment = c
        self.setObjectName("InfoBar")
        outer = QHBoxLayout(self)
        outer.setContentsMargins(10 + min(depth, 4) * INDENT, 8, 12, 8)
        outer.setSpacing(10)
        av = Avatar(c.author, 32)
        outer.addWidget(av, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.lbl_author = QLabel(c.author + ("  (tu)" if c.author == me else ""))
        self.lbl_author.setStyleSheet("font-weight:700;")
        head.addWidget(self.lbl_author)
        if c.author_role:
            head.addWidget(role_chip(c.author_role))
        self.lbl_time = QLabel(human_time(c.ts) + (" · modificato" if c.edited_at and not c.deleted else ""))
        self.lbl_time.setProperty("muted", True)
        self.lbl_time.setToolTip(f"Pubblicato: {full_time(c.ts)}" + (f"\nModificato: {full_time(c.edited_at)}"
                                                                      if c.edited_at else ""))
        head.addWidget(self.lbl_time)
        if parent_author:
            rep = QLabel(f"↩ risposta a {parent_author}")
            rep.setProperty("muted", True)
            head.addWidget(rep)
        head.addStretch(1)
        col.addLayout(head)
        self.anchor_chip: Optional[QFrame] = None
        if c.anchor and not c.deleted:
            self.anchor_chip = chip("↳ " + (anchor_label or c.anchor), T["ACCENT_ALT"], mono=True, max_chars=60)
            self.anchor_chip.setToolTip(f"Riferito a: {c.anchor}")
            row = QHBoxLayout()
            row.addWidget(self.anchor_chip)
            row.addStretch(1)
            col.addLayout(row)
        self.body = QLabel()
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.RichText)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        if c.deleted:
            self.body.setText(f'<i style="color:{T["MUTED"]}">{html.escape(c.text or DELETED_TEXT)}</i>')
        else:
            self.body.setText(comment_html(c.text, c.mentions, me))
        self.body.setStyleSheet("font-size:13px;")
        col.addWidget(self.body)
        acts = QHBoxLayout()
        acts.setSpacing(4)
        self.btn_reply = self._btn("Rispondi", can_reply and not c.deleted, lambda: self.reply.emit(c.id))
        self.btn_edit = self._btn("Modifica", can_edit and not c.deleted, lambda: self.edit.emit(c.id))
        self.btn_delete = self._btn("Elimina", can_delete and not c.deleted, lambda: self.delete.emit(c.id))
        for b in (self.btn_reply, self.btn_edit, self.btn_delete):
            acts.addWidget(b)
        acts.addStretch(1)
        col.addLayout(acts)
        outer.addLayout(col, 1)
        if c.mentions and me.lower() in {m.lower() for m in c.mentions} and not c.deleted:
            self.setStyleSheet(f"QFrame#InfoBar {{ border: 1px solid {tint(T['ACCENT'], 120)}; }}")

    @staticmethod
    def _btn(text: str, visible: bool, slot: Callable[[], None]) -> QPushButton:
        b = QPushButton(text)
        b.setProperty("variant", "ghost")
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet("padding:2px 8px; font-size:12px;")
        b.setVisible(visible)
        b.clicked.connect(slot)
        return b


class CommentsPanel(QWidget):
    """Discussione di una richiesta: elenco a thread + riquadro di scrittura."""

    count_changed = Signal(int)            # numero di commenti non eliminati

    def __init__(self, api: ApiClient, proposal_id: str, my_role: Optional[Role] = None,
                 run: Callable = run_async, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.api, self.pid, self._run = api, proposal_id, run
        self.role = my_role if my_role is not None else api.role
        self.me = api.user.username if api.user else ""
        self.can_write = self.role >= Role.DEVELOPER
        self.now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
        self.comments: list[CommentOut] = []
        self.items: dict[int, CommentItem] = {}
        self._reply_to: Optional[int] = None
        self._editing: Optional[int] = None
        self._anchor_labels: dict[str, str] = {}
        self._scroll_end = False

        # ------------------------------------------------------------ elenco
        self.body = QWidget()
        self.list_lay = QVBoxLayout(self.body)
        self.list_lay.setContentsMargins(0, 0, 6, 0)
        self.list_lay.setSpacing(8)
        self.list_lay.addStretch(1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setWidget(self.body)
        self.empty = EmptyState("users", "Nessun commento: avvia la discussione",
                                "Usa questo spazio per chiarire dubbi sulla richiesta prima di dare il consenso. "
                                "Scrivi @nome per menzionare un collega.", dashed=False, compact=True)

        # ------------------------------------------------------------ scrittura
        self.reply_bar = QFrame()
        self.reply_bar.setObjectName("InfoBar")
        rb = QHBoxLayout(self.reply_bar)
        rb.setContentsMargins(10, 4, 6, 4)
        self.reply_label = QLabel()
        self.reply_label.setProperty("muted", True)
        self.btn_cancel_reply = QPushButton("Annulla")
        self.btn_cancel_reply.setProperty("variant", "ghost")
        self.btn_cancel_reply.clicked.connect(self.cancel_compose)
        rb.addWidget(self.reply_label, 1)
        rb.addWidget(self.btn_cancel_reply)
        self.reply_bar.hide()

        self.anchor_label = QLabel("Riferito a")
        self.anchor_label.setProperty("muted", True)
        self.anchor = QComboBox()
        self.anchor.setMinimumWidth(260)
        self.anchor.addItem(NO_ANCHOR, None)
        self.counter = QLabel("")
        self.counter.setProperty("muted", True)
        self.editor = _Editor()
        self.editor.setPlaceholderText("Scrivi un commento… usa @nome per menzionare un collega (Ctrl+Invio per inviare)")
        self.editor.setFixedHeight(76)
        self.btn_send = QPushButton("Invia")
        self.btn_send.setProperty("variant", "primary")
        self.btn_send.setMinimumHeight(34)
        self.note = QLabel("Sola lettura: il ruolo Viewer può leggere la discussione ma non scrivere.")
        self.note.setProperty("muted", True)
        self.note.setWordWrap(True)

        arow = QHBoxLayout()
        arow.setSpacing(8)
        arow.addWidget(self.anchor_label)
        arow.addWidget(self.anchor)
        arow.addStretch(1)
        arow.addWidget(self.counter)
        brow = QHBoxLayout()
        brow.addWidget(self.note, 1)
        brow.addWidget(self.btn_send, 0, Qt.AlignRight)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(self.empty, 1)
        lay.addWidget(self.scroll, 1)
        lay.addWidget(self.reply_bar)
        lay.addLayout(arow)
        lay.addWidget(self.editor)
        lay.addLayout(brow)

        self.editor.textChanged.connect(self._text_changed)
        self.editor.submit.connect(self.send)
        self.btn_send.clicked.connect(self.send)
        for w in (self.anchor_label, self.anchor, self.editor, self.btn_send):
            w.setVisible(self.can_write)
        self.note.setVisible(not self.can_write)
        self.counter.setVisible(self.can_write)
        self._text_changed()
        self._render()

    # ------------------------------------------------------------------ dati
    @property
    def count(self) -> int:
        return sum(1 for c in self.comments if not c.deleted)

    def reload(self) -> None:
        self._run(lambda: self.api.comments(self.pid), self.set_comments, self._error)

    def on_event(self, ev: dict) -> bool:
        """Inoltrato dalla finestra: ricarica se l'evento ``comment_*`` riguarda questa richiesta."""
        p = ev.get("proposal")
        if str(ev.get("type", "")).startswith("comment_") and p and p.get("id") == self.pid:
            self.reload()
            return True
        return False

    def set_comments(self, comments: list[CommentOut]) -> None:
        self.comments = list(comments)
        if self._reply_to is not None and not any(c.id == self._reply_to for c in self.comments):
            self.cancel_compose()
        if self._editing is not None and not any(c.id == self._editing for c in self.comments):
            self.cancel_compose()
        self._render()
        self.count_changed.emit(self.count)

    def set_anchors(self, items: list[tuple[str, str]]) -> None:
        """``items``: (valore, etichetta) dei cambiamenti / conflitti a cui ci si puo' riferire."""
        keep = self.anchor.currentData()
        self._anchor_labels = {v: lab for v, lab in items}
        self.anchor.blockSignals(True)
        self.anchor.clear()
        self.anchor.addItem(NO_ANCHOR, None)
        for v, lab in items:
            self.anchor.addItem(lab if len(lab) <= 90 else lab[:89] + "…", v)
            self.anchor.setItemData(self.anchor.count() - 1, v, Qt.ToolTipRole)
        i = self.anchor.findData(keep) if keep else 0
        self.anchor.setCurrentIndex(max(0, i))
        self.anchor.blockSignals(False)
        if self.comments:
            self._render()                           # le etichette delle ancore sono cambiate

    # ------------------------------------------------------------------ rendering
    def _can_edit(self, c: CommentOut) -> bool:
        if not self.can_write or c.author != self.me or c.deleted:
            return False
        ts = c.ts if c.ts.tzinfo else c.ts.replace(tzinfo=timezone.utc)
        return (self.now() - ts).total_seconds() <= EDIT_WINDOW_S

    def _can_delete(self, c: CommentOut) -> bool:
        return not c.deleted and ((self.can_write and c.author == self.me) or self.role >= Role.ADMIN)

    def _order(self) -> list[tuple[CommentOut, int]]:
        """(commento, profondita'): radici in ordine cronologico, ciascuna seguita dalle risposte (depth-first)."""
        by_id = {c.id: c for c in self.comments}
        kids: dict[Optional[int], list[CommentOut]] = {}
        for c in sorted(self.comments, key=lambda x: (x.ts, x.id)):
            kids.setdefault(c.reply_to if c.reply_to in by_id else None, []).append(c)
        out: list[tuple[CommentOut, int]] = []

        def walk(parent: Optional[int], depth: int) -> None:
            for c in kids.get(parent, []):
                out.append((c, depth))
                walk(c.id, depth + 1)
        walk(None, 0)
        return out

    def _render(self) -> None:
        bar = self.scroll.verticalScrollBar()
        at_end = self._scroll_end or bar.value() >= bar.maximum() - 4
        old = bar.value()
        while self.list_lay.count() > 1:
            it = self.list_lay.takeAt(0)
            if it.widget():
                drop_widget(it.widget())
        self.items.clear()
        by_id = {c.id: c for c in self.comments}
        for c, depth in self._order():
            parent = by_id.get(c.reply_to) if c.reply_to else None
            w = CommentItem(c, self.me, self.can_write, self._can_edit(c), self._can_delete(c),
                            self._anchor_labels.get(c.anchor or "", ""), depth, parent.author if parent else "")
            w.reply.connect(self.start_reply)
            w.edit.connect(self.start_edit)
            w.delete.connect(self.delete)
            self.list_lay.insertWidget(self.list_lay.count() - 1, w)
            self.items[c.id] = w
        self.empty.setVisible(not self.comments)
        self.scroll.setVisible(bool(self.comments))
        bar.setValue(bar.maximum() if at_end else old)
        if at_end:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))
        self._scroll_end = False

    # ------------------------------------------------------------------ scrittura
    def _text_changed(self) -> None:
        n = len(self.editor.toPlainText().strip())
        over = n > MAX_COMMENT_LEN
        self.counter.setText(f"{n}/{MAX_COMMENT_LEN}")
        self.counter.setStyleSheet(f"color:{T['RED']};" if over else "")
        self.btn_send.setEnabled(self.can_write and 0 < n and not over)

    def start_reply(self, cid: int) -> None:
        c = next((x for x in self.comments if x.id == cid), None)
        if c is None or not self.can_write:
            return
        self._editing, self._reply_to = None, cid
        self.reply_label.setText(f"Rispondi a {c.author}: «{c.text[:70]}{'…' if len(c.text) > 70 else ''}»")
        self.btn_send.setText("Invia")
        self.anchor.setEnabled(True)
        self.reply_bar.show()
        self.editor.setFocus()

    def start_edit(self, cid: int) -> None:
        c = next((x for x in self.comments if x.id == cid), None)
        if c is None or not self._can_edit(c):
            return
        self._reply_to, self._editing = None, cid
        self.reply_label.setText(f"Modifica del tuo commento (entro {EDIT_WINDOW_S // 60} minuti dalla pubblicazione)")
        self.btn_send.setText("Salva modifica")
        self.anchor.setEnabled(False)
        self.editor.setPlainText(c.text)
        self.reply_bar.show()
        self.editor.setFocus()

    def cancel_compose(self) -> None:
        if self._editing is not None:
            self.editor.clear()
        self._reply_to = self._editing = None
        self.btn_send.setText("Invia")
        self.anchor.setEnabled(True)
        self.reply_bar.hide()

    def send(self) -> None:
        text = self.editor.toPlainText().strip()
        if not self.can_write or not text or len(text) > MAX_COMMENT_LEN:
            return
        api, pid = self.api, self.pid
        self.btn_send.setEnabled(False)
        if self._editing is not None:
            cid = self._editing
            self._run(lambda: api.edit_comment(cid, text), self._sent, self._error)
        else:
            reply, anchor = self._reply_to, self.anchor.currentData()
            self._run(lambda: api.add_comment(pid, text, reply_to=reply, anchor=anchor), self._sent, self._error)

    def _sent(self, _c: CommentOut) -> None:
        self.editor.clear()
        self.cancel_compose()
        self.anchor.setCurrentIndex(0)
        self._scroll_end = True
        self._text_changed()
        self.reload()

    def delete(self, cid: int) -> None:
        c = next((x for x in self.comments if x.id == cid), None)
        if c is None or not self._can_delete(c):
            return
        if QMessageBox.question(self, "Elimina commento",
                                "Eliminare questo commento? Resterà il segnaposto «[commento eliminato]».") != QMessageBox.Yes:
            return
        api = self.api
        self._run(lambda: api.delete_comment(cid), lambda _c: self.reload(), self._error)

    def _error(self, exc: Exception) -> None:
        self._text_changed()
        QMessageBox.warning(self, "Discussione", exc.detail if isinstance(exc, ApiError) else str(exc))
        self.reload()
