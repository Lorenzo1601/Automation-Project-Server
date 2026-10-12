"""Master di qualsiasi tipo: L5K (Rockwell ASCII), L5X, testo generico, binario; filtri 'Tutti i file'; icona exe."""
import os
import struct
import time
from pathlib import Path

import pytest

from common import filekinds as FK
from server.merger import L5XError, Resolution
from server.storage import MasterStore
from server.textmerge import UnsupportedMerge, merge_any, merge_text
from tests import sample_l5x as S
from tests.test_api_e2e import ADMIN_PW, env, login  # noqa: F401  (fixture 'env')

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    from client.theme import apply_dark_theme
    app = QApplication.instance() or QApplication([])
    apply_dark_theme(app)
    return app


@pytest.fixture(autouse=True)
def _close_widgets():
    yield
    try:
        from PySide6.QtCore import QEvent
        from PySide6.QtWidgets import QApplication
    except ImportError:
        return
    if QApplication.instance() is None:
        return
    for w in QApplication.topLevelWidgets():
        w.close()
        w.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def l5k(r01a="XIC(A)OTE(B);", r01c="XIC(C)OTE(D);", r02="XIC(G)OTE(H);", name="Linea1") -> str:
    return (f"IE_VER := 2.31;\n\nCONTROLLER {name} (ProcessorType := \"1756-L83E\")\n"
            "\tPROGRAM Main (MainRoutineName := R01)\n"
            "\t\tROUTINE R01\n"
            f"\t\t\tN: {r01a}\n"
            "\t\t\tN: XIC(X)OTE(Y);\n"
            f"\t\t\tN: {r01c}\n"
            "\t\tEND_ROUTINE\n"
            "\t\tROUTINE R02\n"
            f"\t\t\tN: {r02}\n"
            "\t\tEND_ROUTINE\n"
            "\tEND_PROGRAM\nEND_CONTROLLER\n")


# ------------------------------------------------------------------------------------ riconoscimento
def test_detect_kind_and_extensions():
    assert FK.detect_kind(S.base_project().encode()) == FK.L5X
    assert FK.detect_kind(l5k().encode()) == FK.L5K
    assert FK.detect_kind(b"PROGRAM x\nEND_PROGRAM\n") == FK.TEXT
    assert FK.detect_kind(b"\x00\x01\x02binary") == FK.BINARY
    assert FK.canonical_ext("Linea.l5k", FK.L5K) == ".L5K"           # L5K Rockwell, qualunque sia il maiuscolo/minuscolo
    assert FK.canonical_ext("qualsiasi.nome", FK.L5X) == ".L5X"
    assert FK.canonical_ext("prog.st", FK.TEXT) == ".st" and FK.canonical_ext("dati", FK.BINARY) == ".bin"
    assert FK.l5k_controller_name(l5k().encode()) == "Linea1"
    assert ".L5K" in FK.ROCKWELL_EXTS and ".L5X" in FK.ROCKWELL_EXTS


def test_file_dialog_filters_include_all_files():
    f = FK.import_filters()
    assert f.startswith("Tutti i file (*)") and "*.L5K" in f and "*.L5X" in f
    g = FK.file_dialog_filters(".L5K")
    assert g.split(";;")[0].startswith("File del progetto (*.L5K") and g.endswith("Tutti i file (*)")


# ------------------------------------------------------------------------------------ merge testuale
def test_l5k_merge_clean_with_program_routine_context():
    base = l5k().encode()
    mine = l5k(r01a="XIC(A)XIC(Z)OTE(B);").encode()                  # io cambio R01
    theirs = l5k(r02="XIC(G)OTE(H2);").encode()                      # il Master cambia R02
    r = merge_any(FK.L5K, base, mine, theirs)
    assert r.is_clean and not r.conflicts
    out = r.merged_xml.decode()
    assert "XIC(A)XIC(Z)OTE(B);" in out and "OTE(H2);" in out
    assert [c.path.rsplit("/", 1)[0] for c in r.changes] == ["Program[Main]/Routine[R01]"]
    assert [c.path.rsplit("/", 1)[0] for c in r.upstream_changes] == ["Program[Main]/Routine[R02]"]
    assert r.touched_routines == ["Program[Main]/Routine[R01]"]
    assert "\r" not in out and out.endswith("END_CONTROLLER\n")


def test_l5k_conflict_resolutions_and_crlf_preserved():
    base = l5k().encode()
    mine = l5k(r01a="XIC(A)OTE(MIA);").encode()
    theirs = l5k(r01a="XIC(A)OTE(SUA);").replace("\n", "\r\n").encode() if False else \
        l5k(r01a="XIC(A)OTE(SUA);").encode().replace(b"\n", b"\r\n")
    r = merge_text(base, mine, theirs, kind=FK.L5K)
    assert len(r.conflicts) == 1 and not r.is_clean and r.conflicts[0].kind == "modify/modify"
    assert b"OTE(SUA);" in r.merged_xml and b"\r\n" in r.merged_xml            # default provvisorio: Master, con i suoi fine riga
    cid = r.conflicts[0].id
    for choice, expect in (("mine", b"OTE(MIA);"), ("theirs", b"OTE(SUA);")):
        rr = merge_text(base, mine, theirs, {cid: Resolution(choice)}, FK.L5K)
        assert rr.is_clean and expect in rr.merged_xml
    both = merge_text(base, mine, theirs, {cid: Resolution("both")}, FK.L5K)
    assert b"OTE(SUA);" in both.merged_xml and b"OTE(MIA);" in both.merged_xml
    cust = merge_text(base, mine, theirs, {cid: Resolution("custom", ["\t\t\tN: NOP();"])}, FK.L5K)
    assert b"NOP();" in cust.merged_xml and b"OTE(SUA)" not in cust.merged_xml and cust.is_clean
    stale = merge_text(base, mine, theirs, {cid: Resolution("mine", fingerprint="vecchia")}, FK.L5K)
    assert not stale.is_clean                                                     # risoluzione per un conflitto diverso: ignorata


def test_l5k_other_controller_or_invalid_rejected():
    with pytest.raises(L5XError, match="Nome controller diverso"):
        merge_text(l5k().encode(), l5k(name="Altra").encode(), l5k().encode(), kind=FK.L5K)
    with pytest.raises(L5XError, match="CONTROLLER"):
        merge_text(l5k().encode(), b"IE_VER := 1;\nnulla\n", l5k().encode(), kind=FK.L5K)


def test_generic_text_and_latin1_roundtrip_and_binary_unsupported():
    b = "uno\ndue\ntre\n".encode("latin-1")
    m = "uno\ndue e\xe0\ntre\n".encode("latin-1")                      # byte non UTF-8 (cp1252/latin-1)
    t = "uno\ndue\ntre\nquattro\n".encode("latin-1")
    r = merge_any(FK.TEXT, b, m, t)
    assert r.is_clean and r.merged_xml == "uno\ndue e\xe0\ntre\nquattro\n".encode("latin-1")
    with pytest.raises(UnsupportedMerge):
        merge_any(FK.BINARY, b"\x00a", b"\x00b", b"\x00a")


# ------------------------------------------------------------------------------------ storage
def test_store_keeps_extension_per_project(tmp_path):
    st = MasterStore(tmp_path / "p")
    rev = st.commit(l5k().encode(), "admin", "init", filename="Linea1.L5K")
    assert (rev.kind, rev.ext) == ("l5k", ".L5K")
    assert (tmp_path / "p" / "Linea1.L5K").exists() and rev.name == "Linea1" and (tmp_path / "p" / "revisions" / "rev_000001.L5K").exists()
    st.commit(l5k(r02="XIC(G)OTE(Q);").encode(), "admin", "due", filename="ignorato.xyz")   # le revisioni ereditano
    assert st.ext == ".L5K" and st.has_revision(2) and not st.has_revision(3)
    again = MasterStore(tmp_path / "p")                                # ricaricato da history.json
    assert again.kind == "l5k" and again.read_revision(1) == l5k().encode()
    legacy = MasterStore(tmp_path / "q")
    legacy.commit(S.base_project().encode(), "admin")
    assert legacy.ext == ".L5X" and (tmp_path / "q" / "Master.L5X").exists()


# ------------------------------------------------------------------------------------ API
def _project(c, h, name="P"):
    r = c.post("/api/projects", headers=h, json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_api_l5k_master_end_to_end(env):                            # noqa: F811
    c, app = env
    h, _ = login(c, "admin", ADMIN_PW)
    pid = _project(c, h)
    r = c.post(f"/api/projects/{pid}/master/init", headers=h, files={"file": ("Linea1.L5K", l5k().encode())})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "l5k" and r.json()["ext"] == ".L5K"
    proj = [p for p in c.get("/api/projects", headers=h).json() if p["id"] == pid][0]
    assert proj["master_kind"] == "l5k" and proj["master_ext"] == ".L5K"
    d = c.get(f"/api/projects/{pid}/master/download", headers=h)
    assert d.content == l5k().encode() and d.headers["X-File-Ext"] == ".L5K"
    assert d.headers["content-disposition"].endswith('_rev1.L5K"')

    assert c.post(f"/api/projects/{pid}/heartbeat", headers=h).status_code == 204
    mine = l5k(r01a="XIC(A)XIC(Z)OTE(B);")
    r = c.post(f"/api/projects/{pid}/proposals", headers=h, data={"base_revision": 1, "message": "L5K"},
               files={"file": ("mio.L5K", mine.encode())})
    assert r.status_code == 201, r.text
    prop = r.json()
    assert prop["n_changes"] == 1 and prop["n_conflicts"] == 0
    pv = c.get(f"/api/projects/{pid}/proposals/{prop['id']}/preview", headers=h)
    assert pv.headers["content-disposition"].endswith('.L5K"') and b"XIC(Z)" in pv.content
    d = c.post(f"/api/projects/{pid}/proposals/{prop['id']}/approve", headers=h, json={}).json()
    assert d["state"] == "merged" and d["result_revision"] == 2
    assert b"XIC(Z)" in c.get(f"/api/projects/{pid}/master/download", headers=h).content
    assert (app.state.settings.data_dir / "projects").exists()
    ev = [e for e in c.get("/api/audit", headers=h, params={"action": "master_initialized"}).json()]
    assert ev and ev[0]["details"]["kind"] == "l5k" and ev[0]["details"]["controller"] == "Linea1"


def test_api_wrong_kind_proposal_rejected(env):                      # noqa: F811
    c, app = env
    h, _ = login(c, "admin", ADMIN_PW)
    pid = _project(c, h)
    assert c.post(f"/api/projects/{pid}/master/init", headers=h,
                  files={"file": ("m.L5K", l5k().encode())}).status_code == 200
    c.post(f"/api/projects/{pid}/heartbeat", headers=h)
    r = c.post(f"/api/projects/{pid}/proposals", headers=h, data={"base_revision": 1},
               files={"file": ("x.L5X", S.dev_a().encode())})              # un L5X dentro un progetto L5K
    assert r.status_code == 422 and "Master del progetto" in r.json()["detail"]


def test_api_any_file_master_text_and_binary(env):                   # noqa: F811
    c, app = env
    h, _ = login(c, "admin", ADMIN_PW)
    pt, pb = _project(c, h, "Testo"), _project(c, h, "Binario")
    r = c.post(f"/api/projects/{pt}/master/init", headers=h, files={"file": ("main.st", b"PROGRAM Main\nEND_PROGRAM\n")})
    assert r.status_code == 200 and (r.json()["kind"], r.json()["ext"]) == ("text", ".st")
    c.post(f"/api/projects/{pt}/heartbeat", headers=h)
    r = c.post(f"/api/projects/{pt}/proposals", headers=h, data={"base_revision": 1},
               files={"file": ("main.st", b"PROGRAM Main\n  x := 1;\nEND_PROGRAM\n")})
    assert r.status_code == 201 and r.json()["n_changes"] == 1

    blob = b"\x00\x01\x02\x03 progetto codesys"
    r = c.post(f"/api/projects/{pb}/master/init", headers=h, files={"file": ("lib.project", blob)})
    assert r.status_code == 200 and (r.json()["kind"], r.json()["ext"]) == ("binary", ".project")
    assert c.get(f"/api/projects/{pb}/master/download", headers=h).content == blob
    c.post(f"/api/projects/{pb}/heartbeat", headers=h)
    r = c.post(f"/api/projects/{pb}/proposals", headers=h, data={"base_revision": 1},
               files={"file": ("lib.project", blob + b"x")})
    assert r.status_code == 422 and "merge automatico" in r.json()["detail"]
    assert c.post(f"/api/projects/{pb}/master/init", headers=h, files={"file": ("e", b"")}).status_code in (409, 422)


def test_api_invalid_l5x_master_still_rejected(env):                 # noqa: F811
    c, app = env
    h, _ = login(c, "admin", ADMIN_PW)
    pid = _project(c, h)
    bad = b'<?xml version="1.0"?><RSLogix5000Content TargetType="Program"><Controller Name="x"/></RSLogix5000Content>'
    assert c.post(f"/api/projects/{pid}/master/init", headers=h, files={"file": ("a.L5X", bad)}).status_code == 422


def test_cli_seed_project_accepts_l5k(tmp_path):
    from server.__main__ import seed_project
    from server.config import Settings
    from server.main import create_app
    f = tmp_path / "Linea1.L5K"
    f.write_text(l5k(), "utf-8")
    app = create_app(Settings(data_dir=tmp_path / "d", bcrypt_rounds=4, admin_password=ADMIN_PW))
    pid = seed_project(app, "Linea 1", f)
    st = app.state.registry.runtime(pid).store
    assert st.kind == "l5k" and st.ext == ".L5K"


# ------------------------------------------------------------------------------------ client: file locali
def test_candidates_for_l5k_project_and_all_files(tmp_path):
    from client.localfiles import find_merge_candidates, looks_like_kind, master_copy_name, pick_default
    f = tmp_path / "w"
    f.mkdir()
    (f / master_copy_name(".L5K")).write_text(l5k(), "utf-8")
    old = time.time() - 100
    os.utime(f / "Master.L5K", (old, old))
    (f / "Master_rev3.L5K").write_text(l5k(), "utf-8")
    (f / "mio.L5K").write_text(l5k(r01a="XIC(A)XIC(Z)OTE(B);"), "utf-8")
    (f / "falso.L5K").write_text("non e' un L5K", "utf-8")
    (f / "vecchio.L5X").write_text(S.dev_a(), "utf-8")
    (f / "note.txt").write_text("ciao", "utf-8")
    names = {c.name for c in find_merge_candidates(f, ext=".L5K", kind="l5k")}
    assert names == {"mio.L5K", "falso.L5K"}                          # solo L5K, senza Master.L5K / Master_revN
    c = {x.name: x for x in find_merge_candidates(f, ext=".L5K", kind="l5k")}
    assert c["mio.L5K"].looks_ok and c["mio.L5K"].modified and not c["falso.L5K"].looks_ok
    allf = {x.name for x in find_merge_candidates(f, ext=".L5K", kind="l5k", all_files=True)}
    assert allf == {"mio.L5K", "falso.L5K", "vecchio.L5X", "note.txt"}
    best = pick_default(find_merge_candidates(f, ext=".L5K", kind="l5k"))
    assert best is not None and best.name == "mio.L5K"
    assert looks_like_kind(f / "note.txt", "text") and looks_like_kind(f / "note.txt", "binary")
    assert not looks_like_kind(f / "note.txt", "l5k") and looks_like_kind(f / "mio.L5K", "l5k")


def test_merge_dialog_for_other_file_types(qapp, tmp_path):
    from client.views.merge_dialog import MergeRequestDialog
    f = tmp_path / "p"
    f.mkdir()
    (f / "a.st").write_text("PROGRAM\n", "utf-8")
    (f / "b.txt").write_text("altro\n", "utf-8")
    d = MergeRequestDialog(f, "", 1, "ST", ext=".st", kind="text")
    assert d.table.rowCount() == 1 and d.selected_path() == f / "a.st" and d.ok_btn.isEnabled()
    d.chk_all.setChecked(True)                                          # «Mostra tutti i file»
    assert d.table.rowCount() == 2
    d.select_path(f / "b.txt")
    assert d.ok_btn.isEnabled()                                         # per i file generici qualsiasi file e' valido
    l5 = MergeRequestDialog(f, "", 1, "Linea", ext=".L5K", kind="l5k")
    assert l5.table.rowCount() == 0 and ".L5K" in l5.info.text()
    l5.add_external(f / "b.txt")
    assert not l5.ok_btn.isEnabled() and "L5K" in l5.info.text()


# ------------------------------------------------------------------------------------ icona exe
def test_app_ico_is_windows_friendly():
    from PIL import Image
    data = (ROOT / "client" / "theme" / "assets" / "app.ico").read_bytes()
    assert struct.unpack("<HHH", data[:6])[:2] == (0, 1)
    n = struct.unpack("<HHH", data[:6])[2]
    sizes = set()
    for i in range(n):
        w, h, _c, _r, planes, bpp, size, off = struct.unpack("<BBBBHHII", data[6 + 16 * i:22 + 16 * i])
        w = w or 256
        sizes.add(w)
        assert bpp == 32 and planes == 1 and off + size <= len(data)
        payload = data[off:off + size]
        if w >= 256:
            assert payload[:4] == b"\x89PNG"
        else:                                                           # voce BMP/DIB: BITMAPINFOHEADER con altezza doppia
            hdr = struct.unpack("<IiiHH", payload[:16])
            assert hdr[0] == 40 and hdr[1] == w and hdr[2] == 2 * w and hdr[4] == 32
    assert {16, 24, 32, 48, 64, 128, 256} <= sizes
    im = Image.open(ROOT / "client" / "theme" / "assets" / "app.ico")
    assert im.info["sizes"] >= {(16, 16), (32, 32), (256, 256)}


def test_make_icon_regenerates_valid_file(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("make_icon", ROOT / "tools" / "make_icon.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    dst = tmp_path / "x.ico"
    entries = mod.build(mod.SRC, dst)
    assert [k for _s, k, _n in entries].count("png") == 1 and dst.read_bytes()[:4] == b"\x00\x00\x01\x00"
    assert dst.read_bytes() == (ROOT / "client" / "theme" / "assets" / "app.ico").read_bytes()    # deterministico


@pytest.mark.parametrize("bat,exe", [("build_server.bat", "AutomationProjectServer"),
                                     ("build_client.bat", "AutomationProjectClient")])
def test_build_scripts_embed_icon(bat, exe):
    raw = (ROOT / bat).read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")      # CRLF: cmd.exe con LF puro sbaglia label e goto
    s = raw.decode()
    assert '--icon "%ICON%"' in s and 'set "ICON=%~dp0client\\theme\\assets\\app.ico"' in s
    assert "make_icon.py" in s and f"check_exe_icon.py" in s and f"dist\\{exe}.exe" in s
    assert "pillow" in (ROOT / "requirements-build.txt").read_text().lower()
