"""Client: pannello «Discussione» (offscreen) con API reale su server di test, eventi WS simulati, Viewer, chip nella card."""
from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtWidgets import QLabel, QMessageBox

from client.gui_approval import ApprovalDialog
from client.views.comments_panel import CommentsPanel, comment_html
from client.views.dashboard_view import ProposalCard
from common.roles import Role
from tests import sample_l5x as S
from tests.test_client import PW, _live_server, _window, client_for, qapp, sync   # noqa: F401


@pytest.fixture(scope="module")
def cl(tmp_path_factory):
    yield from _live_server(tmp_path_factory)


@pytest.fixture
def prop(cl):
    dev1, dev2 = client_for(cl, "dev1"), client_for(cl, "dev2")
    f = cl["tmp"] / "c.L5X"
    f.write_text(S.dev_b(True), "utf-8")
    p = dev2.submit_proposal(f, 1, "modifiche di B")
    yield p, dev1, dev2
    dev2.cancel(p.id)


def test_comment_html_escapes_and_highlights_valid_mentions():
    h = comment_html("ciao @dev1, <b>x</b> @fantasma\nok", ["dev1"], "dev2")
    assert "&lt;b&gt;" in h and "<br>" in h and "@dev1" in h and "color:#00E5FF" in h
    assert h.count("<span") == 1                                  # @fantasma non e' valida: nessuna evidenza
    assert "background-color" in comment_html("@dev1", ["dev1"], "dev1")      # la mia menzione e' piu' marcata


def test_panel_send_reply_edit_delete(cl, qapp, prop, monkeypatch):
    p, dev1, dev2 = prop
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    dlg = ApprovalDialog(dev1, p.id, Role.DEVELOPER, run=sync)
    pn = dlg.comments
    dlg.tabs.setCurrentWidget(pn)
    assert pn.empty.isVisibleTo(dlg) and pn.items == {} and "Nessun commento" in pn.empty.title.text()
    assert dlg.tabs.tabText(dlg.tabs.indexOf(pn)) == "Discussione"
    assert pn.anchor.count() > 1 and pn.btn_send.isEnabled() is False        # ancore = path dei cambiamenti correnti

    # invio con ancora
    path = pn.anchor.itemData(1)
    pn.anchor.setCurrentIndex(1)
    pn.editor.setPlainText("Ho dubbi su questo punto @dev2")
    assert pn.btn_send.isEnabled()
    pn.send()
    assert len(pn.comments) == 1 and pn.comments[0].anchor == path and pn.comments[0].mentions == ["dev2"]
    cid = pn.comments[0].id
    assert pn.editor.toPlainText() == "" and pn.anchor.currentIndex() == 0
    it = pn.items[cid]
    assert it.anchor_chip is not None and it.btn_edit.isVisibleTo(dlg) is True and it.btn_delete.isVisibleTo(dlg)
    assert "@dev2" in it.body.text()
    assert dlg.tabs.tabText(dlg.tabs.indexOf(pn)) == "Discussione (1)"
    assert dev2.proposals()[0].n_comments == 1

    # risposta di dev2 (altro client) + aggiornamento del pannello da evento
    d2 = dev2.add_comment(p.id, "Risolvo subito", reply_to=cid)
    assert dlg.on_event({"type": "comment_added", "proposal": {"id": p.id}}) is None
    assert [c.id for c in pn.comments] == [cid, d2.id] and pn.comments[1].reply_to == cid
    assert pn.items[d2.id].comment.author == "dev2"
    assert not pn.items[d2.id].btn_edit.isVisibleTo(dlg) and not pn.items[d2.id].btn_delete.isVisibleTo(dlg)   # non e' mio
    assert pn.items[d2.id].layout().contentsMargins().left() > pn.items[cid].layout().contentsMargins().left()  # rientrata
    dlg.on_event({"type": "comment_added", "proposal": {"id": "altra"}})          # altra richiesta: ignorato
    assert len(pn.comments) == 2

    # rispondi
    pn.start_reply(d2.id)
    assert pn.reply_bar.isVisibleTo(dlg) and "dev2" in pn.reply_label.text()
    pn.editor.setPlainText("Grazie")
    pn.send()
    assert pn.comments[-1].reply_to == d2.id and not pn.reply_bar.isVisibleTo(dlg)

    # modifica (solo mio, entro 15 minuti)
    pn.start_edit(cid)
    assert pn.editor.toPlainText() == "Ho dubbi su questo punto @dev2" and pn.btn_send.text() == "Salva modifica"
    pn.editor.setPlainText("Dubbio chiarito")
    pn.send()
    c0 = next(c for c in pn.comments if c.id == cid)
    assert c0.text == "Dubbio chiarito" and c0.edited_at is not None and pn.btn_send.text() == "Invia"
    assert "modificato" in pn.items[cid].lbl_time.text()
    pn.now = lambda: datetime.now(timezone.utc) + timedelta(minutes=16)             # oltre i 15 minuti
    pn._render()
    assert not pn.items[cid].btn_edit.isVisibleTo(dlg) and pn.items[cid].btn_delete.isVisibleTo(dlg)
    pn.start_edit(cid)
    assert pn._editing is None

    # elimina
    pn.delete(cid)
    c0 = next(c for c in pn.comments if c.id == cid)
    assert c0.deleted and "eliminato" in pn.items[cid].body.text() and not pn.items[cid].btn_reply.isVisibleTo(dlg)
    assert pn.count == 2 and dlg.tabs.tabText(dlg.tabs.indexOf(pn)) == "Discussione (2)"

    # Ctrl+Invio nel riquadro
    pn.editor.setPlainText("via scorciatoia")
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    pn.editor.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.ControlModifier))
    assert pn.comments[-1].text == "via scorciatoia"

    # chip nella card della dashboard
    s = next(x for x in dev1.proposals() if x.id == p.id)
    assert s.n_comments == 3
    card, card0 = ProposalCard(s), ProposalCard(s.model_copy(update={"n_comments": 0}))
    assert any("💬 3" in lab.text() for lab in card.findChildren(QLabel))
    assert not any("💬" in lab.text() for lab in card0.findChildren(QLabel))


def test_viewer_is_read_only_and_admin_can_delete(cl, qapp, prop, monkeypatch):
    p, dev1, dev2 = prop
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    c = dev1.add_comment(p.id, "Scrivo io")
    viewer = client_for(cl, "viewer")
    dv = ApprovalDialog(viewer, p.id, Role.VIEWER, run=sync)
    pn = dv.comments
    dv.tabs.setCurrentWidget(pn)
    assert pn.comments[0].id == c.id and pn.items[c.id].comment.text == "Scrivo io"
    assert pn.editor.isHidden() and pn.btn_send.isHidden() and not pn.note.isHidden()
    it = pn.items[c.id]
    assert not it.btn_reply.isVisibleTo(dv) and not it.btn_edit.isVisibleTo(dv) and not it.btn_delete.isVisibleTo(dv)
    pn.editor.setPlainText("hack")
    pn.send()
    assert len(dev1.comments(p.id)) == 1
    # l'Admin vede «Elimina» sui commenti altrui ma non «Modifica»
    admin = client_for(cl, "admin", "AdminPass123", False)
    da = ApprovalDialog(admin, p.id, Role.ADMIN, run=sync)
    da.tabs.setCurrentWidget(da.comments)
    ia = da.comments.items[c.id]
    assert ia.btn_delete.isVisibleTo(da) and not ia.btn_edit.isVisibleTo(da)
    da.comments.delete(c.id)
    assert da.comments.comments[0].deleted and da.comments.count == 0


def test_main_window_live_update_and_notifications(cl, qapp, prop, tmp_path):
    p, dev1, dev2 = prop
    win = _window(cl, tmp_path, "dev1", heartbeat_ms=60_000)
    win._do_login(cl["url"], "dev1", PW)
    win.ws.stop()
    notes = []
    win.notify_user = lambda title, text: notes.append((title, text))
    win.open_proposal(p.id)
    dlg = win.dialogs[p.id]
    pn = dlg.comments
    assert pn.comments == []

    def event(typ, author, mentions=()):
        s = next(x for x in dev1.proposals() if x.id == p.id)
        return {"type": typ, "message": f"{author} ha commentato la richiesta #{p.id}", "developer": author,
                "mentions": list(mentions), "proposal": s.model_dump(mode="json")}

    c = dev2.add_comment(p.id, "Nuovo commento da dev2")
    win.on_event(event("comment_added", "dev2"))                              # pannello aperto e visibile: niente popup
    assert [x.id for x in pn.comments] == [c.id]
    dlg.hide()
    c2 = dev2.add_comment(p.id, "Secondo")
    win.on_event(event("comment_added", "dev2"))                              # sono partecipante: notifica
    assert [t for t, _ in notes] == ["Nuovo commento"] and len(pn.comments) == 2
    c3 = dev2.add_comment(p.id, "ehi @dev1")
    win.on_event(event("comment_added", "dev2", ["dev1"]))
    assert notes[-1][0] == "Sei stato menzionato" and "dev2" in notes[-1][1] and len(pn.comments) == 3
    n = len(notes)
    win.on_event(event("comment_added", "dev1"))                              # mio stesso commento: nessun avviso
    assert len(notes) == n
    dev2.delete_comment(c2.id)
    win.on_event(event("comment_deleted", "dev2"))
    assert pn.count == 2 and len(notes) == n                                  # eliminazione: solo aggiornamento
    # la lista della dashboard riceve n_comments
    assert next(x for x in dev1.proposals() if x.id == p.id).n_comments == 2
    win.ws.stop()
    win.close()
