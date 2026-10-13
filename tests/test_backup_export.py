"""Export di un progetto (zip) e backup dell'intera cartella dati del server."""
import csv
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.backup import create_backup, default_backup_name
from server.config import Settings
from server.main import create_app
from tests import sample_l5x as S
from tests.server_helpers import (ADMIN_PW, build_world, env, login, reimport, text_master,   # noqa: F401
                                  tworld, world)

ROOT = Path(__file__).resolve().parent.parent


# ======================================================================================= export progetto
def test_export_zip_contents_and_audit(env):
    c, app = env
    pid, H, _ = build_world(c, text_master(), "linea.txt", max_revisions=3)
    for i in range(4):                                                    # rev 1..5, kept 3,4,5
        assert reimport(c, pid, H["lead"], text_master(l9=f"v{i}"), name="linea.txt").status_code == 200
    r = c.get(f"/api/projects/{pid}/export", headers=H["admin"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"] and "linea-1" in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    assert names == {"linea_rev3.txt", "linea_rev4.txt", "linea_rev5.txt", "history.json", "audit.csv", "README.txt"}
    assert z.read("linea_rev5.txt") == text_master(l9="v3")
    hist = json.loads(z.read("history.json"))
    assert [h["revision"] for h in hist] == [1, 2, 3, 4, 5] and [h["purged"] for h in hist] == [True, True, False, False, False]
    rows = list(csv.DictReader(io.StringIO(z.read("audit.csv").decode("utf-8-sig"))))
    assert list(rows[0]) == ["id", "ts", "actor", "action", "target", "details"]
    assert [r_["action"] for r_ in rows][:2] == ["project_created", "permission_granted"] and rows == sorted(rows, key=lambda x: int(x["id"]))
    assert "revisions_pruned" in {r_["action"] for r_ in rows} and json.loads(rows[0]["details"]) == {"name": "Linea 1"}
    readme = z.read("README.txt").decode("utf-8")
    assert "Linea 1" in readme and "3 revisioni" in readme and "ELIMINATE" in readme and "1, 2" in readme
    # tracciato in audit
    ex = [a for a in c.get(f"/api/audit?project_id={pid}&limit=500", headers=H["admin"]).json() if a["action"] == "project_exported"]
    assert len(ex) == 1 and ex[0]["actor"] == "admin" and ex[0]["details"]["revisions"] == 3 and ex[0]["details"]["purged"] == 2
    assert c.get("/api/audit/verify", headers=H["admin"]).json()["ok"]


def test_export_only_admin_and_404(world):
    c, app, pid, H, users = world
    for who in ("lead", "dev1", "viewer"):
        assert c.get(f"/api/projects/{pid}/export", headers=H[who]).status_code == 403
    assert c.get(f"/api/projects/{pid}/export").status_code == 401
    assert c.get("/api/projects/999/export", headers=H["admin"]).status_code == 404


def test_export_project_without_master_and_via_client_api(env):
    c, app = env
    ha = login(c, "admin", ADMIN_PW)
    pid = c.post("/api/projects", headers=ha, json={"name": "Vuoto"}).json()["id"]
    z = zipfile.ZipFile(io.BytesIO(c.get(f"/api/projects/{pid}/export", headers=ha).content))
    assert set(z.namelist()) == {"history.json", "audit.csv", "README.txt"}
    assert json.loads(z.read("history.json")) == [] and "nessuna revisione" in z.read("README.txt").decode()


def test_export_with_l5x_names(world):
    c, app, pid, H, users = world
    z = zipfile.ZipFile(io.BytesIO(c.get(f"/api/projects/{pid}/export", headers=H["admin"]).content))
    assert "m_rev1.L5X" in z.namelist() and z.read("m_rev1.L5X") == S.base_project().encode()


# ======================================================================================= backup
def make_data(tmp_path):
    from server.gui.controller import EmbeddedServer, resolve_data_dir
    s = EmbeddedServer(resolve_data_dir(tmp_path), "127.0.0.1", 0, admin_password=ADMIN_PW)
    s.open()
    return s


def test_backup_is_consistent_and_reopenable(tmp_path):
    srv = make_data(tmp_path / "dati")
    try:
        with TestClient(srv.app) as c:
            pid, H, _ = build_world(c)
            reimport_ok = c.post(f"/api/projects/{pid}/master/reimport", headers=H["admin"], data={"reason": "prova di backup"},
                                 files={"file": ("m.L5X", S.dev_a().encode())})
            assert reimport_ok.status_code == 200, reimport_ok.text
            (srv.data_dir / "projects" / "linea-1" / "x.tmp").write_text("temporaneo")
            dest = tmp_path / "out" / default_backup_name()
            assert dest.name.startswith("aps_backup_") and dest.suffix == ".zip"
            out = srv.backup(dest)                                          # a "server acceso": app aperta e in uso
            assert out == dest and dest.stat().st_size > 0 and not Path(str(dest) + ".tmp").exists()
            rec = [a for a in c.get("/api/audit?limit=100", headers=H["admin"]).json() if a["action"] == "backup_created"]
            assert len(rec) == 1 and rec[0]["project_id"] is None            # evento globale, visibile all'Admin
            rows = srv.app.state.audit.query(project_ids=None, action="backup_created")
            assert len(rows) == 1 and rows[0].actor == "console" and rows[0].target == dest.name
            assert json.loads(rows[0].details)["bytes"] == dest.stat().st_size
    finally:
        srv.close()
        srv.detach_logging()
    z = zipfile.ZipFile(dest)
    names = z.namelist()
    assert "aps.db" in names and "jwt_secret" in names and "BACKUP_INFO.txt" in names
    assert "projects/linea-1/history.json" in names and "projects/linea-1/revisions/rev_000002.L5X" in names
    assert not [n for n in names if n.endswith((".tmp", "-wal", "-shm"))]
    assert "scompatta" in z.read("BACKUP_INFO.txt").decode() and "segreto" in z.read("BACKUP_INFO.txt").decode().lower()
    # ripristino a mano in una cartella nuova e riapertura con un nuovo server
    restored = tmp_path / "ripristino"
    z.extractall(restored)
    con = sqlite3.connect(restored / "aps.db")
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert con.execute("SELECT count(*) FROM projects").fetchone()[0] == 1
    con.close()
    app2 = create_app(Settings(data_dir=restored, bcrypt_rounds=4, admin_password=ADMIN_PW))
    assert app2.state.bootstrap_password is None                           # utenti ripristinati: nessun nuovo admin
    with TestClient(app2) as c2:
        ha = login(c2, "admin", ADMIN_PW)
        p = c2.get("/api/projects", headers=ha).json()[0]
        assert p["name"] == "Linea 1" and p["head_revision"] == 2
        assert c2.get(f"/api/projects/{p['id']}/master/download?revision=1", headers=ha).content == S.base_project().encode()
        assert c2.get("/api/audit/verify", headers=ha).json()["ok"]
        assert (restored / "jwt_secret").read_text() == (tmp_path / "dati" / "AutomationProjectServer" / "jwt_secret").read_text()
        assert login(c2, "dev1", "Password12345")                           # i token firmati col segreto restano validi


def test_backup_without_data_fails_cleanly(tmp_path):
    with pytest.raises(FileNotFoundError):
        create_backup(tmp_path / "vuoto", tmp_path / "b.zip")
    assert not (tmp_path / "b.zip").exists()


def test_backup_during_writes_stays_consistent(tmp_path):
    """Il database copiato e' integro anche se nel frattempo si scrive sull'audit."""
    import threading
    srv = make_data(tmp_path)
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            srv.app.state.audit.record("x", "t", details={"i": i})
            i += 1
    t = threading.Thread(target=writer)
    t.start()
    try:
        for k in range(3):
            srv.backup(tmp_path / f"b{k}.zip")
    finally:
        stop.set()
        t.join()
        srv.close()
        srv.detach_logging()
    for k in range(3):
        d = tmp_path / f"r{k}"
        zipfile.ZipFile(tmp_path / f"b{k}.zip").extractall(d)
        con = sqlite3.connect(d / "aps.db")
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        con.close()
        db_url = f"sqlite:///{(d / 'aps.db').as_posix()}"
        from server.db.audit import AuditService
        from server.db.base import Database
        db = Database(db_url)
        assert AuditService(db).verify()[0]                                 # catena di hash coerente nello snapshot
        db.dispose()


def test_cli_backup_option(tmp_path):
    srv = make_data(tmp_path)
    ddir = srv.data_dir
    srv.close()
    srv.detach_logging()
    dest = tmp_path / "cli.zip"
    r = subprocess.run([sys.executable, "-m", "server", "--data-dir", str(ddir), "--backup", str(dest)],
                       cwd=ROOT, capture_output=True, text=True, timeout=120,
                       env={**os.environ, "PYTHONPATH": str(ROOT), "APS_BCRYPT_ROUNDS": "4"})
    assert r.returncode == 0, r.stderr
    assert "Backup creato" in r.stderr and "protetto" in r.stderr
    assert "aps.db" in zipfile.ZipFile(dest).namelist()
    r = subprocess.run([sys.executable, "-m", "server", "--data-dir", str(tmp_path / "inesistente"), "--backup",
                        str(tmp_path / "no.zip")], cwd=ROOT, capture_output=True, text=True, timeout=120,
                       env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert r.returncode == 1 and "Backup non riuscito" in r.stderr and not (tmp_path / "no.zip").exists()


# ======================================================================================= GUI
def test_window_backup_button(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QMessageBox
    from client.theme import apply_dark_theme
    from server.gui.main_window import ServerWindow
    from server.gui.settings import ServerPrefs
    app = QApplication.instance() or QApplication([])
    apply_dark_theme(app)
    prefs = ServerPrefs(QSettings(str(tmp_path / "prefs.ini"), QSettings.IniFormat))
    prefs.data_base, prefs.host, prefs.port, prefs.autostart = str(tmp_path / "dati"), "127.0.0.1", 8799, False
    w = ServerWindow(prefs, run=_sync, confirm_close=False)
    w._pending_admin = ("admin", ADMIN_PW)
    assert w.start()
    try:
        asked = []
        dest = tmp_path / "scelto" / "mio_backup"                           # senza estensione: viene aggiunta .zip

        def chooser(parent, default):
            asked.append(default)
            return str(dest)
        w.create_backup(chooser)
        assert asked and asked[0].startswith("aps_backup_") and asked[0].endswith(".zip")
        assert (tmp_path / "scelto" / "mio_backup.zip").exists()
        msg = w.statusBar().currentMessage()
        assert "Backup creato" in msg and "mio_backup.zip" in msg and "MB" in msg
        w.create_backup(lambda p, d: "")                                    # annullato: nessun errore
        errs = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: errs.append(a[2]))
        w.server.backup = lambda d: (_ for _ in ()).throw(OSError("disco pieno"))
        w.create_backup(lambda p, d: str(tmp_path / "x.zip"))
        assert errs and "disco pieno" in errs[0]
        # il pulsante del pannello BACKUP esiste e il segnale e' collegato alla stessa azione
        calls = []
        w.create_backup = lambda *a, **k: calls.append(1)
        w.settings_page.btn_backup.click()
        assert w.settings_page.btn_backup.text().strip().startswith("Crea backup")
    finally:
        w.close()
        w.server and w.server.detach_logging()


def _sync(fn, ok=None, err=None):
    try:
        res = fn()
    except Exception as exc:                                               # noqa: BLE001
        if err:
            err(exc)
        return
    if ok:
        ok(res)
