"""Client: confronto tra revisioni, etichette/blocco nello storico (progetto aperto e console) e barra «Presa in carico»."""
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QItemSelectionModel                                           # noqa: E402
from PySide6.QtWidgets import QDialog                                                    # noqa: E402

from client.api_client import ApiClient, ApiError                                        # noqa: E402
from client.views.dashboard_view import DashboardView                                    # noqa: E402
from client.views.history_dialog import HistoryDialog                                    # noqa: E402
from client.views.revision_compare_dialog import RevisionCompareDialog                   # noqa: E402
from common.roles import Role                                                            # noqa: E402
from common.schemas import MasterInfo, ProjectCreate                                     # noqa: E402
from common.schemas_revisions import LockOut                                             # noqa: E402
from tests.test_client import ADMIN_PW, PW, qapp, sync                                   # noqa: E402,F401
from tests.test_client_delete_retention import _admin, _errors, _make_project, _statuses, _view   # noqa: E402,F401
from tests.test_client_projects import Msgs, _close, _grant, _login, _open_project, fresh   # noqa: E402,F401

BIN1 = b"\x00\x01\x02ABC\xff" * 20
BIN2 = b"\x00\x01\x02XYZ\xff" * 21


def _info(rev, purged=False, label="", locked=False):
    return MasterInfo(revision=rev, sha256="a" * 64, updated_at=datetime(2026, 1, rev, tzinfo=timezone.utc),
                      updated_by="admin", message=f"msg {rev}", purged=purged, label=label, locked=locked,
                      locked_by="lead" if locked else "")


def _select_rows(dlg, *rows):
    dlg.table.clearSelection()
    m = dlg.table.selectionModel()
    m.setCurrentIndex(dlg.table.model().index(rows[-1], 0), QItemSelectionModel.NoUpdate)      # come il Ctrl+clic: corrente = ultima
    for r in rows:
        m.select(dlg.table.model().index(r, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)


def _grant_all(fresh, pid):
    _grant(fresh, pid, lead="Lead_Engineer", dev1="Developer", dev2="Developer", viewer="Viewer")


# ======================================================================================= HistoryDialog (unita')
def test_history_dialog_label_lock_compare_columns_and_roles(qapp):
    hist = [_info(1, purged=True), _info(2, label="FAT", locked=True), _info(3), _info(4, label="SAT")]
    d = HistoryDialog(hist, Role.LEAD, None, max_revisions=2)
    assert d.table.horizontalHeaderItem(5).text() == "Etichetta"
    assert [d.table.item(r, 5).text() for r in range(4)] == ["SAT", "", "FAT", ""]            # rev 4,3,2,1
    assert d.table.item(2, 4).text() == "bloccata" and not d.table.item(2, 0).icon().isNull()
    assert "2 conservate + 1 bloccate" in d.lbl_note.text() and "conserva le ultime 2 revisioni" in d.lbl_note.text()
    # un Developer non vede ne' etichetta ne' blocco; il confronto e' di tutti
    dev = HistoryDialog(hist, Role.DEVELOPER, None)
    assert dev.btn_label.isHidden() and dev.btn_lock.isHidden() and not dev.btn_compare.isHidden()
    # Lead: su una bloccata il pulsante dice «Sblocca» ma e' disabilitato; l'Admin puo'
    _select_rows(d, 2)
    assert d.btn_lock.text() == "Sblocca" and not d.btn_lock.isEnabled() and "Administrator" in d.btn_lock.toolTip()
    adm = HistoryDialog(hist, Role.ADMIN, None)
    _select_rows(adm, 2)
    assert adm.btn_lock.text() == "Sblocca" and adm.btn_lock.isEnabled()
    _select_rows(adm, 1)
    assert adm.btn_lock.text() == "Blocca revisione" and adm.btn_lock.isEnabled()
    # revisione eliminata: ne' etichetta ne' blocco ne' confronto
    _select_rows(adm, 3)
    assert not adm.btn_label.isEnabled() and not adm.btn_lock.isEnabled()
    assert adm.selected_pair() is None
    _select_rows(adm, 0, 3)                                                              # 4 + eliminata -> una sola valida
    assert adm.selected_pair() is None
    _select_rows(adm, 0, 2, 3)
    assert adm.selected_pair() == (2, 4)                                                  # sempre dalla piu' vecchia
    got = []
    adm.compare_requested.connect(lambda a, b: got.append((a, b)))
    adm.btn_compare.click()
    assert got == [(2, 4)]
    # senza due righe selezionate si scelgono con i due menu (hook)
    adm.pick_pair = lambda: (3, 4)
    _select_rows(adm, 1)
    adm.btn_compare.click()
    assert got[-1] == (3, 4)
    adm.pick_pair = lambda: None
    adm.btn_compare.click()
    assert len(got) == 2
    # etichetta e blocco emettono i segnali (hook al posto dei dialoghi)
    lab, lock = [], []
    adm.label_requested.connect(lambda r, t: lab.append((r, t)))
    adm.lock_requested.connect(lambda r, b: lock.append((r, b)))
    _select_rows(adm, 1)
    adm.ask_label = lambda rev, cur: "Collaudo"
    adm.btn_label.click()
    adm.confirm_lock = lambda rev, lk: True
    adm.btn_lock.click()
    adm.ask_label = lambda rev, cur: None                                                 # annullato
    adm.btn_label.click()
    adm.confirm_lock = lambda rev, lk: False
    adm.btn_lock.click()
    assert lab == [(3, "Collaudo")] and lock == [(3, True)]
    # con una sola revisione conservata non c'e' nulla da confrontare
    assert not HistoryDialog([_info(1)], Role.ADMIN, None).btn_compare.isEnabled()
    assert HistoryDialog([_info(1)], Role.ADMIN, None).selected_pair() is None


# ======================================================================================= storico del progetto aperto
def test_open_project_history_compare_label_lock(fresh, qapp, tmp_path, monkeypatch):
    msgs = Msgs(monkeypatch)
    adm = _admin(fresh)
    pr = _make_project(adm, tmp_path, "Storico Etichette", revisions=4, max_revisions=3)
    _grant_all(fresh, pr.id)
    win = _login(fresh, tmp_path, "lead")
    _open_project(win, pr.id)
    seen = {}

    def fake_exec(self):
        seen["cols"] = self.table.columnCount()
        _select_rows(self, 1, 3)                                                          # righe: rev 4,3,2,1 -> rev 3 e rev 1 (eliminata)
        seen["pair_with_purged"] = self.selected_pair()
        _select_rows(self, 0, 2)                                                          # rev 4 e rev 2
        self.btn_compare.click()
        seen["compare"] = self.actions.compare_dialog
        # etichetta e blocco della rev 3
        self.table.selectRow(1)
        self.ask_label = lambda rev, cur: "FAT"
        self.btn_label.click()
        seen["label_row"] = (self.selected_revision(), self.table.item(1, 5).text())
        self.confirm_lock = lambda rev, lk: True
        self.btn_lock.click()
        seen["lock_row"] = (self.table.item(1, 4).text(), self.btn_lock.text(), self.btn_lock.isEnabled())
        seen["note"] = self.lbl_note.text()
        return QDialog.Accepted
    monkeypatch.setattr(HistoryDialog, "exec", fake_exec)
    shown = []
    monkeypatch.setattr(RevisionCompareDialog, "exec", lambda self: shown.append(self) or QDialog.Accepted)
    win.show_history()

    assert seen["pair_with_purged"] is None                                                # una delle due e' eliminata
    cd = seen["compare"]
    assert cd is shown[0] and cd.a == 2 and cd.b == 4
    assert cd.lbl_title.text() == "rev 2 → rev 4"
    assert seen["label_row"] == (3, "FAT")                                                 # la selezione resta sulla rev 3
    assert seen["lock_row"] == ("bloccata", "Sblocca", False)                              # un Lead non puo' sbloccare
    assert "bloccate" in seen["note"]
    h = {m.revision: m for m in adm.history_for(pr.id)}
    assert h[3].label == "FAT" and h[3].locked and h[3].locked_by == "lead" and h[3].labeled_by == "lead"
    assert "Rev 3 bloccata" in win.dashboard.log.toPlainText()
    _close(win)

    # un Developer non vede nulla di tutto cio' ma puo' confrontare
    dev = _login(fresh, tmp_path, "dev1")
    _open_project(dev, pr.id)
    seen2 = {}

    def fake_exec2(self):
        seen2["hidden"] = (self.btn_label.isHidden(), self.btn_lock.isHidden(), self.btn_compare.isHidden())
        _select_rows(self, 0, 1)
        self.btn_compare.click()
        seen2["cmp"] = self.actions.compare_dialog
        return QDialog.Accepted
    monkeypatch.setattr(HistoryDialog, "exec", fake_exec2)
    dev.show_history()
    assert seen2["hidden"] == (True, True, False) and seen2["cmp"].a == 3 and seen2["cmp"].b == 4
    _close(dev)


def test_compare_dialog_content_filter_and_export(fresh, qapp, tmp_path, monkeypatch):
    adm = _admin(fresh)
    pr = _make_project(adm, tmp_path, "Confronto Vista", revisions=3)
    res = adm.compare_revisions_for(pr.id, 1, 3)
    d = RevisionCompareDialog(res)
    assert d.lbl_title.text() == "rev 1 → rev 3" and "rev 1" in d.lbl_from.text() and "rev 3" in d.lbl_to.text()
    assert res.n_modified == 1 and len(res.changes) == 1
    assert d.browser.tree.topLevelItem(0).childCount() == 1 and d.browser.tree.topLevelItem(1).isHidden()
    assert "Differenze da rev 1 a rev 3 (1)" in d.browser.tree.topLevelItem(0).text(0)
    assert d.browser.tree.currentItem() is not None and d.browser.path.text()               # prima voce selezionata
    d.set_filter("added")
    assert d.visible_changes() == [] and not d.lbl_empty.isHidden() and d.browser.isHidden()
    d.set_filter(None)
    assert len(d.visible_changes()) == 1 and d.lbl_empty.isHidden()
    csv_p, txt_p = tmp_path / "riepilogo.csv", tmp_path / "riepilogo.txt"
    assert d.export_summary(str(csv_p)) == str(csv_p) and d.export_summary(str(txt_p)) == str(txt_p)
    rows = csv_p.read_text("utf-8-sig").splitlines()
    assert rows[0] == "Percorso;Tipo;Titolo;Elemento" and len(rows) == 2 and "Modificato" in rows[1]
    txt = txt_p.read_text("utf-8")
    assert "Confronto rev 1 → rev 3" in txt and "1 differenze" in txt
    # nessuna differenza: messaggio e pulsante di esportazione spento
    res0 = res.model_copy(update={"changes": [], "n_modified": 0})
    d0 = RevisionCompareDialog(res0)
    assert not d0.lbl_empty.isHidden() and not d0.btn_export.isEnabled()
    assert d.export_summary("") is None


def test_compare_errors_are_reported(fresh, qapp, tmp_path, monkeypatch):
    Msgs(monkeypatch)
    adm = _admin(fresh)
    pr = _make_project(adm, tmp_path, "Confronto Errori", revisions=4, max_revisions=2)
    with pytest.raises(ApiError) as e:
        adm.compare_revisions_for(pr.id, 1, 4)
    assert e.value.status == 410
    view = _view(adm, "Confronto Errori")
    errs = _errors(view)
    seen = {}

    def fake_exec(self):
        self.actions.compare(1, 4)                                                         # rev 1 eliminata -> errore mostrato
        seen["errs"] = list(errs)
        self.actions.compare(3, 4)
        seen["cmp"] = self.actions.compare_dialog
        return QDialog.Accepted
    monkeypatch.setattr(HistoryDialog, "exec", fake_exec)
    monkeypatch.setattr(RevisionCompareDialog, "exec", lambda self: QDialog.Accepted)
    view.show_history()
    assert seen["errs"] and "non e' piu' conservata" in seen["errs"][0]
    assert seen["cmp"].a == 3 and seen["cmp"].b == 4


# ======================================================================================= console del server
def test_server_console_history_label_lock_unlock(fresh, qapp, tmp_path, monkeypatch):
    Msgs(monkeypatch)
    adm = _admin(fresh)
    api_pid = adm.project_id
    pr = _make_project(adm, tmp_path, "Console Etichette", revisions=3, max_revisions=2)
    view = _view(adm, "Console Etichette")
    errs, st = _errors(view), _statuses(view)
    seen = {}

    def fake_exec(self):
        self.ask_label = lambda rev, cur: "Consegna cliente"
        self.confirm_lock = lambda rev, lk: True
        self.table.selectRow(1)                                                            # rev 2
        self.btn_label.click()
        self.btn_lock.click()
        seen["row"] = (self.table.item(1, 4).text(), self.table.item(1, 5).text(), self.btn_lock.text())
        self.btn_lock.click()                                                              # Admin: sblocca
        seen["unlocked"] = self.table.item(1, 4).text()
        _select_rows(self, 0, 1)
        self.btn_compare.click()
        return QDialog.Accepted
    monkeypatch.setattr(HistoryDialog, "exec", fake_exec)
    monkeypatch.setattr(RevisionCompareDialog, "exec", lambda self: seen.setdefault("cmp", (self.a, self.b)) and QDialog.Accepted)
    view.show_history()
    assert not errs, errs
    assert seen["row"] == ("bloccata", "Consegna cliente", "Sblocca") and seen["unlocked"] == "disponibile"
    assert seen["cmp"] == (2, 3)
    assert adm.project_id == api_pid                                                       # progetto condiviso intatto
    assert any("sbloccata" in s for s in st)
    actions = {a.action for a in adm.audit(limit=50)}
    assert {"revision_labeled", "revision_locked", "revision_unlocked"} <= actions


# ======================================================================================= barra «Presa in carico» (unita')
def _lock(holder="dev1", mine=False, note="", hours=2):
    now = datetime.now(timezone.utc)
    return LockOut(locked=True, holder=holder, acquired_at=now, expires_at=now + timedelta(hours=hours), note=note,
                   seconds_left=hours * 3600, mine=mine)


def _dash(role, kind, lock=None, init=True):
    d = DashboardView()
    d.set_identity("u1", role)
    d.set_enabled_project(True)
    d.set_master_state(init)
    d.set_master_kind(kind)
    d.set_lock(lock)
    return d


def test_lock_bar_visibility_by_role_and_kind(qapp):
    free = LockOut(locked=False)
    # progetto non inizializzato / nessun progetto: nessuna barra
    assert DashboardView().lock_bar.isHidden()
    assert _dash(Role.LEAD, "l5k", free, init=False).lock_bar.isHidden()

    # Developer su un file mergeabile: prende in carico, niente check-in
    d = _dash(Role.DEVELOPER, "l5k", free)
    assert not d.lock_bar.isHidden() and "Libero" in d.lbl_lock.text()
    assert not d.btn_lock_take.isHidden() and d.btn_lock_take.isEnabled()
    assert d.btn_checkin.isHidden() and d.btn_lock_release.isHidden() and d.btn_lock_force.isHidden()

    # binario: il check-in compare ma e' spento finche' non si ha in carico
    d = _dash(Role.DEVELOPER, "binary", free)
    assert not d.btn_checkin.isHidden() and not d.btn_checkin.isEnabled()
    d.set_lock(_lock("dev1", mine=True))
    assert d.btn_checkin.isEnabled() and not d.btn_lock_release.isHidden() and d.btn_lock_take.isHidden()
    assert "In carico a dev1 fino alle" in d.lbl_lock.text() and "(tu)" in d.lbl_lock.text()
    assert d.lock_is_mine() and d.lock_holder() == "dev1"

    # in carico a un altro: Developer non puo' prendere ne' fare check-in; l'Admin vede «Forza rilascio»
    d.set_lock(_lock("dev2", note="aggiorno il firmware"))
    assert not d.btn_lock_take.isEnabled() and not d.btn_checkin.isEnabled() and d.btn_lock_force.isHidden()
    assert "dev2" in d.lbl_lock.text() and "aggiorno il firmware" in d.lbl_lock.text() and "consultiva" not in d.lbl_lock.text()
    a = _dash(Role.ADMIN, "binary", _lock("dev2"))
    assert not a.btn_lock_force.isHidden()
    a.set_lock(_lock("admin", mine=True))
    assert a.btn_lock_force.isHidden()

    # file mergeabile in carico a un altro: avviso consultivo
    m = _dash(Role.DEVELOPER, "l5x", _lock("dev2"))
    assert "consultiva" in m.lbl_lock.text() and m.btn_checkin.isHidden()

    # Viewer: vede solo lo stato
    v = _dash(Role.VIEWER, "binary", _lock("dev2"))
    assert not v.lock_bar.isHidden() and "In carico a dev2" in v.lbl_lock.text()
    assert all(b.isHidden() for b in (v.btn_lock_take, v.btn_lock_release, v.btn_checkin, v.btn_lock_force))

    got = []
    d2 = _dash(Role.DEVELOPER, "binary", free)
    for sig, name in ((d2.lock_take_requested, "take"), (d2.checkin_requested, "checkin")):
        sig.connect(lambda n=name: got.append(n))
    d2.btn_lock_take.click()
    d2.set_lock(_lock("dev1", mine=True))
    d2.btn_checkin.click()
    assert got == ["take", "checkin"]


# ======================================================================================= finestra principale: lock e check-in
def _binary_project(fresh, tmp_path, name="Firmware"):
    adm = fresh["admin"]
    p = adm.create_project(ProjectCreate(name=name))
    _grant_all(fresh, p.id)
    f = tmp_path / f"{name}.bin"
    f.write_bytes(BIN1)
    adm.init_master_for(p.id, f)
    return p


def test_window_lock_checkin_force_release_and_events(fresh, qapp, tmp_path, monkeypatch):
    msgs = Msgs(monkeypatch)
    infos = []
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: infos.append(a[2]))
    p = _binary_project(fresh, tmp_path)
    dev1, dev2 = _login(fresh, tmp_path, "dev1"), _login(fresh, tmp_path, "dev2")
    adm = _login(fresh, tmp_path, "admin", ADMIN_PW)
    viewer = _login(fresh, tmp_path, "viewer")
    notes = []
    for w in (dev1, dev2, adm, viewer):
        w.notify_user = lambda title, text, _n=notes: _n.append((title, text))
        _open_project(w, p.id)
    assert dev1.master_kind == "binary"
    for w in (dev1, dev2, adm):
        assert "Libero" in w.dashboard.lbl_lock.text() and not w.dashboard.btn_checkin.isHidden()
    assert viewer.dashboard.btn_lock_take.isHidden() and viewer.dashboard.btn_checkin.isHidden()

    # dev1 prende in carico; dev2 riceve l'evento lock_changed (simulato) e la barra si aggiorna + notifica non bloccante
    dev1.dashboard.btn_lock_take.click()
    assert dev1.dashboard.lock_is_mine() and "In carico a dev1 fino alle" in dev1.dashboard.lbl_lock.text()
    assert dev1.dashboard.btn_checkin.isEnabled()
    ev = {"type": "lock_changed", "message": "dev1 ha preso in carico il Master", "developer": "dev1"}
    dev2.on_event(ev)
    assert "In carico a dev1" in dev2.dashboard.lbl_lock.text() and not dev2.dashboard.btn_lock_take.isEnabled()
    assert notes == [("Presa in carico", "dev1 ha preso in carico il Master")]
    dev1.on_event(ev)                                                                      # il proprio evento non notifica
    assert len(notes) == 1
    # dev2: il server rifiuta la presa in carico (409) e la GUI lo mostra
    dev2.take_lock()
    assert dev2.api.lock_state().holder == "dev1"
    # l'Admin vede «Forza rilascio...»
    adm.refresh_lock()
    assert not adm.dashboard.btn_lock_force.isHidden()

    # rinnovo automatico insieme all'heartbeat
    before = dev1.api.lock_state().expires_at
    time.sleep(1.1)
    dev1._heartbeat()
    after = dev1.api.lock_state().expires_at
    assert after > before

    # check-in: messaggio troppo corto -> avviso, nessuna revisione; poi riuscito
    f = tmp_path / "nuovo.bin"
    f.write_bytes(BIN2)
    dev1.checkin_master(str(f), "ok")
    assert msgs.warnings and "5 caratteri" in msgs.warnings[-1] and fresh["admin"].history_for(p.id)[0].revision == 1
    dev1.checkin_master(str(f), "firmware 2.0 dal cliente")
    h = fresh["admin"].history_for(p.id)
    assert h[0].revision == 2 and "firmware 2.0" in h[0].message
    assert dev1.project.head_revision == 2 and "Libero" in dev1.dashboard.lbl_lock.text()
    assert "Check-in eseguito" in dev1.dashboard.log.toPlainText()
    # senza presa in carico il check-in non parte (nessuna nuova revisione)
    dev1.checkin_master(str(f), "secondo tentativo senza presa")
    assert fresh["admin"].history_for(p.id)[0].revision == 2
    assert infos and "prendi in carico" in infos[-1]

    # forza rilascio (solo Admin, con motivo)
    dev2.on_event({"type": "lock_changed", "message": "dev1 ha rilasciato il Master", "developer": "dev1"})
    dev2.dashboard.btn_lock_take.click()
    assert dev2.dashboard.lock_is_mine()
    adm.refresh_lock()
    adm.force_release_lock("no")
    assert msgs.warnings[-1] and "5 caratteri" in msgs.warnings[-1] and adm.api.lock_state().locked
    adm.force_release_lock("dev2 in ferie, serve il file")
    assert not adm.api.lock_state().locked and "Libero" in adm.dashboard.lbl_lock.text()
    dev1.force_release_lock("provo a forzare")                                             # non Admin: nessun effetto
    dev2.refresh_lock()
    assert "Libero" in dev2.dashboard.lbl_lock.text()
    acts = {a.action for a in fresh["admin"].audit(project_id=p.id, limit=100)}
    assert {"lock_acquired", "lock_released", "lock_forced", "master_checkin"} <= acts
    # rilascio volontario
    dev2.dashboard.btn_lock_take.click()
    dev2.dashboard.btn_lock_release.click()
    assert not dev2.api.lock_state().locked and "Libero" in dev2.dashboard.lbl_lock.text()
    _close(dev1, dev2, adm, viewer)


def test_window_mergeable_project_has_no_checkin(fresh, qapp, tmp_path, monkeypatch):
    msgs = Msgs(monkeypatch)
    dev = _login(fresh, tmp_path, "dev1")
    _open_project(dev, fresh["pid"])                                                       # «Linea 1»: Master L5X
    assert dev.master_kind == "l5x" and dev.dashboard.btn_checkin.isHidden()
    assert not dev.dashboard.btn_lock_take.isHidden()
    dev.dashboard.btn_lock_take.click()
    assert dev.dashboard.lock_is_mine()
    f = tmp_path / "x.L5X"
    f.write_text("x", "utf-8")
    seen = []
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: seen.append(a[2]))
    dev.checkin_master(str(f), "messaggio valido")                                         # riservato ai binari
    assert seen and "file binari" in seen[0]
    # avviso consultivo: un altro ha in carico il Master
    dev2 = _login(fresh, tmp_path, "dev2")
    _open_project(dev2, fresh["pid"])
    assert "consultiva" in dev2.dashboard.lbl_lock.text()
    dev.dashboard.btn_lock_release.click()
    _close(dev, dev2)
