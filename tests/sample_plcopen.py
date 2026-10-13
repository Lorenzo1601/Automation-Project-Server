"""Generatore di file PLCopen XML (TC6, stile export CODESYS V3) di esempio: piccoli ma con tutte le parti che contano.

Progetto base (``base_project()``): POU ``PLC_PRG`` (program, ST), ``Utils`` (function, ST), ``MotorFB`` (functionBlock con
variabili, ST), ``Pannello`` (program, LD), DUT ``ST_Motore`` (struct) e ``E_Stato`` (enum), configuration ``Impianto``
con resource ``Res`` e task ``Main``.
"""
from __future__ import annotations

import zlib
from typing import Optional

NS = "http://www.plcopen.org/xml/tc6_0200"
XHTML = "http://www.w3.org/1999/xhtml"

MAIN_ST = ["IF Start THEN", "    Motore.Enable := TRUE;", "END_IF", "", "Motore(Speed := 100);", "Contatore := Contatore + 1;"]
UTILS_ST = ["Scala := Valore * 2;", "// fine"]
MOTOR_ST = ["IF Enable THEN", "    Running := TRUE;", "ELSE", "    Running := FALSE;", "END_IF"]


def var(name: str, typ: str = "BOOL", init: Optional[str] = None, doc: Optional[str] = None,
        address: Optional[str] = None) -> str:
    a = f' address="{address}"' if address else ""
    i = f'<initialValue><simpleValue value="{init}"/></initialValue>' if init is not None else ""
    d = f'<documentation><xhtml:p><![CDATA[{doc}]]></xhtml:p></documentation>' if doc else ""
    t = f"<{typ}/>" if typ.isupper() else f'<derived name="{typ}"/>'
    return f'<variable name="{name}"{a}><type>{t}</type>{i}{d}</variable>'


def group(tag: str, vars_: list[str], attrs: str = "") -> str:
    return f"<{tag}{attrs}>" + "".join(vars_) + f"</{tag}>"


def st_body(lines: list[str]) -> str:
    return f"<body><ST><xhtml:p><![CDATA[{chr(10).join(lines)}]]></xhtml:p></ST></body>"


def ld_body(contacts: list[str]) -> str:
    items = "".join(
        f'<contact localId="{i + 1}" negated="false"><position x="{20 + 40 * i}" y="10"/>'
        f'<connectionPointIn/><connectionPointOut/><variable>{c}</variable></contact>' for i, c in enumerate(contacts))
    return (f'<body><LD><leftPowerRail localId="0"><position x="0" y="0"/><connectionPointOut formalParameter="none"/>'
            f'</leftPowerRail>{items}</LD></body>')


def pou(name: str, pou_type: str, interface: str, body: str, extra: str = "", add: str = "") -> str:
    """``extra`` (actions / transitions) sta prima del corpo, ``add`` (addData) dopo, come nello schema TC6."""
    return f'<pou name="{name}" pouType="{pou_type}"><interface>{interface}</interface>{extra}{body}{add}</pou>'


def action(name: str, body: str) -> str:
    return f'<action name="{name}">{body}</action>'


def actions(*acts: str) -> str:
    return "<actions>" + "".join(acts) + "</actions>"


def method(name: str, lines: list[str], ret: str = "BOOL") -> str:
    """Un metodo CODESYS: ``<data name=".../method"><Method>`` dentro addData del POU."""
    return ('<data name="http://www.3s-software.com/plcopenxml/method" handleUnknown="implementation">'
            f'<Method name="{name}" ObjectId="{zlib.crc32(name.encode()):08x}">'
            f'<interface><returnType><{ret}/></returnType></interface>{st_body(lines)}</Method></data>')


def project_structure(objects: list[str]) -> str:
    """addData di progetto con la struttura ad albero CODESYS (``objects`` = nomi sotto ``Application``)."""
    objs = "".join(f'<Object Name="{o}" ObjectId="id-{o}"/>' for o in objects)
    return ('<addData><data name="http://www.3s-software.com/plcopenxml/projectstructure" handleUnknown="discard">'
            f'<ProjectStructure><Object Name="Application" ObjectId="id-app">{objs}</Object></ProjectStructure>'
            '</data></addData>\n')


def dut_struct(name: str, members: list[tuple[str, str]]) -> str:
    ms = "".join(var(n, t) for n, t in members)
    return f'<dataType name="{name}"><baseType><struct>{ms}</struct></baseType></dataType>'


def dut_enum(name: str, values: list[tuple[str, int]]) -> str:
    vs = "".join(f'<value name="{n}" value="{v}"/>' for n, v in values)
    return f'<dataType name="{name}"><baseType><enum><values>{vs}</values></enum></baseType></dataType>'


def config(interval: str = "T#20ms", priority: int = 1, extra_resource: str = "", name: str = "Impianto",
           instance: str = "PLC_PRG_Inst") -> str:
    return (f'<configuration name="{name}"><resource name="Res">'
            f'<task name="Main" interval="{interval}" priority="{priority}">'
            f'<pouInstance name="{instance}" typeName="PLC_PRG"/></task>{extra_resource}'
            f'</resource></configuration>')


def base_pous() -> list[str]:
    return [
        pou("PLC_PRG", "program",
            group("localVars", [var("Start"), var("Contatore", "INT", "0"), var("Motore", "MotorFB")]),
            st_body(MAIN_ST)),
        pou("Utils", "function",
            "<returnType><INT/></returnType>" + group("inputVars", [var("Valore", "INT")])
            + group("localVars", [var("Scala", "INT")]),
            st_body(UTILS_ST)),
        pou("MotorFB", "functionBlock",
            group("inputVars", [var("Enable", "BOOL", "FALSE", doc="Abilita il motore"), var("Speed", "INT", "0")])
            + group("outputVars", [var("Running")]),
            st_body(MOTOR_ST)),
        pou("Pannello", "program", group("localVars", [var("Lampada")]), ld_body(["Start", "Lampada"])),
    ]


def base_dts() -> list[str]:
    return [dut_struct("ST_Motore", [("Corrente", "REAL"), ("Giri", "INT")]),
            dut_enum("E_Stato", [("Fermo", 0), ("Marcia", 1), ("Errore", 2)])]


def project(pous: Optional[list[str]] = None, dts: Optional[list[str]] = None, configs: Optional[str] = None,
            name: str = "Impianto", company: str = "Acme", modified: str = "2026-10-01T10:00:00",
            author: str = "", ns: str = NS, decl: bool = True, extra_root: str = "") -> str:
    pous = base_pous() if pous is None else pous
    dts = base_dts() if dts is None else dts
    configs = config() if configs is None else configs
    head = '<?xml version="1.0" encoding="utf-8"?>\n' if decl else ""
    return (f'{head}<project xmlns="{ns}" xmlns:xhtml="{XHTML}">\n'
            f'  <fileHeader companyName="{company}" productName="CODESYS" productVersion="CODESYS V3.5 SP19" '
            f'creationDateTime="2026-01-01T08:00:00"/>\n'
            f'  <contentHeader name="{name}" modificationDateTime="{modified}" author="{author}">\n'
            f'    <coordinateInfo><fbd><scaling x="1" y="1"/></fbd><ld><scaling x="1" y="1"/></ld>'
            f'<sfc><scaling x="1" y="1"/></sfc></coordinateInfo>\n'
            f'  </contentHeader>\n'
            f'  <types>\n    <dataTypes>\n      ' + "\n      ".join(dts) + '\n    </dataTypes>\n'
            f'    <pous>\n      ' + "\n      ".join(pous) + '\n    </pous>\n  </types>\n'
            f'  <instances><configurations>{configs}</configurations></instances>\n{extra_root}'
            f'</project>\n')


def base_project(**kw) -> str:
    return project(**kw)


def b(s: str) -> bytes:
    return s.encode("utf-8")
