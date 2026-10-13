import argparse
import sys
from pathlib import Path
from typing import Optional

import uvicorn

from server import tls as tlsmod
from server.config import Settings
from server.discovery import DiscoveryResponder
from server.db.models import Project
from server.main import create_app
from common.filekinds import L5X, detect_kind
from server.merger import parse_l5x


def seed_project(app, name: str, master: Optional[Path] = None) -> int:
    """Crea il progetto ``name`` (se manca) e, se indicato, ne inizializza il Master. Ritorna l'id del progetto.

    Le scritture sull'audit avvengono *dopo* il commit della transazione che crea il progetto: SQLite ammette un solo
    scrittore e due sessioni annidate darebbero 'database is locked'.
    """
    from sqlalchemy import select
    db, reg, audit = app.state.db, app.state.registry, app.state.audit
    created, slug = False, ""
    with db.scope() as s:
        proj = s.scalar(select(Project).where(Project.name == name))
        if proj is None:
            slug = "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-") or "progetto"
            proj = Project(slug=slug, name=name, created_by="system")
            s.add(proj)
            s.flush()
            created = True
        pid = proj.id
    if created:
        audit.record("project_created", "system", project_id=pid, target=slug)
    if master is not None:
        store = reg.runtime(pid).store
        if store.initialized:
            print("Master gia' inizializzato: --master ignorato", file=sys.stderr)
        else:
            data = Path(master).read_bytes()
            if detect_kind(data) == L5X:
                parse_l5x(data)                       # gli export Studio 5000 vengono validati; gli altri file no
            rev = store.commit(data, "system", "Revisione iniziale", filename=Path(master).name)
            audit.record("master_initialized", "system", project_id=pid, target="rev1",
                         details={"sha256": rev.sha256, "kind": rev.kind, "ext": rev.ext})
            print(f"Progetto '{name}' (id {pid}) inizializzato da {master}", file=sys.stderr)
    return pid


def backup_cli(cfg: Settings, dest: Path) -> int:
    """``--backup``: backup della cartella dati (anche con il server acceso). Ritorna il codice di uscita."""
    from server.backup import create_backup
    from server.db.audit import AuditService
    from server.db.base import Database
    try:
        info = create_backup(cfg.data_dir, dest)
    except (OSError, ValueError) as exc:
        print(f"Backup non riuscito: {exc}", file=sys.stderr)
        return 1
    db = Database(cfg.db_url)
    try:
        AuditService(db).record("backup_created", "cli", target=Path(info["path"]).name,
                                details={"path": str(info["path"]), "bytes": info["bytes"], "files": info["files"]})
    finally:
        db.dispose()
    print(f"Backup creato: {info['path']} ({info['bytes']} byte, {info['files']} file).\n"
          "Contiene i dati e il segreto JWT: conservalo in un posto protetto.", file=sys.stderr)
    return 0


def main() -> None:
    cfg = Settings()
    ap = argparse.ArgumentParser(description="Automation Project Server")
    ap.add_argument("--host", default=cfg.host)
    ap.add_argument("--port", type=int, default=cfg.port)
    ap.add_argument("--data-dir", default=str(cfg.data_dir))
    ap.add_argument("--name", default=cfg.server_name, help="Nome del server mostrato ai client (default: nome del PC)")
    ap.add_argument("--discovery-port", type=int, default=cfg.discovery_port,
                    help="Porta UDP della scoperta automatica (0 = disattivata)")
    ap.add_argument("--tls", dest="tls", action="store_true", default=cfg.tls_enabled,
                    help="Usa HTTPS/WSS (certificato autofirmato in <dati>/tls, creato al primo avvio; env APS_TLS_ENABLED)")
    ap.add_argument("--no-tls", dest="tls", action="store_false", help="HTTP in chiaro (solo su reti fidate)")
    ap.add_argument("--tls-cert", default=cfg.tls_certfile, metavar="FILE.pem",
                    help="Certificato PEM proprio (con --tls; env APS_TLS_CERTFILE)")
    ap.add_argument("--tls-key", default=cfg.tls_keyfile, metavar="FILE.key",
                    help="Chiave privata PEM senza password (env APS_TLS_KEYFILE)")
    ap.add_argument("--backup", metavar="FILE.zip",
                    help="Crea il backup della cartella dati (database, progetti, segreto JWT) ed esce; il file va "
                         "conservato in un posto protetto. Ripristino: server fermo, scompattare nella cartella dati")
    ap.add_argument("--seed-project", metavar="NOME", help="Crea il progetto (se non esiste) e, con --master, ne inizializza il Master")
    ap.add_argument("--master", metavar="FILE", help="File Master per la revisione iniziale: L5X / L5K (Rockwell) o qualsiasi altro file")
    a = ap.parse_args()
    cfg.host, cfg.port, cfg.data_dir = a.host, a.port, Path(a.data_dir)
    cfg.server_name, cfg.discovery_port = a.name, a.discovery_port
    if a.tls_cert and not a.tls:
        ap.error("--tls-cert richiede --tls (HTTPS attivo)")
    cfg.tls_enabled, cfg.tls_certfile, cfg.tls_keyfile = a.tls, a.tls_cert, a.tls_key
    if a.backup:
        sys.exit(backup_cli(cfg, Path(a.backup)))
    ssl_kw: dict = {}
    if cfg.tls_enabled:                                           # niente HTTP in chiaro se il certificato non e' utilizzabile
        try:
            mat = tlsmod.resolve_tls(cfg.data_dir, True, cfg.tls_certfile, cfg.tls_keyfile)
        except tlsmod.TlsError as exc:
            print(f"HTTPS non attivabile: {exc}", file=sys.stderr)
            sys.exit(1)
        if mat.generated:
            print(f"Generato un certificato autofirmato: {mat.certfile}\nImpronta SHA-256: {mat.info.fingerprint}\n"
                  "I client la mostreranno alla prima connessione: verificala prima di accettare.", file=sys.stderr)
        else:
            print(f"HTTPS attivo - impronta SHA-256: {mat.info.fingerprint}", file=sys.stderr)
        note = tlsmod.expiry_notice(mat.info)
        if note:
            print(note, file=sys.stderr)
        ssl_kw = {"ssl_certfile": mat.certfile}
        if mat.keyfile != mat.certfile:
            ssl_kw["ssl_keyfile"] = mat.keyfile
    else:
        warn = tlsmod.insecure_warning(cfg.host, False)
        if warn:
            print(f"ATTENZIONE - {warn}", file=sys.stderr)
    app = create_app(cfg)

    if app.state.bootstrap_password:
        bar = "=" * 70
        print(f"\n{bar}\n PRIMO AVVIO - utente amministratore creato\n"
              f"   utente:   {cfg.admin_username}\n   password: {app.state.bootstrap_password}\n"
              f" (verra' richiesto il cambio password al primo accesso; non sara' mostrata di nuovo)\n{bar}\n",
              file=sys.stderr)

    if a.seed_project:
        seed_project(app, a.seed_project, Path(a.master) if a.master else None)
    beacon = None
    if not cfg.host.startswith("127.") and cfg.discovery_port > 0:
        beacon = DiscoveryResponder(cfg.port, cfg.display_name, cfg.discovery_port, scheme=cfg.scheme)
        beacon.start()
    try:
        uvicorn.run(app, host=cfg.host, port=cfg.port, **ssl_kw)
    finally:
        if beacon:
            beacon.stop()


if __name__ == "__main__":
    main()
