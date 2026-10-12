"""Generatore di file L5X di esempio (struttura minima ma realistica)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

HEADER = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
          '<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="35.00" TargetName="{name}" '
          'TargetType="Controller" ContainsContext="false" ExportDate="Mon Oct 12 2026" '
          'ExportOptions="NoRawData L5KData DecoratedData ForceProtectedEncoding AllProjDocTrans">\n')


def rung(n: int, text: str, comment: Optional[str] = None) -> str:
    c = f"<Comment><![CDATA[{comment}]]></Comment>" if comment else ""
    return f'<Rung Number="{n}" Type="N">{c}<Text><![CDATA[{text}]]></Text></Rung>'


def rll_routine(name: str, rungs: list[str]) -> str:
    body = "\n".join(rung(i, t) for i, t in enumerate(rungs))
    return (f'<Routine Name="{name}" Type="RLL"><RLLContent>\n{body}\n</RLLContent></Routine>')


def st_routine(name: str, lines: list[str]) -> str:
    body = "\n".join(f'<Line Number="{i}"><![CDATA[{t}]]></Line>' for i, t in enumerate(lines))
    return f'<Routine Name="{name}" Type="ST"><STContent>\n{body}\n</STContent></Routine>'


def tag(name: str, dtype: str = "BOOL", desc: str = "", value: str = "0") -> str:
    d = f"<Description><![CDATA[{desc}]]></Description>" if desc else ""
    return (f'<Tag Name="{name}" TagType="Base" DataType="{dtype}" Radix="Decimal" '
            f'Constant="false" ExternalAccess="Read/Write">{d}'
            f'<Data Format="L5K"><![CDATA[{value}]]></Data></Tag>')


def udt(name: str, members: list[tuple[str, str]]) -> str:
    ms = "\n".join(f'<Member Name="{n}" DataType="{t}" Dimension="0" Radix="Decimal" '
                   f'Hidden="false" ExternalAccess="Read/Write"/>' for n, t in members)
    return f'<DataType Name="{name}" Family="NoFamily" Class="User"><Members>\n{ms}\n</Members></DataType>'


def aoi(name: str, rungs: list[str], params: list[str]) -> str:
    ps = "\n".join(f'<Parameter Name="{p}" TagType="Base" DataType="BOOL" Usage="Input" Radix="Decimal" '
                   f'Required="true" Visible="true" ExternalAccess="Read/Write"/>' for p in params)
    return (f'<AddOnInstructionDefinition Name="{name}" Class="Standard" Revision="1.0" '
            f'ExecutePrescan="false" ExecutePostscan="false" ExecuteEnableInFalse="false">'
            f'<Parameters>\n{ps}\n</Parameters><LocalTags/>'
            f'<Routines>{rll_routine("Logic", rungs)}</Routines></AddOnInstructionDefinition>')


def program(name: str, tags: list[str], routines: list[str], main: str = "MainRoutine") -> str:
    return (f'<Program Name="{name}" TestEdits="false" MainRoutineName="{main}" Disabled="false" '
            f'UseAsFolder="false"><Tags>\n' + "\n".join(tags) + '\n</Tags><Routines>\n'
            + "\n".join(routines) + '\n</Routines></Program>')


def project(name: str = "PLC_Test", *, udts=(), aois=(), ctrl_tags=(), programs=(), tasks="",
            proc_type: str = "1756-L83ES") -> str:
    return (HEADER.format(name=name)
            + f'<Controller Use="Target" Name="{name}" ProcessorType="{proc_type}" MajorRev="35" MinorRev="11">\n'
            + "<DataTypes>\n" + "\n".join(udts) + "\n</DataTypes>\n"
            + "<Modules><Module Name=\"Local\" CatalogNumber=\"1756-L83ES\" Vendor=\"1\" ProductType=\"14\"/></Modules>\n"
            + "<AddOnInstructionDefinitions>\n" + "\n".join(aois) + "\n</AddOnInstructionDefinitions>\n"
            + "<Tags>\n" + "\n".join(ctrl_tags) + "\n</Tags>\n"
            + "<Programs>\n" + "\n".join(programs) + "\n</Programs>\n"
            + (tasks or '<Tasks><Task Name="MainTask" Type="CONTINUOUS" Priority="10" Watchdog="500" '
                        'DisableUpdateOutputs="false" InhibitTask="false"><ScheduledPrograms>'
                        '<ScheduledProgram Name="Main"/></ScheduledPrograms></Task></Tasks>\n')
            + "</Controller>\n</RSLogix5000Content>\n")


MAIN_RUNGS = [
    "XIC(Start)XIO(Stop)OTE(Motor);",
    "XIC(Motor)TON(T1,?,?);",
    "XIC(T1.DN)OTL(Done);",
    "XIC(Alarm)OTE(Horn);",
    "XIC(Reset)OTU(Done);",
    "NOP();",
]


def base_project() -> str:
    return project(
        udts=[udt("Motore_UDT", [("Run", "BOOL"), ("Speed", "DINT")])],
        aois=[aoi("Blocco_AOI", ["XIC(In)OTE(Out);"], ["In"])],
        ctrl_tags=[tag("Global_Start"), tag("Counter", "DINT", "Contatore pezzi", "0")],
        programs=[
            program("Main", [tag("Start"), tag("Stop"), tag("Motor")],
                    [rll_routine("MainRoutine", MAIN_RUNGS),
                     st_routine("Calc", ["Speed := 10;", "IF Run THEN", "  Speed := 20;", "END_IF;"])]),
            program("Safety", [tag("EStop")], [rll_routine("MainRoutine", ["XIC(EStop)OTE(SafeOut);"])]),
        ],
    )


def dev_a() -> str:
    """A: modifica rung 0, aggiunge rung in coda, nuovo tag in Main, nuova routine, nuovo UDT member."""
    rungs = list(MAIN_RUNGS)
    rungs[0] = "XIC(Start)XIO(Stop)XIO(Fault)OTE(Motor);"
    rungs.append("XIC(Motor)OTE(Lamp);")
    return project(
        udts=[udt("Motore_UDT", [("Run", "BOOL"), ("Speed", "DINT"), ("Fault", "BOOL")])],
        aois=[aoi("Blocco_AOI", ["XIC(In)OTE(Out);"], ["In"])],
        ctrl_tags=[tag("Global_Start"), tag("Counter", "DINT", "Contatore pezzi", "0")],
        programs=[
            program("Main", [tag("Start"), tag("Stop"), tag("Motor"), tag("Fault"), tag("Lamp")],
                    [rll_routine("MainRoutine", rungs),
                     st_routine("Calc", ["Speed := 10;", "IF Run THEN", "  Speed := 20;", "END_IF;"]),
                     rll_routine("Diagnostica", ["XIC(Fault)OTE(Horn);"])]),
            program("Safety", [tag("EStop")], [rll_routine("MainRoutine", ["XIC(EStop)OTE(SafeOut);"])]),
        ],
    )


def dev_b(conflict: bool = False) -> str:
    """B: modifica rung 3 (senza conflitto) e, se conflict, anche rung 0 e UDT; aggiunge tag globale."""
    rungs = list(MAIN_RUNGS)
    rungs[3] = "XIC(Alarm)XIC(Enable)OTE(Horn);"
    udt_members = [("Run", "BOOL"), ("Speed", "DINT")]
    if conflict:
        rungs[0] = "XIC(Start)XIO(Stop)XIC(Enable)OTE(Motor);"
        udt_members.append(("Torque", "REAL"))
    return project(
        udts=[udt("Motore_UDT", udt_members)],
        aois=[aoi("Blocco_AOI", ["XIC(In)OTE(Out);"], ["In"])],
        ctrl_tags=[tag("Global_Start"), tag("Counter", "DINT", "Contatore pezzi", "0"), tag("Enable")],
        programs=[
            program("Main", [tag("Start"), tag("Stop"), tag("Motor")],
                    [rll_routine("MainRoutine", rungs),
                     st_routine("Calc", ["Speed := 10;", "IF Run THEN", "  Speed := 20;", "END_IF;"])]),
            program("Safety", [tag("EStop")], [rll_routine("MainRoutine", ["XIC(EStop)OTE(SafeOut);"])]),
        ],
    )


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "samples"
    out.mkdir(exist_ok=True)
    (out / "Master_rev1_base.L5X").write_text(base_project(), encoding="utf-8")
    (out / "DevA.L5X").write_text(dev_a(), encoding="utf-8")
    (out / "DevB_senza_conflitti.L5X").write_text(dev_b(False), encoding="utf-8")
    (out / "DevB_con_conflitti.L5X").write_text(dev_b(True), encoding="utf-8")
    print("Esempi scritti in", out)
