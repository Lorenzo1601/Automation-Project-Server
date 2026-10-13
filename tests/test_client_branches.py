"""Client: branch di progetto (selettore, cambio di contesto, dialogo, salvataggi, sync/merge, cartelle locali, eventi).

Due livelli: (1) un server FINTO su ``httpx.MockTransport`` (il vero ``ApiClient`` e la vera ``MainWindow``, nessuna rete);
(2) in fondo, se il server dei branch c'e', test end-to-end con il server vivo."""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QDialog, QInputDialog, QMessageBox                         # noqa: E402

from client import gui_main                                                              # noqa: E402
from client.api_client import ApiClient, ApiError                                        # noqa: E402
from client.branch_info import (abilities, behind_text, branch_choices, can_create, kind_label,   # noqa: E402
                                my_open_personal, summary_text, sync_text)
from client.localfiles import (BRANCHES_DIR, branch_folder, find_merge_candidates, prune_local,   # noqa: E402
                               ref_path)
from client.views.branches_dialog import BranchesDialog, NewBranchDialog                 # noqa: E402
from client.views.merge_dialog import MergeRequestDialog                                 # noqa: E402
from client.views.sidebar import Sidebar                                                 # noqa: E402
from client.views.user_mgmt_view import UserMgmtView                                     # noqa: E402
from common.roles import Role                                                            # noqa: E402
from common.schemas import MasterInfo, ProposalDetail, ProposalState, ProjectOut, UserOut   # noqa: E402
from common.schemas_branches import MAX_PERSONAL_BRANCHES, BranchOut                     # noqa: E402
from tests.test_client import ADMIN_PW, PW, _window, qapp, sync                          # noqa: E402,F401

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def bo(slug="mio", kind="personal", owner="dev1", state="open", base=2, head=2, behind=1, ahead=0, can_write=True,
       can_delete=True, name=None, bid=1) -> BranchOut:
    return BranchOut(id=bid, name=name or slug, slug=slug, kind=kind, owner=owner, created_at=NOW, state=state,
                     base_main_revision=base, head_revision=head, behind=behind, ahead=ahead, can_write=can_write,
                     can_delete=can_delete)


def proposal(pid: str) -> dict:
    return ProposalDetail(id=pid, author="dev1", author_session="u2", base_revision=1, head_revision=3,
                          state=ProposalState.AWAITING_CONSENT, state_label="In attesa di consenso", created_at=NOW,
                          updated_at=NOW, n_changes=4, n_conflicts=1).model_dump(mode="json")


# ======================================================================================= server finto
class FakeServer:
    """Risponde alle chiamate REST del client in memoria e le registra in ``calls`` (metodo, percorso, parametri)."""

    def __init__(self, username="dev1", role="Developer", branches=None, main_head=3):
        self.username, self.role = username, role
        self.main_head = main_head
        self.branches: dict[str, BranchOut] = {b.slug: b for b in (branches or [])}
        self.calls: list[tuple[str, str, dict, bytes]] = []
        self.fail_branches: int | None = None
        self.head_sha = "a" * 64                     # sha256 della testa che il finto server dichiara

    # -- utilita'
    def paths(self, method=None, suffix=""):
        return [(m, p, q) for m, p, q, _b in self.calls if (method is None or m == method) and p.endswith(suffix)]

    def last(self, method, suffix):
        return [c for c in self.calls if c[0] == method and c[1].endswith(suffix)][-1]

    def _head(self, branch):
        return self.branches[branch].head_revision if branch else self.main_head

    def _info(self, rev: int) -> dict:
        return MasterInfo(revision=rev, sha256=self.head_sha, updated_at=NOW, updated_by="admin", message=f"rev {rev}",
                          kind="l5x", ext=".L5X", name="Master").model_dump(mode="json")

    def project(self) -> dict:
        return ProjectOut(id=1, slug="linea-1", name="Linea 1", my_role=self.role, head_revision=self.main_head,
                          master_kind="l5x", master_ext=".L5X", master_name="Master", max_revisions=2
                          ).model_dump(mode="json")

    # -- router
    def handler(self, req: httpx.Request) -> httpx.Response:
        path, q = req.url.path, dict(req.url.params)
        self.calls.append((req.method, path, q, req.content))
        branch = q.get("branch")
        ok = lambda body: httpx.Response(200, json=body)           # noqa: E731
        if path == "/api/auth/login":
            u = UserOut(id=2, username=self.username, display_name=self.username.title(), role=self.role)
            return ok({"access_token": "tok", "refresh_token": "ref", "expires_in": 3600, "session_id": "s1",
                       "user": u.model_dump(mode="json")})
        if path in ("/api/auth/logout", "/api/auth/me") or path.endswith("/heartbeat"):
            return ok({})
        if path == "/api/client/update":
            return ok({"available": False})
        if path == "/api/notifications/count":
            return ok({"unread": 0})
        if path == "/api/users":
            return ok([UserOut(id=2, username=self.username, display_name="X", role=self.role).model_dump(mode="json")])
        if path == "/api/projects":
            return ok([self.project()])
        base = "/api/projects/1"
        if path == f"{base}/branches":
            if self.fail_branches:
                return httpx.Response(self.fail_branches, json={"detail": "no"})
            if req.method == "GET":
                return ok([b.model_dump(mode="json") for b in self.branches.values()])
            body = json.loads(req.content)
            slug = re.sub(r"[^a-z0-9_-]+", "-", body["name"].lower())
            b = bo(slug, body.get("kind", "personal"), self.username, base=body.get("from_revision") or self.main_head,
                   head=1, behind=0, name=body["name"], bid=len(self.branches) + 1)
            self.branches[slug] = b
            return ok(b.model_dump(mode="json"))
        m = re.fullmatch(rf"{base}/branches/([^/]+)(?:/(commit|sync|merge|close))?", path)
        if m:
            slug, act = m.groups()
            if req.method == "DELETE":
                self.branches.pop(slug)
                return httpx.Response(204)
            b = self.branches[slug]
            if act == "commit":
                b.head_revision += 1
                b.ahead += 1
                return ok(self._info(b.head_revision))
            if act == "sync":
                return ok(proposal("p-sync"))
            if act == "merge":
                return ok(proposal("p-merge"))
            if act == "close":
                b.state = "closed"
                return ok(b.model_dump(mode="json"))
        if path == f"{base}/master":
            return ok(self._info(self._head(branch)))
        if path == f"{base}/master/history":
            return ok([self._info(r) for r in range(self._head(branch), 0, -1)])
        if path == f"{base}/master/download":
            rev = int(q.get("revision") or self._head(branch))
            return httpx.Response(200, content=f"<RSLogix5000Content branch={branch} rev={rev}/>".encode(),
                                  headers={"X-Revision": str(rev)})
        if path == f"{base}/proposals" and req.method == "POST":
            return ok(proposal("p-req"))
        if path in (f"{base}/proposals", f"{base}/developers", f"{base}/permissions"):
            return ok([])
        if path == f"{base}/lock":
            return ok({"locked": False})
        return httpx.Response(404, json={"detail": f"non gestito: {req.method} {path}"})


class SpyWs:
    """Al posto del WsListener: registra le (ri)sottoscrizioni."""

    def __init__(self):
        self.urls: list[str] = []
        self._want = False
        self.is_up = False

    def start(self, factory):
        self.urls.append(factory() if callable(factory) else factory)
        self._want = True

    def stop(self):
        self._want = False


def make_api(srv: FakeServer, user=None) -> ApiClient:
    api = ApiClient("http://fake")
    api._http = httpx.Client(base_url="http://fake", transport=httpx.MockTransport(srv.handler))
    api.login(user or srv.username, PW)
    api.set_project(1)
    return api


def make_window(tmp_path, monkeypatch, srv: FakeServer):
    monkeypatch.setattr(gui_main, "ApiClient", lambda url, trust=None: make_api_unlogged(srv))
    win = _window(None, tmp_path, "w", heartbeat_ms=600_000)
    win.ws.stop()
    win.ws = SpyWs()
    win.notify_user = lambda *_a: None
    win._do_login("http://fake", srv.username, PW)
    assert win.stack.currentWidget() is win.workspace_widget and win.project is not None
    win.opened = []
    win.open_proposal = lambda pid: win.opened.append((pid, win.api.branch))       # niente ApprovalDialog
    win.settings.setValue(win._folder_key(), str(tmp_path / "proj"))
    return win


def make_api_unlogged(srv: FakeServer) -> ApiClient:
    api = ApiClient("http://fake")
    api._http = httpx.Client(base_url="http://fake", transport=httpx.MockTransport(srv.handler))
    return api


# ======================================================================================= regole pure
def test_branch_rules_by_role_and_state():
    mine = bo()
    a = abilities(mine, Role.DEVELOPER, None)
    assert a.switch and a.commit and a.sync and a.merge and a.close and a.delete and not a.request_merge
    assert not abilities(mine, Role.DEVELOPER, "mio").switch                              # gia' il corrente
    assert not abilities(bo(behind=0), Role.DEVELOPER).sync                                # niente da portare
    assert not abilities(bo(can_write=False, can_delete=False), Role.DEVELOPER).commit     # branch personale altrui
    t = abilities(bo("t", "team"), Role.LEAD)
    assert t.request_merge and t.commit and t.merge
    for st in ("merged", "closed"):                                                         # sola lettura
        r = abilities(bo(state=st), Role.DEVELOPER)
        assert not (r.commit or r.sync or r.merge or r.close) and r.switch and r.delete
    v = abilities(bo(), Role.VIEWER)
    assert v.switch and not (v.commit or v.sync or v.merge or v.request_merge)

    assert can_create(Role.VIEWER, "personal")[0] is False
    assert can_create(Role.DEVELOPER, "personal")[0] and not can_create(Role.DEVELOPER, "team")[0]
    assert can_create(Role.LEAD, "team")[0] and can_create(Role.ADMIN, "team")[0]
    ok, why = can_create(Role.DEVELOPER, "personal", MAX_PERSONAL_BRANCHES)
    assert not ok and str(MAX_PERSONAL_BRANCHES) in why
    rows = [bo("a"), bo("b", owner="altro"), bo("c", state="closed"), bo("t", "team", owner="dev1")]
    assert my_open_personal(rows, "dev1") == 1

    assert behind_text(bo(base=4, behind=2)) == "da main rev 4, 2 indietro"
    assert behind_text(bo(base=4, behind=0)) == "da main rev 4, aggiornato"
    assert sync_text(bo(behind=2, ahead=1)) == "2 indietro · 1 avanti" and sync_text(bo(behind=0, ahead=0)) == "allineato"
    assert summary_text(bo(state="closed")).endswith("chiuso") and kind_label("team") == "team"
    shown = branch_choices(rows, "c")
    assert [b.slug for b in shown] == ["a", "b", "c", "t"]                                 # la chiusa compare perche' e' la corrente
    assert [b.slug for b in branch_choices(rows, None)] == ["a", "b", "t"]


def test_api_adds_branch_param_only_where_it_should(qapp):
    srv = FakeServer(branches=[bo("x")])
    api = make_api(srv)
    api.master_info()
    assert srv.last("GET", "/master")[2] == {}                                            # principale: nessun parametro
    api.set_project(1, "x")
    api.master_info()
    api.proposals()
    api.branches()                                                                        # elenco: mai il parametro
    api.branch_sync("x")
    api.history_for(1)                                                                    # varianti con progetto esplicito = principale
    api.permissions(1)
    assert srv.last("GET", "/master")[2] == {"branch": "x"} and srv.last("GET", "/proposals")[2]["branch"] == "x"
    assert srv.last("GET", "/branches")[2] == {} and srv.last("POST", "/sync")[2] == {}
    assert srv.last("GET", "/master/history")[2] == {}
    assert api.ws_url().endswith("&project=1&branch=x")
    api.set_project(1)
    assert api.branch is None and "branch" not in api.ws_url()
    api.set_project(None, "x")
    assert api.branch is None                                                             # senza progetto non c'e' branch
    with pytest.raises(ApiError):
        api.branches()


# ======================================================================================= selettore nella barra laterale
def test_sidebar_branch_selector(qapp):
    sb = Sidebar()
    assert sb.branch.count() == 1 and sb.branch.itemText(0) == "Principale" and "Linea principale" in sb.lbl_branch.text()
    rows = [bo("mio", base=3, behind=2), bo("squadra", "team", owner="lead", base=3, behind=0, bid=2),
            bo("vecchio", state="closed", bid=3), bo("unito", state="merged", bid=4)]
    got = []
    sb.branch_changed.connect(got.append)
    sb.set_branches(rows, None)
    assert [sb.branch.itemData(i) for i in range(sb.branch.count())] == ["", "mio", "squadra"]     # solo gli aperti
    assert "personale" in sb.branch.itemText(1) and "team" in sb.branch.itemText(2)
    sb.select_branch("mio")
    assert sb.branch.currentData() == "mio"
    assert "Personale" in sb.lbl_branch.text() and "da main rev 3, 2 indietro" in sb.lbl_branch.text()
    sb.select_branch("squadra")
    assert "aggiornato" in sb.lbl_branch.text()
    assert got == []                                                                       # i cambi da codice non emettono segnali
    sb.set_branches(rows, "vecchio")                                                       # il corrente chiuso resta visibile
    assert sb.branch.currentData() == "vecchio" and "chiuso" in sb.branch.currentText()
    sb.branch.setCurrentIndex(0)
    sb.branch.activated.emit(0)                                                            # scelta dell'utente
    assert got == [""]
    sb.select_branch(None)
    assert "Linea principale" in sb.lbl_branch.text()


# ======================================================================================= cambio di branch nella finestra
def test_switch_branch_sets_api_branch_resubscribes_ws_and_reloads(qapp, tmp_path, monkeypatch):
    srv = FakeServer(branches=[bo("mio", base=2, head=4, behind=1), bo("sq", "team", owner="lead", head=2, bid=2)])
    win = make_window(tmp_path, monkeypatch, srv)
    api = win.api
    assert api.branch is None and [b.slug for b in win.branches] == ["mio", "sq"]
    assert win.sidebar.branch.count() == 3 and win.dashboard.branch_bar.isHidden()
    assert win.ws.urls[-1].endswith("&project=1")
    assert "Master rev 3" in win.dashboard.lbl_master.text()

    dlg = QDialog(win)                                                                     # una finestra di consenso aperta
    win.dialogs["p9"] = dlg
    n_ws, n_calls = len(win.ws.urls), len(srv.calls)
    i = win.sidebar.branch.findData("mio")
    win.sidebar.branch.setCurrentIndex(i)
    win.sidebar.branch.activated.emit(i)                                                   # come la scelta dell'utente
    assert api.branch == "mio" and api.project_id == 1
    assert len(win.ws.urls) == n_ws + 1 and win.ws.urls[-1].endswith("&project=1&branch=mio")
    assert not dlg.isVisible() and srv.last("GET", "/master")[2] == {"branch": "mio"}       # finestre chiuse, dati del branch
    after = srv.calls[n_calls:]
    for suffix in ("/master", "/proposals", "/developers", "/lock"):                      # ricaricati TUTTI sul branch
        hits = [c for c in after if c[0] == "GET" and c[1].endswith(suffix)]
        assert hits and all(c[2].get("branch") == "mio" for c in hits), suffix
    assert "Branch «mio» rev 4" in win.dashboard.lbl_master.text()
    assert not win.dashboard.branch_bar.isHidden() and "branch «mio»" in win.dashboard.lbl_branch.text()
    assert "Linea 1" in win.dashboard.title.text() and "mio" in win.dashboard.title.text()
    assert win.sidebar.branch.currentData() == "mio" and "da main rev 2, 1 indietro" in win.sidebar.lbl_branch.text()
    assert "(branch)" in win.dashboard.lbl_local.text()

    win.select_branch("mio")                                                               # gia' li': nulla da fare
    assert len(win.ws.urls) == n_ws + 1
    win.dashboard.btn_branch_leave.click()                                                 # «Torna al principale» dal banner
    assert api.branch is None and win.ws.urls[-1].endswith("&project=1") and win.dashboard.branch_bar.isHidden()
    assert srv.last("GET", "/master")[2] == {} and "Master rev 3" in win.dashboard.lbl_master.text()

    win.select_branch("sq")                                                                # branch team
    assert api.branch == "sq" and win.ws.urls[-1].endswith("branch=sq")
    win.sidebar.project.activated.emit(0)                                                  # riselezionare lo stesso progetto: resta nel branch
    assert api.branch == "sq"
    win.ws.stop()
    win.close()


def test_branch_that_disappears_returns_to_main(qapp, tmp_path, monkeypatch):
    srv = FakeServer(branches=[bo("mio")])
    win = make_window(tmp_path, monkeypatch, srv)
    win.select_branch("mio")
    del srv.branches["mio"]                                                                # eliminato da un altro utente
    win.load_branches()
    assert win.api.branch is None and "non esiste più" in win.dashboard.log.toPlainText()
    assert win.sidebar.branch.currentData() == "" and win.dashboard.branch_bar.isHidden()
    win.close()


def test_server_without_branches_keeps_only_main(qapp, tmp_path, monkeypatch):
    srv = FakeServer()
    srv.fail_branches = 404
    win = make_window(tmp_path, monkeypatch, srv)
    assert win.branches == [] and win.sidebar.branch.count() == 1
    assert not win.statusBar().currentMessage()                                            # nessun errore mostrato
    win.close()


# ======================================================================================= eventi push
def test_events_of_other_branches_are_ignored(qapp, tmp_path, monkeypatch):
    srv = FakeServer(branches=[bo("mio"), bo("altro", bid=2)])
    win = make_window(tmp_path, monkeypatch, srv)
    win.select_branch("mio")
    log = win.dashboard.log

    def n_props():
        return len(srv.paths("GET", "/proposals"))

    n = n_props()
    win.on_event({"type": "lock_changed", "message": "evento di un altro branch", "branch": "altro"})
    win.on_event({"type": "proposal_submitted", "message": "altra proposta", "branch": "altro",
                  "proposal": proposal("px")})
    win.on_event({"type": "lock_changed", "message": "evento del principale", "branch": None})     # principale esplicito
    assert "altro branch" not in log.toPlainText() and "altra proposta" not in log.toPlainText()
    assert "principale" not in log.toPlainText() and n_props() == n

    win.on_event({"type": "developer_joined", "message": "dev2 e' entrato", "branch": "mio"})      # questo branch
    assert "dev2 e' entrato" in log.toPlainText()
    win.on_event({"type": "proposal_submitted", "message": "proposta qui", "proposal": proposal("py")})  # senza campo: vale
    assert "proposta qui" in log.toPlainText() and n_props() > n

    # un evento del principale che sposta «indietro» ricarica solo l'elenco dei branch
    k = len(srv.paths("GET", "/branches"))
    win.on_event({"type": "master_updated", "message": "x", "branch": None, "revision": 9})
    assert len(srv.paths("GET", "/branches")) == k + 1 and not srv.paths("GET", "/download")[1:]
    # sul principale vale l'opposto
    win.select_branch(None)
    win.on_event({"type": "lock_changed", "message": "solo branch mio", "branch": "mio"})
    assert "solo branch mio" not in log.toPlainText()
    win.on_event({"type": "lock_changed", "message": "principale ok", "branch": None})
    assert "principale ok" in log.toPlainText()
    # eventi sui branch: aggiornano l'elenco anche se riguardano un altro branch
    k = len(srv.paths("GET", "/branches"))
    win.on_event({"type": "branch_created", "message": "Nuovo branch «zeta»", "branch": "zeta"})
    assert len(srv.paths("GET", "/branches")) == k + 1 and "Nuovo branch" in log.toPlainText()
    win.close()


# ======================================================================================= dialogo
def test_branches_dialog_abilities_by_role_and_state(qapp):
    rows = [bo("mio", owner="dev1", behind=2, bid=1), bo("altrui", owner="dev2", can_write=False, can_delete=False, bid=2),
            bo("sq", "team", owner="lead", bid=3), bo("chiuso", state="closed", bid=4),
            bo("unito", state="merged", can_write=False, bid=5)]
    proj = ProjectOut(id=1, slug="l", name="Linea 1", head_revision=3, my_role="Developer")

    def dialog(role, current=None, manage_only=False, username="dev1"):
        srv = FakeServer(username=username, role=role.key, branches=rows)
        api = make_api(srv)
        dlg = BranchesDialog(api, proj, role, current, sync, manage_only=manage_only)
        return dlg, srv

    def pick(dlg, slug):
        r = next(i for i, b in enumerate(dlg._rows) if b.slug == slug)
        dlg.table.selectRow(r)

    dev, srv = dialog(Role.DEVELOPER)
    assert dev.table.rowCount() == 5 and [dev.table.horizontalHeaderItem(c).text() for c in range(7)][:4] == [
        "Nome", "Tipo", "Proprietario", "Stato"]
    assert dev.btn_new.isEnabled() and not dev.btn_main.isEnabled()
    pick(dev, "mio")
    r = dev.table.currentRow()
    assert dev.table.item(r, 1).text() == "Personale" and dev.table.item(r, 2).text() == "dev1"
    assert dev.table.item(r, 3).text() == "Aperto" and dev.table.item(r, 4).text() == "rev 2" and dev.table.item(r, 6).text() == "2 indietro"
    assert all(b.isEnabled() for b in (dev.btn_switch, dev.btn_sync, dev.btn_merge, dev.btn_close_branch, dev.btn_delete))
    pick(dev, "altrui")                                                                    # personale altrui: solo passare a guardare
    assert dev.btn_switch.isEnabled() and not (dev.btn_sync.isEnabled() or dev.btn_merge.isEnabled()
                                               or dev.btn_close_branch.isEnabled() or dev.btn_delete.isEnabled())
    assert "sola lettura per te" in dev.lbl_sel.text()
    pick(dev, "chiuso")                                                                    # chiuso: sola lettura, ma eliminabile
    assert dev.btn_switch.isEnabled() and not (dev.btn_sync.isEnabled() or dev.btn_merge.isEnabled()
                                               or dev.btn_close_branch.isEnabled()) and dev.btn_delete.isEnabled()
    pick(dev, "unito")
    assert not (dev.btn_merge.isEnabled() or dev.btn_close_branch.isEnabled()) and "sola lettura" in dev.lbl_sel.text()

    cur, _ = dialog(Role.DEVELOPER, current="mio")
    assert cur.btn_main.isEnabled()
    pick(cur, "mio")
    assert not cur.btn_switch.isEnabled() and "●" in cur.table.item(cur.table.currentRow(), 0).text()

    viewer, _ = dialog(Role.VIEWER, username="viewer")
    assert not viewer.btn_new.isEnabled() and "Viewer" in viewer.btn_new.toolTip()
    for slug in ("mio", "sq", "chiuso"):
        pick(viewer, slug)
        assert viewer.btn_switch.isEnabled() or slug == "mio"
        assert not (viewer.btn_sync.isEnabled() or viewer.btn_merge.isEnabled() or viewer.btn_close_branch.isEnabled())
    assert "Viewer" in viewer.lbl_sel.text()

    adm, _ = dialog(Role.ADMIN, manage_only=True, username="admin")
    assert adm.btn_switch.isHidden() and adm.btn_sync.isHidden() and adm.btn_merge.isHidden() and adm.btn_main.isHidden()
    assert not adm.btn_new.isHidden() and not adm.btn_delete.isHidden()

    # sei gia' al limite di branch personali aperti: un Developer non puo' crearne altri, un Lead si' (team)
    full = [bo(f"b{i}", bid=i) for i in range(MAX_PERSONAL_BRANCHES)]
    srv2 = FakeServer(branches=full)
    d2 = BranchesDialog(make_api(srv2), proj, Role.DEVELOPER, None, sync)
    assert not d2.btn_new.isEnabled() and str(MAX_PERSONAL_BRANCHES) in d2.btn_new.toolTip()
    d3 = BranchesDialog(make_api(FakeServer(username="lead", role="Lead_Engineer", branches=full)), proj, Role.LEAD, None, sync)
    assert d3.btn_new.isEnabled()


def test_branches_dialog_actions_call_the_server(qapp, monkeypatch):
    srv = FakeServer(branches=[bo("mio", behind=2), bo("vecchio", bid=2)])
    api = make_api(srv)
    api.set_project(1, "mio")                                                              # contesto: dentro un branch
    proj = ProjectOut(id=1, slug="l", name="Linea 1", head_revision=3, my_role="Developer")
    dlg = BranchesDialog(api, proj, Role.DEVELOPER, "mio", sync)
    seen = {"switch": [], "sync": [], "merge": [], "changed": 0}
    dlg.switch_requested.connect(seen["switch"].append)
    dlg.sync_requested.connect(seen["sync"].append)
    dlg.merge_requested.connect(seen["merge"].append)
    dlg.changed.connect(lambda: seen.__setitem__("changed", seen["changed"] + 1))
    created_slugs = []
    dlg.created_branch.connect(created_slugs.append)

    # nuovo branch: dialogo sostituito da un hook; l'elenco delle revisioni arriva dal PRINCIPALE (mai ?branch=)
    asked = []
    dlg.ask_new = lambda revs, mine: (asked.append((revs, mine)), ("Nuova Logica", "personal", 2))[1]
    dlg.new_branch()
    assert asked == [([3, 2, 1], 2)]                                                          # 2 = i miei personali aperti
    assert srv.last("GET", "/master/history")[2] == {}
    created = json.loads(srv.last("POST", "/branches")[3])
    assert created == {"name": "Nuova Logica", "kind": "personal", "from_revision": 2}
    assert dlg.created.slug == "nuova-logica" and seen["changed"] == 1
    assert created_slugs == ["nuova-logica"]                                                  # la finestra principale ci passa e scarica la copia
    assert any(b.slug == "nuova-logica" for b in dlg._rows)
    dlg.ask_new = lambda revs, mine: None                                                  # annullato: nessuna chiamata
    n = len(srv.calls)
    dlg.new_branch()
    assert not [c for c in srv.calls[n:] if c[0] == "POST"]

    def select(slug):
        dlg.table.selectRow(next(i for i, b in enumerate(dlg._rows) if b.slug == slug))

    # chiudi: serve conferma
    select("vecchio")
    dlg.confirm = lambda title, text: False
    dlg.btn_close_branch.click()
    assert not srv.paths("POST", "/close")
    dlg.confirm = lambda title, text: True
    dlg.btn_close_branch.click()
    assert srv.paths("POST", "/close") and srv.branches["vecchio"].state == "closed"
    # elimina: serve conferma (e il testo la avvisa)
    texts = []
    dlg.confirm = lambda title, text: (texts.append(text), False)[1]
    dlg.btn_delete.click()
    assert texts and "vecchio" in texts[0] and "vecchio" in srv.branches
    dlg.confirm = lambda title, text: True
    dlg.btn_delete.click()
    assert "vecchio" not in srv.branches and seen["changed"] == 3

    # passa / aggiorna / unisci: il dialogo si chiude e lascia fare alla finestra principale
    select("nuova-logica")
    dlg.btn_switch.click()
    assert seen["switch"] == ["nuova-logica"] and dlg.result() == QDialog.Accepted
    select("mio")
    dlg.btn_sync.click()
    dlg.btn_merge.click()
    assert seen["sync"] == ["mio"] and seen["merge"] == ["mio"]
    dlg.btn_main.click()
    assert seen["switch"][-1] == ""


def test_new_branch_dialog_validation_and_payload(qapp):
    d = NewBranchDialog(Role.DEVELOPER, [1, 2, 3], personal_open=0)
    assert not d.kind.model().item(1).isEnabled()                                          # team: solo Lead/Admin
    assert d.rev.count() == 3 and d.rev.itemData(0) == 3 and not d.rev.isEnabled()
    d._accept()
    assert "nome" in d.error.text().lower() and d.result() != QDialog.Accepted
    d.name.setText("  prova ")
    assert d.payload() == ("prova", "personal", None)
    d.rb_rev.setChecked(True)
    d.rev.setCurrentIndex(d.rev.findData(2))
    assert d.payload() == ("prova", "personal", 2) and d.rev.isEnabled()
    d._accept()
    assert d.result() == QDialog.Accepted
    lead = NewBranchDialog(Role.LEAD, [], personal_open=MAX_PERSONAL_BRANCHES)
    assert lead.kind.model().item(1).isEnabled() and not lead.rb_rev.isEnabled()
    lead.name.setText("x")
    lead._accept()                                                                         # personale ma al limite
    assert str(MAX_PERSONAL_BRANCHES) in lead.error.text() and lead.result() != QDialog.Accepted
    lead.kind.setCurrentIndex(1)
    lead._accept()
    assert lead.payload() == ("x", "team", None) and lead.result() == QDialog.Accepted


# ======================================================================================= pulsanti della dashboard per ruolo/tipo
def test_dashboard_buttons_follow_branch_kind_role_and_state(qapp, tmp_path, monkeypatch):
    srv = FakeServer(branches=[bo("mio"), bo("sq", "team", owner="lead", bid=2), bo("altrui", owner="dev2", can_write=False, bid=3),
                               bo("chiuso", state="closed", bid=4)])
    win = make_window(tmp_path, monkeypatch, srv)
    d = win.dashboard
    assert not d.btn_merge.isHidden() and d.btn_merge.isEnabled() and d.btn_commit.isHidden()
    assert not d.btn_branches.isHidden() and "Master" in d.btn_checkout.text()
    win.select_branch("mio")                                                               # personale: salvataggio diretto
    assert d.btn_merge.isHidden() and not d.btn_commit.isHidden() and d.btn_commit.isEnabled()
    assert d.btn_branch_sync.isEnabled() and d.btn_branch_merge.isEnabled() and "branch" in d.btn_checkout.text()
    assert d.btn_import.isHidden() and d.btn_reimport.isHidden()                           # il file base e' del principale
    win.select_branch("sq")                                                                # team: normali richieste di merge
    assert not d.btn_merge.isHidden() and d.btn_merge.isEnabled() and d.btn_commit.isEnabled()
    win.select_branch("altrui")                                                            # personale altrui: sola lettura
    assert not d.btn_commit.isHidden() and not d.btn_commit.isEnabled()
    assert not d.btn_branch_sync.isEnabled() and not d.btn_branch_merge.isEnabled()
    assert "sola lettura" in d.lbl_branch.text()
    win.select_branch("chiuso")
    assert not d.btn_commit.isEnabled() and not d.btn_branch_merge.isEnabled() and "chiuso: sola lettura" in d.lbl_branch.text()
    win.select_branch(None)
    assert d.btn_merge.isEnabled() and d.btn_commit.isHidden() and d.branch_bar.isHidden()
    win.close()

    viewer = FakeServer(username="viewer", role="Viewer", branches=[bo("mio", owner="dev1", can_write=False, can_delete=False)])
    vw = make_window(tmp_path, monkeypatch, viewer)
    vw.select_branch("mio")
    vd = vw.dashboard
    assert vd.btn_merge.isHidden() and vd.btn_commit.isHidden() and vd.btn_branch_sync.isHidden() and vd.btn_branch_merge.isHidden()
    assert "Scarica" in vd.btn_checkout.text()
    vw.commit_to_branch(str(tmp_path / "x.L5X"), "msg")                                    # a mano: nessun effetto
    vw.sync_branch()
    vw.merge_branch(message="x")
    assert not viewer.paths("POST", "/commit") and not viewer.paths("POST", "/sync") and not viewer.paths("POST", "/merge")
    vw.close()


# ======================================================================================= salvataggio, sync, unione
def test_commit_to_personal_branch_saves_and_tracks_new_base(qapp, tmp_path, monkeypatch):
    srv = FakeServer(branches=[bo("mio", base=2, head=2), bo("sq", "team", owner="lead", bid=2)])
    msgs = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: msgs.append(a[2]))
    win = make_window(tmp_path, monkeypatch, srv)
    win.select_branch("mio")
    ws = win.local_folder
    assert ws == tmp_path / "proj" / BRANCHES_DIR / "mio"
    ws.mkdir(parents=True)
    f = ws / "mie_modifiche.L5X"
    f.write_text("<RSLogix5000Content>mie</RSLogix5000Content>", "utf-8")

    win.commit_to_branch(str(f), "   ")                                                   # messaggio obbligatorio
    assert msgs and not srv.paths("POST", "/commit")
    win.commit_to_branch(str(f), "aggiunta logica nastro")
    post = srv.last("POST", "/commit")
    assert post[1] == "/api/projects/1/branches/mio/commit" and post[2] == {}                 # gli endpoint /branches non portano ?branch=
    assert b"aggiunta logica nastro" in post[3] and b"mie</RSLogix5000Content>" in post[3] and b"mie_modifiche.L5X" in post[3]
    assert srv.branches["mio"].head_revision == 3
    assert win.base_revision == 3 and win.working_file == str(f)                           # il file salvato e' la nuova copia di lavoro
    assert ref_path(ws, "Master", 3, ".L5X").exists()                                      # copia pulita nel .aps del BRANCH
    assert not (tmp_path / "proj" / ".aps").exists()                                       # il principale non e' toccato
    assert "Salvato nel branch" in win.dashboard.log.toPlainText() and "rev base <b>3</b>" in win.dashboard.lbl_local.text()
    assert "Branch «mio» rev 3" in win.dashboard.lbl_master.text()

    # dal dialogo di scelta del file (hook su exec)
    seen = {}

    def fake_exec(self):
        seen["title"], seen["ok"], seen["folder"] = self.windowTitle(), self.ok_btn.text(), self.folder
        seen["names"] = [c.name for c in self.candidates()]
        self.select_path(f)
        self.message.setText("da dialogo")
        return QDialog.Accepted
    monkeypatch.setattr(MergeRequestDialog, "exec", fake_exec)
    win.commit_to_branch()
    assert seen["title"] == "Salva su branch" and seen["ok"] == "Salva nel branch" and seen["folder"] == ws
    assert "mie_modifiche.L5X" in seen["names"] and srv.branches["mio"].head_revision == 4
    # annullando non succede nulla
    monkeypatch.setattr(MergeRequestDialog, "exec", lambda self: QDialog.Rejected)
    win.commit_to_branch()
    assert srv.branches["mio"].head_revision == 4

    # nel branch team il salvataggio passa dalle richieste di merge: nessun commit diretto
    win.select_branch("sq")
    win.commit_to_branch(str(f), "no")
    assert srv.branches["sq"].head_revision == 2
    win.close()


def test_sync_stays_in_branch_and_merge_goes_to_main(qapp, tmp_path, monkeypatch):
    srv = FakeServer(branches=[bo("mio", behind=2), bo("pari", behind=0, bid=2)])
    win = make_window(tmp_path, monkeypatch, srv)

    win.select_branch("mio")
    win.dashboard.btn_branch_sync.click()                                                  # banner: «Aggiorna dal principale»
    assert srv.last("POST", "/sync")[1].endswith("/branches/mio/sync")
    assert win.api.branch == "mio" and win.opened == [("p-sync", "mio")]                   # resta nel branch, apre la proposta
    assert "Aggiornamento dal principale" in win.dashboard.log.toPlainText()

    win.opened.clear()
    ws = win.local_folder
    ws.mkdir(parents=True, exist_ok=True)
    f = ws / "Master_rev3.L5X"
    f.write_bytes(b"identico alla testa")
    srv.head_sha = hashlib.sha256(b"identico alla testa").hexdigest()                      # il file scelto = testa del branch: niente commit
    seen = {}

    def fake_exec(self):
        seen["title"], seen["ok"] = self.windowTitle(), self.ok_btn.text()
        self.select_path(f)
        self.message.setText("unisco tutto")
        return QDialog.Accepted
    monkeypatch.setattr(MergeRequestDialog, "exec", fake_exec)
    win.dashboard.btn_branch_merge.click()                                                 # banner: «Unisci nel principale...»
    post = srv.last("POST", "/merge")
    assert seen == {"title": "Unisci nel principale", "ok": "Unisci nel principale"}       # la stessa finestra di «Richiedi Merge»
    assert not srv.paths("POST", "/commit")
    assert post[1].endswith("/branches/mio/merge") and b"message=unisco+tutto" in post[3]
    assert win.api.branch is None and win.opened == [("p-merge", None)]                     # passa al principale e apre la proposta
    assert win.ws.urls[-1].endswith("&project=1") and win.dashboard.branch_bar.isHidden()

    # chiamate da altri branch (dal dialogo): sync su un branch in cui non si e' porta li' dentro
    win.opened.clear()
    win.sync_branch("mio")
    assert win.api.branch == "mio" and win.opened == [("p-sync", "mio")]
    # niente da portare: nessuna richiesta
    n = len(srv.paths("POST", "/sync"))
    win.sync_branch("pari")
    assert len(srv.paths("POST", "/sync")) == n
    # annullare la domanda sul messaggio non invia nulla
    monkeypatch.setattr(MergeRequestDialog, "exec", lambda self: QDialog.Rejected)
    k = len(srv.paths("POST", "/merge"))
    win.merge_branch("mio")
    assert len(srv.paths("POST", "/merge")) == k
    win.merge_branch("mio", message="")                                                    # messaggio vuoto ammesso
    assert len(srv.paths("POST", "/merge")) == k + 1
    # errore del server: avviso, nessun cambio di contesto
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    win.select_branch("mio")
    srv.handler_orig = srv.handler
    real = srv.handler
    srv.handler = lambda req: (httpx.Response(409, json={"detail": "conflitto di stato"}) if req.url.path.endswith("/merge") else real(req))
    win.api._http = httpx.Client(base_url="http://fake", transport=httpx.MockTransport(srv.handler))
    win.merge_branch("mio", message="x")
    assert warned == ["conflitto di stato"] and win.api.branch == "mio"
    win.close()


def test_branches_window_dialog_wiring(qapp, tmp_path, monkeypatch):
    srv = FakeServer(branches=[bo("mio", behind=2)])
    win = make_window(tmp_path, monkeypatch, srv)
    shown = {}

    def fake_exec(self):
        shown["role"], shown["cur"], shown["rows"] = self.role, self.current, [b.slug for b in self._rows]
        self.table.selectRow(0)
        self.btn_switch.click()                                                            # l'utente sceglie «Passa a questo branch»
        return QDialog.Accepted
    monkeypatch.setattr(BranchesDialog, "exec", fake_exec)
    win.dashboard.btn_branches.click()
    assert shown == {"role": Role.DEVELOPER, "cur": None, "rows": ["mio"]} and win.api.branch == "mio"
    assert win.branches_dialog is not None
    win.close()


def test_new_branch_creates_local_folder_with_export(qapp, tmp_path, monkeypatch):
    """Regressione: dopo «Nuovo branch» la cartella locale <progetto>/branches/<slug>/ esiste subito, con l'export della
    revisione di partenza e la copia pulita in .aps."""
    srv = FakeServer(branches=[])
    win = make_window(tmp_path, monkeypatch, srv)
    base = tmp_path / "proj"

    def fake_exec(self):
        self.ask_new = lambda revs, mine: ("Prova", "personal", None)
        self.new_branch()
        return QDialog.Accepted
    monkeypatch.setattr(BranchesDialog, "exec", fake_exec)
    win.show_branches()
    ws = base / "branches" / "prova"
    assert win.api.branch == "prova" and win.local_folder == ws
    assert ws.is_dir() and list(ws.glob("Master_rev*.L5X")) and list((ws / ".aps").glob("Master_rev*.L5X"))
    assert win.working_file and Path(win.working_file).parent == ws
    win.close()


def test_merge_into_main_saves_modified_file_first(qapp, tmp_path, monkeypatch):
    """Il file scelto nella finestra e' diverso dalla testa: nel branch personale viene prima salvato, poi si unisce;
    nel branch team non si salva (si usa «Richiedi Merge») e non parte nessuna unione."""
    srv = FakeServer(branches=[bo("mio", head=2), bo("sq", "team", owner="lead", bid=2)])
    win = make_window(tmp_path, monkeypatch, srv)
    win.select_branch("mio")
    ws = win.local_folder
    ws.mkdir(parents=True, exist_ok=True)
    f = ws / "mie_modifiche.L5X"
    f.write_text("<RSLogix5000Content>nuove</RSLogix5000Content>", "utf-8")
    monkeypatch.setattr(MergeRequestDialog, "exec", lambda self: (self.select_path(f), self.message.setText("pronto"), QDialog.Accepted)[2])
    win.merge_branch("mio")
    assert [c[1] for c in srv.calls if c[0] == "POST" and c[1].endswith(("/commit", "/merge"))][-2:] == \
        ["/api/projects/1/branches/mio/commit", "/api/projects/1/branches/mio/merge"]
    assert b"nuove</RSLogix5000Content>" in srv.last("POST", "/commit")[3] and win.opened[-1] == ("p-merge", None)
    assert srv.branches["mio"].head_revision == 3

    n = len(srv.paths("POST", "/merge"))
    sq = win.project_folder / BRANCHES_DIR / "sq"
    sq.mkdir(parents=True, exist_ok=True)
    g = sq / "mie_modifiche.L5X"
    g.write_text("<RSLogix5000Content>team</RSLogix5000Content>", "utf-8")
    monkeypatch.setattr(MergeRequestDialog, "exec", lambda self: (self.select_path(g), self.message.setText("x"), QDialog.Accepted)[2])
    win.opened.clear()
    win.merge_branch("sq")                                                                  # branch team: il file va dentro la richiesta di unione
    assert len(srv.paths("POST", "/merge")) == n + 1 and not srv.paths("POST", "/commit")[1:]
    post = srv.last("POST", "/merge")
    assert post[1] == "/api/projects/1/branches/sq/merge" and b"team</RSLogix5000Content>" in post[3] and b"message" in post[3]
    assert win.api.branch is None and win.opened[-1] == ("p-merge", None)                   # richiesta nel PRINCIPALE
    win.close()


def test_switching_to_branch_downloads_its_head_when_folder_is_empty(qapp, tmp_path, monkeypatch):
    """Regressione: un altro utente passa a un branch: la cartella locale non resta vuota, contiene l'ultimo export."""
    srv = FakeServer(branches=[bo("mio", head=4, can_write=False, owner="altro")])
    win = make_window(tmp_path, monkeypatch, srv)
    win.sidebar.branch_changed.emit("mio")                                               # selettore della barra laterale
    ws = tmp_path / "proj" / "branches" / "mio"
    assert win.api.branch == "mio" and win.base_revision == 4
    assert (ws / "Master_rev4.L5X").exists() and (ws / ".aps" / "Master_rev4.L5X").exists()
    assert b"branch=mio" in (ws / "Master_rev4.L5X").read_bytes()
    n = len(srv.paths("GET", "/master/download"))
    win.switch_branch(None)
    win.switch_branch("mio")                                                              # ha gia' una copia: nessun nuovo scarico
    assert len(srv.paths("GET", "/master/download")) == n
    win.close()


# ======================================================================================= console Admin
def test_admin_console_branch_button_and_main_only_history(qapp, tmp_path, monkeypatch):
    srv = FakeServer(username="admin", role="Admin", branches=[bo("mio")])
    api = make_api(srv, "admin")
    api.set_project(1, "mio")                                                              # la dashboard e' dentro un branch...
    view = UserMgmtView(api, run=sync)
    view.tabs.setCurrentIndex(1)
    view.reload()
    assert view.btn_branches.isEnabled()
    opened = {}
    monkeypatch.setattr(BranchesDialog, "exec", lambda self: opened.update(only=self.manage_only, rows=len(self._rows),
                                                                           pid=self.project.id) or QDialog.Accepted)
    view.btn_branches.click()
    assert opened == {"only": True, "rows": 1, "pid": 1} and view.branches_dialog is not None
    # ...ma lo storico/rollback della console parlano sempre del principale
    from client.views.history_dialog import HistoryDialog
    monkeypatch.setattr(HistoryDialog, "exec", lambda self: QDialog.Accepted)
    view.show_history()
    assert srv.last("GET", "/master/history")[2] == {}


# ======================================================================================= cartelle locali per branch
def test_local_folders_per_branch_and_prune(qapp, tmp_path, monkeypatch):
    base = tmp_path / "progetto"
    assert branch_folder(base, None) == base and branch_folder(base, "") == base
    assert branch_folder(base, "mio") == base / "branches" / "mio"
    assert branch_folder(base, "../fuori") == base / "branches" / "fuori"                  # mai fuori dalla cartella dei branch
    assert branch_folder(base, "a/b\\c").parent == base / "branches"

    # il principale non elenca come candidati i file dei branch
    (base / "branches" / "mio").mkdir(parents=True)
    (base / "principale.L5X").write_text("<RSLogix5000Content/>", "utf-8")
    (base / "branches" / "mio" / "nel_branch.L5X").write_text("<RSLogix5000Content/>", "utf-8")
    main_names = [c.name for c in find_merge_candidates(base)]
    assert main_names == ["principale.L5X"]
    assert [c.name for c in find_merge_candidates(base / "branches" / "mio")] == ["nel_branch.L5X"]

    srv = FakeServer(branches=[bo("mio", head=5), bo("sq", "team", owner="lead", head=2, bid=2)])
    win = make_window(tmp_path, monkeypatch, srv)
    win.settings.setValue(win._folder_key(), str(base))
    assert win.local_folder == base and win.project_folder == base
    k_main = win._base_key()
    win.checkout()                                                                          # checkout sul principale: layout invariato
    assert win.base_revision == 3 and (base / "Master_rev3.L5X").exists() and (base / ".aps" / "Master_rev3.L5X").exists()
    assert win.working_file == str(base / "Master_rev3.L5X")

    win.select_branch("mio")
    ws = base / "branches" / "mio"
    assert win.local_folder == ws and win.project_folder == base and win._base_key() != k_main
    assert win.base_revision == 0 and win.working_file == ""                                # ogni branch ha la sua copia di lavoro
    assert str(ws) in win.dashboard.lbl_folder.text()
    win.checkout()
    assert win.base_revision == 5 and (ws / "Master_rev5.L5X").exists() and (ws / ".aps" / "Master_rev5.L5X").exists()
    assert b"branch=mio" in (ws / "Master_rev5.L5X").read_bytes()                           # scaricato dal branch
    assert win.working_file == str(ws / "Master_rev5.L5X")
    win.select_branch("sq")
    assert win.base_revision == 0 and win.local_folder == base / "branches" / "sq"
    win.select_branch(None)
    assert win.base_revision == 3 and win.working_file == str(base / "Master_rev3.L5X")     # il principale ricorda la sua
    # «Cambia cartella» sposta la cartella del progetto (e con lei quelle dei branch)
    newbase = tmp_path / "altra"
    win.pick_folder = lambda *_a: str(newbase)
    win.select_branch("mio")
    assert win.choose_folder() and win.local_folder == newbase / "branches" / "mio"

    # pulizia locale: per branch, senza toccare gli altri
    for folder in (base, base / "branches" / "mio"):
        for rev in range(1, 6):
            data = f"<RSLogix5000Content rev={rev}/>".encode()
            (folder / ".aps").mkdir(exist_ok=True)
            (folder / f"Master_rev{rev}.L5X").write_bytes(data)
            (folder / ".aps" / f"Master_rev{rev}.L5X").write_bytes(data)
    removed = prune_local(base / "branches" / "mio", "Master", ".L5X", 2, [])
    assert {p.name for p in removed} == {f"Master_rev{r}.L5X" for r in (1, 2, 3)}
    left = sorted(p.name for p in (base / "branches" / "mio").glob("Master_rev*.L5X"))
    assert left == ["Master_rev4.L5X", "Master_rev5.L5X"]
    assert sorted(p.name for p in (base / "branches" / "mio" / ".aps").glob("*")) == left
    assert len(list(base.glob("Master_rev*.L5X"))) == 5                                     # il principale e' intatto
    # ...e dalla finestra: _prune_local lavora sulla cartella del contesto corrente (max_revisions = 2)
    win.settings.setValue(win._folder_key(), str(base))
    win.select_branch(None)
    win.select_branch("mio")
    for rev in range(1, 6):
        (base / "branches" / "mio" / f"Master_rev{rev}.L5X").write_bytes(f"<RSLogix5000Content rev={rev}/>".encode())
        (base / "branches" / "mio" / ".aps" / f"Master_rev{rev}.L5X").write_bytes(f"<RSLogix5000Content rev={rev}/>".encode())
    win._prune_local()
    assert sorted(p.name for p in (base / "branches" / "mio").glob("Master_rev*.L5X")) == ["Master_rev4.L5X", "Master_rev5.L5X"]
    assert len(list(base.glob("Master_rev*.L5X"))) == 5
    win.close()


# ======================================================================================= end-to-end con il server vivo
import importlib.util                                                                    # noqa: E402

from tests.test_client import _live_server, client_for, pump                             # noqa: E402

HAS_BRANCH_SERVER = importlib.util.find_spec("server.branches") is not None
needs_server = pytest.mark.skipif(not HAS_BRANCH_SERVER, reason="il server dei branch non e' disponibile")
TEXT = "\n".join(f"riga {i}" for i in range(10))


@pytest.fixture(scope="module")
def blive(tmp_path_factory):
    """Server dedicato con un Master di testo (ogni reimport crea una revisione senza giri di consenso)."""
    yield from _live_server(tmp_path_factory, "Imp.txt", TEXT)


def _lwin(live, tmp_path, user, name=None):
    win = _window(live, tmp_path, name or user, heartbeat_ms=600_000)
    win._do_login(live["url"], user, ADMIN_PW if user == "admin" else PW)
    assert win.stack.currentWidget() is win.workspace_widget
    win.notify_user = lambda *_a: None
    win.opened = []
    win.open_proposal = lambda pid: win.opened.append((pid, win.api.branch))
    return win


def _lapi(live, user, branch=None):
    api = client_for(live, user, ADMIN_PW if user == "admin" else PW, heartbeat=False)
    api.set_project(live["pid"], branch)
    return api


def _bump_main(live, tmp_path, line, text):
    f = tmp_path / "Imp.txt"
    f.write_text(TEXT.replace(line, text), "utf-8")
    return _lapi(live, "admin").reimport_master_for(live["pid"], f, "aggiornamento del principale")


@needs_server
def test_live_personal_branch_full_cycle(blive, qapp, tmp_path, monkeypatch):
    live = blive
    adm = _lapi(live, "admin")
    win = _lwin(live, tmp_path, "dev1")
    assert pump(qapp, lambda: win.ws.is_up) and win.sidebar.branch.count() == 1

    # --- nuovo branch personale dal dialogo; il selettore lo mostra
    made = {}

    def fake_exec(self):
        self.ask_new = lambda revs, mine: ("Prova live", "personal", None)
        self.new_branch()
        made["b"] = self.created
        return QDialog.Accepted
    monkeypatch.setattr(BranchesDialog, "exec", fake_exec)
    win.show_branches()
    b = made["b"]
    assert b.slug == "prova-live" and b.kind == "personal" and b.owner == "dev1" and b.can_write and b.head_revision == 1
    assert [x.slug for x in win.branches] == ["prova-live"] and win.sidebar.branch.findData("prova-live") > 0

    # --- passo al branch: api.branch, WS risottoscritto, dati del branch
    win.sidebar.branch.setCurrentIndex(win.sidebar.branch.findData("prova-live"))
    win.sidebar.branch.activated.emit(win.sidebar.branch.currentIndex())
    assert win.api.branch == "prova-live" and win.ws.is_up is not None
    assert pump(qapp, lambda: win.ws.is_up) and "&branch=prova-live" in win.api.ws_url()
    assert "Branch «Prova live» rev 1" in win.dashboard.lbl_master.text() and not win.dashboard.branch_bar.isHidden()
    assert not win.dashboard.btn_commit.isHidden() and win.dashboard.btn_merge.isHidden()

    # --- checkout nella cartella del branch, modifica, «Salva su branch»
    win.checkout()
    ws = win.local_folder
    assert ws.parent.name == BRANCHES_DIR and ws.name == "prova-live" and (ws / ".aps").is_dir()
    assert win.base_revision == 1 and Path(win.working_file).parent == ws
    f = Path(win.working_file)
    f.write_text(TEXT.replace("riga 3", "riga 3 DEL BRANCH"), "utf-8")
    win.commit_to_branch(str(f), "modifica nel branch")
    assert win.base_revision == 2 and win.working_file == str(f)
    assert "Branch «Prova live» rev 2" in win.dashboard.lbl_master.text()
    assert _lapi(live, "admin", "prova-live").master_info().revision == 2
    assert adm.master_info().revision == 1                                             # il principale non e' toccato
    assert next(x for x in adm.branches() if x.slug == "prova-live").ahead == 1

    # --- il principale avanza: il badge «indietro» si aggiorna da solo (evento main_updated sul WS del branch)
    _bump_main(live, tmp_path, "riga 8", "riga 8 DAL PRINCIPALE")
    assert pump(qapp, lambda: win.branch is not None and win.branch.behind == 1), "badge «indietro» non aggiornato"
    assert "1 indietro" in win.sidebar.lbl_branch.text() and "1 indietro" in win.dashboard.lbl_branch.text()
    assert win.dashboard.btn_branch_sync.isEnabled()

    # --- «Aggiorna dal principale»: si resta nel branch e si apre la proposta di sync
    win.sync_branch()
    assert win.opened and win.opened[-1][1] == "prova-live" and win.api.branch == "prova-live"
    assert next(x for x in adm.branches() if x.slug == "prova-live").behind == 0
    assert pump(qapp, lambda: win.branch is not None and win.branch.behind == 0)
    assert not win.dashboard.btn_branch_sync.isEnabled()
    sync_prop = _lapi(live, "dev1", "prova-live").proposal(win.opened[-1][0])
    assert sync_prop.state == ProposalState.MERGED

    # --- «Unisci nel principale»: si passa al principale e si apre la proposta
    win.merge_branch(message="porto in produzione la logica del nastro")
    assert win.api.branch is None and win.opened[-1][1] is None and win.dashboard.branch_bar.isHidden()
    prop_id = win.opened[-1][0]
    d = adm.proposal(prop_id)
    assert d.state in (ProposalState.AWAITING_CONSENT, ProposalState.MERGED) and d.author == "dev1"
    if d.state != ProposalState.MERGED:
        adm.force_merge(prop_id, "chiusura del test end-to-end")
    assert next(x for x in adm.branches() if x.slug == "prova-live").state == "merged"
    out = tmp_path / "main_dopo_unione.txt"
    adm.download_master(out)                                                           # il principale contiene entrambe le modifiche
    got = out.read_text("utf-8")
    assert "riga 3 DEL BRANCH" in got and "riga 8 DAL PRINCIPALE" in got, got
    win.load_branches()
    assert next(x for x in win.branches if x.slug == "prova-live").state == "merged"
    assert win.sidebar.branch.count() == 1                                              # unito: non piu' nel selettore
    win.ws.stop()
    win.close()


@needs_server
def test_live_viewer_is_read_only_and_team_branch_uses_normal_requests(blive, qapp, tmp_path, monkeypatch):
    live = blive
    lead = _lapi(live, "lead")
    team = lead.create_branch("Squadra live", "team")
    assert team.kind == "team" and team.owner == "lead"

    # Viewer: vede, passa al branch, ma non scrive
    v = _lwin(live, tmp_path, "viewer")
    assert [b.slug for b in v.branches if b.kind == "team"] == ["squadra-live"]
    seen = {}

    def fake_exec(self):
        seen["new"] = self.btn_new.isEnabled()
        self.table.selectRow(next(i for i, r in enumerate(self._rows) if r.slug == "squadra-live"))
        seen["btns"] = {n: getattr(self, n).isEnabled() for n in
                        ("btn_switch", "btn_sync", "btn_merge", "btn_close_branch", "btn_delete")}
        return QDialog.Rejected
    monkeypatch.setattr(BranchesDialog, "exec", fake_exec)
    v.show_branches()
    assert seen["new"] is False and seen["btns"] == {"btn_switch": True, "btn_sync": False, "btn_merge": False,
                                                     "btn_close_branch": False, "btn_delete": False}
    v.select_branch("squadra-live")
    vd = v.dashboard
    assert vd.btn_merge.isHidden() and vd.btn_commit.isHidden() and vd.btn_branch_merge.isHidden()
    v.sync_branch()
    v.merge_branch(message="x")
    v.ws.stop()
    v.close()

    # Developer nel branch team: normali richieste di merge, isolate dal principale
    win = _lwin(live, tmp_path, "dev2")
    win.select_branch("squadra-live")
    d = win.dashboard
    assert not d.btn_merge.isHidden() and d.btn_merge.isEnabled() and not d.btn_commit.isHidden()
    win.checkout()
    assert win.local_folder.parent.name == BRANCHES_DIR and win.base_revision >= 1
    f = Path(win.working_file)
    f.write_text(TEXT.replace("riga 5", "riga 5 TEAM"), "utf-8")
    win.submit_merge(f, "modifica di squadra")
    in_branch = _lapi(live, "dev2", "squadra-live").proposals(active_only=True)
    on_main = _lapi(live, "dev2").proposals(active_only=True)
    assert [p.message for p in in_branch] == ["modifica di squadra"] and not [p for p in on_main if p.message == "modifica di squadra"]
    assert win.opened and win.opened[-1] == (in_branch[0].id, "squadra-live")
    assert [p.id for p in win.dashboard.visible_proposals()] == [in_branch[0].id]
    win.select_branch(None)                                                            # sul principale non c'e'
    assert not [p for p in win.dashboard.visible_proposals() if p.message == "modifica di squadra"]

    # --- «Salva su branch» anche nel team: unione 3-way con quanto gia' nel branch, senza votazione
    warned_live = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned_live.append(a[2]))
    win.select_branch("squadra-live")
    win.commit_to_branch(str(f), "salvo nel branch di squadra")
    assert not warned_live, warned_live
    team_api = _lapi(live, "dev2", "squadra-live")
    head = team_api.master_info().revision
    assert head >= 2
    out = tmp_path / "head_squadra.txt"
    team_api.download_master(out)
    assert "riga 5 TEAM" in out.read_text("utf-8")                                      # un collega che fa il Checkout la trova
    assert next(x for x in team_api.branches() if x.slug == "squadra-live").head_revision == head
    assert win.base_revision == head                                                   # nessun altro aveva toccato: il file e' la nuova testa
    win.ws.stop()
    win.close()


@needs_server
def test_live_branch_deleted_by_someone_else_returns_to_main(blive, qapp, tmp_path):
    live = blive
    dev = _lapi(live, "dev1")
    b = dev.create_branch("Effimero live", "personal")
    win = _lwin(live, tmp_path, "dev1", "dev1b")
    win.select_branch(b.slug)
    assert win.api.branch == b.slug and pump(qapp, lambda: win.ws.is_up)
    _lapi(live, "admin").delete_branch(b.slug)                                         # lo elimina l'Admin
    assert pump(qapp, lambda: win.api.branch is None), "non e' tornato al principale"
    assert win.dashboard.branch_bar.isHidden() and win.sidebar.branch.currentData() == ""
    assert pump(qapp, lambda: win.ws.is_up) and "branch" not in win.ws._factory()
    win.ws.stop()
    win.close()
