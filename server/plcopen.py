"""Merge 3-way *strutturale* per file PLCopen XML (standard TC6: export/import XML di CODESYS V3 e di altri IDE IEC 61131-3).

Stessa terminologia e stesso formato di risultato di ``server.merger`` (``base`` / ``mine`` / ``theirs`` = Master corrente;
``Change`` / ``Conflict`` / ``MergeResult`` / ``Resolution``), cosi' consenso, conflitti, anteprima e storico funzionano
identici per ogni tipo di file.

Unita' di merge (chiave = nome, *case-sensitive* come in CODESYS)::

    project
    +- fileHeader, contentHeader   attributi uno per uno (modificationDateTime: vince sempre il piu' recente, mai conflitto)
    +- types/dataTypes/dataType[@name]        struct -> membri per nome; enum -> valori per nome; altro: unita' atomica
    +- types/pous/pou[@name]                  (@pouType e' un attributo)
    |   +- interface/<inputVars|outputVars|localVars|...>/variable[@name]   campi: type, initialValue, documentation, @address
    |   +- body  ST/IL  -> merge per RIGHE con il contesto del POU;   LD/FBD/SFC -> unita' atomica
    |   +- actions/action[@name], transitions/transition[@name]            (sotto-unita' con corpo e variabili proprie)
    |   +- addData      vendor-specific: un'unita' atomica per ``data`` (in piu' Method / Property di CODESYS per nome)
    +- instances/configurations/configuration[@name]/resource[@name]/task[@name]/pouInstance[@name], globalVars per variabile

Regola 3-way come nel resto del programma: e' conflitto solo se *entrambi* hanno cambiato la stessa unita' in modo
diverso; add/add e modify/delete come negli altri merger. Tutto cio' che non e' descritto qui e' confrontato come testo
canonico (c14n) dell'elemento: sempre corretto, solo meno fine.

Il file risultante mantiene dichiarazione XML, codifica, BOM, namespace/prefissi, rientro e fine riga del Master; se il
risultato coincide con il Master, il Master viene restituito *byte per byte*.
"""
from __future__ import annotations

import copy
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Optional

from lxml import etree

from server.merger import (Change, Conflict, L5XError, MergeResult, Resolution, _cid, _fp, _j, _Merger, _parser, diff3)

PLCOPEN_NS = "http://www.plcopen.org/xml/tc6"
XHTML_NS = "http://www.w3.org/1999/xhtml"
LANGS = ("ST", "IL", "LD", "FBD", "SFC", "CFC")
TEXT_LANGS = ("ST", "IL")
VAR_GROUPS = ("inputVars", "outputVars", "inOutVars", "localVars", "tempVars", "externalVars", "globalVars")
TIMESTAMP_ATTRS = ("modificationDateTime",)         # vince il piu' recente, senza conflitto
_MAX_WARN_REFS = 8


# --------------------------------------------------------------------------
# Specifica della struttura
# --------------------------------------------------------------------------
@dataclass
class Kid:
    """Come trattare un figlio con un certo nome (locale) dentro un tipo di nodo."""
    key: Optional[str] = None       # attributo chiave; "sig" = firma degli attributi; "data" = chiave dei <data> di addData
    deep: Any = None                # tipo di nodo per la ricorsione (str o funzione elemento -> str); None = atomico
    slot: Optional[str] = None      # identificativo dello slot, se diverso dal nome (es. "lang" per ST/IL/LD/FBD/SFC)
    special: Optional[str] = None   # "lang" = corpo di un POU
    seg: Optional[str] = None       # segmento del path (se diverso dal predefinito)


_DEF = Kid()
_VARS = {g: Kid(key="sig", deep="vars") for g in VAR_GROUPS}
_ADD = Kid(deep="addData")
_ID_KIND = Kid(key="name", deep="variable")


def _data_deep(el: etree._Element) -> Optional[str]:
    name = (el.get("name") or "").lower()
    if name.endswith("/projectstructure"):
        return "data_ps"
    if name.endswith("/method"):
        return "data_method"
    if name.endswith("/property"):
        return "data_property"
    return None


SPECS: dict[str, dict[str, Kid]] = {
    "project": {"fileHeader": Kid(deep="header"), "contentHeader": Kid(deep="header"), "types": Kid(deep="types"),
                "instances": Kid(deep="instances"), "addData": _ADD},
    "header": {"addData": _ADD},
    "types": {"dataTypes": Kid(deep="dataTypes"), "pous": Kid(deep="pous")},
    "dataTypes": {"dataType": Kid(key="name", deep="dataType")},
    "dataType": {"baseType": Kid(deep="baseType"), "addData": _ADD},
    "baseType": {"struct": Kid(deep="struct"), "enum": Kid(deep="enum")},
    "struct": {"variable": Kid(key="name", deep="variable", seg="Member")},
    "enum": {"values": Kid(deep="enumvalues")},
    "enumvalues": {"value": Kid(key="name", seg="Value")},
    "pous": {"pou": Kid(key="name", deep="pou")},
    "pou": {"interface": Kid(deep="interface"), "actions": Kid(deep="actions"), "transitions": Kid(deep="transitions"),
            "body": Kid(deep="body"), "addData": _ADD},
    "interface": {**_VARS, "addData": _ADD},
    "vars": {"variable": _ID_KIND, "addData": _ADD},
    "variable": {},                                               # campi: type / initialValue / documentation / addData
    "actions": {"action": Kid(key="name", deep="action")},
    "transitions": {"transition": Kid(key="name", deep="action")},
    "action": {"body": Kid(deep="body"), "addData": _ADD},
    "body": {**{lang: Kid(slot="lang", special="lang", seg="Corpo") for lang in LANGS}, "addData": _ADD},
    "instances": {"configurations": Kid(deep="configurations")},
    "configurations": {"configuration": Kid(key="name", deep="configuration")},
    "configuration": {"resource": Kid(key="name", deep="resource"), "globalVars": Kid(key="sig", deep="vars"),
                      "addData": _ADD},
    "resource": {"task": Kid(key="name", deep="task"), "pouInstance": Kid(key="name"),
                 "globalVars": Kid(key="sig", deep="vars"), "addData": _ADD},
    "task": {"pouInstance": Kid(key="name"), "addData": _ADD},
    "addData": {"data": Kid(key="data", deep=_data_deep)},
    # --- estensioni CODESYS (best effort: se la struttura non e' quella attesa si ricade nel confronto atomico)
    "data_ps": {"ProjectStructure": Kid(deep="psnode")},
    "psnode": {"Folder": Kid(key="Name", deep="psnode"), "Object": Kid(key="Name", deep="psnode")},
    "data_method": {"Method": Kid(key="name", deep="method")},
    "data_property": {"Property": Kid(key="name", deep="property")},
    "method": {"interface": Kid(deep="interface"), "body": Kid(deep="body"), "addData": _ADD},
    "property": {"interface": Kid(deep="interface"), "Get": Kid(deep="method"), "Set": Kid(deep="method"),
                 "addData": _ADD},
}

# segmenti del path: nome predefinito di un figlio con chiave, e contenitori che non compaiono nel path
_SEG = {"pou": "POU", "dataType": "DataType", "variable": "Variable", "action": "Action", "transition": "Transition",
        "configuration": "Configuration", "resource": "Resource", "task": "Task", "pouInstance": "PouInstance",
        "Folder": "Folder", "Object": "Object", "value": "Value"}
_NO_SEG = {"types", "pous", "dataTypes", "instances", "configurations", "actions", "transitions", "interface",
           "addData", "baseType", "struct", "enum", "values", "ProjectStructure", "body"}
_LABEL = {"pou": "POU", "dataType": "DataType", "variable": "Variabile", "action": "Azione", "transition": "Transizione",
          "configuration": "Configuration", "resource": "Resource", "task": "Task", "pouInstance": "Istanza POU",
          "Method": "Metodo", "Property": "Proprieta'", "data": "addData", "addData": "addData", "body": "Corpo",
          "value": "Valore enum", "interface": "Interfaccia", "Folder": "Cartella", "Object": "Oggetto"}


def local(tag: Any) -> str:
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def _kids(el: etree._Element) -> list[etree._Element]:
    return [c for c in el if isinstance(c.tag, str)]


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def parse_plcopen(data: bytes) -> etree._Element:
    """Radice ``<project>`` di un file PLCopen XML (TC6); niente DOCTYPE / entita' (XXE) e niente rete."""
    if b"<!ENTITY" in data:
        raise L5XError("XML non ammesso: contiene dichiarazioni di entita' (<!ENTITY)")
    try:
        root = etree.fromstring(data, _parser())
    except etree.XMLSyntaxError as exc:
        raise L5XError(f"XML non valido: {exc}") from exc
    if root.getroottree().docinfo.doctype:
        raise L5XError("XML non ammesso: contiene un DOCTYPE (i file PLCopen non ne hanno)")
    ns = etree.QName(root.tag).namespace or ""
    if local(root.tag) != "project" or not ns.startswith(PLCOPEN_NS):
        raise L5XError(f"Root inatteso <{root.tag}>: non e' un file PLCopen XML (<project> nel namespace {PLCOPEN_NS}...)")
    return root


def _child(root: etree._Element, name: str) -> Optional[etree._Element]:
    return next((c for c in _kids(root) if local(c.tag) == name), None)


def project_identity(root: etree._Element) -> tuple[str, str]:
    """(nome progetto = ``contentHeader/@name``, azienda = ``fileHeader/@companyName``)."""
    ch, fh = _child(root, "contentHeader"), _child(root, "fileHeader")
    return ((ch.get("name") or "") if ch is not None else "", (fh.get("companyName") or "") if fh is not None else "")


def plcopen_identity(data: bytes) -> tuple[str, str]:
    return project_identity(parse_plcopen(data))


# --------------------------------------------------------------------------
# Rappresentazione per la GUI
# --------------------------------------------------------------------------
_XMLNS = re.compile(r'\s+xmlns(?::[\w.-]+)?="[^"]*"')


def _pretty(el: etree._Element, limit: int = 60_000) -> str:
    c = copy.deepcopy(el)
    c.tail = None
    etree.indent(c, space="  ")
    s = _XMLNS.sub("", etree.tostring(c, encoding="unicode"))
    return s if len(s) <= limit else s[:limit] + "\n... [troncato]"


def _show(el: Optional[etree._Element]) -> str:
    if el is None:
        return ""
    if local(el.tag) in LANGS and local(el.getparent().tag if el.getparent() is not None else "") == "body" \
            and local(el.tag) not in TEXT_LANGS:
        n = sum(1 for _ in el.iter()) - 1
        return (f"<!-- Corpo grafico {local(el.tag)}: {n} elementi, confrontato come unita' atomica: "
                f"scegli la versione intera -->\n" + _pretty(el))
    return _pretty(el)


def _text_lines(p: Optional[etree._Element]) -> list[str]:
    return ((p.text if p is not None else "") or "").split("\n")


def _st_p(el: etree._Element) -> tuple[Optional[etree._Element], bool]:
    """(``xhtml:p`` col codice, corpo semplice?): ``<ST/>`` vuoto oppure ``<ST><xhtml:p>codice</xhtml:p></ST>``."""
    if (el.text or "").strip():
        return None, False
    kids = _kids(el)
    if not kids:
        return None, True
    if len(kids) == 1 and local(kids[0].tag) == "p" and not len(kids[0]) and not kids[0].attrib:
        return kids[0], True
    return None, False


def _dup(el: etree._Element) -> etree._Element:
    c = copy.deepcopy(el)
    c.tail = None
    return c


# --------------------------------------------------------------------------
# Merger
# --------------------------------------------------------------------------
class _PMerger(_Merger):
    def __init__(self, resolutions: Optional[dict[str, Resolution]], nsmap: dict):
        super().__init__(resolutions)
        self.nsmap = {k: v for k, v in nsmap.items() if v}
        self.warn: list[str] = []

    # ---- decisione atomica (come _Merger._atomic, ma con titoli leggibili e risoluzione custom tollerante)
    def _atomic(self, path: str, label: str, b: Any, m: Any, t: Any, *, key: Callable, text: Callable,
                parse_custom: Callable, title: Optional[str] = None) -> Any:
        segs = path.split("/")
        if title is None:
            title = "/".join(segs[-2:]) if (label == "@attr" or "[" not in segs[-1]) else segs[-1]
        bk = None if b is None else key(b)
        mk = None if m is None else key(m)
        tk = None if t is None else key(t)
        if mk == tk:
            if mk != bk:
                self._note(self.upstream, path, label, title, "both", b, t, text)
            return t
        if mk == bk:
            self._note(self.upstream, path, label, title, "master", b, t, text)
            return t
        if tk == bk:
            self._note(self.changes, path, label, title, "proposal", b, m, text)
            return m
        kind = "add/add" if b is None else ("modify/delete" if (m is None or t is None) else "modify/modify")
        cid, fp = _cid("atomic", path), _fp(bk, mk, tk)
        c = Conflict(id=cid, path=path, title=title, label=label, kind=kind, sequence=False,
                     base_text="" if b is None else text(b), mine_text="" if m is None else text(m),
                     theirs_text="" if t is None else text(t), fingerprint=fp)
        chosen = t                                     # default provvisorio: Master
        r = self.res.get(cid)
        if r is not None and r.fingerprint in (None, fp):
            if r.choice == "mine":
                c.resolved, c.resolution, chosen = True, r, m
            elif r.choice == "custom":
                try:
                    chosen = parse_custom(r.custom or [])
                    c.resolved, c.resolution = True, r
                except L5XError as exc:                # XML scritto a mano e non valido: il conflitto resta aperto
                    self.warn.append(f"{title}: risoluzione personalizzata ignorata ({exc})")
            else:
                c.resolved, c.resolution = True, r     # theirs (e 'both', non applicabile): resta il Master
        self.conflicts.append(c)
        return chosen

    # ---- attributi: il timestamp di modifica non genera mai conflitti
    def _merge_attrs(self, path: str, be, me, te) -> dict[str, str]:
        names = list(te.attrib)
        names += [a for a in me.attrib if a not in te.attrib]
        names += [a for a in be.attrib if a not in te.attrib and a not in me.attrib]
        out: dict[str, str] = {}
        for a in names:
            if local(a) in TIMESTAMP_ATTRS:
                vals = [v for v in (me.get(a), te.get(a)) if v is not None]
                if vals:
                    out[a] = max(vals)
                continue
            v = self._atomic(_j(path, f"@{a}"), "@attr", be.get(a), me.get(a), te.get(a),
                             key=lambda x: x, text=lambda x: str(x), parse_custom=lambda c: c[0] if c else None)
            if v is not None:
                out[a] = v
        return out

    # ---- XML scritto a mano per una risoluzione 'custom'
    def _parse_custom(self, texts: list[str]) -> Optional[etree._Element]:
        s = "\n".join(texts).strip()
        if not s:
            return None
        decl = " ".join(f'xmlns{":" + p if p else ""}="{u}"' for p, u in self.nsmap.items())
        try:
            w = etree.fromstring(f"<_w {decl}>{s}</_w>".encode("utf-8"), _parser())
        except etree.XMLSyntaxError as exc:
            raise L5XError(f"XML non valido: {exc}") from exc
        return next((c for c in w if isinstance(c.tag, str)), None)

    # ---- slot dei figli
    @staticmethod
    def _key_value(ch: etree._Element, kid: Kid) -> str:
        if kid.key is None:
            return ""
        if kid.key == "sig":
            return ",".join(f"{local(k)}={v}" for k, v in sorted(ch.attrib.items()))
        if kid.key == "data":
            sub = next(iter(_kids(ch)), None)
            subname = (sub.get("name") or sub.get("Name") or "") if sub is not None else ""
            return f"{ch.get('name') or ''}|{local(sub.tag) if sub is not None else ''}:{subname}"
        return ch.get(kid.key) or ""

    def _slots(self, el: etree._Element, spec: dict[str, Kid]) -> "OrderedDict[tuple, etree._Element]":
        d: "OrderedDict[tuple, etree._Element]" = OrderedDict()
        seen: dict[tuple, int] = {}
        for ch in _kids(el):
            ln = local(ch.tag)
            kid = spec.get(ln, _DEF)
            base = (kid.slot or ln, self._key_value(ch, kid))
            n = seen.get(base, 0)
            seen[base] = n + 1
            d[(base[0], base[1] if n == 0 else f"{base[1]}#{n}")] = ch
        return d

    @staticmethod
    def _segment(ln: str, ident: tuple, kid: Kid, el: etree._Element) -> str:
        """Segmento del path per un figlio ('' = contenitore che non compare nel path)."""
        name = ident[1]
        if kid.seg and kid.key == "name":
            return f"{kid.seg}[{name}]"
        if ln == "data":
            nm = (el.get("name") or "").rsplit("/", 1)[-1]
            return "" if nm in ("method", "property", "projectstructure") else f"Data[{nm}]"
        if ln in ("Method", "Property"):
            return f"{ln}[{name}]"
        if ln in _NO_SEG:
            return ""
        if kid.key == "sig":
            return f"{ln}({name})" if name else ln
        if kid.key and name:
            return f"{_SEG.get(ln, ln)}[{name}]"
        return kid.seg or ln

    # ---- nodo con struttura nota
    def merge_node(self, kind: str, be, me, te, path: str) -> etree._Element:
        spec = SPECS.get(kind, {})
        out = etree.Element(te.tag, nsmap=te.nsmap)
        for k, v in self._merge_attrs(path, be, me, te).items():
            out.set(k, v)
        SB, SM, ST = self._slots(be, spec), self._slots(me, spec), self._slots(te, spec)
        order = list(ST)
        prev: Optional[tuple] = None
        for k in SM:                                   # elementi nuovi della proposta: dopo il predecessore
            if k not in order:
                order.insert(order.index(prev) + 1 if prev in order else 0, k)
            prev = k
        for k in order:
            chosen = self._merge_slot(spec, k, SB.get(k), SM.get(k), ST.get(k), path)
            if chosen is not None:
                out.append(_dup(chosen))
        return out

    def _merge_slot(self, spec: dict[str, Kid], ident: tuple, be, me, te, path: str):
        ref = te if te is not None else (me if me is not None else be)
        ln = local(ref.tag)
        kid = spec.get(ln, _DEF)
        seg = self._segment(ln, ident, kid, ref)
        ipath = _j(path, seg) if seg else path
        if kid.special == "lang":
            return self._merge_lang(_j(path, seg or "Corpo"), be, me, te)
        deep = kid.deep(ref) if callable(kid.deep) else kid.deep
        if deep and be is not None and me is not None and te is not None and self.canon(me) != self.canon(te):
            return self.merge_node(deep, be, me, te, ipath)
        return self._atomic(_j(path, seg or self._atomic_segment(ln, ref)), _LABEL.get(ln, ln), be, me, te,
                            key=self.canon, text=_show, parse_custom=self._parse_custom)

    @staticmethod
    def _atomic_segment(ln: str, el: etree._Element) -> str:
        """Segmento per un contenitore trattato come unita' atomica (es. un ``data`` con un metodo dentro)."""
        if ln == "data":
            sub = next(iter(_kids(el)), None)
            if sub is not None:
                nm = sub.get("name") or sub.get("Name")
                return f"{local(sub.tag)}[{nm}]" if nm else local(sub.tag)
        return ln

    # ---- corpo di un POU / azione / transizione / metodo
    def _merge_lang(self, bpath: str, be, me, te):
        els = [x for x in (be, me, te) if x is not None]
        tags = {local(x.tag) for x in els}
        texty = len(els) == 3 and len(tags) == 1 and tags <= set(TEXT_LANGS) and all(_st_p(x)[1] for x in els)
        if not texty:
            lang = next((local(x.tag) for x in (te, me, be) if x is not None), "")
            return self._atomic(bpath, f"Corpo {lang}".strip(), be, me, te, key=self.canon, text=_show,
                                parse_custom=self._parse_custom)
        pb, pm, pt = _st_p(be)[0], _st_p(me)[0], _st_p(te)[0]
        T = _text_lines(pt)
        res = self._merge_lines(bpath, _text_lines(pb), _text_lines(pm), T)
        if res == T:
            return te
        out = _dup(te)
        p = _st_p(out)[0]
        if p is None:                                   # Master con <ST/> vuoto: serve il paragrafo
            donor = pm if pm is not None else pb
            p = etree.SubElement(out, donor.tag if donor is not None else f"{{{XHTML_NS}}}p")
        text = "\n".join(res)
        cdata = pt is None or b"<![CDATA[" in etree.tostring(pt)
        p.text = etree.CDATA(text) if (cdata and text and "]]>" not in text) else (text or None)
        return out

    def _merge_lines(self, bpath: str, B: list[str], M: list[str], T: list[str]) -> list[str]:
        out: list[str] = []
        txt = "\n".join

        def change(bucket, path, title, origin, bs, ns) -> None:
            bucket.append(Change(path=path, title=title, label="Riga",
                                 kind="added" if not bs else ("deleted" if not ns else "modified"),
                                 origin=origin, base_text=txt(bs), new_text=txt(ns)))

        for kind_r, (b0, b1), (m0, m1), (t0, t1) in diff3(B, M, T):
            if kind_r == "stable":
                out.append(T[t0])
                continue
            bs, ms, ts = B[b0:b1], M[m0:m1], T[t0:t1]
            start = len(out)
            title, rpath = f"Riga {start + 1}", f"{bpath}/Riga{start + 1}"
            if ms == ts:
                chosen = ts
                if ms != bs:
                    change(self.upstream, rpath, title, "both", bs, ts)
            elif ms == bs:
                chosen = ts
                change(self.upstream, rpath, title, "master", bs, ts)
            elif ts == bs:
                chosen = ms
                change(self.changes, rpath, title, "proposal", bs, ms)
            else:
                chosen = ts                             # default provvisorio: Master
                cid, fp = _cid("txt", f"{bpath}|{b0}:{b1}"), _fp(bs, ms, ts)
                c = Conflict(id=cid, path=rpath, title=title, label="Riga",
                             kind="modify/delete" if (not ms or not ts) else "modify/modify" if bs else "add/add",
                             sequence=True, base_text=txt(bs), mine_text=txt(ms), theirs_text=txt(ts), fingerprint=fp)
                r = self.res.get(cid)
                if r is not None and r.fingerprint in (None, fp):
                    c.resolved, c.resolution = True, r
                    if r.choice == "mine":
                        chosen = ms
                    elif r.choice == "both":
                        chosen = ts + ms
                    elif r.choice == "custom":
                        chosen = [ln for s in (r.custom or []) for ln in s.split("\n")]
                self.conflicts.append(c)
            out.extend(chosen)
        return out


# --------------------------------------------------------------------------
# Risultato, serializzazione, controlli
# --------------------------------------------------------------------------
_UNIT_SEGS = ("POU[", "DataType[", "Configuration[", "Resource[", "Action[", "Transition[", "Method[", "Property[")


@dataclass
class PlcopenMergeResult(MergeResult):
    @property
    def touched_routines(self) -> list[str]:
        """Unita' toccate dalla proposta (POU, azione, DUT, configurazione ...), per le notifiche."""
        units: set[str] = set()
        for ch in list(self.changes) + list(self.conflicts):
            parts = ch.path.split("/")
            n = 0
            while n < len(parts) and parts[n].startswith(_UNIT_SEGS):
                n += 1
            if n:
                units.add("/".join(parts[:n]))
            else:
                units.add("Struttura del progetto" if parts[0].startswith(("Object[", "Folder[")) else parts[0] or "Progetto")
        return sorted(units)


_BOM = b"\xef\xbb\xbf"
_DECL = re.compile(rb"\A\s*(<\?xml[^>]*\?>)")
_INDENT = re.compile(rb"\r?\n([ \t]+)<")


def _indent(el: etree._Element, unit: str, level: int = 0) -> None:
    """Come ``etree.indent`` ma senza toccare i contenuti misti (testo + elementi, es. documentazione xhtml)."""
    kids = list(el)
    if not kids or (el.text or "").strip() or any((c.tail or "").strip() for c in kids):
        return
    pad = "\n" + unit * (level + 1)
    el.text = pad
    for c in kids:
        if isinstance(c.tag, str):
            _indent(c, unit, level + 1)
        c.tail = pad
    kids[-1].tail = "\n" + unit * level


def _serialize(root: etree._Element, theirs: bytes, rt: etree._Element) -> bytes:
    """Come il Master: dichiarazione XML, BOM, codifica, rientro, fine riga e newline finale."""
    m = _INDENT.search(theirs)
    unit = ("\t" if b"\t" in m.group(1) else " " * min(len(m.group(1)), 8)) if m else "  "
    _indent(root, unit)
    enc = rt.getroottree().docinfo.encoding or "UTF-8"
    body = etree.tostring(root, encoding=enc, xml_declaration=False)
    crlf = theirs.count(b"\r\n") * 2 >= theirs.count(b"\n") > 0
    eol = b"\r\n" if crlf else b"\n"
    if crlf:
        body = body.replace(b"\n", eol)
    start = theirs[len(_BOM):] if theirs.startswith(_BOM) else theirs
    decl = _DECL.match(start)
    head = (_BOM if theirs.startswith(_BOM) else b"") + (decl.group(1) + eol if decl else b"")
    return head + body + (eol if theirs.endswith(b"\n") else b"")


def _graphical_bodies(root: etree._Element) -> list[str]:
    names: list[str] = []
    for body in root.iter():
        if not isinstance(body.tag, str) or local(body.tag) != "body":
            continue
        lang = next((local(c.tag) for c in _kids(body) if local(c.tag) in LANGS), "")
        if lang and lang not in TEXT_LANGS:
            anc = next((a for a in body.iterancestors() if local(a.tag) in ("pou", "Method", "Get", "Set")), None)
            nm = (anc.get("name") or local(anc.tag)) if anc is not None else "?"
            owner = body.getparent()
            sub = f"/{owner.get('name')}" if owner is not None and local(owner.tag) in ("action", "transition") else ""
            names.append(f"{nm}{sub} ({lang})")
    return names


def _names(root: etree._Element) -> set[str]:
    return {e.get("name") for e in root.iter() if isinstance(e.tag, str) and local(e.tag) in ("pou", "dataType")
            and e.get("name")}


def _check(roots: list[etree._Element], result: etree._Element) -> list[str]:
    """Controlli di coerenza leggeri (warning, non errori)."""
    w: list[str] = []
    gone = set().union(*(_names(r) for r in roots)) - _names(result)
    if gone:
        refs = 0
        for e in result.iter():
            if not isinstance(e.tag, str):
                continue
            ln, nm = local(e.tag), None
            if ln == "derived":
                nm = e.get("name")
            elif ln == "pouInstance":
                nm = e.get("typeName")
            if nm in gone and refs < _MAX_WARN_REFS:
                refs += 1
                anc = next((a for a in e.iterancestors() if a.get("name") and local(a.tag) in
                            ("pou", "dataType", "task", "resource", "configuration")), None)
                w.append(f"«{nm}» non esiste piu' dopo il merge ma e' ancora usato"
                         + (f" in {local(anc.tag)} «{anc.get('name')}»" if anc is not None else ""))
    return w


def merge_plcopen(base: bytes, mine: bytes, theirs: bytes,
                  resolutions: Optional[dict[str, Resolution]] = None) -> MergeResult:
    rb, rm, rt = parse_plcopen(base), parse_plcopen(mine), parse_plcopen(theirs)
    nss = {etree.QName(r.tag).namespace for r in (rb, rm, rt)}
    if len(nss) > 1:
        raise L5XError("Versione dello schema PLCopen diversa tra base, proposta e Master ("
                       + ", ".join(sorted(n or "" for n in nss)) + "): riesporta il progetto con la stessa versione")
    ids = [project_identity(r)[0] for r in (rb, rm, rt)]
    if not (ids[0] == ids[1] == ids[2]):
        raise L5XError(f"Nome progetto diverso (base='{ids[0]}', proposta='{ids[1]}', master='{ids[2]}'): "
                       "il file non appartiene a questo progetto")
    mg = _PMerger(resolutions, rt.nsmap)
    out = mg.merge_node("project", rb, rm, rt, "")
    same = etree.tostring(out, method="c14n") == etree.tostring(rt, method="c14n")
    data = theirs if same else _serialize(out, theirs, rt)
    warnings = ["PLCopen XML: merge strutturale per POU, tipi, variabili, istanze e righe ST/IL; il resto (addData di "
                "altri produttori) e' confrontato come unita' atomica, controlla il risultato prima del consenso"]
    gfx = _graphical_bodies(out)
    if gfx:
        shown = ", ".join(gfx[:6]) + (f" e altri {len(gfx) - 6}" if len(gfx) > 6 else "")
        warnings.append(f"Corpi grafici (LD/FBD/SFC) trattati come unita' atomiche ({len(gfx)}): se due persone "
                        f"modificano lo stesso corpo si crea un conflitto da risolvere scegliendo una versione intera ({shown})")
    warnings += _check([rb, rm, rt], out) + mg.warn
    return PlcopenMergeResult(merged_xml=data, changes=mg.changes, upstream_changes=mg.upstream,
                              conflicts=mg.conflicts, warnings=warnings)
