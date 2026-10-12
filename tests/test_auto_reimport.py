"""Reimport automatico dopo il merge delle proprie modifiche: server reale, GUI offscreen, runner sincrono."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from client.api_client import ApiError                                                  # noqa: E402
from client.auto_reimport import foreign_changes, manual_checkout_notice, merge_notice  # noqa: E402
from client.gui_main import MainWindow                                                  # noqa: E402
from common.roles import Role                                                           # noqa: E402
from common.schemas import ChangeOut, ProposalDetail, ProposalState                     # noqa: E402
from tests import sample_l5x as S                                                       # noqa: E402
from tests.test_client import (PW, _close_widgets, _live_server, _window, client_for, qapp,   # noqa: E402,F401
                               sync)


@pytest.fixture
def fresh(tmp_path_factory):
    """Server nuovo per ogni test: le revisioni ripartono da 1."""
    yield from _live_server(tmp_path_factory)


def _change(origin: str, title: str = "Rung#3", path: str = "Program[Main]/Routine[MainRoutine]/Rung#3") -> ChangeOut:
    return ChangeOut(path=path, title=title, label="Rung", kind="modified", origin=origin)


def _detail(upstream=(), result_revision=None) -> ProposalDetail:
    now = datetime.now(timezone.utc)
    return ProposalDetail(id="p1", author="A", author_session="s1", base_revision=1, head_revision=2,
                          state=ProposalState.MERGED, state_label="x", created_at=now, updated_at=now,
                          result_revision=result_revision, upstream_changes=list(upstream))


# ============================================================================ funzioni pure
def test_foreign_changes_counts_only_origin_master():
    assert foreign_changes(_detail()) == []
    assert foreign_changes(_detail([_change("both"), _change("both", "Tag#2")])) == []       # le avevo fatte anch'io
    assert foreign_changes(_detail([_change("proposal")])) == []
    mixed = _detail([_change("both"), _change("master", "Tag Enable"), _change("master", "Rung#3")])
    assert [c.title for c in foreign_changes(mixed)] == ["Tag Enable", "Rung#3"]


def test_notice_texts_list_at_most_five_units_and_name_the_file():
    ch = [_change("master", f"Unita {i}", f"P/{i}") for i in range(8)] + [_change("master", "Unita 0", "P/0")]
    text = merge_notice(7, ch, "Linea1_rev7.L5X", 7)
    assert "Master rev 7" in text and "9 modifiche" in text and "Linea1_rev7.L5X" in text and "rev 7" in text
    assert text.count("•") == 5 and "Unita 4" in text and "Unita 5" not in text and "altre 3" in text
    assert "invariato" in text
    assert "1 modifica " in merge_notice(2, ch[:1], "x_rev2.L5X", 2)
    kept = merge_notice(2, ch[:1], "x_rev2.L5X", 2, kept_existing=True)
    assert "NON è stato sovrascritto" in kept
    assert "Checkout" in manual_checkout_notice(2, ch, "boom") and "boom" in manual_checkout_notice(2, ch, "boom")


# ============================================================================ GUI + server reale
def _login(live, tmp_path, name, user, monkeypatch=None):
    win = _window(live, tmp_path, name, heartbeat_ms=600_000)
    win._do_login(live["url"], user, PW)
    win.ws.stop()                                    # gli eventi si pilotano a mano con on_event(): niente doppioni
    notes: list = []
    win.notify_user = lambda title, text: notes.append((title, text))
    return win, notes


def _merge_all(proposal_id, approvers, lead):
    """Tutti i partecipanti ancora 'pending' approvano, poi il Consenso Finale del Lead: ritorna il dettaglio."""
    d = lead.proposal(proposal_id)
    for api in approvers:
        sid = api.session_key
        if any(p.session_id == sid and p.decision == "pending" for p in d.participants):
            d = api.approve(proposal_id)
    if d.state != ProposalState.MERGED:
        d = lead.approve(proposal_id)
    assert d.state == ProposalState.MERGED, d.state
    return d


def _event(api, pid, message="Merge completato"):
    """Evento WS ``proposal_merged`` come lo manda il server (riepilogo della richiesta)."""
    summary = next(p for p in api.proposals() if p.id == pid)
    return {"type": "proposal_merged", "message": message, "proposal": summary.model_dump(mode="json")}


def test_my_merge_with_foreign_changes_reimports_new_master_and_notifies_once(fresh, qapp, tmp_path):
    live = fresh
    win, notes = _login(live, tmp_path, "A", "dev1")
    win.checkout()
    assert win.base_revision == 1
    old_file = Path(win.working_file)
    assert old_file.name == "master_rev1.L5X"
    lead = client_for(live, "lead", heartbeat=False)
    dev_b = client_for(live, "dev2")

    # B (stessa base rev 1) viene unito per primo
    fb = tmp_path / "b.L5X"
    fb.write_text(S.dev_b(False), "utf-8")
    pb = dev_b.submit_proposal(fb, 1, "modifiche di B")
    _merge_all(pb.id, [win.api, dev_b], lead)
    assert win.api.master_info().revision == 2

    # A modifica il suo file (basato sulla rev 1) e lo invia: il server trova le modifiche di B come 'master'
    old_file.write_text(S.dev_a(), "utf-8")
    pa = win.api.submit_proposal(old_file, win.base_revision, "modifiche di A")
    assert pa.n_upstream >= 1
    merged = _merge_all(pa.id, [win.api, dev_b], lead)
    assert merged.result_revision == 3
    n_foreign = len(foreign_changes(merged))
    assert n_foreign >= 1

    ev = _event(win.api, pa.id)
    win.on_event(ev)
    assert win.base_revision == 3
    new_file = Path(win.working_file)
    assert new_file.name == "master_rev3.L5X" and new_file.parent == old_file.parent and new_file.exists()
    assert "Enable" in new_file.read_text("utf-8") and "Fault" in new_file.read_text("utf-8")    # modifiche di B e di A
    assert (old_file.parent / ".aps" / "master_rev3.L5X").read_bytes() == new_file.read_bytes()
    assert old_file.read_text("utf-8") == S.dev_a()                      # il file precedente non e' stato toccato
    assert "master_rev3.L5X" in win.dashboard.lbl_local.text()          # etichetta locale della dashboard aggiornata

    assert len(notes) == 1
    title, text = notes[0]
    assert "Master rev 3" in text and f"{n_foreign} modific" in text and "master_rev3.L5X" in text
    assert "rev 3" in text and "invariato" in text
    assert "aggiornata automaticamente" in text

    win.on_event(ev)                                                     # evento ripetuto: nessuna doppia elaborazione
    win.on_event(_event(win.api, pa.id))
    assert len(notes) == 1 and win.base_revision == 3
    win.close()


def test_my_merge_without_upstream_changes_does_nothing_automatic(fresh, qapp, tmp_path):
    live = fresh
    win, notes = _login(live, tmp_path, "A", "dev1")
    win.checkout()
    work = Path(win.working_file)
    work.write_text(S.dev_a(), "utf-8")
    lead = client_for(live, "lead", heartbeat=False)
    pa = win.api.submit_proposal(work, win.base_revision, "solo io")
    assert pa.n_upstream == 0
    _merge_all(pa.id, [win.api], lead)
    assert win.api.master_info().revision == 2

    win.on_event(_event(win.api, pa.id))
    assert notes == []
    assert win.base_revision == 1 and Path(win.working_file) == work and work.read_text("utf-8") == S.dev_a()
    assert sorted(p.name for p in work.parent.glob("*.L5X")) == ["master_rev1.L5X"]        # nessun nuovo file
    win.close()


def test_merge_authored_by_someone_else_does_not_reimport(fresh, qapp, tmp_path):
    live = fresh
    win, notes = _login(live, tmp_path, "A", "dev1")
    win.checkout()
    before = Path(win.working_file).read_bytes()
    lead = client_for(live, "lead", heartbeat=False)
    dev_b = client_for(live, "dev2")
    fb = tmp_path / "b.L5X"
    fb.write_text(S.dev_b(False), "utf-8")
    pb = dev_b.submit_proposal(fb, 1, "modifiche di B")
    _merge_all(pb.id, [win.api, dev_b], lead)

    called = []
    orig = win.api.proposal
    win.api.proposal = lambda pid: (called.append(pid), orig(pid))[1]
    win.on_event(_event(dev_b, pb.id))
    assert notes == [] and called == []                                  # non e' un mio merge: nessuna richiesta, nessun avviso
    assert win.base_revision == 1 and Path(win.working_file).read_bytes() == before
    win.close()


def test_failed_automatic_download_shows_error_and_asks_manual_checkout(fresh, qapp, tmp_path, monkeypatch):
    live = fresh
    win, notes = _login(live, tmp_path, "A", "dev1")
    win.checkout()
    work = Path(win.working_file)
    lead = client_for(live, "lead", heartbeat=False)
    dev_b = client_for(live, "dev2")
    fb = tmp_path / "b.L5X"
    fb.write_text(S.dev_b(False), "utf-8")
    _merge_all(dev_b.submit_proposal(fb, 1, "B").id, [win.api, dev_b], lead)
    work.write_text(S.dev_a(), "utf-8")
    pa = win.api.submit_proposal(work, 1, "A")
    _merge_all(pa.id, [win.api, dev_b], lead)

    def boom(*_a, **_k):
        raise ApiError(503, "server non raggiungibile")
    monkeypatch.setattr(MainWindow, "_download_working_copy", staticmethod(boom))
    win.on_event(_event(win.api, pa.id))
    assert win.base_revision == 1 and Path(win.working_file) == work                   # nulla e' cambiato
    assert "server non raggiungibile" in notes[0][1]
    assert len(notes) == 1 and "Checkout" in notes[0][1] and "non è riuscito" in notes[0][1]
    win.close()


def test_viewer_never_reimports(fresh, qapp, tmp_path):
    live = fresh
    win, notes = _login(live, tmp_path, "V", "viewer")
    assert win.project_role == Role.VIEWER
    called = []
    win.api.proposal = lambda pid: called.append(pid)
    win.on_event({"type": "proposal_merged", "message": "x",
                  "proposal": {"id": "zz", "author_session": win.api.session_key, "result_revision": 2}})
    assert notes == [] and called == [] and win.base_revision == 0
    win.close()
