"""Merge strutturale per PLCopen XML (TC6 / CODESYS): riconoscimento, motore, server end-to-end, file locali del client."""
import os
import re
import time

import pytest
from lxml import etree

from common import filekinds as FK
from server.merger import L5XError, Resolution
from server.plcopen import merge_plcopen, parse_plcopen, plcopen_identity
from server.textmerge import merge_any
from tests import sample_l5x as SL
from tests import sample_plcopen as P
from tests.sample_plcopen import b
from tests.test_api_e2e import ADMIN_PW, env, login, online, world  # noqa: F401  (fixture 'env', 'world')
from tests.test_filetypes import l5k, qapp  # noqa: F401  (fixture 'qapp')

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
NSMAP = {"p": P.NS, "x": P.XHTML}


# ------------------------------------------------------------------------------------ helpers
def main_pou(lines=None, contatore="0", extra_vars=(), start_doc=None):
    return P.pou("PLC_PRG", "program",
                 P.group("localVars", [P.var("Start", doc=start_doc), P.var("Contatore", "INT", contatore),
                                       P.var("Motore", "MotorFB"), *extra_vars]),
                 P.st_body(P.MAIN_ST if lines is None else lines))


def motor_pou(lines=None, speed="0", enable_addr=None, extra_out=()):
    return P.pou("MotorFB", "functionBlock",
                 P.group("inputVars", [P.var("Enable", "BOOL", "FALSE", doc="Abilita il motore", address=enable_addr),
                                       P.var("Speed", "INT", speed)])
                 + P.group("outputVars", [P.var("Running"), *extra_out]),
                 P.st_body(P.MOTOR_ST if lines is None else lines))


def utils_pou(lines=None):
    return P.pou("Utils", "function",
                 "<returnType><INT/></returnType>" + P.group("inputVars", [P.var("Valore", "INT")])
                 + P.group("localVars", [P.var("Scala", "INT")]), P.st_body(P.UTILS_ST if lines is None else lines))


def ld_pou(contacts=("Start", "Lampada")):
    return P.pou("Pannello", "program", P.group("localVars", [P.var("Lampada")]), P.ld_body(list(contacts)))


def proj(main=None, utils=None, motor=None, ld=None, extra_pous=(), dts=None, **kw):
    pous = [main or main_pou(), utils or utils_pou(), motor or motor_pou(), ld or ld_pou(), *extra_pous]
    return b(P.project(pous, dts, **kw))


BASE = proj()


def merged_tree(res) -> etree._Element:
    return etree.fromstring(res.merged_xml)


def pou_names(res) -> list[str]:
    return [e.get("name") for e in merged_tree(res).iterfind(".//p:pou", NSMAP)]


def st_lines(res, pou_name: str) -> list[str]:
    root = merged_tree(res)
    p = root.find(f".//p:pou[@name='{pou_name}']/p:body/p:ST/x:p", NSMAP)
    return p.text.split("\n")


def var_xml(res, pou_name: str, var_name: str) -> etree._Element:
    return merged_tree(res).find(f".//p:pou[@name='{pou_name}']//p:variable[@name='{var_name}']", NSMAP)


def paths(changes) -> list[str]:
    return [c.path for c in changes]


# ------------------------------------------------------------------------------------ riconoscimento
def test_detect_kind_plcopen_and_neighbours():
    assert FK.detect_kind(BASE) == FK.PLCOPEN
    assert FK.detect_kind(b(P.project(ns="http://www.plcopen.org/xml/tc6_0201"))) == FK.PLCOPEN
    assert FK.detect_kind(b(P.project(decl=False))) == FK.PLCOPEN
    assert FK.detect_kind(b"\xef\xbb\xbf" + BASE) == FK.PLCOPEN                         # con BOM
    pref = b'<plc:project xmlns:plc="http://www.plcopen.org/xml/tc6_0200"><plc:fileHeader/></plc:project>'
    assert FK.detect_kind(pref) == FK.PLCOPEN
    # un XML qualunque (o con <project> di altri strumenti) resta testo; L5X e L5K restano quello che sono
    assert FK.detect_kind(b'<?xml version="1.0"?><project name="x"><target/></project>') == FK.TEXT
    assert FK.detect_kind(b'<project xmlns="http://maven.apache.org/POM/4.0.0"/>') == FK.TEXT
    assert FK.detect_kind(b'<a><project xmlns="http://www.plcopen.org/xml/tc6_0200"/></a>') == FK.TEXT     # non e' la radice
    assert FK.detect_kind(b"<doc>nessun namespace http://www.plcopen.org/xml/tc6_0200</doc>") == FK.TEXT
    evil = BASE.replace(b"<project", b'<!DOCTYPE project [<!ENTITY e SYSTEM "file:///etc/passwd">]><project', 1)
    assert FK.detect_kind(evil) == FK.PLCOPEN                    # riconosciuto, cosi' il server lo puo' rifiutare
    assert FK.detect_kind(SL.base_project().encode()) == FK.L5X
    assert FK.detect_kind(l5k().encode()) == FK.L5K
    # un L5X che cita il namespace PLCopen in un commento e' sempre L5X (priorita')
    sneaky = SL.base_project().replace("<Controller", '<!-- <project xmlns="http://www.plcopen.org/xml/tc6_0200"> --><Controller', 1)
    assert FK.detect_kind(sneaky.encode()) == FK.L5X


def test_plcopen_kind_metadata():
    assert FK.PLCOPEN in FK.KINDS and FK.PLCOPEN in FK.MERGEABLE
    assert FK.KIND_LABELS[FK.PLCOPEN] == "PLCopen XML (CODESYS)"
    assert FK.canonical_ext("Impianto.xml", FK.PLCOPEN) == ".xml" and FK.canonical_ext("Impianto", FK.PLCOPEN) == ".xml"
    assert FK.canonical_ext("Impianto.XML", FK.PLCOPEN) == ".XML" and FK.canonical_ext("a.plcopen", FK.PLCOPEN) == ".plcopen"
    assert FK.media_type(FK.PLCOPEN) == "application/xml"
    f = FK.import_filters()
    assert f.startswith("Tutti i file (*)") and "Rockwell" in f and "PLCopen XML (*.xml" in f
    g = FK.file_dialog_filters(".xml", FK.PLCOPEN)
    assert g.split(";;")[0].startswith("File del progetto (*.XML") and "PLCopen XML (*.xml" in g and g.endswith("Tutti i file (*)")
    assert FK.compatible_kinds(FK.PLCOPEN, FK.PLCOPEN) and not FK.compatible_kinds(FK.PLCOPEN, FK.TEXT)
    assert FK.compatible_kinds(FK.TEXT, FK.PLCOPEN) and not FK.compatible_kinds(FK.L5X, FK.PLCOPEN)
    assert FK.rev_filename("Impianto", 4, ".xml") == "Impianto_rev4.xml"


def test_plcopen_identity_and_parse_errors():
    assert plcopen_identity(BASE) == ("Impianto", "Acme")
    with pytest.raises(L5XError, match="non e' un file PLCopen"):
        parse_plcopen(b'<project xmlns="http://altro"/>')
    with pytest.raises(L5XError, match="XML non valido"):
        parse_plcopen(BASE[:-30])


# ------------------------------------------------------------------------------------ merge senza conflitti
def test_clean_merge_changes_in_different_pous():
    mine = proj(utils=utils_pou(["Scala := Valore * 3;", "// fine"]), modified="2026-10-02T09:00:00")
    theirs = proj(motor=motor_pou(speed="10"), modified="2026-10-03T09:00:00")
    r = merge_any(FK.PLCOPEN, BASE, mine, theirs)
    assert r.is_clean and not r.conflicts
    assert st_lines(r, "Utils")[0] == "Scala := Valore * 3;"
    assert var_xml(r, "MotorFB", "Speed").find(".//p:simpleValue", NSMAP).get("value") == "10"
    assert paths(r.changes) == ["POU[Utils]/Corpo/Riga1"] and r.changes[0].origin == "proposal"
    assert r.changes[0].label == "Riga" and r.changes[0].kind == "modified"
    assert r.changes[0].base_text == "Scala := Valore * 2;" and r.changes[0].new_text == "Scala := Valore * 3;"
    assert paths(r.upstream_changes) == ["POU[MotorFB]/inputVars/Variable[Speed]/initialValue"]
    assert r.upstream_changes[0].origin == "master" and r.touched_routines == ["POU[Utils]"]
    assert pou_names(r) == ["PLC_PRG", "Utils", "MotorFB", "Pannello"]
    # il timestamp non genera ne' conflitti ne' modifiche: vince il piu' recente
    assert merged_tree(r).find("p:contentHeader", NSMAP).get("modificationDateTime") == "2026-10-03T09:00:00"
    assert r.digest and r.merged_xml.startswith(b'<?xml version="1.0" encoding="utf-8"?>\n')


def test_same_pou_different_lines_and_different_variables():
    mine = proj(main=main_pou(lines=["IF Start THEN", "    Motore.Enable := FALSE;", "END_IF", "", "Motore(Speed := 100);",
                                     "Contatore := Contatore + 1;"], contatore="5"))
    theirs = proj(main=main_pou(lines=[*P.MAIN_ST, "Allarme := FALSE;"],
                                extra_vars=[P.var("Allarme", "BOOL", "FALSE", doc="Nuovo allarme")]))
    r = merge_any(FK.PLCOPEN, BASE, mine, theirs)
    assert r.is_clean and not r.conflicts
    lines = st_lines(r, "PLC_PRG")
    assert lines[1] == "    Motore.Enable := FALSE;" and lines[-1] == "Allarme := FALSE;"
    assert var_xml(r, "PLC_PRG", "Contatore").find(".//p:simpleValue", NSMAP).get("value") == "5"
    assert var_xml(r, "PLC_PRG", "Allarme") is not None
    assert set(paths(r.changes)) == {"POU[PLC_PRG]/localVars/Variable[Contatore]/initialValue", "POU[PLC_PRG]/Corpo/Riga2"}
    assert {c.origin for c in r.upstream_changes} == {"master"}
    assert r.touched_routines == ["POU[PLC_PRG]"]


def test_same_change_on_both_sides_is_not_a_conflict():
    both = proj(utils=utils_pou(["Scala := Valore * 3;", "// fine"]))
    r = merge_any(FK.PLCOPEN, BASE, both, both)
    assert r.is_clean and not r.changes and [c.origin for c in r.upstream_changes] == ["both"]
    assert r.merged_xml == both                                  # identico al Master, byte per byte


# ------------------------------------------------------------------------------------ conflitti e risoluzioni
def _st_conflict():
    mine = proj(main=main_pou(lines=["IF Start THEN", "    Motore.Enable := FALSE;", "END_IF", "", "Motore(Speed := 100);",
                                     "Contatore := Contatore + 1;"]))
    theirs = proj(main=main_pou(lines=["IF Start THEN", "    Motore.Enable := Auto;", "END_IF", "", "Motore(Speed := 100);",
                                       "Contatore := Contatore + 1;"]))
    return mine, theirs


def test_conflict_on_same_st_line_and_resolutions():
    mine, theirs = _st_conflict()
    r = merge_any(FK.PLCOPEN, BASE, mine, theirs)
    assert len(r.conflicts) == 1 and not r.is_clean
    c = r.conflicts[0]
    assert c.kind == "modify/modify" and c.sequence and c.label == "Riga" and c.path == "POU[PLC_PRG]/Corpo/Riga2"
    assert (c.base_text, c.mine_text, c.theirs_text) == ("    Motore.Enable := TRUE;", "    Motore.Enable := FALSE;",
                                                         "    Motore.Enable := Auto;")
    assert st_lines(r, "PLC_PRG")[1] == "    Motore.Enable := Auto;"          # default provvisorio: Master
    for choice, expect in (("mine", ["    Motore.Enable := FALSE;"]), ("theirs", ["    Motore.Enable := Auto;"]),
                           ("both", ["    Motore.Enable := Auto;", "    Motore.Enable := FALSE;"])):
        rr = merge_any(FK.PLCOPEN, BASE, mine, theirs, {c.id: Resolution(choice)})
        assert rr.is_clean and rr.conflicts[0].resolved
        assert st_lines(rr, "PLC_PRG")[1:1 + len(expect)] == expect
    cu = merge_any(FK.PLCOPEN, BASE, mine, theirs, {c.id: Resolution("custom", ["    // deciso a mano", "    NOP;"])})
    assert cu.is_clean and st_lines(cu, "PLC_PRG")[1:3] == ["    // deciso a mano", "    NOP;"]
    stale = merge_any(FK.PLCOPEN, BASE, mine, theirs, {c.id: Resolution("mine", fingerprint="vecchia")})
    assert not stale.is_clean                                               # risoluzione di un altro conflitto: ignorata


def test_conflict_on_same_variable_field_and_resolutions():
    mine = proj(main=main_pou(contatore="5"))
    theirs = proj(main=main_pou(contatore="9"))
    r = merge_any(FK.PLCOPEN, BASE, mine, theirs)
    assert len(r.conflicts) == 1
    c = r.conflicts[0]
    assert c.path == "POU[PLC_PRG]/localVars/Variable[Contatore]/initialValue" and c.kind == "modify/modify"
    assert not c.sequence and "value=\"5\"" in c.mine_text and "value=\"9\"" in c.theirs_text and "xmlns" not in c.mine_text
    assert var_xml(r, "PLC_PRG", "Contatore").find(".//p:simpleValue", NSMAP).get("value") == "9"
    mm = merge_any(FK.PLCOPEN, BASE, mine, theirs, {c.id: Resolution("mine")})
    assert mm.is_clean and var_xml(mm, "PLC_PRG", "Contatore").find(".//p:simpleValue", NSMAP).get("value") == "5"
    th = merge_any(FK.PLCOPEN, BASE, mine, theirs, {c.id: Resolution("theirs")})
    assert th.is_clean and var_xml(th, "PLC_PRG", "Contatore").find(".//p:simpleValue", NSMAP).get("value") == "9"
    cu = merge_any(FK.PLCOPEN, BASE, mine, theirs, {c.id: Resolution(
        "custom", ['<initialValue><simpleValue value="7"/></initialValue>'])})
    assert cu.is_clean and var_xml(cu, "PLC_PRG", "Contatore").find(".//p:simpleValue", NSMAP).get("value") == "7"
    bad = merge_any(FK.PLCOPEN, BASE, mine, theirs, {c.id: Resolution("custom", ["<initialValue>"])})
    assert not bad.is_clean and any("risoluzione personalizzata" in w for w in bad.warnings)    # XML non valido: resta aperto


def test_conflict_on_variable_address_attribute_only():
    mine = proj(motor=motor_pou(enable_addr="%IX0.0"))
    theirs = proj(motor=motor_pou(enable_addr="%IX1.0", speed="3"))
    r = merge_any(FK.PLCOPEN, BASE, mine, theirs)
    assert [c.path for c in r.conflicts] == ["POU[MotorFB]/inputVars/Variable[Enable]/@address"]
    assert r.conflicts[0].label == "@attr" and r.conflicts[0].mine_text == "%IX0.0"
    assert paths(r.upstream_changes) == ["POU[MotorFB]/inputVars/Variable[Speed]/initialValue"]    # il resto passa
    rr = merge_any(FK.PLCOPEN, BASE, mine, theirs, {r.conflicts[0].id: Resolution("mine")})
    assert rr.is_clean and var_xml(rr, "MotorFB", "Enable").get("address") == "%IX0.0"


def test_pou_add_add_modify_delete_and_identical_add():
    new_a = P.pou("Nuovo", "function", "<returnType><BOOL/></returnType>", P.st_body(["Nuovo := TRUE;"]))
    new_b = P.pou("Nuovo", "function", "<returnType><BOOL/></returnType>", P.st_body(["Nuovo := FALSE;"]))
    r = merge_any(FK.PLCOPEN, BASE, proj(extra_pous=[new_a]), proj(extra_pous=[new_b]))
    assert [(c.kind, c.path) for c in r.conflicts] == [("add/add", "POU[Nuovo]")]
    assert "TRUE" in r.conflicts[0].mine_text and "FALSE" in r.conflicts[0].theirs_text and "xmlns" not in r.conflicts[0].mine_text
    mm = merge_any(FK.PLCOPEN, BASE, proj(extra_pous=[new_a]), proj(extra_pous=[new_b]), {r.conflicts[0].id: Resolution("mine")})
    assert mm.is_clean and pou_names(mm)[-1] == "Nuovo" and st_lines(mm, "Nuovo") == ["Nuovo := TRUE;"]
    same = merge_any(FK.PLCOPEN, BASE, proj(extra_pous=[new_a]), proj(extra_pous=[new_a]))
    assert same.is_clean and not same.changes and [c.origin for c in same.upstream_changes] == ["both"]

    # io cancello Utils (che il Master non ha toccato): cancellazione pulita
    no_utils = b(P.project([main_pou(), motor_pou(), ld_pou()]))
    d = merge_any(FK.PLCOPEN, BASE, no_utils, BASE)
    assert d.is_clean and pou_names(d) == ["PLC_PRG", "MotorFB", "Pannello"]
    assert [(c.kind, c.path) for c in d.changes] == [("deleted", "POU[Utils]")]
    # io cancello Utils ma il Master l'ha modificato: modify/delete
    md = merge_any(FK.PLCOPEN, BASE, no_utils, proj(utils=utils_pou(["Scala := 1;"])))
    assert [(c.kind, c.path) for c in md.conflicts] == [("modify/delete", "POU[Utils]")]
    assert md.conflicts[0].mine_text == "" and pou_names(md)[1] == "Utils"                  # provvisorio: Master
    ok = merge_any(FK.PLCOPEN, BASE, no_utils, proj(utils=utils_pou(["Scala := 1;"])), {md.conflicts[0].id: Resolution("mine")})
    assert ok.is_clean and "Utils" not in pou_names(ok)
    # il Master cancella, io modifico
    dm = merge_any(FK.PLCOPEN, BASE, proj(utils=utils_pou(["Scala := 1;"])), no_utils)
    assert [(c.kind, c.path) for c in dm.conflicts] == [("modify/delete", "POU[Utils]")] and dm.conflicts[0].theirs_text == ""
    # cancellare un POU ancora usato produce un avviso
    gone = b(P.project([main_pou(), utils_pou(), ld_pou()]))
    w = merge_any(FK.PLCOPEN, BASE, gone, BASE)
    assert any("MotorFB" in x and "non esiste piu'" in x for x in w.warnings)


def test_dut_struct_enum_members_and_add_delete():
    base_dts = P.base_dts()
    # struct: io aggiungo un membro, il Master modifica un altro -> pulito; enum: aggiunta valore
    mine_dts = [P.dut_struct("ST_Motore", [("Corrente", "REAL"), ("Giri", "INT"), ("Temp", "REAL")]), base_dts[1]]
    theirs_dts = [P.dut_struct("ST_Motore", [("Corrente", "LREAL"), ("Giri", "INT")]),
                  P.dut_enum("E_Stato", [("Fermo", 0), ("Marcia", 1), ("Errore", 2), ("Manuale", 3)])]
    r = merge_any(FK.PLCOPEN, BASE, proj(dts=mine_dts), proj(dts=theirs_dts))
    assert r.is_clean and not r.conflicts
    root = merged_tree(r)
    members = [v.get("name") for v in root.iterfind(".//p:dataType[@name='ST_Motore']//p:variable", NSMAP)]
    assert members == ["Corrente", "Giri", "Temp"]
    assert root.find(".//p:dataType[@name='ST_Motore']//p:variable[@name='Corrente']//p:LREAL", NSMAP) is not None
    assert [v.get("name") for v in root.iterfind(".//p:dataType[@name='E_Stato']//p:value", NSMAP)] == [
        "Fermo", "Marcia", "Errore", "Manuale"]
    assert "DataType[ST_Motore]/Member[Temp]" in paths(r.changes)
    assert "DataType[E_Stato]/Value[Manuale]" in paths(r.upstream_changes)
    assert set(r.touched_routines) == {"DataType[ST_Motore]"}
    # stesso membro modificato in modo diverso: conflitto
    c1 = proj(dts=[P.dut_struct("ST_Motore", [("Corrente", "INT"), ("Giri", "INT")]), base_dts[1]])
    c2 = proj(dts=[P.dut_struct("ST_Motore", [("Corrente", "LREAL"), ("Giri", "INT")]), base_dts[1]])
    rc = merge_any(FK.PLCOPEN, BASE, c1, c2)
    assert [c.path for c in rc.conflicts] == ["DataType[ST_Motore]/Member[Corrente]/type"]
    # add/add di un DUT e modify/delete di un DUT
    new1, new2 = P.dut_struct("ST_Nuovo", [("A", "INT")]), P.dut_struct("ST_Nuovo", [("A", "BOOL")])
    ra = merge_any(FK.PLCOPEN, BASE, proj(dts=[*base_dts, new1]), proj(dts=[*base_dts, new2]))
    assert [(c.kind, c.path) for c in ra.conflicts] == [("add/add", "DataType[ST_Nuovo]")]
    rd = merge_any(FK.PLCOPEN, BASE, proj(dts=[base_dts[0]]), proj(dts=[base_dts[0], P.dut_enum("E_Stato", [("Fermo", 0)])]))
    assert [(c.kind, c.path) for c in rd.conflicts] == [("modify/delete", "DataType[E_Stato]")]
    # il Master non l'ha toccato: la mia cancellazione passa
    rok = merge_any(FK.PLCOPEN, BASE, proj(dts=[base_dts[0]]), BASE)
    assert rok.is_clean and [(c.kind, c.path) for c in rok.changes] == [("deleted", "DataType[E_Stato]")]


def test_graphical_ld_body_is_atomic_with_warning():
    base_ld = BASE
    mine = proj(ld=ld_pou(("Start", "Lampada", "Extra")))
    theirs = proj(motor=motor_pou(speed="4"))
    r = merge_any(FK.PLCOPEN, base_ld, mine, theirs)
    assert r.is_clean and [(c.kind, c.label, c.path) for c in r.changes] == [("modified", "Corpo LD", "POU[Pannello]/Corpo")]
    assert any("Corpi grafici" in w and "Pannello (LD)" in w for w in r.warnings)
    assert "Extra" in r.changes[0].new_text and "unita' atomica" in r.changes[0].new_text
    both_a, both_b = proj(ld=ld_pou(("Start", "A"))), proj(ld=ld_pou(("Start", "B")))
    c = merge_any(FK.PLCOPEN, base_ld, both_a, both_b)
    assert [(x.kind, x.label, x.sequence) for x in c.conflicts] == [("modify/modify", "Corpo LD", False)]
    assert "Corpo grafico LD" in c.conflicts[0].mine_text and "<variable>A</variable>" in c.conflicts[0].mine_text
    for choice, who in (("mine", "A"), ("theirs", "B")):
        rr = merge_any(FK.PLCOPEN, base_ld, both_a, both_b, {c.conflicts[0].id: Resolution(choice)})
        xs = [v.text for v in merged_tree(rr).iterfind(".//p:pou[@name='Pannello']//p:contact/p:variable", NSMAP)]
        assert rr.is_clean and who in xs and ("B" if who == "A" else "A") not in xs
    cust = merge_any(FK.PLCOPEN, base_ld, both_a, both_b, {c.conflicts[0].id: Resolution(
        "custom", ['<LD><contact localId="1" negated="false"><variable>Z</variable></contact></LD>'])})
    assert cust.is_clean and merged_tree(cust).find(".//p:pou[@name='Pannello']//p:contact/p:variable", NSMAP).text == "Z"
    assert "x:p" not in c.warnings[0]                                      # solo avvisi leggibili


def test_actions_are_named_sub_units():
    def fb(a1=("Reset", ["cnt := 0;"]), a2=("Inc", ["cnt := cnt + 1;"])):
        return P.pou("Cont", "functionBlock", P.group("localVars", [P.var("cnt", "INT")]), P.st_body(["// corpo"]),
                     extra=P.actions(P.action(a1[0], P.st_body(a1[1])), P.action(a2[0], P.st_body(a2[1]))))
    base = proj(extra_pous=[fb()])
    mine = proj(extra_pous=[fb(a1=("Reset", ["cnt := 0;", "ok := TRUE;"]))])
    theirs = proj(extra_pous=[fb(a2=("Inc", ["cnt := cnt + 2;"]))])
    r = merge_any(FK.PLCOPEN, base, mine, theirs)
    assert r.is_clean and paths(r.changes) == ["POU[Cont]/Action[Reset]/Corpo/Riga2"]
    assert paths(r.upstream_changes) == ["POU[Cont]/Action[Inc]/Corpo/Riga1"]
    assert r.touched_routines == ["POU[Cont]/Action[Reset]"]
    clash = proj(extra_pous=[fb(a1=("Reset", ["cnt := 99;"]))])
    rc = merge_any(FK.PLCOPEN, base, mine, clash)
    assert [c.path for c in rc.conflicts] == ["POU[Cont]/Action[Reset]/Corpo/Riga1"]
    # una nuova azione della proposta e' inserita dopo la sua vicina
    mine2 = proj(extra_pous=[P.pou("Cont", "functionBlock", P.group("localVars", [P.var("cnt", "INT")]), P.st_body(["// corpo"]),
                                   extra=P.actions(P.action("Reset", P.st_body(["cnt := 0;"])), P.action("Nuova", P.st_body(["x;"])),
                                                   P.action("Inc", P.st_body(["cnt := cnt + 1;"]))))])
    rn = merge_any(FK.PLCOPEN, base, mine2, theirs)
    assert rn.is_clean and [a.get("name") for a in merged_tree(rn).iterfind(".//p:action", NSMAP)] == ["Reset", "Nuova", "Inc"]
    assert merge_any(FK.PLCOPEN, base, base, base).merged_xml == base


def test_configuration_resource_task_and_global_vars():
    gvl = lambda *names: "".join(P.group("globalVars", [P.var(n, "INT") for n in names], ' name="GVL"') for _ in [0])
    base = proj(configs=P.config(extra_resource=gvl("g1")))
    mine = proj(configs=P.config(interval="T#10ms", extra_resource=gvl("g1", "gMia")))
    theirs = proj(configs=P.config(priority=3, extra_resource=gvl("g1", "gSua")))
    r = merge_any(FK.PLCOPEN, base, mine, theirs)
    assert r.is_clean and not r.conflicts
    t = merged_tree(r).find(".//p:task[@name='Main']", NSMAP)
    assert t.get("interval") == "T#10ms" and t.get("priority") == "3"
    assert [v.get("name") for v in merged_tree(r).iterfind(".//p:resource/p:globalVars/p:variable", NSMAP)] == ["g1", "gMia", "gSua"]
    assert "Configuration[Impianto]/Resource[Res]/Task[Main]/@interval" in paths(r.changes)
    assert any(p.startswith("Configuration[Impianto]/Resource[Res]/globalVars(name=GVL)/Variable[gSua]") for p in paths(r.upstream_changes))
    assert r.touched_routines == ["Configuration[Impianto]/Resource[Res]"]
    clash = proj(configs=P.config(interval="T#5ms", extra_resource=gvl("g1")))
    rc = merge_any(FK.PLCOPEN, base, mine, clash)
    assert [c.path for c in rc.conflicts] == ["Configuration[Impianto]/Resource[Res]/Task[Main]/@interval"]
    assert merge_any(FK.PLCOPEN, base, mine, clash, {rc.conflicts[0].id: Resolution("theirs")}).is_clean
    # nuova task aggiunta da me: inserita dopo la task nota
    extra_task = '<task name="Veloce" interval="T#2ms" priority="0"><pouInstance name="Fast" typeName="Utils"/></task>'
    r2 = merge_any(FK.PLCOPEN, base, proj(configs=P.config(extra_resource=gvl("g1") + extra_task)), base)
    assert [t.get("name") for t in merged_tree(r2).iterfind(".//p:task", NSMAP)] == ["Main", "Veloce"]


def test_header_timestamp_never_conflicts_and_other_header_fields_do():
    m = proj(modified="2026-10-05T10:00:00", author="Anna")
    t = proj(modified="2026-10-04T10:00:00", author="Anna")
    r = merge_any(FK.PLCOPEN, BASE, m, t)
    assert r.is_clean and not r.changes                       # un cambio di sola data non e' una modifica da proporre
    assert merged_tree(r).find("p:contentHeader", NSMAP).get("modificationDateTime") == "2026-10-05T10:00:00"
    rc = merge_any(FK.PLCOPEN, BASE, proj(author="Anna"), proj(author="Bruno"))
    assert [c.path for c in rc.conflicts] == ["contentHeader/@author"]
    rr = merge_any(FK.PLCOPEN, BASE, proj(author="Anna"), BASE)
    assert paths(rr.changes) == ["contentHeader/@author"]


def test_foreign_project_or_schema_version_rejected():
    with pytest.raises(L5XError, match="Nome progetto diverso"):
        merge_any(FK.PLCOPEN, BASE, proj(name="Altro"), BASE)
    with pytest.raises(L5XError, match="Versione dello schema"):
        merge_any(FK.PLCOPEN, BASE, b(P.project(ns="http://www.plcopen.org/xml/tc6_0201")), BASE)
    with pytest.raises(L5XError):
        merge_any(FK.PLCOPEN, BASE, SL.base_project().encode(), BASE)


# ------------------------------------------------------------------------------------ CODESYS: addData
def test_codesys_methods_and_project_structure_are_merged_by_name():
    def fbm(m1=("Avvia", ["Avvia := TRUE;"]), m2=("Ferma", ["Ferma := TRUE;"]), more=()):
        ms = "".join(P.method(n, ls) for n, ls in (m1, m2, *more))
        return P.pou("Pompa", "functionBlock", P.group("localVars", [P.var("x")]), P.st_body(["// pompa"]),
                     add=f"<addData>{ms}</addData>")
    base = b(P.project([fbm()], extra_root=P.project_structure(["Pompa"])))
    mine = b(P.project([fbm(m1=("Avvia", ["Avvia := TRUE;", "// mio"]))], extra_root=P.project_structure(["Pompa", "Mia"])))
    theirs = b(P.project([fbm(m2=("Ferma", ["Ferma := FALSE;"]), more=[("Nuovo", ["Nuovo := 1;"])])],
                         extra_root=P.project_structure(["Pompa", "Sua"])))
    r = merge_any(FK.PLCOPEN, base, mine, theirs)
    assert r.is_clean and not r.conflicts, [c.path for c in r.conflicts]
    root = merged_tree(r)
    assert [m.get("name") for m in root.iter("{*}Method")] == ["Avvia", "Ferma", "Nuovo"]
    assert "POU[Pompa]/Method[Avvia]/Corpo/Riga2" in paths(r.changes)
    assert "POU[Pompa]/Method[Ferma]/Corpo/Riga1" in paths(r.upstream_changes)
    assert "POU[Pompa]/Method[Nuovo]" in paths(r.upstream_changes)              # metodo nuovo del Master
    assert [o.get("Name") for o in root.iter("{*}Object")] == ["Application", "Pompa", "Mia", "Sua"]      # struttura unita' per nome
    assert "Object[Application]/Object[Mia]" in paths(r.changes)
    assert r.touched_routines == ["POU[Pompa]/Method[Avvia]", "Struttura del progetto"]
    clash = b(P.project([fbm(m1=("Avvia", ["Avvia := FALSE;"]))], extra_root=P.project_structure(["Pompa"])))
    rc = merge_any(FK.PLCOPEN, base, mine, clash)
    assert [c.path for c in rc.conflicts] == ["POU[Pompa]/Method[Avvia]/Corpo/Riga1"]


# ------------------------------------------------------------------------------------ risultato: forma, ordine, byte
def test_idempotent_merge_is_byte_identical():
    r = merge_any(FK.PLCOPEN, BASE, BASE, BASE)
    assert r.merged_xml == BASE and r.is_clean and not r.changes and not r.upstream_changes
    # anche con BOM + CRLF + rientro a tab (file "alla CODESYS" su Windows)
    crlf = b"\xef\xbb\xbf" + re.sub(rb"\n( +)", lambda m: b"\r\n" + b"\t" * (len(m.group(1)) // 2), BASE).replace(b"\n", b"\r\n").replace(b"\r\r\n", b"\r\n")
    assert merge_any(FK.PLCOPEN, crlf, crlf, crlf).merged_xml == crlf
    # io non ho cambiato nulla, il Master si': il risultato e' il Master, byte per byte
    theirs = proj(utils=utils_pou(["Scala := 7;", "// fine"]))
    r2 = merge_any(FK.PLCOPEN, BASE, BASE, theirs)
    assert r2.merged_xml == theirs and not r2.changes and len(r2.upstream_changes) == 1
    # la stessa modifica da entrambe le parti
    assert merge_any(FK.PLCOPEN, BASE, theirs, theirs).merged_xml == theirs


def test_result_keeps_declaration_namespaces_bom_eol_and_indent():
    tabbed = re.sub(rb"\n( +)", lambda m: b"\r\n" + b"\t" * (len(m.group(1)) // 2), BASE).replace(b"\n", b"\r\n").replace(b"\r\r\n", b"\r\n")
    base = b"\xef\xbb\xbf" + tabbed
    mine = base.replace(b"Scala := Valore * 2;", b"Scala := Valore * 5;")
    r = merge_plcopen(base, mine, base)
    out = r.merged_xml
    assert out.startswith(b'\xef\xbb\xbf<?xml version="1.0" encoding="utf-8"?>\r\n<project xmlns="' + P.NS.encode())
    assert out.count(b"\n") == out.count(b"\r\n") and out.endswith(b"\r\n")        # niente fine riga misti
    assert b"\r\n\t<fileHeader" in out and b"\r\n\t\t<contentHeader" not in out        # rientro a tab del Master
    assert b"xmlns:xhtml=" in out and out.count(b"xmlns=") == 1 and b"<xhtml:p>" in out and b"ns0:" not in out
    assert b"<![CDATA[" in out and b"Valore * 5;" in out
    root = etree.fromstring(out)                                                   # riaperto dal parser
    assert root.tag == f"{{{P.NS}}}project" and root.nsmap[None] == P.NS and root.nsmap["xhtml"] == P.XHTML
    # senza dichiarazione XML nel Master non se ne aggiunge una
    nodecl = b(P.project(decl=False))
    rn = merge_plcopen(nodecl, nodecl.replace(b"Valore * 2;", b"Valore * 4;"), nodecl)
    assert rn.merged_xml.startswith(b"<project") and b"Valore * 4;" in rn.merged_xml


def test_order_of_master_kept_and_new_items_after_known_neighbour():
    new_a = P.pou("DopoUtils", "function", "<returnType><INT/></returnType>", P.st_body(["x := 1;"]))
    mine = b(P.project([main_pou(), utils_pou(), new_a, motor_pou(), ld_pou()]))
    new_m = P.pou("InFondo", "function", "<returnType><INT/></returnType>", P.st_body(["x := 2;"]))
    theirs = b(P.project([main_pou(), utils_pou(), motor_pou(), ld_pou(), new_m]))
    r = merge_any(FK.PLCOPEN, BASE, mine, theirs)
    assert r.is_clean and pou_names(r) == ["PLC_PRG", "Utils", "DopoUtils", "MotorFB", "Pannello", "InFondo"]
    # variabili: la nuova variabile della proposta segue la sua vicina
    m2 = proj(main=main_pou(extra_vars=[P.var("Zeta", "INT")]))
    m2 = b(P.project([P.pou("PLC_PRG", "program", P.group("localVars", [P.var("Start"), P.var("Nuova", "INT"),
                                                                       P.var("Contatore", "INT", "0"), P.var("Motore", "MotorFB")]),
                            P.st_body(P.MAIN_ST)), utils_pou(), motor_pou(), ld_pou()]))
    t2 = proj(utils=utils_pou(["Scala := 0;", "// fine"]))
    r2 = merge_any(FK.PLCOPEN, BASE, m2, t2)
    names = [v.get("name") for v in merged_tree(r2).iterfind(".//p:pou[@name='PLC_PRG']//p:variable", NSMAP)]
    assert names == ["Start", "Nuova", "Contatore", "Motore"]
    # elementi nuovi in un contenitore assente nel Master (es. nuova sezione di interfaccia): ordine dello schema
    m3 = b(P.project([P.pou("PLC_PRG", "program", P.group("localVars", [P.var("Start"), P.var("Contatore", "INT", "0"), P.var("Motore", "MotorFB")])
                            + P.group("outputVars", [P.var("Out")]), P.st_body(P.MAIN_ST)), utils_pou(), motor_pou(), ld_pou()]))
    r3 = merge_any(FK.PLCOPEN, BASE, m3, BASE)
    iface = merged_tree(r3).find(".//p:pou[@name='PLC_PRG']/p:interface", NSMAP)
    assert [etree.QName(c).localname for c in iface] == ["localVars", "outputVars"]


def test_cdata_vs_plain_text_bodies_and_empty_st():
    plain = BASE.replace(b"<![CDATA[Scala := Valore * 2;\n// fine]]>", b"Scala := Valore * 2;\n// fine")
    assert plain != BASE
    mine = plain.replace(b"Scala := Valore * 2;", b"Scala := Valore * 9;")
    r = merge_any(FK.PLCOPEN, plain, mine, plain)
    assert r.is_clean and b"Valore * 9;" in r.merged_xml and b"<![CDATA[Scala" not in r.merged_xml    # resta testo semplice
    # corpo ST vuoto nel Master, il codice lo scrivo io
    empty_utils = P.pou("Utils", "function", "<returnType><INT/></returnType>", "<body><ST/></body>")
    base = proj(utils=empty_utils)
    r2 = merge_any(FK.PLCOPEN, base, BASE, base)
    assert r2.is_clean and st_lines(r2, "Utils") == P.UTILS_ST
    # il codice contiene ]]> : niente CDATA, ma testo escapato
    tricky = proj(utils=P.pou("Utils", "function", "<returnType><INT/></returnType>" + P.group("inputVars", [P.var("Valore", "INT")])
                              + P.group("localVars", [P.var("Scala", "INT")]),
                              "<body><ST><xhtml:p>a := b[c[1]]&gt;0;\n// fine</xhtml:p></ST></body>"))
    r3 = merge_any(FK.PLCOPEN, BASE, tricky, BASE)
    assert st_lines(r3, "Utils")[0] == "a := b[c[1]]>0;"


def test_mixed_content_documentation_is_not_reindented():
    doc = BASE.replace(b"<![CDATA[Abilita il motore]]>", b"Abilita <xhtml:b>il</xhtml:b> motore")
    r = merge_any(FK.PLCOPEN, doc, doc.replace(b"Valore * 2", b"Valore * 5"), doc)
    assert r.is_clean and b"Abilita <xhtml:b>il</xhtml:b> motore</xhtml:p>" in r.merged_xml and b"Valore * 5;" in r.merged_xml


def test_xxe_and_doctype_rejected():
    evil = BASE.replace(b"<project", b'<!DOCTYPE project [<!ENTITY e SYSTEM "file:///etc/passwd">]><project', 1)
    evil = evil.replace(b"Scala", b"&e;Scala", 1)
    with pytest.raises(L5XError, match="entita|DOCTYPE"):
        merge_any(FK.PLCOPEN, BASE, evil, BASE)
    with pytest.raises(L5XError):
        parse_plcopen(evil)
    dtd = BASE.replace(b"<project", b'<!DOCTYPE project SYSTEM "http://example.invalid/x.dtd"><project', 1)
    with pytest.raises(L5XError, match="DOCTYPE"):
        parse_plcopen(dtd)


# ------------------------------------------------------------------------------------ server end-to-end
def _plc_project(c, H, users, name="Impianto PLCopen", content=BASE, filename="Impianto.xml"):
    pid = c.post("/api/projects", headers=H["admin"], json={"name": name}).json()["id"]
    for n, role in [("lead", "Lead_Engineer"), ("dev1", "Developer"), ("dev2", "Developer"), ("viewer", "Viewer")]:
        assert c.put(f"/api/projects/{pid}/permissions", headers=H["admin"],
                     json={"user_id": users[n], "role": role}).status_code == 200
    r = c.post(f"/api/projects/{pid}/master/init", headers=H["admin"], files={"file": (filename, content)})
    return pid, r


def _propose(c, pid, h, content, base=1, name="mio.xml", msg="PLCopen"):
    return c.post(f"/api/projects/{pid}/proposals", headers=h, data={"base_revision": base, "message": msg},
                  files={"file": (name, content)})


def test_api_plcopen_master_end_to_end(world):
    c, app, _l5x_pid, H, users = world
    pid, r = _plc_project(c, H, users)
    assert r.status_code == 200, r.text
    j = r.json()
    assert (j["kind"], j["ext"], j["name"], j["revision"]) == ("plcopen", ".xml", "Impianto", 1)
    proj_row = [p for p in c.get("/api/projects", headers=H["admin"]).json() if p["id"] == pid][0]
    assert proj_row["master_kind"] == "plcopen" and proj_row["master_ext"] == ".xml"
    d = c.get(f"/api/projects/{pid}/master/download", headers=H["viewer"])
    assert d.content == BASE and d.headers["content-type"].startswith("application/xml")
    assert d.headers["content-disposition"].endswith('Impianto_rev1.xml"')
    ev = c.get("/api/audit", headers=H["admin"], params={"action": "master_initialized"}).json()
    assert any(e["details"].get("kind") == "plcopen" and e["details"].get("project_name") == "Impianto" for e in ev)

    online(c, pid, H, "lead", "dev1")
    mine = proj(utils=utils_pou(["Scala := Valore * 3;", "// fine"]))
    rp = _propose(c, pid, H["lead"], mine)
    assert rp.status_code == 201, rp.text
    prop = rp.json()
    assert prop["n_changes"] == 1 and prop["n_conflicts"] == 0 and not prop["needs_lead_approval"]
    base = f"/api/projects/{pid}/proposals/{prop['id']}"
    pv = c.get(base + "/preview", headers=H["dev1"])
    assert pv.status_code == 200 and b"Valore * 3;" in pv.content and pv.headers["content-disposition"].endswith('_anteprima.xml"')
    assert etree.fromstring(pv.content).tag.endswith("}project")
    detail = c.get(base, headers=H["dev1"]).json()
    assert any("POU[Utils]" in ch["path"] for ch in detail["changes"]) and detail["touched"] == ["POU[Utils]"]
    c.post(base + "/approve", headers=H["lead"], json={})
    d2 = c.post(base + "/approve", headers=H["dev1"], json={}).json()
    assert d2["state"] == "merged" and d2["result_revision"] == 2
    got = c.get(f"/api/projects/{pid}/master/download", headers=H["viewer"])
    assert b"Valore * 3;" in got.content and got.headers["content-disposition"].endswith('Impianto_rev2.xml"')
    assert [h["revision"] for h in c.get(f"/api/projects/{pid}/master/history", headers=H["viewer"]).json()] == [2, 1]

    # una seconda proposta basata sulla rev 1 (a Master avanzato) con modifica in un altro POU: merge pulito, e un conflitto
    other = proj(motor=motor_pou(speed="12"))
    rp2 = _propose(c, pid, H["lead"], other, base=1)
    assert rp2.status_code == 201 and rp2.json()["n_conflicts"] == 0 and rp2.json()["n_changes"] == 1
    clash = proj(utils=utils_pou(["Scala := Valore * 4;", "// fine"]))
    rp3 = _propose(c, pid, H["lead"], clash, base=1)
    assert rp3.status_code == 201 and rp3.json()["state"] == "conflict" and rp3.json()["n_unresolved"] == 1
    base3 = f"/api/projects/{pid}/proposals/{rp3.json()['id']}"
    cid = c.get(base3, headers=H["lead"]).json()["conflicts"][0]["id"]
    ok = c.post(base3 + "/resolutions", headers=H["lead"], json={"resolutions": {cid: {"choice": "mine"}}})
    assert ok.status_code == 200 and ok.json()["state"] == "awaiting_consent"
    assert b"Valore * 4;" in c.get(base3 + "/preview", headers=H["lead"]).content


def test_api_plcopen_wrong_kinds_and_invalid_files(world):
    c, app, _pid0, H, users = world
    pid, r = _plc_project(c, H, users)
    assert r.status_code == 200
    online(c, pid, H, "lead")
    for name, content in (("x.L5X", SL.dev_a().encode()), ("x.xml", b"<root><a/></root>"), ("x.L5K", l5k().encode())):
        rp = _propose(c, pid, H["lead"], content, name=name)
        assert rp.status_code == 422 and "Master del progetto" in rp.json()["detail"], rp.text
    foreign = _propose(c, pid, H["lead"], proj(name="Altro progetto", utils=utils_pou(["x := 1;"])))
    assert foreign.status_code == 422 and "Nome progetto diverso" in foreign.json()["detail"]
    assert _propose(c, pid, H["lead"], BASE).status_code == 422                       # nessuna modifica rispetto al Master
    # init: XML PLCopen non valido, con DOCTYPE/entita', e L5X dentro un progetto PLCopen reimportato
    for bad in (BASE[:-20], BASE.replace(b"<project", b'<!DOCTYPE p [<!ENTITY e SYSTEM "file:///etc/passwd">]><project', 1)):
        p2, r2 = _plc_project(c, H, users, name="Cattivo", content=bad)
        assert r2.status_code == 422, r2.text


def test_api_plcopen_reimport_requires_same_project(world):
    c, app, _pid0, H, users = world
    pid, r = _plc_project(c, H, users)
    assert r.status_code == 200

    def reimport(content, name="Impianto_v2.xml", reason="nuova esportazione da CODESYS"):
        return c.post(f"/api/projects/{pid}/master/reimport", headers=H["lead"], data={"reason": reason},
                      files={"file": (name, content)})
    diff_name = reimport(proj(name="Altro impianto"))
    assert diff_name.status_code == 422 and "Altro impianto" in diff_name.json()["detail"] and "un altro progetto" in diff_name.json()["detail"]
    diff_company = reimport(proj(company="Beta Srl", utils=utils_pou(["x := 1;"])))
    assert diff_company.status_code == 422 and "Beta Srl" in diff_company.json()["detail"]
    assert reimport(SL.dev_a().encode(), name="a.L5X").status_code == 422             # tipo diverso
    assert reimport(b"<root/>", name="a.xml").status_code == 422
    assert reimport(BASE).status_code == 422                                          # identico al Master
    ok = reimport(proj(utils=utils_pou(["Scala := 1;", "// fine"])), name="Impianto_v2_rev9.xml")
    assert ok.status_code == 200, ok.text
    assert ok.json()["revision"] == 2 and ok.json()["kind"] == "plcopen" and ok.json()["name"] == "Impianto_v2" \
        and ok.json()["ext"] == ".xml"
    # anche con un'estensione diversa da quella del Master, il contenuto decide
    ok2 = reimport(proj(utils=utils_pou(["Scala := 2;", "// fine"])), name="export.XML")
    assert ok2.status_code == 200 and ok2.json()["ext"] == ".xml"


def test_api_old_text_project_still_accepts_plcopen_content(world):
    """Un progetto creato come «testo» (prima del riconoscimento PLCopen) non rifiuta file ora riconosciuti come PLCopen."""
    c, app, _pid0, H, users = world
    pid, r = _plc_project(c, H, users, name="Vecchio", content=b"<a>uno</a>\n<b>due</b>\n", filename="vecchio.xml")
    assert r.status_code == 200 and r.json()["kind"] == "text"
    online(c, pid, H, "lead")
    # un file PLCopen su un Master di testo: accettato dal controllo di tipo (poi si unisce per righe)
    rp = _propose(c, pid, H["lead"], BASE)
    assert rp.status_code == 201, rp.text


# ------------------------------------------------------------------------------------ client: file locali
def test_candidates_for_plcopen_project(tmp_path):
    from client.localfiles import find_merge_candidates, looks_like_kind, pick_default, ref_path
    f = tmp_path / "w"
    f.mkdir()
    (f / ".aps").mkdir()
    ref = ref_path(f, "Impianto", 3, ".xml")
    ref.write_bytes(BASE)
    old = time.time() - 100
    os.utime(ref, (old, old))
    (f / "Impianto_rev3.xml").write_bytes(BASE)                                      # copia scaricata e mai toccata
    os.utime(f / "Impianto_rev3.xml", (old, old))
    (f / "Impianto_rev2.xml").write_bytes(BASE.replace(b"Valore * 2", b"Valore * 0"))   # revisione vecchia, diversa dal riferimento
    (f / "mio.xml").write_bytes(BASE.replace(b"Valore * 2", b"Valore * 8"))
    (f / "altro.xml").write_bytes(b"<doc><x/></doc>")
    (f / "vecchio.L5X").write_text(SL.dev_a(), "utf-8")
    cands = {x.name: x for x in find_merge_candidates(f, ext=".xml", kind="plcopen", name="Impianto", base_rev=3)}
    assert set(cands) == {"mio.xml", "altro.xml", "Impianto_rev2.xml"}                # niente copia pulita, niente .L5X
    assert cands["mio.xml"].looks_ok and cands["mio.xml"].modified and not cands["altro.xml"].looks_ok
    best = pick_default(find_merge_candidates(f, ext=".xml", kind="plcopen", name="Impianto", base_rev=3))
    assert best is not None and best.name in ("mio.xml", "Impianto_rev2.xml") and best.looks_ok
    assert looks_like_kind(f / "mio.xml", "plcopen") and not looks_like_kind(f / "altro.xml", "plcopen")
    assert not looks_like_kind(f / "vecchio.L5X", "plcopen") and looks_like_kind(f / "vecchio.L5X", "l5x")
    allf = {x.name for x in find_merge_candidates(f, ext=".xml", kind="plcopen", name="Impianto", base_rev=3, all_files=True)}
    assert "vecchio.L5X" in allf
    from common.filekinds import rev_filename
    assert rev_filename("Impianto", 4, ".xml") == "Impianto_rev4.xml"


def test_merge_dialog_recognizes_plcopen(qapp, tmp_path):
    from client.views.merge_dialog import MergeRequestDialog
    f = tmp_path / "p"
    f.mkdir()
    (f / "bene.xml").write_bytes(BASE.replace(b"Valore * 2", b"Valore * 5"))
    (f / "male.xml").write_bytes(b"<doc/>")
    d = MergeRequestDialog(f, "", 1, "Impianto", ext=".xml", kind="plcopen")
    assert d.table.rowCount() == 2
    d.select_path(f / "bene.xml")
    assert d.ok_btn.isEnabled()
    d.select_path(f / "male.xml")
    assert not d.ok_btn.isEnabled() and "PLCopen" in d.info.text()
    d.close()
