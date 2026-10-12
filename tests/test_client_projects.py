"""Schermata principale: creazione progetto (solo Admin), importazione e reimportazione del file base (Lead/Admin)."""
from pathlib import Path

import pytest

from PySide6.QtWidgets import QDialog, QInputDialog, QMessageBox                        # noqa: E402

from client.api_client import ApiError                                                  # noqa: E402
from client.views.project_dialogs import NewProjectDialog                               # noqa: E402
from common.roles import Role                                                           # noqa: E402
from tests import sample_l5x as S                                                       # noqa: E402
from tests.test_client import (ADMIN_PW, PW, _live_server, _window, client_for, qapp, sync)  # noqa: E402,F401
from tests.test_filetypes import l5k                                                    # noqa: E402


@pytest.fixture(scope="module")
def fresh(tmp_path_factory):
    """Server dedicato: i test creano progetti nuovi senza toccare quelli degli altri moduli."""
    yield from _live_server(tmp_path_factory)


class Msgs:
    """Intercetta QMessageBox/QInputDialog: nessun dialogo bloccante, risposte programmabili."""

    def __init__(self, monkeypatch):
        self.answer = QMessageBox.Yes
        self.reason = "motivo di test valido"
        self.warnings, self.questions = [], []
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: (self.questions.append(a[2]), self.answer)[1])
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: self.warnings.append(a[2]))
        monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *a, **k: (self.reason, True))


def _login(live, tmp_path, user, pw=PW):
    win = _window(live, tmp_path, user, heartbeat_ms=60_000)
    win._do_login(live["url"], user, pw)
    assert win.stack.currentWidget() is win.workspace_widget
    return win


def _open_project(win, pid):
    win.sidebar.project.setCurrentIndex(win.sidebar.project.findData(pid))
    win._select_project(pid)
    assert win.project.id == pid


def _picker(win, path):
    seen = []
    win.pick_file = lambda parent, title, start, filters: (seen.append(filters), str(path) if path else "")[1]
    return seen


def _new_project_dialog(monkeypatch, name):
    def fake_exec(self):
        self.name.setText(name)
        self.desc.setText("creato dai test")
        return QDialog.Accepted
    monkeypatch.setattr(NewProjectDialog, "exec", fake_exec)


def _grant(live, pid, **roles):
    for user, role in roles.items():
        live["admin"].set_permission(pid, live["ids"][user], role)


def _close(*wins):
    for w in wins:
        w.ws.stop()
        w.close()


def _l5k_file(tmp_path, name="Impianto.L5K", **kw):
    f = tmp_path / name
    f.write_text(l5k(**kw), "utf-8")
    return f


# ======================================================================================= creazione progetto
def test_new_project_button_is_admin_only(fresh, qapp, tmp_path):
    adm, dev = _login(fresh, tmp_path, "admin", ADMIN_PW), _login(fresh, tmp_path, "dev1")
    lead = _login(fresh, tmp_path, "lead")
    assert not adm.sidebar.btn_new_project.isHidden()
    assert dev.sidebar.btn_new_project.isHidden() and lead.sidebar.btn_new_project.isHidden()
    dev.new_project()                                   # anche chiamato a mano, un non-Admin non fa nulla
    assert len(fresh["admin"].projects()) == 1
    _close(adm, dev, lead)


def test_admin_creates_project_from_main_screen_and_imports_l5k(fresh, qapp, tmp_path, monkeypatch):
    msgs = Msgs(monkeypatch)
    _new_project_dialog(monkeypatch, "Impianto Nuovo")
    win = _login(fresh, tmp_path, "admin", ADMIN_PW)
    base = _l5k_file(tmp_path)
    seen = _picker(win, base)

    win.sidebar.btn_new_project.click()                # dal pulsante «+» della sidebar
    p = win.project
    assert p is not None and p.name == "Impianto Nuovo"
    assert win.sidebar.project.currentData() == p.id and win.sidebar.project.currentText() == "Impianto Nuovo"
    assert any(x.id == p.id for x in win.projects)
    assert msgs.questions and "file base" in msgs.questions[0].lower()
    # il file dialog e' stato aperto subito, con "tutti i file" per primo e Rockwell L5K/L5X disponibili
    assert seen and seen[0].startswith("Tutti i file (*)") and "*.L5K" in seen[0] and "*.L5X" in seen[0]

    p = win.project
    assert p.head_revision == 1 and win.master_name == "Impianto" and win.master_ext == ".L5K"
    assert win.master_kind == "l5k"
    assert win.dashboard.btn_import.isHidden() and not win.dashboard.btn_reimport.isHidden()
    assert "Master rev 1" in win.dashboard.lbl_master.text()
    assert win.api.project_id == p.id
    _close(win)


def test_decline_import_then_use_dashboard_button_and_role_gating(fresh, qapp, tmp_path, monkeypatch):
    msgs = Msgs(monkeypatch)
    msgs.answer = QMessageBox.No                        # «Importare ora?» -> No
    _new_project_dialog(monkeypatch, "Importo Dopo")
    adm = _login(fresh, tmp_path, "admin", ADMIN_PW)
    seen = _picker(adm, None)
    adm.sidebar.btn_new_project.click()
    pid = adm.project.id
    assert adm.project.name == "Importo Dopo" and adm.project.head_revision is None and not seen
    _grant(fresh, pid, lead="Lead_Engineer", dev1="Developer", viewer="Viewer")

    # Admin e Lead vedono «Importa file base...»; Developer e Viewer solo l'avviso arancione
    assert not adm.dashboard.btn_import.isHidden() and adm.dashboard.btn_reimport.isHidden()
    lead, dev, viewer = (_login(fresh, tmp_path, u) for u in ("lead", "dev1", "viewer"))
    for w in (lead, dev, viewer):
        w.sidebar.project.setCurrentIndex(w.sidebar.project.findData(pid))
        w._select_project(pid)
        assert w.project.id == pid and w.project.head_revision is None
        assert "Master non ancora inizializzato" in w.dashboard.lbl_master.text()
    assert lead.project_role == Role.LEAD
    assert not lead.dashboard.btn_import.isHidden() and lead.dashboard.btn_reimport.isHidden()
    for w in (dev, viewer):
        assert w.dashboard.btn_import.isHidden() and w.dashboard.btn_reimport.isHidden()
        w.import_master()                               # chiamata a mano: nessun effetto
    assert next(x for x in fresh["admin"].projects() if x.id == pid).head_revision is None

    # il Lead importa un file base qualsiasi (.L5X) con il pulsante della dashboard
    f = tmp_path / "Base.L5X"
    f.write_text(S.base_project(), "utf-8")
    seen = _picker(lead, f)
    lead.dashboard.btn_import.click()
    assert seen and seen[0].startswith("Tutti i file (*)")
    assert lead.project.head_revision == 1 and lead.master_name == "Base" and lead.master_ext == ".L5X"
    assert lead.dashboard.btn_import.isHidden() and not lead.dashboard.btn_reimport.isHidden()
    assert "Master rev 1" in lead.dashboard.lbl_master.text()
    _close(adm, lead, dev, viewer)


def test_import_dialog_cancelled_does_nothing(fresh, qapp, tmp_path, monkeypatch):
    Msgs(monkeypatch)
    adm = _login(fresh, tmp_path, "admin", ADMIN_PW)
    pid = next(p.id for p in fresh["admin"].projects() if p.name == "Importo Dopo")
    other = fresh["admin"].create_project(__import__("common.schemas", fromlist=["ProjectCreate"]).ProjectCreate(name="Vuoto"))
    adm.load_projects()
    adm._select_project(other.id)
    _picker(adm, None)
    adm.import_master()
    assert adm.project.head_revision is None and not adm.dashboard.btn_import.isHidden()
    assert pid != other.id
    _close(adm)


# ======================================================================================= reimportazione
def test_reimport_buttons_visibility_by_project_role(fresh, qapp, tmp_path):
    pid = fresh["pid"]                                  # «Linea 1»: Master gia' inizializzato (rev 1)
    wins = {u: _login(fresh, tmp_path, u, ADMIN_PW if u == "admin" else PW) for u in
            ("admin", "lead", "dev1", "viewer")}
    for w in wins.values():
        _open_project(w, pid)
    for u in ("admin", "lead"):
        d = wins[u].dashboard
        assert not d.btn_reimport.isHidden() and d.btn_import.isHidden(), u
    for u in ("dev1", "viewer"):
        d = wins[u].dashboard
        assert d.btn_reimport.isHidden() and d.btn_import.isHidden(), u
    _close(*wins.values())


def test_reimport_rejects_short_reason_client_side(fresh, qapp, tmp_path, monkeypatch):
    msgs = Msgs(monkeypatch)
    msgs.reason = "  no  "
    lead = _login(fresh, tmp_path, "lead")
    _open_project(lead, fresh["pid"])
    calls = []
    real = lead.api.reimport_master_for
    lead.api.reimport_master_for = lambda *a: (calls.append(a), real(*a))[1]
    _picker(lead, _l5k_file(tmp_path, "x.L5K"))
    lead.reimport_master()
    assert not calls and msgs.warnings and "5 caratteri" in msgs.warnings[-1]
    assert lead.project.head_revision == 1
    _close(lead)


def test_reimport_cancel_at_confirmation_or_picker_does_nothing(fresh, qapp, tmp_path, monkeypatch):
    msgs = Msgs(monkeypatch)
    lead = _login(fresh, tmp_path, "lead")
    _open_project(lead, fresh["pid"])
    calls = []
    lead.api.reimport_master_for = lambda *a: calls.append(a)
    _picker(lead, None)
    lead.reimport_master()                               # nessun file scelto
    _picker(lead, _l5k_file(tmp_path, "y.L5K"))
    msgs.answer = QMessageBox.No                         # conferma negata
    lead.reimport_master()
    assert not calls
    assert "NUOVA revisione" in msgs.questions[-1] and "cronologia" in msgs.questions[-1] and "consensi" in msgs.questions[-1]
    _close(lead)


def test_reimport_creates_new_revision_and_shows_server_errors(fresh, qapp, tmp_path, monkeypatch):
    """Scrivere per ULTIMO: richiede ``ApiClient.reimport_master_for`` (endpoint di reimportazione del Master)."""
    from client.api_client import ApiClient
    if not hasattr(ApiClient, "reimport_master_for"):
        pytest.skip("ApiClient.reimport_master_for non ancora disponibile")
    msgs = Msgs(monkeypatch)
    adm = _login(fresh, tmp_path, "admin", ADMIN_PW)
    pid = next(p.id for p in adm.projects if p.name == "Impianto Nuovo")      # Master L5K gia' importato (rev 1)
    adm.sidebar.project.setCurrentIndex(adm.sidebar.project.findData(pid))
    adm._select_project(pid)
    assert adm.project.head_revision == 1
    api = adm.api

    # ---- errori del server: messaggio in italiano mostrato cosi' com'e'
    other_kind = tmp_path / "altro.L5X"
    other_kind.write_text(S.base_project(), "utf-8")
    _picker(adm, other_kind)
    with pytest.raises(ApiError) as e422:
        api.reimport_master_for(pid, other_kind, "motivo valido")
    assert e422.value.status == 422
    adm.reimport_master()
    assert msgs.warnings[-1] == e422.value.detail and adm.project.head_revision == 1

    same = _l5k_file(tmp_path, "Impianto.L5K")                                  # identico alla testa
    _picker(adm, same)
    adm.reimport_master()
    assert "identic" in msgs.warnings[-1].lower() and adm.project.head_revision == 1

    # ---- 409: progetto non ancora inizializzato (la vista aveva dati vecchi)
    empty = next(p for p in api.projects() if p.name == "Vuoto")
    adm.load_projects()
    adm._select_project(empty.id)
    adm.project.head_revision = 1
    _picker(adm, same)
    adm.reimport_master()
    assert msgs.warnings[-1] and "master" in msgs.warnings[-1].lower()
    adm._select_project(pid)

    # ---- successo: nuova revisione 2, cronologia intatta, vista aggiornata
    new = _l5k_file(tmp_path, "Impianto_aggiornato.L5K", r01a="XIC(A)XIC(Z)OTE(B);")
    _picker(adm, new)
    msgs.reason = "  aggiornamento dal cliente  "
    adm.reimport_master()
    assert adm.project.head_revision == 2
    assert "Master rev 2" in adm.dashboard.lbl_master.text()
    hist = api.history()
    assert [m.revision for m in hist] == [1, 2] or sorted(m.revision for m in hist) == [1, 2]
    assert "aggiornamento dal cliente" in adm.dashboard.log.toPlainText()
    assert adm.master_name == "Impianto_aggiornato" and adm.master_ext == ".L5K"   # nome del file reimportato
    _close(adm)


# ======================================================================================= reimport dalla console del server
def test_server_console_reimport_button_and_flow(fresh, qapp, tmp_path, monkeypatch):
    """Pagina «Utenti & Progetti» (console del server e tab Admin del client): Reimporta file base."""
    from client.api_client import ApiClient
    from client.views import user_mgmt_view as U
    from common.schemas import ProjectCreate
    msgs = Msgs(monkeypatch)
    api = ApiClient(fresh["url"])
    api.login("admin", ADMIN_PW)
    nuovo = api.create_project(ProjectCreate(name="Da console"))
    view = U.UserMgmtView(api, run=sync)
    view.tabs.setCurrentIndex(1)
    view.reload()
    view.proj_combo.setCurrentIndex(view.proj_combo.findData(nuovo.id))
    view._load_perms()

    # progetto senza Master: si importa, non si reimporta
    assert not view.btn_init.isHidden() and view.btn_reimport.isHidden()
    base = tmp_path / "Console.L5K"
    base.write_text(l5k(name="Console"), "utf-8")
    view.init_master(str(base))
    view.proj_combo.setCurrentIndex(view.proj_combo.findData(nuovo.id))
    view._load_perms()
    assert view.btn_init.isHidden() and not view.btn_reimport.isHidden()
    assert view.current_project().head_revision == 1

    # annullo al file picker / alla conferma / motivo troppo corto: nulla cambia
    monkeypatch.setattr(U.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: ("", "")))
    view.reimport_master()
    assert view.current_project().head_revision == 1
    new = tmp_path / "Console_nuovo.L5K"
    new.write_text(l5k(name="Console", r01a="XIC(A)XIC(Z)OTE(B);"), "utf-8")
    monkeypatch.setattr(U.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(new), "")))
    msgs.answer = QMessageBox.No
    view.reimport_master()
    assert "NUOVA revisione" in msgs.questions[-1] and view.current_project().head_revision == 1
    msgs.answer = QMessageBox.Yes
    msgs.reason = "ok"
    view.reimport_master()
    assert msgs.warnings and view.current_project().head_revision == 1

    # errore del server (file identico): messaggio mostrato, nessuna nuova revisione
    errors = []
    view._err = lambda e: errors.append(e.detail)
    view.reimport_master(str(base), "motivo valido")
    assert errors and "identico" in errors[-1].lower() and view.current_project().head_revision == 1

    # successo: nuova revisione 2, segnale per ricaricare i progetti, evento nell'audit
    emitted = []
    view.projects_changed.connect(lambda: emitted.append(1))
    msgs.reason = "  nuovo file dal cliente  "
    view.reimport_master()
    assert view.current_project().head_revision == 2 and emitted
    assert "Master rev 2" in view.proj_info.text()
    assert any(e.action == "master_reimported" and e.target == "rev2" for e in api.audit(limit=20))
