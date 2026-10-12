"""Il Master mantiene il nome del file importato; ai client arriva come <nome>_revN<ext>."""
import json
import os
import time

from common.filekinds import rev_filename, safe_stem
from server.storage import MasterStore
from tests import sample_l5x as S
from tests.test_api_e2e import ADMIN_PW, env, login  # noqa: F401
from tests.test_filetypes import l5k


def test_safe_stem_keeps_name_and_drops_extension_and_bad_chars():
    assert safe_stem("Linea1.L5K") == "Linea1"
    assert safe_stem("Linea 1 (v2).L5X") == "Linea 1 (v2)"
    assert safe_stem("C:\\Progetti\\Mio Impianto.l5k") == "Mio Impianto"
    assert safe_stem("a:b?c*.L5K") == "a_b_c_"[:-1] or safe_stem("a:b?c*.L5K").startswith("a_b_c")
    assert safe_stem("Linea1_rev5.L5K") == "Linea1" and safe_stem("Linea1 REV12.L5K") == "Linea1"   # un solo _revN in fondo
    assert safe_stem("CON.L5K") == "CON_" and safe_stem("") == "Master" and safe_stem("x" * 300) == "x" * 120
    assert rev_filename("Linea1", 7, ".L5K") == "Linea1_rev7.L5K"


def test_store_persists_name_and_legacy_history_defaults_to_master(tmp_path):
    st = MasterStore(tmp_path / "p")
    rev = st.commit(l5k().encode(), "admin", "init", filename="Linea Rossa.L5K")
    assert rev.name == "Linea Rossa" and (tmp_path / "p" / "Linea Rossa.L5K").exists()
    st.commit(l5k(r02="XIC(G)OTE(Q);").encode(), "admin", "due", filename="altro_nome.L5K")      # le revisioni ereditano il nome
    assert st.name == "Linea Rossa" and MasterStore(tmp_path / "p").name == "Linea Rossa"
    assert (tmp_path / "p" / "Linea Rossa.L5K").read_bytes() == l5k(r02="XIC(G)OTE(Q);").encode()
    # history.json scritto da una versione precedente (senza kind/ext/name)
    legacy = tmp_path / "old"
    (legacy / "revisions").mkdir(parents=True)
    (legacy / "revisions" / "rev_000001.L5X").write_bytes(S.base_project().encode())
    (legacy / "history.json").write_text(json.dumps([{"revision": 1, "sha256": "x", "updated_at": "2026-01-01T00:00:00+00:00",
                                                       "updated_by": "admin", "message": "", "proposal_id": None}]))
    old = MasterStore(legacy)
    assert (old.name, old.ext, old.kind) == ("Master", ".L5X", "l5x") and old.read_revision(1)


def test_api_exports_with_imported_name(env):                            # noqa: F811
    c, app = env
    h, _ = login(c, "admin", ADMIN_PW)
    pid = c.post("/api/projects", headers=h, json={"name": "P"}).json()["id"]
    r = c.post(f"/api/projects/{pid}/master/init", headers=h, files={"file": ("Linea 1_rev5.L5K", l5k().encode())})
    assert r.status_code == 200 and r.json()["name"] == "Linea 1"
    proj = [p for p in c.get("/api/projects", headers=h).json() if p["id"] == pid][0]
    assert proj["master_name"] == "Linea 1" and proj["master_ext"] == ".L5K"
    d = c.get(f"/api/projects/{pid}/master/download", headers=h)
    assert d.headers["content-disposition"] == 'attachment; filename="Linea 1_rev1.L5K"'
    assert [x["name"] for x in c.get(f"/api/projects/{pid}/master/history", headers=h).json()] == ["Linea 1"]
    assert (app.state.settings.data_dir / "projects").glob("*/Linea 1.L5K")
    assert list((app.state.settings.data_dir / "projects").glob("*/Linea 1.L5K"))
    c.post(f"/api/projects/{pid}/heartbeat", headers=h)
    prop = c.post(f"/api/projects/{pid}/proposals", headers=h, data={"base_revision": 1},
                  files={"file": ("x.L5K", l5k(r02="XIC(G)OTE(Z);").encode())}).json()
    pv = c.get(f"/api/projects/{pid}/proposals/{prop['id']}/preview", headers=h)
    assert pv.headers["content-disposition"] == 'attachment; filename="Linea 1_anteprima.L5K"'
    c.post(f"/api/projects/{pid}/proposals/{prop['id']}/approve", headers=h, json={})
    d2 = c.get(f"/api/projects/{pid}/master/download", headers=h)
    assert d2.headers["content-disposition"] == 'attachment; filename="Linea 1_rev2.L5K"'          # nome fisso, cambia solo la rev
    ev = c.get("/api/audit", headers=h, params={"action": "master_initialized"}).json()
    assert ev[0]["details"]["name"] == "Linea 1"


def test_candidates_with_name_use_hidden_reference_copies(tmp_path):
    from client.localfiles import find_merge_candidates, hide_ref_dir, pick_default, ref_path
    f = tmp_path / "w"
    f.mkdir()
    old = time.time() - 100

    def put(path, text, mtime=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8")
        if mtime:
            os.utime(path, (mtime, mtime))
    base = l5k()
    # checkout della rev 3 (lavoro) + copia pulita + una rev 4 scaricata dal push (mai toccata) + file dell'utente
    put(ref_path(f, "Linea1", 3, ".L5K"), base, old)
    put(f / "Linea1_rev3.L5K", l5k(r01a="XIC(A)XIC(Z)OTE(B);"))                      # file di lavoro modificato
    put(ref_path(f, "Linea1", 4, ".L5K"), l5k(r02="XIC(G)OTE(NEW);"), old)
    put(f / "Linea1_rev4.L5K", l5k(r02="XIC(G)OTE(NEW);"), old)                      # copia del Master identica: non va proposta
    put(f / "Linea1_rev2.L5K", l5k(r02="XIC(G)OTE(OLD);"))                          # vecchio lavoro mai unito, senza copia pulita
    put(f / "altro.L5K", l5k(r01c="XIC(C)OTE(D2);"))
    hide_ref_dir(f)
    wf = str(f / "Linea1_rev3.L5K")
    c = {x.name: x for x in find_merge_candidates(f, wf, ext=".L5K", kind="l5k", name="Linea1", base_rev=3)}
    assert set(c) == {"Linea1_rev3.L5K", "Linea1_rev2.L5K", "altro.L5K"}              # niente .aps, niente copie identiche
    assert c["Linea1_rev3.L5K"].is_working and c["Linea1_rev3.L5K"].modified and not c["Linea1_rev3.L5K"].unchanged
    assert c["altro.L5K"].modified and c["Linea1_rev2.L5K"].modified and not c["Linea1_rev2.L5K"].unchanged
    best = pick_default(list(c.values()))
    assert best is not None and best.modified
    put(f / "Linea1_rev2.L5K", base, old)                                           # copia vecchia ma identica alla base: nascosta
    assert "Linea1_rev2.L5K" not in {x.name for x in find_merge_candidates(f, wf, ext=".L5K", kind="l5k", name="Linea1", base_rev=3)}
    # file di lavoro non toccato: resta in elenco (e' quello del checkout) ma 'identico al Master'
    put(f / "Linea1_rev3.L5K", base, old)
    c2 = {x.name: x for x in find_merge_candidates(f, wf, ext=".L5K", kind="l5k", name="Linea1", base_rev=3)}
    assert c2["Linea1_rev3.L5K"].unchanged and c2["Linea1_rev3.L5K"].is_working and not c2["Linea1_rev3.L5K"].modified
