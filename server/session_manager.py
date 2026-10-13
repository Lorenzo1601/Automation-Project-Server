"""Sessioni dei programmatori + macchina a stati del consenso.

Regola chiave: **nulla viene scritto su ``Master.L5X`` senza il consenso esplicito
di tutti i partecipanti richiesti**, e ogni consenso e' legato al *digest* del
risultato del merge che l'utente ha visto. Se il risultato cambia (nuove
risoluzioni dei conflitti, oppure Master avanzato per un'altra proposta) i
consensi vengono azzerati e la proposta torna in "In attesa di consenso".

Partecipanti richiesti = autore + tutti gli sviluppatori (ruolo >= Developer) attivi
al momento dell'invio. Chi si disconnette (scade la sessione) viene escluso dal quorum.
I Viewer non partecipano al consenso.

Ruoli (RBAC): se la richiesta e' di un Developer e il progetto ha ``require_lead_approval``,
serve anche il **Consenso Finale** di un Lead/Admin diverso dall'autore (anche non connesso).
L'Admin puo' risolvere conflitti al posto dell'autore, **forzare** un merge (con motivo, tracciato)
e **ripristinare** una revisione precedente del Master.

Stati: AWAITING_CONSENT -> (CONFLICT <-> AWAITING_CONSENT) -> APPROVED -> MERGED
       oppure REJECTED / CANCELLED.

Thread-safety: un solo RLock protegge tutte le mutazioni; i metodi sono sincroni
e vengono eseguiti dal threadpool di FastAPI.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from common.roles import Role
from common.schemas import (ACTIVE_STATES, STATE_LABELS, ChangeOut, ConflictOut, DeveloperOut,
                            Event, MasterInfo, ParticipantOut, ProposalDetail, ProposalState,
                            ProposalSummary)
from common.filekinds import (KIND_LABELS, L5K, L5X, MERGEABLE, PLCOPEN, canonical_ext, compatible_kinds, detect_kind,
                              l5k_controller_name)
from server.textmerge import merge_any
from server.merger import L5XError, MergeResult, Resolution, parse_l5x
from server.plcopen import plcopen_identity
from server.notifier import EventSink, NullSink
from server.storage import MasterStore, Revision


class SessionError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Developer:
    session_id: str
    name: str
    registered_at: datetime
    last_seen: float
    role: Role = Role.DEVELOPER     # ruolo effettivo sul progetto
    ws_connected: bool = False
    revision: int = 0               # ultima revisione di Master scaricata dal client


@dataclass
class Approval:
    session_id: str
    developer: str
    decision: str                   # approve | reject
    comment: str
    at: datetime
    digest: str


@dataclass
class Proposal:
    id: str
    author_session: str
    author: str
    base_revision: int
    message: str
    created_at: datetime
    updated_at: datetime
    state: ProposalState
    file_path: str
    analyzed_head: int = 0
    result: Optional[MergeResult] = None
    resolutions: dict[str, Resolution] = field(default_factory=dict)
    required: dict[str, str] = field(default_factory=dict)       # session_id -> nome
    approvals: dict[str, Approval] = field(default_factory=dict)
    result_revision: Optional[int] = None
    log: list[str] = field(default_factory=list)
    author_role: Role = Role.DEVELOPER
    needs_lead: bool = False
    lead_approval: Optional[Approval] = None
    forced_by: Optional[str] = None
    # Branch: ``kind`` = normal | sync («Aggiorna dal principale» dentro un branch) | merge («Unisci nel principale»),
    # ``source_branch`` = slug del branch di origine di un merge nel principale, ``main_head`` = revisione del principale
    # portata da un sync. ``base_data``: base del 3-way presa dal principale (invece che da una revisione di questo store).
    # ``swap``: sync: il file caricato e' la testa del principale (lato «theirs») e «mine» e' la testa del branch.
    meta: dict = field(default_factory=dict)
    base_data: Optional[bytes] = None
    swap: bool = False


class SessionManager:
    def __init__(self, store: MasterStore, sink: Optional[EventSink] = None, ttl: float = 90.0,
                 clock: Callable[[], float] = time.time,
                 audit: Optional[Callable[[str, str, str, dict], None]] = None,
                 require_lead_approval: bool = True, max_revisions: int = 10):
        self.store, self.sink, self.ttl, self.clock = store, sink or NullSink(), ttl, clock
        self._audit_cb = audit
        self.require_lead_approval = require_lead_approval
        self.max_revisions = max_revisions          # revisioni conservate sul disco (le piu' vecchie vengono potate)
        self._lock = threading.RLock()
        self._devs: dict[str, Developer] = {}
        self._proposals: dict[str, Proposal] = {}
        self.comment_count: Callable[[str], int] = lambda _pid: 0     # n. commenti non eliminati (lo imposta il registry)
        # ---- estensioni per i branch (impostate dal registry; i valori di default non cambiano il comportamento)
        self.extra_protect: Callable[[], set[int]] = lambda: set()    # revisioni da non potare (basi dei branch aperti)
        self.on_merged: Optional[Callable[[Proposal, Revision], None]] = None       # richiesta consolidata in una revisione
        self.finalize_guard: Optional[Callable[[Proposal], Optional[str]]] = None   # motivo per annullare invece di consolidare
        self.auto_approve = False       # branch personale: consenso del solo autore, dato in automatico se non ci sono conflitti

    def _log_audit(self, action: str, actor: str, target: str = "", **details) -> None:
        if self._audit_cb:
            self._audit_cb(action, actor, target, details)

    # ================================================================ sessioni
    def join(self, user_id: int, name: str, role: Role) -> Developer:
        """Registra l'utente autenticato su questo progetto (get-or-create, idempotente).

        NON lo rende "attivo": per entrare nel quorum del consenso serve la connessione WebSocket
        oppure un heartbeat recente (chi chiama le API per altro non blocca i merge degli altri).
        """
        with self._lock:
            sid = f"u{user_id}"
            dev = self._devs.get(sid)
            if dev is None:
                dev = Developer(sid, name, _now(), float("-inf"), role=role,
                                revision=self.store.head().revision if self.store.initialized else 0)
                self._devs[sid] = dev
            dev.name, dev.role = name, role
            return dev

    def login(self, name: str, role: Role = Role.DEVELOPER) -> Developer:
        """Crea una sessione nuova (sostituisce quella omonima). Usata dai test e dai client senza account."""
        with self._lock:
            for d in [d for d in self._devs.values() if d.name == name]:
                self._drop_dev(d, announce=False)
            dev = Developer(uuid.uuid4().hex[:12], name, _now(), self.clock(), role=role,
                            revision=self.store.head().revision if self.store.initialized else 0)
            self._devs[dev.session_id] = dev
            self._broadcast("developer_joined", f"{name} si e' collegato", developer=name)
            return dev

    def logout(self, session_id: str) -> None:
        with self._lock:
            dev = self._devs.get(session_id)
            if dev:
                self._drop_dev(dev, announce=True)

    def _drop_dev(self, dev: Developer, announce: bool) -> None:
        self._devs.pop(dev.session_id, None)
        for p in list(self._proposals.values()):
            if p.state in ACTIVE_STATES:
                if p.author_session == dev.session_id:
                    self._set_state(p, ProposalState.CANCELLED, f"{dev.name} si e' disconnesso: richiesta annullata")
                    self._notify_proposal("proposal_updated", p, f"Richiesta di {dev.name} annullata (autore disconnesso)")
                elif dev.session_id in p.required:
                    self._prune_and_check(p)
        if announce:
            self._broadcast("developer_left", f"{dev.name} si e' disconnesso", developer=dev.name)

    def touch(self, dev: Developer, revision: Optional[int] = None) -> None:
        with self._lock:
            dev.last_seen = self.clock()
            if revision is not None:
                dev.revision = revision

    def set_ws(self, session_id: str, connected: bool) -> None:
        with self._lock:
            dev = self._devs.get(session_id)
            if dev is None:
                return
            was_alive = self._alive(dev)
            dev.ws_connected = connected
            if connected:
                dev.last_seen = self.clock()
                if not was_alive and dev.role >= Role.DEVELOPER:
                    self._broadcast("developer_joined", f"{dev.name} si e' collegato", developer=dev.name)
            elif dev.role >= Role.DEVELOPER:
                self._broadcast("developer_left", f"{dev.name} si e' disconnesso", developer=dev.name)

    def _alive(self, dev: Optional[Developer]) -> bool:
        return bool(dev) and (dev.ws_connected or self.clock() - dev.last_seen <= self.ttl)

    def developers(self) -> list[DeveloperOut]:
        """Presenza: solo chi si e' fatto vivo almeno una volta (WebSocket o heartbeat); chi ha solo fatto
        chiamate HTTP ha ``last_seen = -inf`` e non e' ne' attivo ne' elencato."""
        with self._lock:
            return [DeveloperOut(session_id=d.session_id, developer=d.name, role=d.role.key,
                                 connected=self._alive(d),
                                 checked_out_revision=d.revision,
                                 last_seen=datetime.fromtimestamp(d.last_seen, timezone.utc))
                    for d in self._devs.values() if d.last_seen != float("-inf")]

    # ================================================================ proposte
    @staticmethod
    def _run_merge(kind: str, base: bytes, incoming: bytes, head_bytes: bytes, swap: bool,
                   resolutions: Optional[dict[str, Resolution]] = None) -> MergeResult:
        """3-way: normale = (base, mine=file inviato, theirs=testa dello store); ``swap`` (sync di un branch):
        mine = testa dello store (il branch) e theirs = file inviato (la testa del principale)."""
        if swap:
            return merge_any(kind, base, head_bytes, incoming, resolutions)
        return merge_any(kind, base, incoming, head_bytes, resolutions)

    def submit(self, dev: Developer, data: bytes, base_revision: int, message: str = "", *,
               base_data: Optional[bytes] = None, meta: Optional[dict] = None, swap: bool = False,
               auto: bool = False) -> Proposal:
        """Nuova richiesta di merge. ``base_data``: base del 3-way presa da fuori (sync di un branch: e' una revisione del
        principale, ``base_revision`` ne e' solo il numero mostrato); ``meta``: vedi ``Proposal``; ``swap``: vedi ``_run_merge``; ``auto``: consenso del solo autore, dato in
        automatico se non ci sono conflitti (salvataggio nel branch di team)."""
        with self._lock:
            if dev.role < Role.DEVELOPER:
                raise SessionError(403, "I Viewer non possono inviare richieste di merge")
            if not self.store.initialized:
                raise SessionError(409, "Master non inizializzato")
            if base_data is None:
                if self.store.is_purged(base_revision):
                    raise SessionError(409, f"La revisione base {base_revision} non e' piu' conservata dal server "
                                            f"(ne tiene {self.max_revisions}): esegui il Checkout dell'ultimo Master, "
                                            f"rifai le modifiche e rinvia")
                if not self.store.has_revision(base_revision):
                    raise SessionError(422, f"Revisione base {base_revision} inesistente")
            head = self.store.head()
            got = detect_kind(data)
            if head.kind in MERGEABLE and not compatible_kinds(head.kind, got):
                raise SessionError(422, f"Il file inviato e' di tipo «{KIND_LABELS[got]}» ma il Master del progetto e' "
                                        f"«{KIND_LABELS[head.kind]}» ({head.ext})")
            try:
                base = base_data if base_data is not None else self.store.read_revision(base_revision)
                result = self._run_merge(head.kind, base, data, self.store.read_revision(head.revision), swap)
            except L5XError as exc:
                raise SessionError(422, str(exc)) from exc
            if not result.changes and not result.conflicts and not (swap and result.upstream_changes):
                raise SessionError(422, "Nessuna modifica da proporre rispetto al Master")

            meta = dict(meta or {})
            if auto:
                meta["auto"] = True
            solo = self.auto_approve or auto
            for old in [p for p in self._proposals.values()
                        if p.author_session == dev.session_id and p.state in ACTIVE_STATES
                        and p.meta.get("kind", "normal") == meta.get("kind", "normal")
                        and p.meta.get("source_branch") == meta.get("source_branch")]:
                self._set_state(old, ProposalState.CANCELLED, "Sostituita da una nuova richiesta dello stesso autore")
                self._notify_proposal("proposal_updated", old, f"Richiesta di {old.author} sostituita da una nuova")

            pid = uuid.uuid4().hex[:10]
            path = self.store.save_upload(pid, data)
            required = {dev.session_id: dev.name}
            if not solo:                                  # branch personale / salvataggio: consenso del solo autore
                required.update({d.session_id: d.name for d in self._devs.values()
                                 if d.session_id != dev.session_id and d.role >= Role.DEVELOPER and self._alive(d)})
            p = Proposal(id=pid, author_session=dev.session_id, author=dev.name, base_revision=base_revision,
                         message=message, created_at=_now(), updated_at=_now(),
                         state=ProposalState.AWAITING_CONSENT, file_path=str(path), required=required,
                         author_role=dev.role, needs_lead=self.require_lead_approval and not solo and dev.role < Role.LEAD,
                         meta=meta, base_data=base_data, swap=swap)
            self._proposals[pid] = p
            self._apply_result(p, result, head.revision, reason=None)
            units = ", ".join(result.touched_routines[:6]) or "il progetto"
            self._notify_proposal("proposal_submitted", p, f"{dev.name} vuole unire modifiche a: {units}")
            self._log_audit("merge_requested", dev.name, pid, base_revision=base_revision,
                            head_revision=head.revision, changes=len(result.changes),
                            conflicts=len(result.conflicts), units=result.touched_routines[:20],
                            needs_lead_approval=p.needs_lead, message=message[:200],
                            **({"request_kind": meta["kind"]} if meta.get("kind") else {}),
                            **({"source_branch": meta["source_branch"]} if meta.get("source_branch") else {}))
            self._audit()
            self._auto_approve(p)
            return p

    def _auto_approve(self, p: Proposal) -> None:
        """Branch personale: se non restano conflitti il consenso dell'autore e' dato in automatico (consolida subito)."""
        if not (self.auto_approve or p.meta.get("auto")) or p.state != ProposalState.AWAITING_CONSENT or (p.result and p.result.unresolved):
            return
        author = self._devs.get(p.author_session)
        if author is not None:
            self.approve(author, p.id, "Approvazione automatica (branch personale / salvataggio nel branch)")

    def cancel_matching(self, pred: Callable[[Proposal], bool], reason: str, actor: str = "system") -> list[str]:
        """Annulla le richieste attive per cui ``pred`` e' vera (es. il branch di origine e' cambiato). Ritorna gli id."""
        with self._lock:
            done: list[str] = []
            for p in [p for p in self._proposals.values() if p.state in ACTIVE_STATES and pred(p)]:
                self._set_state(p, ProposalState.CANCELLED, f"Annullata: {reason}")
                self._notify_proposal("proposal_updated", p, f"Richiesta di {p.author} annullata: {reason}")
                self._log_audit("merge_cancelled", actor, p.id, reason=reason[:200])
                done.append(p.id)
            if done:
                self._audit()
            return done

    def get(self, pid: str) -> Proposal:
        p = self._proposals.get(pid)
        if p is None:
            raise SessionError(404, "Proposta inesistente")
        return p

    def list(self, active_only: bool = False) -> list[Proposal]:
        with self._lock:
            ps = sorted(self._proposals.values(), key=lambda p: p.created_at, reverse=True)
            return [p for p in ps if p.state in ACTIVE_STATES] if active_only else ps

    # ---- risoluzione conflitti (autore o Admin)
    def set_resolutions(self, dev: Developer, pid: str, resolutions: dict[str, Resolution]) -> Proposal:
        with self._lock:
            p = self.get(pid)
            self._require_active(p)
            if dev.session_id != p.author_session and dev.role < Role.ADMIN:
                raise SessionError(403, "Solo l'autore della richiesta (o un Admin) puo' risolvere i conflitti")
            conflicts = {c.id: c for c in p.result.conflicts}
            for cid, r in resolutions.items():
                c = conflicts.get(cid)
                if c is None:
                    raise SessionError(422, f"Conflitto sconosciuto: {cid}")
                if r.choice == "both" and not c.sequence:
                    raise SessionError(422, "'both' e' valido solo per blocchi di rung/righe")
                if r.choice == "custom" and not r.custom:
                    raise SessionError(422, "Risoluzione 'custom' senza contenuto")
                p.resolutions[cid] = Resolution(r.choice, r.custom, fingerprint=c.fingerprint)
            self._reanalyze(p, reason="Risoluzioni dei conflitti aggiornate")
            self._notify_proposal("proposal_updated", p, f"{dev.name} ha aggiornato la risoluzione dei conflitti")
            self._log_audit("conflicts_resolved", dev.name, pid, resolved=sorted(resolutions),
                            by_admin=dev.session_id != p.author_session)
            self._audit()
            self._auto_approve(p)
            return p

    # ---- consenso
    def approve(self, dev: Developer, pid: str, comment: str = "") -> Proposal:
        with self._lock:
            p = self.get(pid)
            self._require_active(p)
            self._prune_and_check(p, check=False)
            if dev.role < Role.DEVELOPER:
                raise SessionError(403, "I Viewer non possono dare consenso")
            in_required = dev.session_id in p.required
            is_lead = dev.role >= Role.LEAD and dev.session_id != p.author_session
            if not in_required and not is_lead:
                raise SessionError(403, "Non sei tra i partecipanti richiesti per questa richiesta")
            if p.result.unresolved:
                raise SessionError(409, f"Ci sono {len(p.result.unresolved)} conflitti da risolvere prima del consenso")
            ap = Approval(dev.session_id, dev.name, "approve", comment, _now(), p.result.digest)
            if in_required:
                p.approvals[dev.session_id] = ap
            if is_lead:
                p.lead_approval = ap
            p.log.append(f"{dev.name}: consenso" + (" finale (Lead)" if is_lead else ""))
            self._touch(p)
            self._notify_proposal("consent_given", p, f"{dev.name} ha dato il consenso"
                                  + (" finale al merge" if is_lead else " al merge"))
            self._log_audit("consent_given", dev.name, pid, role=dev.role.key, lead_final=is_lead,
                            digest=p.result.digest[:16], comment=comment[:200])
            self._check_quorum(p)
            self._audit()
            return p

    def reject(self, dev: Developer, pid: str, comment: str = "") -> Proposal:
        with self._lock:
            p = self.get(pid)
            self._require_active(p)
            if dev.role < Role.DEVELOPER:
                raise SessionError(403, "I Viewer non possono rifiutare richieste")
            if dev.session_id not in p.required and dev.role < Role.LEAD:
                raise SessionError(403, "Non sei tra i partecipanti richiesti per questa richiesta")
            p.approvals[dev.session_id] = Approval(dev.session_id, dev.name, "reject", comment, _now(),
                                                   p.result.digest if p.result else "")
            self._set_state(p, ProposalState.REJECTED, f"{dev.name}: rifiuto - {comment}")
            self._notify_proposal("proposal_rejected", p,
                                  f"{dev.name} ha rifiutato la richiesta di {p.author}"
                                  + (f": {comment}" if comment else ""))
            self._log_audit("merge_rejected", dev.name, pid, comment=comment[:300])
            self._audit()
            return p

    def cancel(self, dev: Developer, pid: str) -> Proposal:
        with self._lock:
            p = self.get(pid)
            self._require_active(p)
            if dev.session_id != p.author_session and dev.role < Role.ADMIN:
                raise SessionError(403, "Solo l'autore (o un Admin) puo' annullare la richiesta")
            self._set_state(p, ProposalState.CANCELLED, f"{dev.name}: annullata")
            self._notify_proposal("proposal_updated", p, f"{dev.name} ha annullato la richiesta")
            self._log_audit("merge_cancelled", dev.name, pid)
            self._audit()
            return p

    # ---- poteri dell'Admin
    def force_merge(self, dev: Developer, pid: str, comment: str) -> Proposal:
        """Consolida la proposta senza attendere il consenso (conflitti risolti obbligatoriamente)."""
        with self._lock:
            if dev.role < Role.ADMIN:
                raise SessionError(403, "Solo un Project Administrator puo' forzare un merge")
            if len(comment.strip()) < 5:
                raise SessionError(422, "Indica il motivo del merge forzato (almeno 5 caratteri)")
            p = self.get(pid)
            self._require_active(p)
            head = self.store.head()
            if p.analyzed_head != head.revision:
                self._reanalyze(p, reason=f"Master avanzato a rev {head.revision}: analisi ripetuta")
            if p.result.unresolved:
                raise SessionError(409, f"Risolvi prima i {len(p.result.unresolved)} conflitti (Admin o autore)")
            p.forced_by = dev.name
            p.log.append(f"{dev.name}: MERGE FORZATO - {comment}")
            self._finalize(p, forced_by=dev.name, note=comment)
            self._audit()
            return p

    def restore(self, dev: Developer, revision: int, reason: str):
        """Ripristina il contenuto di una revisione passata come NUOVA revisione (lo storico resta intatto)."""
        with self._lock:
            if dev.role < Role.ADMIN:
                raise SessionError(403, "Solo un Project Administrator puo' ripristinare una revisione")
            if self.store.is_purged(revision):
                raise SessionError(422, f"La revisione {revision} non e' piu' conservata dal server (ne tiene "
                                        f"{self.max_revisions}): si possono ripristinare solo le revisioni conservate")
            if not self.store.has_revision(revision):
                raise SessionError(404, f"Revisione {revision} inesistente")
            head = self.store.head()
            if revision == head.revision:
                raise SessionError(422, f"La rev {revision} e' gia' quella corrente")
            rev = self.store.commit(self.store.read_revision(revision), dev.name,
                                    f"Ripristino rev {revision}: {reason}", None)
            self._log_audit("master_restored", dev.name, f"rev{rev.revision}", restored_from=revision,
                            previous_head=head.revision, sha256=rev.sha256, reason=reason[:300])
            self._broadcast("master_updated", f"Master ripristinato alla rev {revision} (nuova rev {rev.revision})",
                            revision=rev.revision)
            self._after_master_advance(rev.revision, skip=None)
            self.prune_revisions(dev.name)
            return rev

    def reimport(self, dev: Developer, data: bytes, filename: str, reason: str):
        """Reimporta il file base (Master) su un progetto gia' avviato come NUOVA revisione (storico intatto).

        Le richieste in corso vengono rianalizzate sul nuovo Master e i loro consensi azzerati.
        """
        with self._lock:
            if dev.role < Role.LEAD:
                raise SessionError(403, "Solo un Lead Engineer o un Project Administrator puo' reimportare il Master")
            if not self.store.initialized:
                raise SessionError(409, "Master non inizializzato: usa il caricamento iniziale")
            reason = (reason or "").strip()
            if len(reason) < 5:
                raise SessionError(422, "Indica il motivo della reimportazione (almeno 5 caratteri)")
            if not data:
                raise SessionError(422, "Il file e' vuoto")
            head = self.store.head()
            self._check_replacement(data, filename, head)
            if hashlib.sha256(data).hexdigest() == head.sha256:
                raise SessionError(422, "Il file e' identico al Master corrente: nulla da reimportare")
            rev = self.store.commit(data, dev.name, f"Reimportazione del Master: {reason}", None,
                                    filename=filename, rename=True)
            self._log_audit("master_reimported", dev.name, f"rev{rev.revision}", previous_head=head.revision,
                            sha256=rev.sha256, kind=rev.kind, ext=rev.ext, name=rev.name,
                            filename=(filename or "")[:120], reason=reason[:300])
            self._broadcast("master_updated", f"Master reimportato da {dev.name} (rev {rev.revision}): {reason}",
                            revision=rev.revision)
            self._after_master_advance(rev.revision, skip=None)
            self.prune_revisions(dev.name)
            return rev

    def _check_replacement(self, data: bytes, filename: str, head: Revision) -> None:
        """File che sostituisce la testa (reimport, commit di un branch personale): stesso tipo, stesso Controller/progetto,
        stessa estensione per i tipi senza identita'. Altrimenti ``SessionError`` 422."""
        got = detect_kind(data)
        if not compatible_kinds(head.kind, got):
            raise SessionError(422, f"Il file e' di tipo «{KIND_LABELS[got]}» ma il Master del progetto e' "
                                    f"«{KIND_LABELS[head.kind]}» ({head.ext})")
        try:
            if got == L5X:
                new_ctrl = parse_l5x(data).find("Controller").get("Name")
                old_ctrl = parse_l5x(self.store.read_revision(head.revision)).find("Controller").get("Name")
            elif got == L5K:
                new_ctrl = l5k_controller_name(data)
                old_ctrl = l5k_controller_name(self.store.read_revision(head.revision))
            elif got == PLCOPEN:
                new_ctrl = plcopen_identity(data)
                old_ctrl = plcopen_identity(self.store.read_revision(head.revision))
            else:
                new_ctrl = old_ctrl = ""
        except L5XError as exc:
            raise SessionError(422, str(exc)) from exc
        if got == PLCOPEN and new_ctrl != old_ctrl:
            raise SessionError(422, f"Il file contiene il progetto PLCopen «{new_ctrl[0]}» (azienda «{new_ctrl[1]}») ma il "
                                    f"progetto e' «{old_ctrl[0]}» (azienda «{old_ctrl[1]}»): nome progetto e azienda "
                                    f"devono coincidere, altrimenti e' un altro progetto")
        if got in (L5X, L5K) and new_ctrl != old_ctrl:
            raise SessionError(422, f"Il file contiene il Controller «{new_ctrl}» ma il progetto e' «{old_ctrl}»: "
                                    f"un Controller diverso e' un altro progetto")
        if got not in (L5X, L5K, PLCOPEN) and canonical_ext(filename, got).lower() != head.ext.lower():
            raise SessionError(422, f"L'estensione del file ({canonical_ext(filename, got)}) non corrisponde a quella "
                                    f"del progetto ({head.ext})")

    def commit_direct(self, dev: Developer, data: bytes, filename: str, message: str) -> Revision:
        """Salvataggio diretto di una nuova revisione (branch personale; i permessi li verifica chi chiama).

        Stessi controlli del reimport (tipo compatibile, stesso Controller/progetto, stessa estensione, file non vuoto e
        diverso dalla testa); le richieste in corso nel branch vengono rianalizzate. Non scrive l'audit (lo fa il chiamante).
        """
        with self._lock:
            if not self.store.initialized:
                raise SessionError(409, "Il branch non ha ancora nessuna revisione")
            if not data:
                raise SessionError(422, "Il file e' vuoto")
            head = self.store.head()
            self._check_replacement(data, filename, head)
            if hashlib.sha256(data).hexdigest() == head.sha256:
                raise SessionError(422, "Il file e' identico alla testa del branch: nulla da salvare")
            rev = self.store.commit(data, dev.name, message, None, filename=filename)
            self._broadcast("master_updated", f"Nuova revisione {rev.revision} nel branch ({dev.name})",
                            revision=rev.revision)
            self._after_master_advance(rev.revision, skip=None)
            self.prune_revisions(dev.name)
            return rev

    def prune_revisions(self, actor: str = "system") -> list[int]:
        """Pota le revisioni oltre ``max_revisions``, tenendo la testa e le basi delle richieste ATTIVE (servono al 3-way)."""
        with self._lock:
            if not self.store.initialized:
                return []
            protect = {p.base_revision for p in self._proposals.values()
                       if p.state in ACTIVE_STATES and p.base_data is None}      # (con base_data la base non e' di questo store)
            protect |= set(self.extra_protect())                                 # basi dei branch aperti (revisioni di fork)
            gone = self.store.prune(self.max_revisions, protect)
            if gone:
                self._log_audit("revisions_pruned", actor, f"rev{gone[0]}-{gone[-1]}" if len(gone) > 1 else f"rev{gone[0]}",
                                pruned=gone, keep=self.max_revisions)
            return gone

    def housekeeping(self) -> None:
        """Da chiamare periodicamente: esclude dal quorum chi e' scaduto e rivaluta il consenso."""
        with self._lock:
            for p in [p for p in self._proposals.values() if p.state in ACTIVE_STATES]:
                self._prune_and_check(p)

    # ============================================================== interni
    def _require_active(self, p: Proposal) -> None:
        if p.state not in (ProposalState.AWAITING_CONSENT, ProposalState.CONFLICT):
            raise SessionError(409, f"Richiesta non piu' modificabile (stato: {STATE_LABELS[p.state]})")

    def _touch(self, p: Proposal) -> None:
        p.updated_at = _now()

    def _set_state(self, p: Proposal, st: ProposalState, log: str) -> None:
        p.state = st
        p.log.append(log)
        if st not in ACTIVE_STATES and st != ProposalState.APPROVED:
            p.base_data = None                               # la base del 3-way (copia in memoria) non serve piu'
        self._touch(p)

    def _apply_result(self, p: Proposal, result: MergeResult, head_rev: int, reason: Optional[str]) -> None:
        changed = p.result is None or p.result.digest != result.digest or p.analyzed_head != head_rev
        p.result, p.analyzed_head = result, head_rev
        if changed and (p.approvals or p.lead_approval):
            p.approvals.clear()                              # il consenso vale per UN risultato preciso
            p.lead_approval = None
            p.log.append("Consensi azzerati: il risultato del merge e' cambiato")
        st = ProposalState.CONFLICT if result.unresolved else ProposalState.AWAITING_CONSENT
        self._set_state(p, st, reason or f"Analisi completata: {len(result.changes)} modifiche, "
                                         f"{len(result.unresolved)} conflitti")

    def _reanalyze(self, p: Proposal, reason: str) -> None:
        head = self.store.head()
        try:
            with open(p.file_path, "rb") as f:
                mine = f.read()
            base = p.base_data if p.base_data is not None else self.store.read_revision(p.base_revision)
            res = self._run_merge(head.kind, base, mine, self.store.read_revision(head.revision), p.swap, p.resolutions)
        except L5XError as exc:                             # non dovrebbe accadere: i file erano gia' validi
            self._set_state(p, ProposalState.CANCELLED, f"Errore di analisi: {exc}")
            return
        self._apply_result(p, res, head.revision, reason)

    def _prune_and_check(self, p: Proposal, check: bool = True) -> None:
        for sid in [s for s in p.required if s != p.author_session and not self._alive(self._devs.get(s))]:
            name = p.required.pop(sid)
            p.approvals.pop(sid, None)
            p.log.append(f"{name} escluso dal quorum (non piu' connesso)")
            self._notify_proposal("proposal_updated", p, f"{name} e' offline: escluso dal quorum del consenso")
        if check and p.state == ProposalState.AWAITING_CONSENT:
            self._check_quorum(p)

    def _lead_ok(self, p: Proposal) -> bool:
        if not p.needs_lead:
            return True
        return p.lead_approval is not None and p.result is not None and p.lead_approval.digest == p.result.digest

    def _check_quorum(self, p: Proposal) -> None:
        if p.state != ProposalState.AWAITING_CONSENT or p.result is None:
            return
        ok = all((a := p.approvals.get(sid)) is not None and a.decision == "approve"
                 and a.digest == p.result.digest for sid in p.required)
        if not ok:
            return
        if not self._lead_ok(p):
            return                                           # manca il Consenso Finale del Lead
        self._set_state(p, ProposalState.APPROVED, "Consenso unanime")
        self._notify_proposal("proposal_updated", p, "Consenso unanime: consolidamento in corso")
        self._finalize(p)

    def _finalize(self, p: Proposal, forced_by: Optional[str] = None, note: str = "") -> None:
        if self.finalize_guard is not None and (why := self.finalize_guard(p)):
            self._set_state(p, ProposalState.CANCELLED, f"Annullata: {why}")
            self._notify_proposal("proposal_updated", p, f"Richiesta di {p.author} annullata: {why}")
            self._log_audit("merge_cancelled", "system", p.id, reason=why[:200])
            self._audit()
            return
        head = self.store.head()
        if not forced_by and head.revision != p.analyzed_head:      # Master avanzato nel frattempo
            self._reanalyze(p, reason=f"Master avanzato a rev {head.revision}: analisi ripetuta")
            self._notify_proposal("proposal_updated", p, "Il Master e' cambiato: il consenso va ripetato")
            return
        msg = p.message or f"Merge richiesta {p.id} di {p.author}"
        if forced_by:
            msg = f"[MERGE FORZATO da {forced_by}: {note}] {msg}"
        rev = self.store.commit(p.result.merged_xml, p.author, msg, p.id)
        p.result_revision = rev.revision
        self._set_state(p, ProposalState.MERGED, f"Merged in Master rev {rev.revision}")
        self._notify_proposal("proposal_merged", p, f"Merge completato: nuovo Master rev {rev.revision}"
                              + (f" (forzato da {forced_by})" if forced_by else ""))
        self._log_audit("merge_forced" if forced_by else "merge_completed",
                        forced_by or (p.lead_approval.developer if p.lead_approval else p.author), p.id,
                        revision=rev.revision, sha256=rev.sha256, author=p.author,
                        approvers=sorted(a.developer for a in p.approvals.values() if a.decision == "approve"),
                        lead_approved_by=p.lead_approval.developer if p.lead_approval else None,
                        base_revision=p.base_revision, reason=note[:300] if forced_by else "")
        if self.on_merged is not None:
            try:
                self.on_merged(p, rev)
            except Exception:                                # noqa: BLE001  (il consolidamento e' gia' avvenuto)
                logging.getLogger("aps.session").exception("Errore nel gancio on_merged della richiesta %s", p.id)
        self._broadcast("master_updated", f"Nuovo Master rev {rev.revision} ({p.author})", revision=rev.revision)
        self._after_master_advance(rev.revision, skip=p)
        self.prune_revisions(forced_by or p.author)

    def _after_master_advance(self, rev: int, skip: Optional[Proposal]) -> None:
        for q in [q for q in self._proposals.values()
                  if q is not skip and q.state in (ProposalState.AWAITING_CONSENT, ProposalState.CONFLICT)]:
            self._reanalyze(q, reason=f"Master avanzato a rev {rev}: analisi ripetuta")
            self._notify_proposal("proposal_updated", q,
                                  f"Il Master e' avanzato (rev {rev}): la richiesta di {q.author} "
                                  f"e' stata rianalizzata, consenso da ripetere")

    # ============================================================ eventi / DTO
    def _participants(self, p: Proposal) -> list[ParticipantOut]:
        out = []
        for sid, name in p.required.items():
            a = p.approvals.get(sid)
            d = self._devs.get(sid)
            out.append(ParticipantOut(session_id=sid, developer=name, role=d.role.key if d else "",
                                      is_author=sid == p.author_session,
                                      decision=a.decision if a else "pending", comment=a.comment if a else "",
                                      online=self._alive(d)))
        return out

    def summary(self, p: Proposal) -> ProposalSummary:
        r = p.result
        return ProposalSummary(
            id=p.id, author=p.author, author_session=p.author_session, base_revision=p.base_revision,
            head_revision=p.analyzed_head, state=p.state, state_label=STATE_LABELS[p.state],
            message=p.message, created_at=p.created_at, updated_at=p.updated_at,
            n_changes=len(r.changes) if r else 0, n_upstream=len(r.upstream_changes) if r else 0,
            n_conflicts=len(r.conflicts) if r else 0, n_unresolved=len(r.unresolved) if r else 0,
            touched=r.touched_routines if r else [], participants=self._participants(p),
            digest=r.digest if r else "", warnings=r.warnings if r else [], result_revision=p.result_revision,
            needs_lead_approval=p.needs_lead, forced_by=p.forced_by, author_role=p.author_role.key,
            n_comments=self.comment_count(p.id),
            lead_approved_by=p.lead_approval.developer if (p.lead_approval and self._lead_ok(p)) else None)

    def detail(self, p: Proposal) -> ProposalDetail:
        r = p.result
        ch = lambda c: ChangeOut(**c.__dict__)
        return ProposalDetail(
            **self.summary(p).model_dump(),
            changes=[ch(c) for c in r.changes], upstream_changes=[ch(c) for c in r.upstream_changes],
            conflicts=[ConflictOut(id=c.id, path=c.path, title=c.title, label=c.label, kind=c.kind,
                                   sequence=c.sequence, base_text=c.base_text, mine_text=c.mine_text,
                                   theirs_text=c.theirs_text, resolved=c.resolved,
                                   choice=c.resolution.choice if c.resolution else None)
                       for c in r.conflicts])

    def master_info(self) -> MasterInfo:
        h: Revision = self.store.head()
        return MasterInfo(revision=h.revision, sha256=h.sha256, updated_at=h.updated_at,
                          updated_by=h.updated_by, message=h.message, proposal_id=h.proposal_id,
                          kind=h.kind, ext=h.ext, name=h.name or "Master",
                          label=h.label, locked=h.locked, labeled_by=h.labeled_by, locked_by=h.locked_by)

    def _notify_proposal(self, typ: str, p: Proposal, message: str) -> None:
        targets = set(p.required) | {p.author_session}
        self.sink.publish(Event(type=typ, message=message, proposal=self.summary(p)).model_dump(mode="json"),
                          targets)

    def _broadcast(self, typ: str, message: str, **kw) -> None:
        self.sink.publish(Event(type=typ, message=message, **kw).model_dump(mode="json"), None)

    def _audit(self) -> None:
        self.store.write_audit([{
            "id": p.id, "author": p.author, "base_revision": p.base_revision, "state": p.state.value,
            "created_at": p.created_at, "updated_at": p.updated_at, "result_revision": p.result_revision,
            "required": list(p.required.values()), "forced_by": p.forced_by,
            "approvals": {a.developer: {"decision": a.decision, "comment": a.comment, "at": a.at}
                          for a in p.approvals.values()},
            "log": p.log} for p in self._proposals.values()])
