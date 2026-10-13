"""Client: anteprima conflitti (dry-run), blocco/scadenza account, ecc. — con il server vivo."""
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QMessageBox                                                # noqa: E402

from client.views.merge_dialog import MergeRequestDialog                                 # noqa: E402
from tests.test_client import qapp, sync, pump                                           # noqa: E402,F401
from tests.test_client_branches import (TEXT, _bump_main, _lapi, _lwin, blive, needs_server)   # noqa: E402,F401


@needs_server
def test_live_dry_run_in_branch_dialog_and_merge_dialog(blive, qapp, tmp_path, monkeypatch):
    live = blive
    warns = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warns.append(a[2]))
    win = _lwin(live, tmp_path, "dev1", "dev1dry")
    dev = _lapi(live, "dev1")
    b = dev.create_branch("Anteprima live", "personal")
    win.select_branch(b.slug)
    win.checkout()
    f = win.working_file
    open(f, "w", encoding="utf-8").write(TEXT.replace("riga 2", "riga 2 DEL BRANCH"))
    _bump_main(live, tmp_path, "riga 2", "riga 2 DAL PRINCIPALE")

    # anteprima dall'API e dalla finestra principale: nessuna richiesta creata
    win.preview_branch(b.slug, "merge", show=False)
    assert pump(qapp, lambda: win.last_preview is not None)
    assert win.last_preview.nothing_to_merge            # il branch non ha ancora salvato il file modificato
    before = len(_lapi(live, "dev1").proposals())
    # dialogo di merge: «Verifica conflitti» sul file di lavoro modificato
    dlg = MergeRequestDialog(win.local_folder, win.working_file, win.base_revision, "x", ext=win.master_ext,
                             kind=win.master_kind, name=win.master_name, run=sync,
                             check_fn=lambda p: win.api.branch_dry_run(b.slug, "merge", p))
    assert dlg.select_path(f) and not dlg.btn_check.isHidden() and dlg.btn_check.isEnabled()
    dlg.check_conflicts()
    assert "1 conflitti" in dlg.info.text(), dlg.info.text()
    assert len(_lapi(live, "dev1").proposals()) == before and not warns

    # dry-run della richiesta normale nel principale
    p = _lapi(live, "dev1").dry_run(Path(f), 1)
    assert p.n_conflicts == 1 and not p.clean
    win.ws.stop()
    win.close()


@needs_server
def test_live_notification_bell_and_dialog(blive, qapp, tmp_path, monkeypatch):
    from client.views.notifications_dialog import NotificationsDialog
    live = blive
    warns = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warns.append(a[2]))
    w1, w2 = _lwin(live, tmp_path, "dev1", "dev1n"), _lwin(live, tmp_path, "dev2", "dev2n")
    assert pump(qapp, lambda: w1.ws.is_up and w2.ws.is_up)
    base = w2.sidebar.unread
    # dev1 propone una modifica al principale: dev2 (nel quorum) riceve una notifica persistente + l'evento in tempo reale
    f = tmp_path / "Imp.txt"
    f.write_text(TEXT.replace("riga 6", "riga 6 NOTIFICA"), "utf-8")
    a1 = _lapi(live, "dev1")
    head = a1.master_info().revision
    d = a1.submit_proposal(f, head, "per la campanella")
    assert pump(qapp, lambda: w2.sidebar.unread == base + 1), (w2.sidebar.unread, base)
    assert "non lette" in w2.sidebar.btn_bell.toolTip() and w2.sidebar.btn_bell.text() == str(base + 1)
    # la finestra «Notifiche» elenca, segna come lette e apre la richiesta collegata
    dlg = NotificationsDialog(w2.api, run=sync)
    assert dlg._rows and dlg._rows[0].kind == "merge_request" and dlg._rows[0].ref == d.id and not dlg._rows[0].read
    dlg.table.selectRow(0)
    got = []
    dlg.open_requested.connect(got.append)
    dlg.open_selected()
    assert got and got[0].ref == d.id
    w2.open_notification(got[0])
    assert w2.opened[-1][0] == d.id
    dlg2 = NotificationsDialog(w2.api, run=sync)
    dlg2.mark_all()
    assert w2.api.notifications_unread() == 0 and not warns
    a1.cancel(d.id)                                       # non lasciare richieste aperte al resto del modulo
    for w in (w1, w2):
        w.ws.stop()
        w.close()


@needs_server
def test_live_resolve_all_conflicts_at_once(blive, qapp, tmp_path, monkeypatch):
    from client.gui_approval import ApprovalDialog
    live = blive
    warns = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warns.append(a[2]))
    adm, dev = _lapi(live, "admin"), _lapi(live, "dev1")
    head = adm.master_info().revision
    t = adm.master_info()
    cur = tmp_path / "cur.txt"
    adm.download_master(cur)
    base_text = cur.read_text("utf-8").split("\n")
    # due righe distanti modificate sia dal principale sia da dev1 -> 2 conflitti
    mine = list(base_text)
    mine[1], mine[7] = "UNO mio", "SETTE mio"
    theirs = list(base_text)
    theirs[1], theirs[7] = "UNO master", "SETTE master"
    f_m, f_t = tmp_path / "mine.txt", tmp_path / "theirs.txt"
    f_m.write_text("\n".join(mine), "utf-8")
    f_t.write_text("\n".join(theirs), "utf-8")
    adm.reimport_master_for(live["pid"], f_t, "master avanzato")
    d = dev.submit_proposal(f_m, head, "conflitti")
    assert d.n_unresolved == 2
    dlg = ApprovalDialog(dev, d.id, run=sync)
    assert dlg.perms.resolve and all(b.isEnabled() for b in dlg.bulk_btns.values())
    assert dlg.apply_all("theirs") == 2 and len(dlg._pending) == 2
    dlg._apply()
    d2 = dev.proposal(d.id)
    assert d2.n_unresolved == 0 and not warns, warns
    assert all(c.choice == "theirs" for c in d2.conflicts)
    dev.cancel(d.id)


@needs_server
def test_live_new_revision_in_my_branch_is_signalled_and_downloaded(blive, qapp, tmp_path, monkeypatch):
    live = blive
    warns = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warns.append(a[2]))
    lead = _lapi(live, "lead")
    team = lead.create_branch("Avviso live", "team")
    win = _lwin(live, tmp_path, "dev2", "dev2av")
    asked = []
    monkeypatch.setattr(type(win), "_on_branch_new_revision", lambda self, rev, msg="", **k: asked.append((rev, msg)))
    win.switch_branch(team.slug)
    assert pump(qapp, lambda: win.base_revision >= 1)
    before = win.base_revision
    assert win.newer_revision() == 0 and pump(qapp, lambda: win.ws.is_up)
    # il lead salva nel branch di team (rev nuova): dev2 vede l'avviso e l'indicatore «disponibile la rev N»
    f = tmp_path / "lead.txt"
    f.write_text(TEXT.replace("riga 1", "riga 1 DAL LEAD"), "utf-8")
    lead.set_project(live["pid"], team.slug)
    lead.report_revision(1)
    res = lead.branch_save(team.slug, f, "salvo nel team", base_revision=1)
    assert res.state.value == "merged", res.state
    assert pump(qapp, lambda: asked and asked[-1][0] > before), asked
    assert pump(qapp, lambda: win.newer_revision() > before)
    win._update_local_label()
    assert "disponibile la rev" in win.dashboard.lbl_local.text()
    # accettando, il Checkout porta la copia di lavoro alla nuova revisione
    monkeypatch.undo()
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warns.append(a[2]))
    newer = win.newer_revision()
    win._on_branch_new_revision(newer, "x", delay_ms=0, ask=lambda text: "rev" in text)
    assert pump(qapp, lambda: win.base_revision == newer), (win.base_revision, newer)
    assert win.newer_revision() == 0 and not warns
    win.ws.stop()
    win.close()


@needs_server
def test_live_client_update_offer_download_and_apply(blive, qapp, tmp_path, monkeypatch):
    import hashlib
    from client import updater
    live = blive
    warns = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warns.append(a[2]))
    exe = tmp_path / "AutomationProjectClient.exe"
    data = b"MZ" + b"\x00" * 3000 + b"nuovo-client"
    exe.write_bytes(data)
    _lapi(live, "admin").publish_client(exe, "9.9", "Nuove funzioni")
    win = _lwin(live, tmp_path, "dev1", "dev1up")
    questions = []
    # da sorgenti: la versione nuova viene segnalata ma non si sostituisce nulla
    win.check_for_update(ask=lambda t: questions.append(t) or True)
    assert win.update_info is not None and win.update_info.version == "9.9" and not questions
    # dall'eseguibile: chiede, scarica, verifica e prepara la sostituzione
    target = tmp_path / "Installato.exe"
    monkeypatch.setattr(updater, "running_exe", lambda: target)
    launched = []
    monkeypatch.setattr(updater, "apply", lambda p, **k: launched.append(Path(p)) or Path(p))
    win.update_info = None
    monkeypatch.setattr(type(win), "install_update", lambda self, info, quit_app=True: updater_install(self, info, launched))

    def updater_install(self, info, sink):
        sink.append(updater.download(self.api, info, tmp_path / "dl"))
    win.check_for_update(ask=lambda t: questions.append(t) or True)
    assert questions and "9.9" in questions[0] and "Nuove funzioni" in questions[0]
    assert launched and launched[0].read_bytes() == data
    assert hashlib.sha256(launched[0].read_bytes()).hexdigest() == win.update_info.sha256 and not warns
    # chi e' gia' aggiornato non vede nulla
    assert _lapi(live, "dev1").client_update("9.9").available is False
    win.ws.stop()
    win.close()
