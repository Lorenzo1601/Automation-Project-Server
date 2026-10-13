"""Richieste di merge persistenti: sopravvivono al riavvio del server."""
from common.roles import Role
from common.schemas import ProposalState as St
from server.merger import Resolution
from server.session_manager import SessionManager
from server.storage import MasterStore
from tests import sample_l5x as S


def _mgr(tmp_path):
    store = MasterStore(tmp_path)
    if not store.initialized:
        store.commit(S.base_project().encode(), "admin", "init")
    return store, SessionManager(store, require_lead_approval=False)


def test_pending_request_survives_restart(tmp_path):
    store, m1 = _mgr(tmp_path)
    a, b = m1.join(1, "A", Role.DEVELOPER), m1.join(2, "B", Role.DEVELOPER)
    m1.touch(a), m1.touch(b)
    p = m1.submit(a, S.dev_a().encode(), 1, "Diagnostica")
    m1.approve(a, p.id)
    digest = p.result.digest

    store2, m2 = _mgr(tmp_path)                                   # "riavvio"
    assert m2.restore_pending() == [p.id]
    q = m2.get(p.id)
    assert q.state == St.AWAITING_CONSENT and q.message == "Diagnostica" and q.author == "A"
    assert q.result.digest == digest and set(q.required) == {"u1", "u2"}
    assert "u1" in q.approvals                                    # il consenso resta valido (stesso risultato)
    b2 = m2.join(2, "B", Role.DEVELOPER)
    m2.touch(b2)
    m2.approve(b2, p.id)                                          # il flusso prosegue dopo il riavvio
    assert m2.get(p.id).state == St.MERGED and store2.head().revision == 2


def test_closed_requests_not_restored_and_consent_reset_if_master_moved(tmp_path):
    store, m1 = _mgr(tmp_path)
    a = m1.join(1, "A", Role.DEVELOPER)
    p = m1.submit(a, S.dev_a().encode(), 1)
    m1.cancel(a, p.id)
    _, m2 = _mgr(tmp_path)
    assert m2.restore_pending() == []

    store, m3 = _mgr(tmp_path)
    a = m3.join(1, "A", Role.DEVELOPER)
    q = m3.submit(a, S.dev_a().encode(), 1)
    store.commit(S.dev_b().encode(), "x", "avanza")                 # il Master cambia mentre il server e' fermo
    _, m4 = _mgr(tmp_path)
    m4.restore_pending()
    r = m4.get(q.id)
    assert r.analyzed_head == 2 and not r.approvals
