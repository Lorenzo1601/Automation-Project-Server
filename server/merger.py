"""Engine di 3-way merge per file Rockwell ``.L5X`` (Studio 5000).

Terminologia (coerente con il workflow del server):

* ``base``   -> revisione di Master da cui l'autore ha fatto il checkout
* ``mine``   -> file ``.L5X`` proposto dall'autore (Programmatore A)
* ``theirs`` -> Master corrente (include il lavoro gia' consolidato, es. di B)

Il merge e' *strutturale*, non testuale:

* ``Controller`` : attributi unione 3-way, sezioni a chiave ``Name``
  (DataTypes, Modules, AddOnInstructionDefinitions, Tags, Programs, Tasks)
* ``Program`` / ``AddOnInstructionDefinition`` : ricorsione su Tags, Routines,
  Parameters, LocalTags
* ``Routine`` RLL/ST : merge a sequenze (diff3) di ``<Rung>`` / ``<Line>``;
  FBD/SFC e altri figli sono confrontati come unita' atomiche
* Un elemento e' in conflitto solo se *entrambi* lo hanno cambiato in modo
  diverso rispetto alla base.

``merge_l5x`` e' una funzione pura e deterministica: per applicare le
risoluzioni dei conflitti si richiama con ``resolutions=...``.
"""
from __future__ import annotations

import copy
import difflib
import hashlib
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from lxml import etree


class L5XError(ValueError):
    """File non valido o non supportato (es. export parziale)."""


# --------------------------------------------------------------------------
# Specifiche della struttura L5X
# --------------------------------------------------------------------------
@dataclass
class Keyed:
    child: str                   # tag degli elementi nel contenitore
    key: str = "Name"            # attributo chiave
    deep: Optional[str] = None   # nome della Node-spec per il merge ricorsivo


@dataclass
class Node:
    containers: dict[str, Keyed] = field(default_factory=dict)
    sequences: dict[str, str] = field(default_factory=dict)   # tag contenuto -> tag item


NODE_SPECS: dict[str, Node] = {
    "Controller": Node(containers={
        "DataTypes": Keyed("DataType"),
        "Modules": Keyed("Module"),
        "AddOnInstructionDefinitions": Keyed("AddOnInstructionDefinition", deep="AOI"),
        "Tags": Keyed("Tag"),
        "Programs": Keyed("Program", deep="Program"),
        "Tasks": Keyed("Task"),
    }),
    "Program": Node(containers={
        "Tags": Keyed("Tag"),
        "Routines": Keyed("Routine", deep="Routine"),
    }),
    "AOI": Node(containers={
        "Parameters": Keyed("Parameter"),
        "LocalTags": Keyed("LocalTag"),
        "Routines": Keyed("Routine", deep="Routine"),
    }),
    "Routine": Node(sequences={"RLLContent": "Rung", "STContent": "Line"}),
}


# --------------------------------------------------------------------------
# Strutture risultato
# --------------------------------------------------------------------------
@dataclass
class Resolution:
    choice: str                              # mine | theirs | both | custom
    custom: Optional[list[str]] = None
    fingerprint: Optional[str] = None        # se valorizzato, valida solo per quel conflitto


@dataclass
class Change:
    path: str
    title: str
    label: str
    kind: str                                # added | modified | deleted
    origin: str                              # proposal | master | both
    base_text: str = ""
    new_text: str = ""


@dataclass
class Conflict:
    id: str
    path: str
    title: str
    label: str
    kind: str                                # modify/modify | modify/delete | add/add
    sequence: bool
    base_text: str
    mine_text: str
    theirs_text: str
    fingerprint: str
    resolved: bool = False
    resolution: Optional[Resolution] = None


@dataclass
class MergeResult:
    merged_xml: bytes
    changes: list[Change]            # cosa la proposta cambia su Master
    upstream_changes: list[Change]   # cosa e' cambiato su Master dopo la base
    conflicts: list[Conflict]
    warnings: list[str]

    @property
    def unresolved(self) -> list[Conflict]:
        return [c for c in self.conflicts if not c.resolved]

    @property
    def is_clean(self) -> bool:
        return not self.unresolved

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.merged_xml).hexdigest()

    @property
    def touched_routines(self) -> list[str]:
        """Unita' (routine / AOI / altro) toccate dalla proposta, per le notifiche."""
        units: set[str] = set()
        for ch in self.changes + [c for c in self.conflicts]:
            parts = ch.path.split("/")
            cut = next((i for i, p in enumerate(parts) if p.startswith("Routine[")), None)
            if cut is not None:
                units.add("/".join(parts[: cut + 1]))
            else:
                units.add("/".join(parts[:2]) if parts[0].startswith(("Program[", "AddOnInstructionDefinition[")) else parts[0])
        return sorted(units)


# --------------------------------------------------------------------------
# Utility XML
# --------------------------------------------------------------------------
def _parser() -> etree.XMLParser:
    return etree.XMLParser(
        strip_cdata=False, remove_blank_text=True, resolve_entities=False,
        no_network=True, huge_tree=True,
    )


def parse_l5x(data: bytes) -> etree._Element:
    try:
        root = etree.fromstring(data, _parser())
    except etree.XMLSyntaxError as exc:
        raise L5XError(f"XML non valido: {exc}") from exc
    if root.tag != "RSLogix5000Content":
        raise L5XError(f"Root inatteso <{root.tag}>: non e' un file L5X")
    if root.get("TargetType", "Controller") != "Controller":
        raise L5XError(
            f"TargetType='{root.get('TargetType')}': sono supportati solo export "
            "dell'intero Controller"
        )
    if root.find("Controller") is None:
        raise L5XError("Elemento <Controller> mancante")
    return root


def _pretty(el: etree._Element, limit: int = 60_000) -> str:
    c = copy.deepcopy(el)
    c.tail = None
    etree.indent(c, space="  ")
    s = etree.tostring(c, encoding="unicode")
    return s if len(s) <= limit else s[:limit] + "\n... [troncato]"


def display(el: Optional[etree._Element]) -> str:
    """Rappresentazione testuale per la GUI."""
    if el is None:
        return ""
    if el.tag == "Rung":
        text = el.findtext("Text") or ""
        comment = el.findtext("Comment")
        return (f"// {comment.strip()}\n" if comment and comment.strip() else "") + text.strip()
    if el.tag == "Line":
        return (el.text or "").rstrip()
    return _pretty(el)


def _cid(prefix: str, path: str) -> str:
    return hashlib.sha1(f"{prefix}|{path}".encode()).hexdigest()[:12]


def _fp(*keys: Any) -> str:
    return hashlib.sha1(repr(keys).encode()).hexdigest()[:16]


def _j(path: str, seg: str) -> str:
    return f"{path}/{seg}" if path else seg


def _last(path: str) -> str:
    return path.rsplit("/", 1)[-1] if path else "Controller"


# --------------------------------------------------------------------------
# diff3 su sequenze di chiavi canoniche
# --------------------------------------------------------------------------
def diff3(base: list, mine: list, theirs: list):
    """Restituisce regioni ``("stable"|"unstable", (b0,b1), (m0,m1), (t0,t1))``.

    Le regioni *stable* sono righe di base presenti invariate in entrambe le
    versioni (punti di sincronizzazione); le *unstable* sono i tratti tra due
    punti di sync e vanno risolti con la regola 3-way.
    """
    def mapping(other: list) -> dict[int, int]:
        sm = difflib.SequenceMatcher(None, base, other, autojunk=False)
        mp: dict[int, int] = {}
        for blk in sm.get_matching_blocks():
            for k in range(blk.size):
                mp[blk.a + k] = blk.b + k
        return mp

    mm, mt = mapping(mine), mapping(theirs)
    pb = pm = pt = 0
    for i in [i for i in range(len(base)) if i in mm and i in mt] + [None]:
        nb, nm, nt = (len(base), len(mine), len(theirs)) if i is None else (i, mm[i], mt[i])
        if nb > pb or nm > pm or nt > pt:
            yield ("unstable", (pb, nb), (pm, nm), (pt, nt))
        if i is not None:
            yield ("stable", (i, i + 1), (nm, nm + 1), (nt, nt + 1))
            pb, pm, pt = i + 1, nm + 1, nt + 1


# --------------------------------------------------------------------------
# Merger
# --------------------------------------------------------------------------
class _Merger:
    def __init__(self, resolutions: Optional[dict[str, Resolution]]):
        self.res = resolutions or {}
        self.changes: list[Change] = []
        self.upstream: list[Change] = []
        self.conflicts: list[Conflict] = []
        self._canon_cache: dict[tuple, tuple] = {}

    # ---- canonicalizzazione
    def canon(self, el: etree._Element, ignore: tuple[str, ...] = ()) -> str:
        # La cache tiene un riferimento all'elemento: i proxy lxml vengono ricreati
        # quando non referenziati e id() potrebbe essere riusato da un altro nodo.
        k = (id(el), ignore)
        hit = self._canon_cache.get(k)
        if hit is not None:
            return hit[1]
        if ignore:
            c = copy.deepcopy(el)
            for a in ignore:
                c.attrib.pop(a, None)
            v = etree.tostring(c, method="c14n", with_tail=False).decode()
        else:
            v = etree.tostring(el, method="c14n", with_tail=False).decode()
        self._canon_cache[k] = (el, v)
        return v

    # ---- registrazione cambiamenti
    @staticmethod
    def _kind(b: Any, n: Any) -> str:
        return "added" if b is None else ("deleted" if n is None else "modified")

    def _note(self, bucket: list[Change], path: str, label: str, title: str, origin: str,
              b: Any, n: Any, text: Callable[[Any], str]) -> None:
        bucket.append(Change(
            path=path, title=title, label=label, kind=self._kind(b, n), origin=origin,
            base_text="" if b is None else text(b), new_text="" if n is None else text(n),
        ))

    # ---- decisione atomica 3-way
    def _atomic(self, path: str, label: str, b: Any, m: Any, t: Any, *,
                key: Callable, text: Callable, parse_custom: Callable) -> Any:
        title = "/".join(path.split("/")[-2:]) if label == "@attr" else _last(path)
        bk = None if b is None else key(b)
        mk = None if m is None else key(m)
        tk = None if t is None else key(t)
        if mk == tk:                                   # identici (o entrambi cancellati)
            if mk != bk:
                self._note(self.upstream, path, label, title, "both", b, t, text)
            return t
        if mk == bk:                                   # ha cambiato solo Master
            self._note(self.upstream, path, label, title, "master", b, t, text)
            return t
        if tk == bk:                                   # ha cambiato solo la proposta
            self._note(self.changes, path, label, title, "proposal", b, m, text)
            return m
        # --- conflitto
        kind = "add/add" if b is None else ("modify/delete" if (m is None or t is None) else "modify/modify")
        cid, fp = _cid("atomic", path), _fp(bk, mk, tk)
        c = Conflict(id=cid, path=path, title=title, label=label, kind=kind, sequence=False,
                     base_text="" if b is None else text(b),
                     mine_text="" if m is None else text(m),
                     theirs_text="" if t is None else text(t), fingerprint=fp)
        chosen = t                                     # default provvisorio: Master
        r = self.res.get(cid)
        if r is not None and r.fingerprint in (None, fp):
            c.resolved, c.resolution = True, r
            if r.choice == "mine":
                chosen = m
            elif r.choice == "custom":
                chosen = parse_custom(r.custom or [])
        self.conflicts.append(c)
        return chosen

    # ---- attributi
    def _merge_attrs(self, path: str, be, me, te) -> dict[str, str]:
        names = list(te.attrib)
        names += [a for a in me.attrib if a not in te.attrib]
        names += [a for a in be.attrib if a not in te.attrib and a not in me.attrib]
        out: dict[str, str] = {}
        for a in names:
            v = self._atomic(_j(path, f"@{a}"), "@attr", be.get(a), me.get(a), te.get(a),
                             key=lambda x: x, text=lambda x: str(x),
                             parse_custom=lambda c: c[0] if c else None)
            if v is not None:
                out[a] = v
        return out

    # ---- nodo con struttura nota (Controller, Program, AOI, Routine)
    def merge_node(self, kind: str, be, me, te, path: str) -> etree._Element:
        spec = NODE_SPECS[kind]
        out = etree.Element(te.tag, self._merge_attrs(path, be, me, te))

        def group(e):
            d: "OrderedDict[str, list]" = OrderedDict()
            for ch in e:
                if isinstance(ch.tag, str):
                    d.setdefault(ch.tag, []).append(ch)
            return d

        gb, gm, gt = group(be), group(me), group(te)
        tags = list(gt) + [x for x in gm if x not in gt] + [x for x in gb if x not in gt and x not in gm]
        for tag in tags:
            cb, cm, ct = gb.get(tag), gm.get(tag), gt.get(tag)
            first = lambda l: l[0] if l else None
            if tag in spec.containers:
                items = self._merge_keyed(path, first(cb), first(cm), first(ct), spec.containers[tag])
                src = next(x for x in (first(ct), first(cm), first(cb)) if x is not None)
                if items or ct:
                    cont = etree.SubElement(out, tag, dict(src.attrib))
                    for it in items:
                        cont.append(copy.deepcopy(it))
            elif tag in spec.sequences:
                items_el = self._merge_sequence(_j(path, tag), first(cb), first(cm), first(ct), tag,
                                                spec.sequences[tag])
                out.append(items_el)
            else:
                chosen = self._atomic(
                    _j(path, tag), tag, cb, cm, ct,
                    key=lambda lst: tuple(self.canon(x) for x in lst),
                    text=lambda lst: "\n".join(display(x) for x in lst),
                    parse_custom=lambda c: [etree.fromstring(s, _parser()) for s in c],
                )
                for x in chosen or []:
                    out.append(copy.deepcopy(x))
        return out

    # ---- contenitore a chiave (Tags, DataTypes, Programs, Routines ...)
    def _merge_keyed(self, path: str, bc, mc, tc, spec: Keyed) -> list[etree._Element]:
        def index(cont) -> "OrderedDict[str, etree._Element]":
            d: "OrderedDict[str, etree._Element]" = OrderedDict()
            if cont is not None:
                for ch in cont:
                    if ch.tag == spec.child and ch.get(spec.key) is not None:
                        d[ch.get(spec.key)] = ch
            return d

        B, M, T = index(bc), index(mc), index(tc)
        order = list(T)
        prev: Optional[str] = None
        for k in M:                                    # elementi nuovi della proposta: dopo il predecessore
            if k not in order:
                order.insert(order.index(prev) + 1 if prev in order else 0, k)
            prev = k
        out: list[etree._Element] = []
        for k in order:
            be, me, te = B.get(k), M.get(k), T.get(k)
            ipath = _j(path, f"{spec.child}[{k}]")
            chosen: Optional[etree._Element]
            # ricorsione ogni volta che i tre elementi esistono e proposta/Master divergono:
            # il risultato e' identico al merge atomico, ma i cambiamenti sono elencati a grana fine
            if spec.deep and be is not None and me is not None and te is not None \
                    and self.canon(me) != self.canon(te):
                chosen = self.merge_node(spec.deep, be, me, te, ipath)
            else:
                chosen = self._atomic(ipath, spec.child, be, me, te, key=self.canon, text=display,
                                      parse_custom=lambda c: etree.fromstring(c[0], _parser()) if c else None)
            if chosen is not None:
                out.append(chosen)
        return out

    # ---- sequenze di Rung / Line
    def _merge_sequence(self, path: str, be, me, te, tag: str, item: str) -> etree._Element:
        get = lambda e: [] if e is None else [c for c in e if c.tag == item]
        B, M, T = get(be), get(me), get(te)
        key = lambda x: self.canon(x, ignore=("Number",))
        kb, km, kt = [key(x) for x in B], [key(x) for x in M], [key(x) for x in T]
        src = te if te is not None else (me if me is not None else be)
        out_el = etree.Element(tag, dict(src.attrib))
        res: list[etree._Element] = []
        txt = lambda els: "\n".join(display(x) for x in els)

        for kind, (b0, b1), (m0, m1), (t0, t1) in diff3(kb, km, kt):
            if kind == "stable":
                res.append(T[t0])
                continue
            bs, ms, ts = kb[b0:b1], km[m0:m1], kt[t0:t1]
            Bs, Ms, Ts = B[b0:b1], M[m0:m1], T[t0:t1]
            start = len(res)
            title = f"{item} #{start}"
            rpath = f"{path}#{item}{start}"
            if ms == ts:
                chosen = Ts
                if ms != bs:
                    self.upstream.append(self._seq_change(rpath, item, title, "both", Bs, Ts, txt))
            elif ms == bs:
                chosen = Ts
                self.upstream.append(self._seq_change(rpath, item, title, "master", Bs, Ts, txt))
            elif ts == bs:
                chosen = Ms
                self.changes.append(self._seq_change(rpath, item, title, "proposal", Bs, Ms, txt))
            else:
                chosen = Ts                            # default provvisorio: Master
                cid, fp = _cid("seq", f"{path}|{b0}:{b1}"), _fp(bs, ms, ts)
                c = Conflict(id=cid, path=rpath, title=title, label=item, kind=(
                    "modify/delete" if (not ms or not ts) else "modify/modify" if bs else "add/add"),
                    sequence=True, base_text=txt(Bs), mine_text=txt(Ms), theirs_text=txt(Ts),
                    fingerprint=fp)
                r = self.res.get(cid)
                if r is not None and r.fingerprint in (None, fp):
                    c.resolved, c.resolution = True, r
                    if r.choice == "mine":
                        chosen = Ms
                    elif r.choice == "both":
                        chosen = Ts + Ms
                    elif r.choice == "custom":
                        chosen = [self._build_item(item, s) for s in (r.custom or [])]
                self.conflicts.append(c)
            res.extend(chosen)

        for i, el in enumerate(res):                   # rinumerazione Rung / Line
            c = copy.deepcopy(el)
            c.tail = None
            c.set("Number", str(i))
            out_el.append(c)
        return out_el

    @staticmethod
    def _seq_change(path, item, title, origin, B, N, txt) -> Change:
        kind = "added" if not B else ("deleted" if not N else "modified")
        return Change(path=path, title=title, label=item, kind=kind, origin=origin,
                      base_text=txt(B), new_text=txt(N))

    @staticmethod
    def _build_item(item: str, text: str) -> etree._Element:
        if item == "Rung":
            r = etree.Element("Rung", Number="0", Type="N")
            etree.SubElement(r, "Text").text = etree.CDATA(text)
            return r
        ln = etree.Element("Line", Number="0")
        ln.text = etree.CDATA(text)
        return ln


# --------------------------------------------------------------------------
# API pubblica
# --------------------------------------------------------------------------
def validate_tree(root: etree._Element) -> list[str]:
    """Controlli di coerenza leggeri sul risultato (warning, non errori)."""
    w: list[str] = []
    ctrl = root.find("Controller")
    programs = {p.get("Name"): p for p in ctrl.findall("Programs/Program")}
    for name, p in programs.items():
        main = p.get("MainRoutineName")
        if main and p.find(f"Routines/Routine[@Name='{main}']") is None:
            w.append(f"Program '{name}': MainRoutineName '{main}' non esiste dopo il merge")
    for sp in ctrl.findall("Tasks/Task/ScheduledPrograms/ScheduledProgram"):
        if sp.get("Name") not in programs:
            w.append(f"Task '{sp.getparent().getparent().get('Name')}': programma schedulato "
                     f"'{sp.get('Name')}' non esiste dopo il merge")
    return w


def merge_l5x(base: bytes, mine: bytes, theirs: bytes,
              resolutions: Optional[dict[str, Resolution]] = None) -> MergeResult:
    rb, rm, rt = parse_l5x(base), parse_l5x(mine), parse_l5x(theirs)
    cb, cm, ct = rb.find("Controller"), rm.find("Controller"), rt.find("Controller")
    if not (cb.get("Name") == cm.get("Name") == ct.get("Name")):
        raise L5XError(
            f"Nome controller diverso (base='{cb.get('Name')}', proposta='{cm.get('Name')}', "
            f"master='{ct.get('Name')}'): il file non appartiene a questo progetto"
        )
    mg = _Merger(resolutions)
    ctrl = mg.merge_node("Controller", cb, cm, ct, "")

    out_root = etree.Element(rt.tag, dict(rt.attrib), nsmap=rt.nsmap)
    for ch in rt:
        out_root.append(ctrl if ch.tag == "Controller" else copy.deepcopy(ch))
    warnings = validate_tree(out_root)
    etree.indent(out_root, space="  ")
    xml = etree.tostring(out_root, xml_declaration=True, encoding="UTF-8", standalone=True)
    return MergeResult(merged_xml=xml, changes=mg.changes, upstream_changes=mg.upstream,
                       conflicts=mg.conflicts, warnings=warnings)
