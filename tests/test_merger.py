import pytest
from lxml import etree

from server.merger import L5XError, Resolution, merge_l5x
from tests import sample_l5x as S


def b(s):
    return s.encode("utf-8")


def rung_texts(xml: bytes, prog="Main", routine="MainRoutine"):
    root = etree.fromstring(xml)
    r = root.find(f".//Program[@Name='{prog}']/Routines/Routine[@Name='{routine}']")
    return [x.findtext("Text") for x in r.iter("Rung")], [x.get("Number") for x in r.iter("Rung")]


def test_clean_merge_proposal_and_master_changes():
    res = merge_l5x(b(S.base_project()), b(S.dev_a()), b(S.dev_b(False)))
    assert res.is_clean and not res.conflicts
    texts, nums = rung_texts(res.merged_xml)
    assert texts[0] == "XIC(Start)XIO(Stop)XIO(Fault)OTE(Motor);"       # da A
    assert texts[3] == "XIC(Alarm)XIC(Enable)OTE(Horn);"                # da B (master)
    assert texts[-1] == "XIC(Motor)OTE(Lamp);"                          # aggiunta da A
    assert nums == [str(i) for i in range(len(texts))]                  # rinumerazione
    root = etree.fromstring(res.merged_xml)
    assert root.find(".//Program[@Name='Main']/Routines/Routine[@Name='Diagnostica']") is not None
    assert {t.get("Name") for t in root.findall("./Controller/Tags/Tag")} >= {"Enable"}   # tag di B conservato
    assert {t.get("Name") for t in root.findall(".//Program[@Name='Main']/Tags/Tag")} >= {"Fault", "Lamp"}
    kinds = {(c.path.split("/")[-1], c.origin) for c in res.changes}
    assert any(p.startswith("Routine[Diagnostica]") or p == "Routine[Diagnostica]" for p, _ in kinds)
    assert any(c.origin == "master" for c in res.upstream_changes)
    assert "Main/Routine[MainRoutine]" in "".join(res.touched_routines) or res.touched_routines


def test_cdata_preserved():
    res = merge_l5x(b(S.base_project()), b(S.dev_a()), b(S.dev_b(False)))
    assert b"<![CDATA[XIC(Start)XIO(Stop)XIO(Fault)OTE(Motor);]]>" in res.merged_xml


def test_conflicts_detected():
    res = merge_l5x(b(S.base_project()), b(S.dev_a()), b(S.dev_b(True)))
    assert not res.is_clean
    ids = {c.title: c for c in res.conflicts}
    seq = [c for c in res.conflicts if c.sequence]
    atomic = [c for c in res.conflicts if not c.sequence]
    assert len(seq) == 1 and "Start" in seq[0].mine_text and "Enable" in seq[0].theirs_text
    assert len(atomic) == 1 and "DataType[Motore_UDT]" in atomic[0].path
    # il default provvisorio e' la versione Master
    texts, _ = rung_texts(res.merged_xml)
    assert texts[0] == "XIC(Start)XIO(Stop)XIC(Enable)OTE(Motor);"


@pytest.mark.parametrize("choice,expect0", [
    ("mine", "XIC(Start)XIO(Stop)XIO(Fault)OTE(Motor);"),
    ("theirs", "XIC(Start)XIO(Stop)XIC(Enable)OTE(Motor);"),
])
def test_resolutions(choice, expect0):
    first = merge_l5x(b(S.base_project()), b(S.dev_a()), b(S.dev_b(True)))
    res_map = {c.id: Resolution(choice, fingerprint=c.fingerprint) for c in first.conflicts}
    res = merge_l5x(b(S.base_project()), b(S.dev_a()), b(S.dev_b(True)), res_map)
    assert res.is_clean
    assert rung_texts(res.merged_xml)[0][0] == expect0
    udt = etree.fromstring(res.merged_xml).find(".//DataType[@Name='Motore_UDT']")
    names = [m.get("Name") for m in udt.iter("Member")]
    assert ("Fault" in names) == (choice == "mine") and ("Torque" in names) == (choice == "theirs")


def test_custom_resolution_and_both():
    first = merge_l5x(b(S.base_project()), b(S.dev_a()), b(S.dev_b(True)))
    seq = next(c for c in first.conflicts if c.sequence)
    at = next(c for c in first.conflicts if not c.sequence)
    custom_udt = ('<DataType Name="Motore_UDT" Family="NoFamily" Class="User"><Members>'
                  '<Member Name="Run" DataType="BOOL"/><Member Name="Merged" DataType="BOOL"/></Members></DataType>')
    res = merge_l5x(b(S.base_project()), b(S.dev_a()), b(S.dev_b(True)), {
        seq.id: Resolution("custom", ["XIC(Start)XIO(Stop)XIO(Fault)XIC(Enable)OTE(Motor);"]),
        at.id: Resolution("custom", [custom_udt]),
    })
    assert res.is_clean
    assert rung_texts(res.merged_xml)[0][0] == "XIC(Start)XIO(Stop)XIO(Fault)XIC(Enable)OTE(Motor);"
    assert "Merged" in res.merged_xml.decode()
    res2 = merge_l5x(b(S.base_project()), b(S.dev_a()), b(S.dev_b(True)),
                     {seq.id: Resolution("both"), at.id: Resolution("theirs")})
    t, _ = rung_texts(res2.merged_xml)
    assert t[0].endswith("XIC(Enable)OTE(Motor);") and t[1].endswith("XIO(Fault)OTE(Motor);")


def test_stale_fingerprint_ignored():
    first = merge_l5x(b(S.base_project()), b(S.dev_a()), b(S.dev_b(True)))
    res = merge_l5x(b(S.base_project()), b(S.dev_a()), b(S.dev_b(True)),
                    {c.id: Resolution("mine", fingerprint="xxxx") for c in first.conflicts})
    assert len(res.unresolved) == len(first.conflicts)


def test_modify_delete_conflict():
    base = S.base_project()
    mine = base.replace('<Tag Name="Counter"', '<Tag Name="Counter" Foo="1"')       # A modifica Counter
    theirs = S.project(udts=[S.udt("Motore_UDT", [("Run", "BOOL"), ("Speed", "DINT")])],
                       aois=[S.aoi("Blocco_AOI", ["XIC(In)OTE(Out);"], ["In"])],
                       ctrl_tags=[S.tag("Global_Start")],                          # B cancella Counter
                       programs=[S.program("Main", [S.tag("Start"), S.tag("Stop"), S.tag("Motor")],
                                           [S.rll_routine("MainRoutine", S.MAIN_RUNGS),
                                            S.st_routine("Calc", ["Speed := 10;", "IF Run THEN", "  Speed := 20;", "END_IF;"])]),
                                 S.program("Safety", [S.tag("EStop")], [S.rll_routine("MainRoutine", ["XIC(EStop)OTE(SafeOut);"])])])
    res = merge_l5x(b(base), b(mine), b(theirs))
    assert [c.kind for c in res.conflicts] == ["modify/delete"]
    r2 = merge_l5x(b(base), b(mine), b(theirs), {res.conflicts[0].id: Resolution("mine")})
    assert etree.fromstring(r2.merged_xml).find("./Controller/Tags/Tag[@Name='Counter']") is not None


def test_idempotent_when_equal():
    base = b(S.base_project())
    res = merge_l5x(base, base, base)
    assert res.is_clean and not res.changes and not res.upstream_changes


def test_same_change_on_both_sides_no_conflict():
    res = merge_l5x(b(S.base_project()), b(S.dev_a()), b(S.dev_a()))
    assert res.is_clean and not res.changes


def test_rejects_other_controller_and_partial_export():
    other = S.project(name="ALTRO_PLC")
    with pytest.raises(L5XError):
        merge_l5x(b(S.base_project()), b(other), b(S.base_project()))
    with pytest.raises(L5XError):
        merge_l5x(b("<x/>"), b(S.base_project()), b(S.base_project()))
    partial = S.base_project().replace('TargetType="Controller"', 'TargetType="Program"')
    with pytest.raises(L5XError):
        merge_l5x(b(S.base_project()), b(partial), b(S.base_project()))


def test_xxe_not_resolved():
    evil = S.base_project().replace("<RSLogix5000Content", '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><RSLogix5000Content', 1)
    evil = evil.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n', "")
    evil = evil.replace('Counter"', 'Counter&e;"', 1)
    try:
        res = merge_l5x(b(S.base_project()), b(evil), b(S.base_project()))
        assert b"root:" not in res.merged_xml
    except L5XError:
        pass


def test_order_independent_non_overlapping_rung_edits():
    # A modifica rung 1, B modifica rung 4: entrambi applicati, nessun conflitto
    base = S.project(programs=[S.program("Main", [S.tag("x")], [S.rll_routine("MainRoutine", S.MAIN_RUNGS)])])
    ra, rb_ = list(S.MAIN_RUNGS), list(S.MAIN_RUNGS)
    ra[1] = "XIC(Motor)TON(T1,5,0);"; rb_[4] = "XIC(Reset)OTU(Done)OTU(Horn);"
    A = S.project(programs=[S.program("Main", [S.tag("x")], [S.rll_routine("MainRoutine", ra)])])
    B = S.project(programs=[S.program("Main", [S.tag("x")], [S.rll_routine("MainRoutine", rb_)])])
    r1 = merge_l5x(b(base), b(A), b(B))
    r2 = merge_l5x(b(base), b(B), b(A))
    assert r1.is_clean and r2.is_clean
    assert rung_texts(r1.merged_xml)[0] == rung_texts(r2.merged_xml)[0]
    assert rung_texts(r1.merged_xml)[0][1] == ra[1] and rung_texts(r1.merged_xml)[0][4] == rb_[4]
