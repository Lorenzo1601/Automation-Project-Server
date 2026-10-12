import pytest
from lxml import etree

from common.schemas import ProposalState as St
from common.roles import Role
from server.merger import Resolution
from server.notifier import RecordingSink
from server.session_manager import SessionError, SessionManager
from server.storage import MasterStore
from tests import sample_l5x as S


def b(s):
    return s.encode()


class Clock:
    t = 1000.0
    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    store = MasterStore(tmp_path)
    store.commit(b(S.base_project()), "admin", "init")
    sink, clock = RecordingSink(), Clock()
    mgr = SessionManager(store, sink, ttl=60, clock=clock, require_lead_approval=False)
    return store, mgr, sink, clock


def head_rungs(store):
    r = etree.fromstring(store.read_revision())
    return [x.findtext("Text") for x in r.findall(".//Program[@Name='Main']//Routine[@Name='MainRoutine']//Rung")]


def test_full_consent_flow_two_developers(env):
    store, mgr, sink, _ = env
    A, B = mgr.login("A"), mgr.login("B")
    p = mgr.submit(A, b(S.dev_a()), 1, "Diagnostica + fault")
    assert p.state == St.AWAITING_CONSENT and set(p.required.values()) == {"A", "B"}
    # B viene notificato
    ev = [(e, t) for e, t in sink.events if e["type"] == "proposal_submitted"]
    assert ev and B.session_id in ev[0][1] and "Diagnostica" in ev[0][0]["message"]

    mgr.approve(A, p.id)
    assert p.state == St.AWAITING_CONSENT and store.head().revision == 1     # non ancora su disco
    mgr.approve(B, p.id, "ok")
    assert p.state == St.MERGED and store.head().revision == 2
    assert "XIC(Motor)OTE(Lamp);" in head_rungs(store)
    assert any(e["type"] == "master_updated" and e["revision"] == 2 for e, _ in sink.events)


def test_nothing_written_without_unanimity_and_reject(env):
    store, mgr, _, _ = env
    A, B = mgr.login("A"), mgr.login("B")
    p = mgr.submit(A, b(S.dev_a()), 1)
    mgr.approve(A, p.id)
    mgr.reject(B, p.id, "manca il commento sul rung 0")
    assert p.state == St.REJECTED and store.head().revision == 1
    with pytest.raises(SessionError) as e:
        mgr.approve(B, p.id)
    assert e.value.status == 409


def test_solo_author_still_needs_own_consent(env):
    store, mgr, _, _ = env
    A = mgr.login("A")
    p = mgr.submit(A, b(S.dev_a()), 1)
    assert store.head().revision == 1
    mgr.approve(A, p.id)
    assert p.state == St.MERGED


def test_conflict_must_be_resolved_by_author_and_resets_consent(env):
    store, mgr, _, _ = env
    A, B = mgr.login("A"), mgr.login("B")
    # B ha gia' consolidato la sua versione (con Enable) su Master
    pb = mgr.submit(B, b(S.dev_b(True)), 1)
    mgr.approve(A, pb.id); mgr.approve(B, pb.id)
    assert store.head().revision == 2
    # A invia da rev 1: conflitto con quanto consolidato da B
    pa = mgr.submit(A, b(S.dev_a()), 1)
    assert pa.state == St.CONFLICT and len(pa.result.unresolved) == 2
    with pytest.raises(SessionError) as e:
        mgr.approve(B, pa.id)
    assert e.value.status == 409
    with pytest.raises(SessionError) as e:
        mgr.set_resolutions(B, pa.id, {c.id: Resolution("mine") for c in pa.result.conflicts})
    assert e.value.status == 403
    with pytest.raises(SessionError):
        mgr.set_resolutions(A, pa.id, {"nope": Resolution("mine")})
    mgr.set_resolutions(A, pa.id, {c.id: Resolution("theirs" if not c.sequence else "both") for c in pa.result.conflicts})
    assert pa.state == St.AWAITING_CONSENT
    mgr.approve(B, pa.id)
    d1 = pa.result.digest
    # A cambia idea: il consenso di B decade
    seq = next(c for c in pa.result.conflicts if c.sequence)
    mgr.set_resolutions(A, pa.id, {seq.id: Resolution("mine")})
    assert pa.result.digest != d1 and not pa.approvals
    mgr.approve(A, pa.id); mgr.approve(B, pa.id)
    assert pa.state == St.MERGED and store.head().revision == 3


def test_master_advance_reanalyzes_pending_and_resets_consent(env):
    store, mgr, _, _ = env
    A, B = mgr.login("A"), mgr.login("B")
    pa = mgr.submit(A, b(S.dev_a()), 1)
    mgr.approve(A, pa.id)                                   # A ha gia' dato consenso
    # prima che B risponda, arriva la proposta di B e viene consolidata
    pb = mgr.submit(B, b(S.dev_b(False)), 1)
    mgr.approve(A, pb.id); mgr.approve(B, pb.id)
    assert pb.state == St.MERGED and store.head().revision == 2
    assert pa.analyzed_head == 2 and pa.state == St.AWAITING_CONSENT and not pa.approvals
    mgr.approve(A, pa.id); mgr.approve(B, pa.id)
    assert pa.state == St.MERGED and store.head().revision == 3
    rungs = head_rungs(store)
    assert rungs[3] == "XIC(Alarm)XIC(Enable)OTE(Horn);" and rungs[0].startswith("XIC(Start)XIO(Stop)XIO(Fault)")


def test_offline_developer_dropped_from_quorum(env):
    store, mgr, _, clock = env
    A, B = mgr.login("A"), mgr.login("B")
    p = mgr.submit(A, b(S.dev_a()), 1)
    mgr.approve(A, p.id)
    clock.t += 500                                          # B scade (nessun heartbeat/WS)
    mgr.touch(A)
    mgr.housekeeping()
    assert p.state == St.MERGED and list(p.required.values()) == ["A"]


def test_ws_connected_developer_stays_required(env):
    store, mgr, _, clock = env
    A, B = mgr.login("A"), mgr.login("B")
    mgr.set_ws(B.session_id, True)
    p = mgr.submit(A, b(S.dev_a()), 1)
    clock.t += 500; mgr.touch(A); mgr.approve(A, p.id)
    assert p.state == St.AWAITING_CONSENT


def test_validation_errors(env):
    _, mgr, _, _ = env
    A = mgr.login("A")
    with pytest.raises(SessionError) as e:
        mgr.submit(A, b"<nonxml", 1)
    assert e.value.status == 422
    with pytest.raises(SessionError):
        mgr.submit(A, b(S.base_project()), 1)               # nessuna modifica
    with pytest.raises(SessionError):
        mgr.submit(A, b(S.dev_a()), 99)                     # base inesistente


def test_new_submission_replaces_previous_and_author_logout_cancels(env):
    _, mgr, _, _ = env
    A, B = mgr.login("A"), mgr.login("B")
    p1 = mgr.submit(A, b(S.dev_a()), 1)
    p2 = mgr.submit(A, b(S.dev_a()), 1)
    assert p1.state == St.CANCELLED and p2.state == St.AWAITING_CONSENT
    mgr.logout(A.session_id)
    assert p2.state == St.CANCELLED


# ------------------------------------------------------------------ ruoli (RBAC) nel consenso
@pytest.fixture
def lead_env(tmp_path):
    store = MasterStore(tmp_path)
    store.commit(b(S.base_project()), "admin", "init")
    audit = []
    mgr = SessionManager(store, RecordingSink(), ttl=60, clock=Clock(), require_lead_approval=True,
                         audit=lambda action, actor, target, details: audit.append((action, actor, target, details)))
    return store, mgr, audit


def test_developer_request_needs_lead_final_approval(lead_env):
    store, mgr, audit = lead_env
    dev1, dev2 = mgr.login("dev1", Role.DEVELOPER), mgr.login("dev2", Role.DEVELOPER)
    p = mgr.submit(dev1, b(S.dev_a()), 1)
    assert p.needs_lead
    mgr.approve(dev1, p.id); mgr.approve(dev2, p.id)
    assert p.state == St.AWAITING_CONSENT and store.head().revision == 1       # unanimita' dei dev NON basta
    lead = mgr.login("lead", Role.LEAD)                                         # lead non era tra i partecipanti
    mgr.approve(lead, p.id)
    assert p.state == St.MERGED and store.head().revision == 2
    assert p.lead_approval.developer == "lead"
    acts = [a[0] for a in audit]
    assert acts.count("consent_given") == 3 and "merge_completed" in acts and "merge_requested" in acts
    done = next(a for a in audit if a[0] == "merge_completed")
    assert done[3]["revision"] == 2 and done[3]["lead_approved_by"] == "lead"


def test_author_cannot_final_approve_own_request(lead_env):
    store, mgr, _ = lead_env
    dev = mgr.login("dev", Role.DEVELOPER)
    lead = mgr.login("lead", Role.LEAD)
    p = mgr.submit(dev, b(S.dev_a()), 1)
    mgr.approve(dev, p.id)
    assert p.state == St.AWAITING_CONSENT
    mgr.approve(lead, p.id)
    assert p.state == St.MERGED


def test_lead_authored_request_needs_no_final_approval(lead_env):
    store, mgr, _ = lead_env
    lead, dev = mgr.login("lead", Role.LEAD), mgr.login("dev", Role.DEVELOPER)
    p = mgr.submit(lead, b(S.dev_a()), 1)
    assert not p.needs_lead
    mgr.approve(lead, p.id); mgr.approve(dev, p.id)
    assert p.state == St.MERGED


def test_viewer_is_read_only(lead_env):
    store, mgr, _ = lead_env
    viewer, dev = mgr.login("viewer", Role.VIEWER), mgr.login("dev", Role.DEVELOPER)
    with pytest.raises(SessionError) as e:
        mgr.submit(viewer, b(S.dev_a()), 1)
    assert e.value.status == 403
    p = mgr.submit(dev, b(S.dev_a()), 1)
    assert set(p.required.values()) == {"dev"}                                  # i viewer non partecipano
    with pytest.raises(SessionError) as e:
        mgr.approve(viewer, p.id)
    assert e.value.status == 403
    with pytest.raises(SessionError):
        mgr.reject(viewer, p.id)


def test_admin_force_merge_and_restore(lead_env):
    store, mgr, audit = lead_env
    admin, dev = mgr.login("admin", Role.ADMIN), mgr.login("dev", Role.DEVELOPER)
    p = mgr.submit(dev, b(S.dev_a()), 1)
    with pytest.raises(SessionError) as e:
        mgr.force_merge(dev, p.id, "forzo io")
    assert e.value.status == 403
    with pytest.raises(SessionError) as e:
        mgr.force_merge(admin, p.id, "ok")                                     # motivo troppo breve
    assert e.value.status == 422
    mgr.force_merge(admin, p.id, "urgenza di collaudo")
    assert p.state == St.MERGED and p.forced_by == "admin" and store.head().revision == 2
    assert "[MERGE FORZATO" in store.head().message
    assert any(a[0] == "merge_forced" and a[1] == "admin" for a in audit)
    # ripristino rev 1 -> nuova rev 3 con contenuto di rev 1 (storico intatto)
    with pytest.raises(SessionError):
        mgr.restore(dev, 1, "no")
    mgr.restore(admin, 1, "regressione in campo")
    assert store.head().revision == 3 and store.read_revision(3) == store.read_revision(1)
    assert store.read_revision(2) != store.read_revision(1)
    assert any(a[0] == "master_restored" and a[3]["restored_from"] == 1 for a in audit)
    with pytest.raises(SessionError):
        mgr.restore(admin, 3, "gia corrente")


def test_force_merge_blocked_by_unresolved_conflicts_admin_can_resolve(lead_env):
    store, mgr, _ = lead_env
    admin, a, b_ = mgr.login("admin", Role.ADMIN), mgr.login("a", Role.DEVELOPER), mgr.login("b", Role.LEAD)
    pb = mgr.submit(b_, b(S.dev_b(True)), 1)
    for who in (b_, a, admin):
        mgr.approve(who, pb.id)
    assert store.head().revision == 2
    pa = mgr.submit(a, b(S.dev_a()), 1)
    assert pa.state == St.CONFLICT
    with pytest.raises(SessionError) as e:
        mgr.force_merge(admin, pa.id, "bloccato da giorni")
    assert e.value.status == 409
    mgr.set_resolutions(admin, pa.id, {c.id: Resolution("theirs" if not c.sequence else "both") for c in pa.result.conflicts})
    assert pa.state == St.AWAITING_CONSENT
    mgr.force_merge(admin, pa.id, "bloccato da giorni")
    assert pa.state == St.MERGED and store.head().revision == 3
