import argparse
import sys
from pathlib import Path
from typing import Optional

import uvicorn

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


def main() -> None:
    cfg = Settings()
    ap = argparse.ArgumentParser(description="Automation Project Server")
    ap.add_argument("--host", default=cfg.host)
    ap.add_argument("--port", type=int, default=cfg.port)
    ap.add_argument("--data-dir", default=str(cfg.data_dir))
    ap.add_argument("--name", default=cfg.server_name, help="Nome del server mostrato ai client (default: nome del PC)")
    ap.add_argument("--discovery-port", type=int, default=cfg.discovery_port,
                    help="Porta UDP della scoperta automatica (0 = disattivata)")
    ap.add_argument("--seed-project", metavar="NOME", help="Crea il progetto (se non esiste) e, con --master, ne inizializza il Master")
    ap.add_argument("--master", metavar="FILE", help="File Master per la revisione iniziale: L5X / L5K (Rockwell) o qualsiasi altro file")
    a = ap.parse_args()
    cfg.host, cfg.port, cfg.data_dir = a.host, a.port, Path(a.data_dir)
    cfg.server_name, cfg.discovery_port = a.name, a.discovery_port
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
        beacon = DiscoveryResponder(cfg.port, cfg.display_name, cfg.discovery_port)
        beacon.start()
    try:
        uvicorn.run(app, host=cfg.host, port=cfg.port)
    finally:
        if beacon:
            beacon.stop()


if __name__ == "__main__":
    main()
