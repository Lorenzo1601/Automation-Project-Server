"""Test del client: funzioni pure (diff, permessi, CSV, tema) + GUI offscreen contro un server reale (JWT + RBAC)."""
import os
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings                                                    # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QInputDialog, QLabel, QMessageBox  # noqa: E402

from client.api_client import ApiClient, ApiError                                       # noqa: E402
from client.gui_approval import ApprovalDialog, compute_permissions                     # noqa: E402
from client.theme import apply_dark_theme, build_stylesheet                             # noqa: E402
from client.theme.widgets import badge_kind                                             # noqa: E402
from client.views import diff_viewer as D                                               # noqa: E402
from client.views.audit_view import to_csv                                              # noqa: E402
from client.views.user_mgmt_view import suggest_password                                # noqa: E402
from common.roles import Role                                                           # noqa: E402
from common.schemas import (AuditOut, ParticipantOut, ProjectCreate, ProposalDetail,    # noqa: E402
                            ProposalState, UserCreate)
from tests import sample_l5x as S                                                       # noqa: E402

PW = "Password12345"
ADMIN_PW = "AdminPass123"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    apply_dark_theme(app)
    return app


@pytest.fixture(autouse=True)
def _close_widgets(qapp):
    """Chiude e distrugge le finestre create dal test: evita crash di Qt (animazioni, QWebSocket) alla chiusura."""
    yield
    from PySide6.QtCore import QEvent
    for w in QApplication.topLevelWidgets():
        w.close()
        w.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def sync(fn, ok=None, err=None):
    """Runner sincrono per i test: stessa semantica di ``run_async`` senza thread."""
    try:
        res = fn()
    except Exception as exc:                                                    # noqa: BLE001
        if err:
            err(exc)
        return                                                                  # come run_async: senza on_error si ignora
    if ok:
        ok(res)


def pump(qapp, cond, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


# ======================================================================================= funzioni pure
def test_diff_rows_are_aligned_and_marked():
    old = "XIC(A)OTE(B);\nXIC(C)OTE(D);\nXIO(E)OTE(F);"
    new = "XIC(A)OTE(B);\nXIC(C)TON(T1,1000,0);\nXIO(E)OTE(F);\nNOP();"
    l, r = D.side_by_side_rows(old, new)
    assert len(l) == len(r)
    assert [x.kind for x in l] == ["equal", "chg", "equal", "blank"]
    assert [x.kind for x in r] == ["equal", "chg", "equal", "add"]
    assert l[1].spans and r[1].spans                       # evidenziazione intra-riga
    assert D.diff_stats(old, new) == (1, 0, 1)
    inl = D.inline_rows(old, new)
    assert [x.kind for x in inl] == ["equal", "del", "add", "equal", "add"]
    assert D.rows_vs_base("a", "")[0].kind == "del"        # elemento eliminato
    assert [x.kind for x in D.rows_vs_base("", "x\ny")] == ["add", "add"]


def test_intraline_spans_only_changed_tokens():
    a, b = "XIC(Motor_Run)", "XIC(Motor_Stop)"
    sa, sb = D.intraline_spans(a, b)
    assert [a[s:e] for s, e in sa] == ["Motor_Run"] and [b[s:e] for s, e in sb] == ["Motor_Stop"]


def test_logix_highlighter_formats_instructions(qapp):
    from PySide6.QtGui import QTextDocument
    doc = QTextDocument()
    doc.setPlainText("BST XIC(Start) NXB XIO(Stop) BND TON(T1,5000,0) OTE(Out);")
    hl = D.LogixHighlighter(doc)
    hl.rehighlight()
    colors = {f.format.foreground().color().name().lower() for f in doc.firstBlock().layout().formats()}
    assert D.T["SYN_INSTR"].lower() in colors and D.T["SYN_BRANCH"].lower() in colors


def test_diff_viewer_widgets_render(qapp):
    v = D.DiffViewer()
    v.set_diff("a\nb", "a\nB\nc", "Base", "Dopo")
    assert v.left.rows() and len(v.left.rows()) == len(v.right.rows())
    v.set_mode("inline")
    assert v.inline.rows() and v.mode == "inline"
    t = D.ThreeWayView()
    t.set_texts("x", "y", "", ("B", "M", "T"))
    assert t.views[2].rows()[0].kind == "del"


def test_stylesheet_has_no_unresolved_tokens():
    css = build_stylesheet()
    assert "@" not in css and "#00E5FF" in css and "#121417" in css


def test_password_suggestion_matches_policy():
    for _ in range(20):
        pw = suggest_password()
        assert len(pw) >= 10 and any(c.isalpha() for c in pw) and any(c.isdigit() for c in pw)


def test_badge_kind_mapping():
    assert badge_kind(ProposalState.AWAITING_CONSENT) == "pending"
    assert badge_kind(ProposalState.AWAITING_CONSENT, needs_lead_pending=True) == "lead"
    assert badge_kind(ProposalState.CONFLICT) == "conflict"
    assert badge_kind(ProposalState.MERGED, forced=True) == "forced"
    assert badge_kind(ProposalState.APPROVED) == "approved"


def test_audit_csv_export():
    e = AuditOut(id=1, ts=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc), actor="dev1", action="merge_requested",
                 project_id=3, target="abc", details={"n": 2, "nome": "è"}, hash="f" * 64)
    rows = to_csv([e], {3: "Linea 1"}).strip().splitlines()
    assert rows[0].startswith("id;timestamp_utc") and "Linea 1" in rows[1] and "dev1" in rows[1]


def _detail(state=ProposalState.AWAITING_CONSENT, author="u1", parts=(), needs_lead=False, lead_by=None, unresolved=0):
    now = datetime.now(timezone.utc)
    return ProposalDetail(id="p1", author="A", author_session=author, base_revision=1, head_revision=1, state=state,
                          state_label="x", created_at=now, updated_at=now, n_unresolved=unresolved,
                          participants=[ParticipantOut(session_id=s, developer=s, decision=d) for s, d in parts],
                          needs_lead_approval=needs_lead, lead_approved_by=lead_by)


def test_compute_permissions_rules():
    d = _detail(parts=[("u1", "approve"), ("u2", "pending")], needs_lead=True)
    p2 = compute_permissions(d, "u2", Role.DEVELOPER)
    assert p2.approve and p2.reject and not p2.cancel and not p2.resolve and not p2.force
    p1 = compute_permissions(d, "u1", Role.DEVELOPER)
    assert not p1.approve and p1.cancel and p1.resolve                      # autore: gia' approvato, puo' annullare
    lead = compute_permissions(d, "u9", Role.LEAD)                          # Lead non partecipante: consenso finale
    assert lead.approve and lead.approve_as_lead and lead.reject
    assert not compute_permissions(d, "u9", Role.DEVELOPER).approve         # Developer estraneo: niente
    v = compute_permissions(d, "u2", Role.VIEWER)
    assert v.read_only and not (v.approve or v.reject or v.cancel or v.force)
    adm = compute_permissions(d, "u9", Role.ADMIN)
    assert adm.force and adm.cancel and adm.resolve
    assert not compute_permissions(_detail(unresolved=2), "u9", Role.ADMIN).force      # conflitti aperti
    assert not compute_permissions(d, "u9", Role.ADMIN, pending_resolutions=1).force   # risoluzioni non applicate
    closed = compute_permissions(_detail(state=ProposalState.MERGED, parts=[("u2", "pending")]), "u2", Role.ADMIN)
    assert not (closed.approve or closed.reject or closed.cancel or closed.force)
    conf = compute_permissions(_detail(state=ProposalState.CONFLICT, parts=[("u2", "pending")]), "u2", Role.DEVELOPER)
    assert not conf.approve and conf.reject                                  # niente consenso finche' ci sono conflitti


def test_run_async_callbacks_return_on_gui_thread(qapp):
    from client.workers import run_async
    got = {}
    run_async(lambda: threading.current_thread().name, lambda r: got.update(ok=r, cb=threading.current_thread().name))
    assert pump(qapp, lambda: "ok" in got)
    assert got["cb"] == threading.main_thread().name and got["ok"] != got["cb"]       # lavoro fuori, callback dentro
    run_async(lambda: 1 / 0, None, lambda e: got.update(err=type(e).__name__))
    assert pump(qapp, lambda: "err" in got) and got["err"] == "ZeroDivisionError"


# ======================================================================================= server reale
def _live_server(tmp_path_factory, master_name="master.L5X", master_text=None):
    import uvicorn
    from server.config import Settings
    from server.main import create_app
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    app = create_app(Settings(data_dir=tmp_path_factory.mktemp("srv"), bcrypt_rounds=4, admin_password=ADMIN_PW))
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.05)
    url = f"http://127.0.0.1:{port}"

    admin = ApiClient(url)
    admin.login("admin", ADMIN_PW)
    ids = {}
    roster = [("lead", "Lead_Engineer"), ("dev1", "Developer"), ("dev2", "Developer"), ("viewer", "Viewer"),
              ("nuovo", "Developer")]
    for name, role in roster:
        u = admin.create_user(UserCreate(username=name, display_name=name.title(), password=PW, role=role,
                                         must_change_password=(name == "nuovo")))
        ids[name] = u.id
    proj = admin.create_project(ProjectCreate(name="Linea 1"))
    for name, role in roster:
        admin.set_permission(proj.id, ids[name], role)
    tmp = tmp_path_factory.mktemp("files")
    master = tmp / master_name
    master.write_text(S.base_project() if master_text is None else master_text, "utf-8")
    admin.init_master_for(proj.id, master)
    yield {"url": url, "pid": proj.id, "ids": ids, "admin": admin, "tmp": tmp}
    srv.should_exit = True
    t.join(5)


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    yield from _live_server(tmp_path_factory)


@pytest.fixture(scope="module")
def live_l5k(tmp_path_factory):
    from tests.test_filetypes import l5k
    yield from _live_server(tmp_path_factory, "Linea1.L5K", l5k())


def client_for(live, name, pw=PW, heartbeat=True) -> ApiClient:
    c = ApiClient(live["url"])
    c.login(name, pw)
    c.set_project(live["pid"])
    if heartbeat:
        c.report_revision(1)
    return c


def test_rbac_visible_in_client_api(live):
    viewer = client_for(live, "viewer")
    with pytest.raises(ApiError) as e:
        viewer.users()
    assert e.value.status == 403
    f = live["tmp"] / "v.L5X"
    f.write_text(S.dev_a(), "utf-8")
    with pytest.raises(ApiError) as e:
        viewer.submit_proposal(f, 1, "no")
    assert e.value.status == 403
    assert viewer.download_master(live["tmp"] / "dl.L5X") >= 1                  # il Viewer puo' scaricare
    with pytest.raises(ApiError) as e:
        ApiClient(live["url"]).login("dev1", "sbagliata")
    assert e.value.status == 401


def test_gui_consent_flow_with_roles(live, qapp, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *a, **k: ("motivo di test valido", True))
    tmp = live["tmp"]
    dev1, dev2 = client_for(live, "dev1"), client_for(live, "dev2")
    lead = client_for(live, "lead", heartbeat=False)
    viewer = client_for(live, "viewer")
    admin = client_for(live, "admin", ADMIN_PW, False)

    fb, fa = tmp / "b.L5X", tmp / "a.L5X"
    fb.write_text(S.dev_b(True), "utf-8")
    fa.write_text(S.dev_a(), "utf-8")

    # --- dev2 propone: serve il consenso di dev1 e dev2 (online) + Consenso Finale di un Lead
    pb = dev2.submit_proposal(fb, 1, "modifiche di B")
    assert pb.needs_lead_approval and pb.author_role == "Developer"
    d1 = ApprovalDialog(dev1, pb.id, Role.DEVELOPER, run=sync)
    d2 = ApprovalDialog(dev2, pb.id, Role.DEVELOPER, run=sync)
    dl = ApprovalDialog(lead, pb.id, Role.LEAD, run=sync)
    dv = ApprovalDialog(viewer, pb.id, Role.VIEWER, run=sync)
    assert d1.btn_approve.isEnabled() and d1.btn_reject.isEnabled() and not d1.btn_cancel.isEnabled()
    assert d2.btn_cancel.isEnabled()                                         # l'autore puo' annullare
    assert dl.btn_approve.isEnabled() and "Lead" in dl.btn_approve.text()   # Lead non partecipante: consenso finale
    assert dv.btn_approve.isHidden() and dv.btn_reject.isHidden()            # Viewer: sola lettura
    assert d1.changes.tree.topLevelItemCount() == 2 and d1.badge.kind == "pending"
    assert d1.changes.diff.left.rows()                                       # diff Ladder caricato

    d1._approve()
    d2._approve()
    assert d2.detail.state == ProposalState.AWAITING_CONSENT and d2.badge.kind == "lead"    # manca il Lead
    dl.reload()
    dl._approve()
    assert dl.detail.state == ProposalState.MERGED and dl.detail.lead_approved_by == "lead"
    assert dl.badge.kind == "merged" and not dl.btn_approve.isEnabled()
    assert admin.master_info().revision == 2

    # --- dev1 propone da rev 1: conflitti con il Master appena consolidato
    pa = dev1.submit_proposal(fa, 1, "modifiche di A")
    assert pa.state == ProposalState.CONFLICT and pa.n_unresolved >= 1
    a1 = ApprovalDialog(dev1, pa.id, Role.DEVELOPER, run=sync)
    a2 = ApprovalDialog(dev2, pa.id, Role.DEVELOPER, run=sync)
    ad = ApprovalDialog(admin, pa.id, Role.ADMIN, run=sync)
    assert a1.badge.kind == "conflict" and not a1.btn_approve.isEnabled() and not a2.btn_approve.isEnabled()
    assert a1.conf_list.count() == pa.n_conflicts and a1.conf_panel.view.views[1].rows()
    assert not a2.conf_panel.rb["mine"].isEnabled()                          # solo l'autore (o l'Admin) risolve
    assert not ad.btn_force.isHidden() and not ad.btn_force.isEnabled()      # conflitti aperti: niente forzatura
    for row in range(a1.conf_list.count()):                                  # l'autore sceglie e applica
        a1.conf_list.setCurrentRow(row)
        c = a1.detail.conflicts[row]
        (a1.conf_panel.rb["both"] if c.sequence else a1.conf_panel.rb["mine"]).setChecked(True)
    assert a1.btn_apply.isEnabled()
    a1._apply()
    assert a1.detail.n_unresolved == 0 and a1.detail.state == ProposalState.AWAITING_CONSENT
    assert a1.btn_approve.isEnabled()

    # --- l'Admin forza il merge (motivo obbligatorio, finisce nell'audit)
    ad.reload()
    assert ad.btn_force.isEnabled()
    ad._force()
    assert ad.detail.state == ProposalState.MERGED and ad.detail.forced_by == "admin" and ad.badge.kind == "forced"
    assert admin.master_info().revision == 3
    forced = admin.audit(action="merge_forced")
    assert forced and forced[0].actor == "admin"


def _window(live, tmp_path, name, **kw):
    from client.gui_main import MainWindow
    st = QSettings(str(tmp_path / f"{name}.ini"), QSettings.IniFormat)
    st.setValue("workspace", str(tmp_path / "ws"))
    win = MainWindow(run=sync, settings=st, use_tray=False, **kw)
    win.pick_folder = lambda _p, _t, _s: str(tmp_path / f"progetto_{name}")       # nessun dialogo nei test
    return win


def test_main_window_login_workspace_and_push(live, qapp, tmp_path):
    win = _window(live, tmp_path, "dev1", heartbeat_ms=60_000)
    win.show()
    assert win.stack.currentWidget() is win.login

    win._do_login(live["url"], "dev1", "sbagliata")
    assert win.login.error.text() and win.stack.currentWidget() is win.login        # errore mostrato, nessun accesso

    win._do_login(live["url"], "dev1", PW)
    assert win.stack.currentWidget() is win.workspace_widget
    assert win.project and win.project.id == live["pid"] and win.project_role == Role.DEVELOPER
    assert win.sidebar.buttons["users"].isHidden()                                 # solo l'Admin gestisce gli utenti
    assert not win.dashboard.btn_merge.isHidden() and "Master rev" in win.dashboard.lbl_master.text()

    win.checkout()                                                                  # scarica Lavoro_revN + Master.L5X
    files = sorted(p.name for p in win.workdir.glob("*.L5X"))
    assert "Master.L5X" in files and any(f.startswith("Lavoro_rev") for f in files)
    rev = win.base_revision
    assert rev >= 1 and win.working_file.endswith(f"Lavoro_rev{rev}.L5X")

    # notifiche push: un altro utente invia una richiesta -> si apre la schermata di consenso
    assert pump(qapp, lambda: win.ws.is_up), "WebSocket non connesso"
    win._heartbeat()
    f = tmp_path / "x.L5X"
    f.write_text(S.dev_b(False), "utf-8")
    dev2 = client_for(live, "dev2")
    base = dev2.master_info().revision
    dev2.report_revision(base)
    p = dev2.submit_proposal(f, base, "push test")
    assert pump(qapp, lambda: p.id in win.dialogs), "la schermata di consenso non si e' aperta"
    assert pump(qapp, lambda: any(x.id == p.id for x in win.dashboard.visible_proposals()))
    dev2.cancel(p.id)

    win.sidebar.logout_requested.emit()
    assert win.stack.currentWidget() is win.login and win.api is None and not win.ws.is_up
    win.close()


def test_viewer_window_is_read_only(live, qapp, tmp_path):
    win = _window(live, tmp_path, "viewer")
    win._do_login(live["url"], "viewer", PW)
    assert win.project_role == Role.VIEWER
    assert win.dashboard.btn_merge.isHidden() and "Scarica" in win.dashboard.btn_checkout.text()
    win.checkout()
    assert list(win.workdir.glob("Master_rev*.L5X")) and not (win.workdir / "Master.L5X").exists()
    assert win.base_revision == 0                                                  # il Viewer non ha una copia di lavoro
    win._nav("audit")
    assert win.audit.table.rowCount() > 0 and win.audit.btn_verify.isHidden()
    win.ws.stop()
    win.close()


def test_admin_window_user_management_and_audit(live, qapp, tmp_path, monkeypatch):
    from client.views.user_mgmt_view import UserDialog
    win = _window(live, tmp_path, "admin")
    win._do_login(live["url"], "admin", ADMIN_PW)
    assert not win.sidebar.buttons["users"].isHidden() and win.project_role == Role.ADMIN

    win._nav("users")
    um = win.users
    assert um.users_table.rowCount() >= 6
    um.tabs.setCurrentIndex(1)
    um.reload()
    assert um.proj_combo.count() >= 1 and um.perm_table.rowCount() >= 6
    labels = {um.perm_table.cellWidget(r, 0).findChildren(QLabel)[0].text() for r in range(um.perm_table.rowCount())}
    assert any("Dev1" in n for n in labels)
    # il combo dei permessi non offre un ruolo superiore a quello dell'account (viewer -> solo Viewer / nessun accesso)
    row = next(r for r in range(um.perm_table.rowCount()) if um.perm_table.item(r, 1).text().startswith("Viewer"))
    cb = um.perm_table.cellWidget(row, 2).layout().itemAt(0).widget()
    assert [cb.itemData(i) for i in range(cb.count())] == [None, "Viewer"]
    # revoca e riassegnazione di un permesso
    um._perm_changed(live["ids"]["viewer"], None)
    assert [p for p in live["admin"].permissions(live["pid"]) if p.username == "viewer"][0].role is None
    um._perm_changed(live["ids"]["viewer"], "Viewer")
    assert [p for p in live["admin"].permissions(live["pid"]) if p.username == "viewer"][0].role == "Viewer"

    # creazione utente dalla vista (dialog pilotato)
    def fake_exec(self):
        self.username.setText("creato.da.gui")
        self.display.setText("Creato")
        self._generate()
        self._accept()
        return QDialog.Accepted if self.result() == QDialog.Accepted else QDialog.Rejected
    monkeypatch.setattr(UserDialog, "exec", fake_exec)
    um.tabs.setCurrentIndex(0)
    um.new_user()
    assert any(u.username == "creato.da.gui" and u.must_change_password for u in live["admin"].users())
    um.tabs.setCurrentIndex(2)
    um.reload()
    assert um.sess_table.rowCount() >= 1

    win._nav("audit")
    assert not win.audit.btn_verify.isHidden() and win.audit.table.rowCount() > 0
    win.audit.scope.setCurrentIndex(1)
    win.audit.action.setCurrentIndex(win.audit.action.findData("user_created"))
    win.audit.request()
    assert win.audit.table.rowCount() >= 1 and all(e.action == "user_created" for e in win.audit._entries)
    win._audit_verify()
    assert "Integrità verificata" in win.audit.banner.text()
    out = win.audit.export_csv(str(tmp_path / "audit.csv"))
    assert Path(out).read_text("utf-8-sig").startswith("id;timestamp_utc")
    win.ws.stop()
    win.close()


def test_forced_password_change_on_first_login(live, qapp, tmp_path, monkeypatch):
    from client.views.login_view import PasswordChangeDialog
    monkeypatch.setattr(PasswordChangeDialog, "exec", lambda self: (
        self.old.setText(PW), self.new.setText("NuovaPassword99"), self.new2.setText("NuovaPassword99"),
        QDialog.Accepted)[-1])
    win = _window(live, tmp_path, "nuovo")
    win._do_login(live["url"], "nuovo", PW)
    assert win.stack.currentWidget() is win.workspace_widget and not win.api.user.must_change_password
    win.ws.stop()
    win.close()
    ApiClient(live["url"]).login("nuovo", "NuovaPassword99")                       # la nuova password funziona
    with pytest.raises(ApiError):
        ApiClient(live["url"]).login("nuovo", PW)


def test_expired_session_returns_to_login(live, qapp, tmp_path):
    win = _window(live, tmp_path, "expired")
    win._do_login(live["url"], "dev2", PW)
    for s in live["admin"].sessions(all_users=True):                               # l'Admin revoca le sessioni di dev2
        if s.username == "dev2" and not s.revoked:
            live["admin"].revoke_session(s.id)
    win.api._expires_at = 0                                                         # access token da rinnovare
    win.refresh_all()                                                               # il refresh fallisce -> 401
    assert win.stack.currentWidget() is win.login and "scaduta" in win.login.error.text()
    win.close()


# ======================================================================================= cartella locale e merge
def test_find_merge_candidates(tmp_path):
    from client.localfiles import MASTER_COPY, find_merge_candidates, pick_default
    f = tmp_path / "proj"
    (f / "sub").mkdir(parents=True)
    (f / "a" / "b" / "c").mkdir(parents=True)
    master = f / MASTER_COPY
    master.write_text(S.base_project(), "utf-8")
    old = time.time() - 100
    os.utime(master, (old, old))
    (f / "Lavoro_rev1.L5X").write_text(S.base_project(), "utf-8")             # identico al Master
    os.utime(f / "Lavoro_rev1.L5X", (old, old))
    (f / "mio.L5X").write_text(S.dev_a(), "utf-8")                            # modificato dopo il checkout
    (f / "sub" / "altro.l5x").write_text(S.dev_b(False), "utf-8")
    (f / "Master_rev3.L5X").write_text("x", "utf-8")                          # copia scaricata: esclusa
    (f / "~temp.L5X").write_text("x", "utf-8")
    (f / "note.txt").write_text("x", "utf-8")
    (f / "rotto.L5X").write_text("non e' xml", "utf-8")
    (f / "a" / "b" / "c" / "troppo_profondo.L5X").write_text(S.dev_a(), "utf-8")
    c = {x.name: x for x in find_merge_candidates(f, working_file=str(f / "Lavoro_rev1.L5X"))}
    assert set(c) == {"Lavoro_rev1.L5X", "mio.L5X", "altro.l5x", "rotto.L5X"}
    assert c["Lavoro_rev1.L5X"].unchanged and c["Lavoro_rev1.L5X"].is_working and not c["Lavoro_rev1.L5X"].modified
    assert c["mio.L5X"].modified and c["mio.L5X"].looks_l5x and not c["mio.L5X"].unchanged
    assert not c["rotto.L5X"].looks_l5x
    best = pick_default(find_merge_candidates(f))
    assert best is not None and best.looks_l5x and best.modified
    assert find_merge_candidates(tmp_path / "inesistente") == []


def test_merge_dialog_lists_folder_and_selects_latest(qapp, tmp_path):
    from client.localfiles import MASTER_COPY
    from client.views.merge_dialog import MergeRequestDialog
    f = tmp_path / "p"
    f.mkdir()
    (f / MASTER_COPY).write_text(S.base_project(), "utf-8")
    old = time.time() - 100
    os.utime(f / MASTER_COPY, (old, old))
    (f / "mio.L5X").write_text(S.dev_a(), "utf-8")
    (f / "rotto.L5X").write_text("no", "utf-8")
    d = MergeRequestDialog(f, "", 3, "Linea 1")
    assert d.table.rowCount() == 2 and d.selected_path() == f / "mio.L5X" and d.ok_btn.isEnabled()
    d.select_path(f / "rotto.L5X")
    assert not d.ok_btn.isEnabled() and "valido" in d.info.text()                 # un file non L5X non si puo' inviare
    ext = tmp_path / "fuori.L5X"
    ext.write_text(S.dev_b(False), "utf-8")
    d.add_external(ext)
    assert d.selected_path() == ext and d.ok_btn.isEnabled()
    d.rescan()
    assert d.selected_path() == ext                                                # la scelta manuale sopravvive all'aggiornamento
    empty = MergeRequestDialog(tmp_path / "vuota", "", 1)
    assert empty.table.rowCount() == 0 and not empty.ok_btn.isEnabled() and "Nessun file" in empty.info.text()


def test_window_local_folder_choice_checkout_and_merge_scan(live, qapp, tmp_path, monkeypatch):
    from client.views.merge_dialog import MergeRequestDialog
    win = _window(live, tmp_path, "dev2")
    asked = []
    chosen = tmp_path / "Miei Progetti" / "Linea 1"

    def pick(_p, title, start):
        asked.append((title, start))
        return str(chosen)
    win.pick_folder = pick
    win._do_login(live["url"], "dev2", PW)
    assert not win.folder_chosen and str(tmp_path / "ws") in str(win.local_folder)    # proposta di default
    assert "proposta" in win.dashboard.lbl_folder.text()

    win.checkout()                                                                   # primo checkout: chiede la cartella
    assert len(asked) == 1 and "Linea 1" in asked[0][0] and win.folder_chosen
    assert win.local_folder == chosen and (chosen / "Master.L5X").exists()
    assert win.working_file.startswith(str(chosen)) and str(chosen) in win.dashboard.lbl_folder.text()
    assert "proposta" not in win.dashboard.lbl_folder.text()
    win.checkout()
    assert len(asked) == 1                                                           # le volte successive non chiede piu'

    # l'utente salva da Studio 5000 nella cartella scelta: la finestra di merge lo trova da sola
    (chosen / "mie_modifiche.L5X").write_text(S.dev_a(), "utf-8")
    seen = {}

    def fake_exec(self):
        seen["folder"], seen["names"] = self.folder, [c.name for c in self.candidates()]
        seen["default"] = self.selected_path()
        self.message.setText("scansione cartella")
        return QDialog.Accepted
    monkeypatch.setattr(MergeRequestDialog, "exec", fake_exec)
    win.request_merge()
    assert seen["folder"] == chosen and "mie_modifiche.L5X" in seen["names"] and "Master.L5X" not in seen["names"]
    assert seen["default"] == chosen / "mie_modifiche.L5X"
    props = win.api.proposals()
    mine = [p for p in props if p.message == "scansione cartella"]
    assert mine and mine[0].n_changes > 0
    win.api.cancel(mine[0].id)

    # cambio cartella: la nuova scelta vale per questo progetto e utente
    other = tmp_path / "altra"
    win.pick_folder = lambda *_a: str(other)
    assert win.choose_folder() and win.local_folder == other
    win.ws.stop()
    win.close()


def test_window_with_l5k_master_uses_l5k_extension_everywhere(live_l5k, qapp, tmp_path, monkeypatch):
    """Progetto con Master .L5K: checkout, copia Master, ricerca file da unire e sincronizzazione usano .L5K."""
    from client.views.merge_dialog import MergeRequestDialog
    from tests.test_filetypes import l5k
    live = live_l5k
    win = _window(live, tmp_path, "dev1")
    win._do_login(live["url"], "dev1", PW)
    assert win.master_ext == ".L5K" and win.master_kind == "l5k"
    win.checkout()
    ws = win.local_folder
    assert (ws / "Master.L5K").exists() and (ws / "Lavoro_rev1.L5K").exists() and not list(ws.glob("*.L5X"))
    assert win.working_file.endswith("Lavoro_rev1.L5K")

    (ws / "mia.L5K").write_text(l5k(r01a="XIC(A)XIC(Z)OTE(B);"), "utf-8")
    (ws / "vecchio.L5X").write_text(S.dev_a(), "utf-8")
    seen = {}

    def fake_exec(self):
        seen.update(ext=self.ext, kind=self.kind, names=[c.name for c in self.candidates()],
                    default=self.selected_path())
        self.message.setText("merge L5K")
        return QDialog.Accepted
    monkeypatch.setattr(MergeRequestDialog, "exec", fake_exec)
    win.request_merge()
    assert seen["ext"] == ".L5K" and seen["kind"] == "l5k"
    assert "mia.L5K" in seen["names"] and "vecchio.L5X" not in seen["names"] and "Master.L5K" not in seen["names"]
    assert seen["default"] == ws / "mia.L5K"
    mine = [p for p in win.api.proposals() if p.message == "merge L5K"]
    assert mine and mine[0].n_changes == 1
    win.api.cancel(mine[0].id)

    saved = []
    monkeypatch.setattr("client.gui_main.QFileDialog.getSaveFileName",
                        staticmethod(lambda *a, **k: (saved.append(a) or str(tmp_path / "salvato.L5K"), "")))
    win._download_revision(1)
    assert (tmp_path / "salvato.L5K").read_bytes() == l5k().encode()
    assert saved[0][2] == "Master_rev1.L5K" and "Tutti i file (*)" in saved[0][3]
    win.ws.stop()
    win.close()


def test_init_master_dialog_offers_all_files_filter(live, qapp, monkeypatch):
    from client.views import user_mgmt_view as U
    seen = {}

    def fake_open(*a, **k):
        seen["filters"] = a[3]
        return ("", "")
    monkeypatch.setattr(U.QFileDialog, "getOpenFileName", staticmethod(fake_open))
    api = ApiClient(live["url"])
    api.login("admin", ADMIN_PW)
    view = U.UserMgmtView(api, run=sync)
    view.tabs.setCurrentIndex(1)
    view.reload()
    assert view.current_project() is not None
    view.init_master()
    assert seen["filters"].startswith("Tutti i file (*)") and "*.L5K" in seen["filters"] and "*.L5X" in seen["filters"]
