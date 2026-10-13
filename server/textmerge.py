"""Merge 3-way *testuale* (per riga) per ``.L5K`` e per qualsiasi altro file di testo, piu' il dispatcher per tipo.

Stessa logica del merge L5X (``diff3`` + regola 3-way: e' conflitto solo se entrambi hanno cambiato lo stesso tratto in
modo diverso) e stesso formato di risultato, cosi' consenso, conflitti e storico funzionano identici per ogni tipo.
Per gli ``.L5K`` il percorso di ogni modifica porta il contesto ``Program[X]/Routine[Y]``.
"""
from __future__ import annotations

import re
from typing import Optional

from common.filekinds import KIND_LABELS, L5K, L5X, PLCOPEN, TEXT, l5k_controller_name
from server.merger import (Change, Conflict, L5XError, MergeResult, Resolution, _cid, _fp, diff3, merge_l5x)
from server.plcopen import merge_plcopen

_PROG = re.compile(r"^\s*PROGRAM\s+(\w+)", re.I)
_ROUT = re.compile(r"^\s*ROUTINE\s+(\w+)", re.I)
_END_PROG = re.compile(r"^\s*END_PROGRAM\b", re.I)
_END_ROUT = re.compile(r"^\s*END_ROUTINE\b", re.I)


class UnsupportedMerge(L5XError):
    """Il tipo di file non ammette il merge automatico (es. file binario)."""


def _decode_all(*blobs: bytes) -> tuple[list[str], str]:
    try:
        return [b.decode("utf-8") for b in blobs], "utf-8"
    except UnicodeDecodeError:
        return [b.decode("latin-1") for b in blobs], "latin-1"           # mai errori: i byte tornano identici


def _lines(text: str) -> list[str]:
    return [ln.rstrip("\r\n") for ln in text.splitlines()]


def _eol(text: str) -> str:
    crlf, lf = text.count("\r\n"), text.count("\n")
    return "\r\n" if lf and crlf * 2 >= lf else "\n"


def _l5k_context(lines: list[str]) -> list[str]:
    """Per ogni riga: ``Program[P]/Routine[R]`` (o parziale) in cui si trova, per etichettare le modifiche."""
    prog = rout = ""
    out: list[str] = []
    for ln in lines:
        if _END_ROUT.match(ln):
            rout = ""
        elif _END_PROG.match(ln):
            prog = rout = ""
        elif (m := _PROG.match(ln)):
            prog = m.group(1)
        elif prog and (m := _ROUT.match(ln)):
            rout = m.group(1)
        out.append(f"Program[{prog}]" + (f"/Routine[{rout}]" if rout else "") if prog else "File")
    return out


def merge_text(base: bytes, mine: bytes, theirs: bytes, resolutions: Optional[dict[str, Resolution]] = None,
               kind: str = TEXT) -> MergeResult:
    (sb, sm, st), enc = _decode_all(base, mine, theirs)
    if kind == L5K:
        names = {l5k_controller_name(x) for x in (base, mine, theirs)}
        if "" in names:
            raise L5XError("Il file non e' un export L5K di un controller (manca CONTROLLER ...)")
        if len(names) > 1:
            raise L5XError(f"Nome controller diverso (base='{l5k_controller_name(base)}', proposta="
                           f"'{l5k_controller_name(mine)}', master='{l5k_controller_name(theirs)}'): "
                           "il file non appartiene a questo progetto")
    B, M, T = _lines(sb), _lines(sm), _lines(st)
    ctx = _l5k_context(T) if kind == L5K else None
    res_map = resolutions or {}
    changes: list[Change] = []
    upstream: list[Change] = []
    conflicts: list[Conflict] = []
    out: list[str] = []
    txt = lambda xs: "\n".join(xs)

    def where(t0: int) -> str:
        if ctx is None or not T:
            return "File"
        return ctx[min(t0, len(T) - 1)]

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
        title, rpath = f"Riga {start + 1}", f"{where(t0)}/Riga{start + 1}"
        if ms == ts:
            chosen = ts
            if ms != bs:
                change(upstream, rpath, title, "both", bs, ts)
        elif ms == bs:
            chosen = ts
            change(upstream, rpath, title, "master", bs, ts)
        elif ts == bs:
            chosen = ms
            change(changes, rpath, title, "proposal", bs, ms)
        else:
            chosen = ts                                   # default provvisorio: Master
            cid, fp = _cid("txt", f"{b0}:{b1}"), _fp(bs, ms, ts)
            c = Conflict(id=cid, path=rpath, title=title, label="Riga",
                         kind="modify/delete" if (not ms or not ts) else "modify/modify" if bs else "add/add",
                         sequence=True, base_text=txt(bs), mine_text=txt(ms), theirs_text=txt(ts), fingerprint=fp)
            r = res_map.get(cid)
            if r is not None and r.fingerprint in (None, fp):
                c.resolved, c.resolution = True, r
                if r.choice == "mine":
                    chosen = ms
                elif r.choice == "both":
                    chosen = ts + ms
                elif r.choice == "custom":
                    chosen = [s for s in (r.custom or [])]
            conflicts.append(c)
        out.extend(chosen)

    eol = _eol(st)
    body = eol.join(out) + (eol if (st.endswith("\n") or not st) and out else "")
    warnings = [f"{KIND_LABELS[kind]}: merge testuale riga per riga (non strutturale), controlla il risultato "
                "prima di dare il consenso"]
    return MergeResult(merged_xml=body.encode(enc), changes=changes, upstream_changes=upstream,
                       conflicts=conflicts, warnings=warnings)


def merge_any(kind: str, base: bytes, mine: bytes, theirs: bytes,
              resolutions: Optional[dict[str, Resolution]] = None) -> MergeResult:
    """Merge 3-way in base al tipo del Master del progetto."""
    if kind == L5X:
        return merge_l5x(base, mine, theirs, resolutions)
    if kind == PLCOPEN:
        return merge_plcopen(base, mine, theirs, resolutions)
    if kind in (L5K, TEXT):
        return merge_text(base, mine, theirs, resolutions, kind)
    raise UnsupportedMerge(f"{KIND_LABELS.get(kind, 'Questo tipo di file')} non ammette il merge automatico: "
                           "il Master viene solo versionato, scaricato e (da un Admin) ripristinato; per aggiornarlo usa "
                           "«Prendi in carico» e poi «Check-in nuova revisione» (non «Richiedi Merge»)")
