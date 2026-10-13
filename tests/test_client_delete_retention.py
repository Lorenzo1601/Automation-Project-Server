"""Eliminazione progetto, retention delle revisioni, storico/rollback (con revisioni eliminate) e pulizia locale."""
import os
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QDialog, QMessageBox                                       # noqa: E402

from client.api_client import ApiClient, ApiError                                        # noqa: E402
from client.localfiles import prune_local, ref_path                                      # noqa: E402
from client.views import user_mgmt_view as U                                             # noqa: E402
from client.views.history_dialog import PURGED_LABEL, HistoryDialog                      # noqa: E402
from client.views.project_dialogs import DeleteProjectDialog, EditProjectDialog          # noqa: E402
from common.roles import Role                                                            # noqa: E402
from common.schemas import MasterInfo, ProjectCreate, ProjectUpdate                      # noqa: E402
from tests.test_client import ADMIN_PW, PW, qapp, sync                                   # noqa: E402,F401
from tests.test_client import _live_server                                                # noqa: E402
from tests.test_client_projects import Msgs, _close, _grant, _login, _open_project, fresh   # noqa: E402,F401
from tests.test_filetypes import l5k                                                     # noqa: E402


# ======================================================================================= utilita'
def _admin(fresh) -> ApiClient:
    api = ApiClient(fresh["url"])
    api.login("admin", ADMIN_PW)
    return api


def _make_project(api: ApiClient, tmp_path: Path, name: str, revisions: int = 1, **kw):
    """Progetto con Master L5K «Imp.L5K» e ``revisions`` revisioni (stesso nome file, contenuti diversi)."""
    p = api.create_project(ProjectCreate(name=name, **kw))
    d = tmp_path / f"src_{p.id}"
    d.mkdir(parents=True, exist_ok=True)
    base = d / "Imp.L5K"
    base.write_text(l5k(name="Imp"), "utf-8")
    api.init_master_for(p.id, base)
    for i in range(2, revisions + 1):
        _reimport(api, p.id, d, i)
    return next(x for x in api.projects() if x.id == p.id)


def _reimport(api: ApiClient, pid: int, d: Path, i: int):
    f = d / "Imp.L5K"
    f.write_text(l5k(name="Imp", r01a=f"XIC(A{i})OTE(B);"), "utf-8")
    return api.reimport_master_for(pid, f, f"revisione numero {i}")


def _view(api: ApiClient, name: str):
    view = U.UserMgmtView(api, run=sync)
    view.tabs.setCurrentIndex(1)
    view.reload()
    view.proj_combo.setCurrentIndex(view.proj_combo.findData(next(p.id for p in view._projects if p.name == name)))
    view._load_perms()
    assert view.current_project().name == name
    return view


def _errors(view):
    errs = []
    view._err = lambda e: errs.append(e.detail if isinstance(e, ApiError) else str(e))
    return errs


def _statuses(view):
    out = []
    view.status.connect(out.append)
    return out


# ======================================================================================= prune_local (funzione pura)
def _mk(folder: Path, name: str, revs, ext=".L5X", text=None):
    """Crea ``<name>_revN<ext>`` visibili + copie pulite in .aps con lo stesso contenuto."""
    (folder / ".aps").mkdir(parents=True, exist_ok=True)
    for r in revs:
        body = text(r) if text else f"contenuto rev {r}\n"
        (folder / f"{name}_rev{r}{ext}").write_text(body, "utf-8")
        (folder / ".aps" / f"{name}_rev{r}{ext}").write_text(body, "utf-8")


def _names(folder: Path):
    return sorted(p.name for p in folder.iterdir() if p.is_file()), sorted(p.name for p in (folder / ".aps").iterdir())


def test_prune_keeps_latest_n_and_removes_clean_copies(tmp_path):
    _mk(tmp_path, "Linea", range(1, 7))
    removed = prune_local(tmp_path, "Linea", ".L5X", 3, [])
    vis, aps = _names(tmp_path)
    assert vis == ["Linea_rev4.L5X", "Linea_rev5.L5X", "Linea_rev6.L5X"] and aps == vis
    assert len(removed) == 6 and all(not p.exists() for p in removed)           # 3 revisioni x (file + copia .aps)


def test_prune_keep_greater_or_equal_than_count_does_nothing(tmp_path):
    _mk(tmp_path, "Linea", range(1, 4))
    before = _names(tmp_path)
    assert prune_local(tmp_path, "Linea", ".L5X", 3, []) == []
    assert prune_local(tmp_path, "Linea", ".L5X", 50, []) == []
    assert _names(tmp_path) == before
    for bad in (0, -1, None, "x"):
        assert prune_local(tmp_path, "Linea", ".L5X", bad, []) == [] and _names(tmp_path) == before
    assert prune_local(tmp_path / "inesistente", "Linea", ".L5X", 2, []) == []


def test_prune_never_deletes_modified_file_or_its_reference(tmp_path):
    _mk(tmp_path, "Linea", range(1, 6))
    mine = tmp_path / "Linea_rev2.L5X"
    mine.write_text("LAVORO DELL'UTENTE\n", "utf-8")
    prune_local(tmp_path, "Linea", ".L5X", 2, [])
    vis, aps = _names(tmp_path)
    assert vis == ["Linea_rev2.L5X", "Linea_rev4.L5X", "Linea_rev5.L5X"]
    assert mine.read_text("utf-8") == "LAVORO DELL'UTENTE\n"
    assert aps == vis                                                              # resta anche la copia pulita di riferimento


def test_prune_protects_working_file_and_its_base_copy(tmp_path):
    _mk(tmp_path, "Linea", range(1, 7))
    work = tmp_path / "Linea_rev2.L5X"                         # identico alla copia pulita: sarebbe cancellabile
    prune_local(tmp_path, "Linea", ".L5X", 2, [str(work)])
    vis, aps = _names(tmp_path)
    assert vis == ["Linea_rev2.L5X", "Linea_rev5.L5X", "Linea_rev6.L5X"] and aps == vis
    # anche se il percorso protetto e' la sola copia .aps (o un percorso relativo/non normalizzato)
    prune_local(tmp_path, "Linea", ".L5X", 1, [tmp_path / ".aps" / "Linea_rev5.L5X", tmp_path / "x" / ".." / "Linea_rev2.L5X"])
    vis, aps = _names(tmp_path)
    assert vis == ["Linea_rev2.L5X", "Linea_rev5.L5X", "Linea_rev6.L5X"] and aps == vis


def test_prune_ignores_names_that_do_not_match_exactly(tmp_path):
    _mk(tmp_path, "Linea", range(1, 5))
    odd = ["Linea_rev1.L5X.bak", "Linea_rev01.L5X", "Linea_rev0.L5X", "Altro_rev1.L5X", "Linea_rev1.L5K", "xLinea_rev1.L5X",
           "Linea_rev1 - copia.L5X", "linea_rev1.L5X", "Linea_revX.L5X", "Linea_rev.L5X", "Linea_rev-1.L5X", "note.txt"]
    for n in odd:
        (tmp_path / n).write_text("miei dati", "utf-8")
        (tmp_path / ".aps" / n).write_text("miei dati", "utf-8")
    (tmp_path / "sotto").mkdir()
    (tmp_path / "sotto" / "Linea_rev1.L5X").write_text("in sottocartella", "utf-8")
    prune_local(tmp_path, "Linea", ".L5X", 1, [])
    for n in odd:
        assert (tmp_path / n).read_text("utf-8") == "miei dati" and (tmp_path / ".aps" / n).exists(), n
    assert (tmp_path / "sotto" / "Linea_rev1.L5X").exists()
    for r in (1, 2, 3):                                                          # solo i nomi esatti sono stati potati
        assert not (tmp_path / f"Linea_rev{r}.L5X").exists() and not (tmp_path / ".aps" / f"Linea_rev{r}.L5X").exists()
    assert (tmp_path / "Linea_rev4.L5X").exists() and (tmp_path / ".aps" / "Linea_rev4.L5X").exists()


def test_prune_without_clean_reference_does_not_delete(tmp_path):
    """File scaricato senza copia in .aps (Viewer): non si puo' verificare -> si conserva."""
    for r in range(1, 5):
        (tmp_path / f"Linea_rev{r}.L5X").write_text(f"r{r}", "utf-8")
    assert prune_local(tmp_path, "Linea", ".L5X", 2, []) == []
    assert len(list(tmp_path.glob("*.L5X"))) == 4
    # con l'hash noto dal server si puo' verificare: cancella solo quelli che coincidono
    import hashlib
    sha = {r: hashlib.sha256(f"r{r}".encode()).hexdigest() for r in (1, 2, 3, 4)}
    sha[1] = "0" * 64                                                              # rev 1 non coincide col server
    removed = prune_local(tmp_path, "Linea", ".L5X", 2, [], known_sha=sha)
    assert sorted(p.name for p in removed) == ["Linea_rev2.L5X"]
    assert sorted(p.name for p in tmp_path.glob("*.L5X")) == ["Linea_rev1.L5X", "Linea_rev3.L5X", "Linea_rev4.L5X"]


def test_prune_orphan_reference_and_symlinks_and_other_ext(tmp_path):
    _mk(tmp_path, "Linea", range(1, 4))
    (tmp_path / "Linea_rev1.L5X").unlink()                                         # l'utente l'aveva gia' cancellato
    _mk(tmp_path, "Linea", [], ext=".L5K")
    (tmp_path / "Linea_rev1.L5K").write_text("l5k", "utf-8")
    prune_local(tmp_path, "Linea", ".L5X", 2, [])
    assert not (tmp_path / ".aps" / "Linea_rev1.L5X").exists()                     # riferimento orfano: rimosso
    assert (tmp_path / "Linea_rev1.L5K").exists()                                  # altra estensione: mai toccata
    outside = tmp_path.parent / "fuori_cartella.txt"
    outside.write_text("fuori", "utf-8")
    link = tmp_path / "Linea_rev0x.L5X"
    for r in (7, 8, 9):
        (tmp_path / f"Linea_rev{r}.L5X").write_text("x", "utf-8")
        (tmp_path / ".aps" / f"Linea_rev{r}.L5X").write_text("x", "utf-8")
    try:
        (tmp_path / "Linea_rev5.L5X").symlink_to(outside)
        (tmp_path / ".aps" / "Linea_rev5.L5X").write_text("fuori", "utf-8")
    except OSError:
        link = None
    prune_local(tmp_path, "Linea", ".L5X", 2, [])
    assert outside.read_text("utf-8") == "fuori"
    if link is not None:
        assert (tmp_path / "Linea_rev5.L5X").is_symlink()                          # i collegamenti simbolici non si toccano


def test_prune_default_name_and_extension_normalisation(tmp_path):
    _mk(tmp_path, "Master", range(1, 5), ext=".st")
    prune_local(tmp_path, "", "st", 2, [])                                         # nome vuoto -> «Master», ext senza punto
    assert sorted(p.name for p in tmp_path.glob("*.st")) == ["Master_rev3.st", "Master_rev4.st"]


# ======================================================================================= HistoryDialog
def _info(rev, purged=False, msg="m"):
    return MasterInfo(revision=rev, sha256="a" * 64, updated_at=datetime(2026, 1, rev, tzinfo=timezone.utc),
                      updated_by="admin", message=msg, purged=purged)


def test_history_dialog_marks_purged_rows_and_selects_latest_kept(qapp):
    hist = [_info(1, True), _info(2, True), _info(3), _info(4), _info(5)]
    d = HistoryDialog(hist, Role.ADMIN, None, max_revisions=3)
    assert [d.table.item(r, 0).text() for r in range(5)] == ["rev 5", "rev 4", "rev 3", "rev 2", "rev 1"]
    assert d.selected_revision() == 5                                                # la piu' recente conservata
    assert d.table.item(3, 4).text() == PURGED_LABEL == "eliminata (limite revisioni)"
    assert d.table.item(0, 4).text() != PURGED_LABEL
    from client.theme.tokens import T
    assert d.table.item(3, 0).foreground().color().name().lower() == T["MUTED"].lower()
    assert d.table.item(0, 0).foreground().color().name().lower() != T["MUTED"].lower()
    assert "conserva le ultime 3 revisioni" in d.lbl_note.text() and "2 revisioni" in d.lbl_note.text()
    got = []
    d.download_requested.connect(got.append)
    d.restore_requested.connect(got.append)
    d.table.selectRow(3)                                                             # rev 2: eliminata
    assert d.selected_revision() == 2 and not d.btn_dl.isEnabled() and not d.btn_restore.isEnabled()
    d.btn_dl.click()
    d.btn_restore.click()
    d._emit(d.download_requested)
    assert got == []                                                                 # nessun segnale su una revisione eliminata
    d.table.selectRow(2)                                                             # rev 3: conservata, non e' la testa
    assert d.btn_dl.isEnabled() and d.btn_restore.isEnabled()
    d.btn_restore.click()
    d.table.selectRow(0)                                                             # la testa: scaricabile ma gia' corrente
    assert d.btn_dl.isEnabled() and not d.btn_restore.isEnabled()
    d.btn_dl.click()
    assert got == [3, 5]
    d.set_history(hist + [_info(6)])                                                 # aggiornamento dopo un rollback
    assert d.selected_revision() == 6 and d.table.rowCount() == 6


def test_history_dialog_backward_compatible_and_roles(qapp):
    d = HistoryDialog([_info(1), _info(2)], Role.DEVELOPER, None)                    # firma storica (history, role, parent)
    assert d.btn_restore.isHidden() and d.lbl_note.isHidden() and d.selected_revision() == 2
    adm = HistoryDialog([_info(1), _info(2)], Role.ADMIN, None)
    adm.show()
    assert not adm.btn_restore.isHidden()
    only_purged = HistoryDialog([_info(1, True)], Role.ADMIN, None)
    assert only_purged.selected_revision() == 1 and not only_purged.btn_dl.isEnabled()
    assert HistoryDialog([], Role.ADMIN, None).selected_revision() is None


# ======================================================================================= dialoghi
def test_delete_dialog_requires_exact_name(qapp, fresh):
    api = _admin(fresh)
    p = next(x for x in api.projects() if x.name == "Linea 1")
    d = DeleteProjectDialog(p)
    assert not d.btn_delete.isEnabled() and d.chk_export.isChecked() and d.btn_delete.property("variant") == "danger"
    txt = d.lbl_effects.text().lower()
    assert "revisioni" in txt and "permessi" in txt and "audit" in txt
    for wrong in ("linea 1", "Linea 1 ", " Linea 1", "Linea", "Linea  1", ""):
        d.confirm.setText(wrong)
        assert not d.btn_delete.isEnabled(), wrong
    d.confirm.setText("Linea 1")
    assert d.btn_delete.isEnabled() and d.typed_name() == "Linea 1" and d.wants_export()
    d.chk_export.setChecked(False)
    assert not d.wants_export()


def test_edit_project_dialog_payload_only_changed_fields(qapp, fresh):
    api = _admin(fresh)
    p = next(x for x in api.projects() if x.name == "Linea 1")
    d = EditProjectDialog(p)
    assert d.name.text() == "Linea 1" and d.payload().name is None and d.payload().description is None
    d.desc.setText("nuova descrizione")
    assert d.payload().name is None and d.payload().description == "nuova descrizione"
    d.name.setText("  Altro  ")
    assert d.payload().name == "Altro"


# ======================================================================================= vista Admin (server reale)
def test_edit_project_name_and_description(fresh, qapp, tmp_path, monkeypatch):
    msgs = Msgs(monkeypatch)
    api = _admin(fresh)
    _make_project(api, tmp_path, "Da Rinominare")
    view = _view(api, "Da Rinominare")
    st, emitted = _statuses(view), []
    view.projects_changed.connect(lambda: emitted.append(1))

    monkeypatch.setattr(EditProjectDialog, "exec", lambda self: QDialog.Rejected)       # annullato: nulla cambia
    view.edit_project()
    assert view.current_project().name == "Da Rinominare" and not emitted

    def accept(self):
        self.name.setText("Rinominato")
        self.desc.setText("descrizione nuova")
        return QDialog.Accepted
    monkeypatch.setattr(EditProjectDialog, "exec", accept)
    view.edit_project()
    p = next(x for x in api.projects() if x.name == "Rinominato")
    assert p.description == "descrizione nuova" and emitted and any("Rinominato" in s for s in st)
    assert view.proj_combo.currentText() == "Rinominato"

    view.edit_project(name="  ")                                                         # nome vuoto -> avviso, nulla cambia
    assert msgs.warnings and next(x for x in api.projects() if x.id == p.id).name == "Rinominato"
    view.edit_project(description="solo descrizione")                                    # solo un campo
    assert next(x for x in api.projects() if x.id == p.id).description == "solo descrizione"
    n = len(emitted)
    view.edit_project(name="Rinominato", description="solo descrizione")                 # nessuna modifica: non chiama il server
    assert len(emitted) == n


def test_settings_box_state_follows_selection(fresh, qapp, tmp_path):
    api = _admin(fresh)
    _make_project(api, tmp_path, "Stato Box", revisions=3)
    api.create_project(ProjectCreate(name="Stato Vuoto"))
    view = _view(api, "Stato Box")
    assert view.spin_revisions.minimum() == 2 and view.spin_revisions.maximum() == 200 and view.spin_revisions.value() == 10
    assert "Conservate: 3 di 3" in view.lbl_retention.text() and "limite attuale: 10" in view.lbl_retention.text()
    assert not view.btn_apply_revisions.isEnabled() and view.btn_history.isEnabled()
    for b in (view.btn_edit_proj, view.btn_export, view.btn_delete):
        assert b.isEnabled()
    assert view.btn_delete.property("variant") == "danger"
    view.spin_revisions.setValue(4)
    assert view.btn_apply_revisions.isEnabled()
    view.spin_revisions.setValue(10)
    assert not view.btn_apply_revisions.isEnabled()
    view.proj_combo.setCurrentIndex(view.proj_combo.findText("Stato Vuoto"))
    view._load_perms()
    assert not view.btn_history.isEnabled() and "Conservate: 0 di 0" in view.lbl_retention.text()   # Master non inizializzato


def test_retention_reduce_asks_for_confirmation_and_purges(fresh, qapp, tmp_path, monkeypatch):
    msgs = Msgs(monkeypatch)
    api = _admin(fresh)
    pr = _make_project(api, tmp_path, "Retention Vista", revisions=5)
    view = _view(api, "Retention Vista")
    st, emitted, errs = _statuses(view), [], _errors(view)
    view.projects_changed.connect(lambda: emitted.append(1))

    view.spin_revisions.setValue(2)
    msgs.answer = QMessageBox.No                                                         # annullato: il server non cambia
    view.btn_apply_revisions.click()
    q = msgs.questions[-1]
    assert "3 revisioni più vecchie" in q and "definitivamente" in q and "scaricare né ripristinare" in q
    p = next(x for x in api.projects() if x.id == pr.id)
    assert p.max_revisions == 10 and p.purged_revisions == 0 and not emitted
    assert view.spin_revisions.value() == 10                                             # la vista torna al valore reale

    view.spin_revisions.setValue(2)
    msgs.answer = QMessageBox.Yes
    view.btn_apply_revisions.click()
    p = next(x for x in api.projects() if x.id == pr.id)
    assert p.max_revisions == 2 and p.purged_revisions == 3 and emitted and not errs
    assert any("ultime 2 revisioni" in s for s in st)
    assert "Conservate: 2 di 5" in view.lbl_retention.text() and "limite attuale: 2" in view.lbl_retention.text()
    hist = api.history_for(pr.id)
    assert [m.purged for m in sorted(hist, key=lambda m: m.revision)] == [True, True, True, False, False]

    asked = len(msgs.questions)
    view.apply_retention(8)                                                              # aumentare non chiede conferma
    assert len(msgs.questions) == asked
    assert next(x for x in api.projects() if x.id == pr.id).max_revisions == 8
    view.apply_retention(8)                                                              # invariato: nessuna chiamata
    view.apply_retention(7, confirm=False)                                               # 7 >= 2 conservate: nessuna domanda
    assert next(x for x in api.projects() if x.id == pr.id).max_revisions == 7
    view.apply_retention(1)                                                              # fuori range: errore del server mostrato
    assert errs


def test_history_download_and_rollback_from_admin_view(fresh, qapp, tmp_path, monkeypatch):
    msgs = Msgs(monkeypatch)
    api = _admin(fresh)
    pr = _make_project(api, tmp_path, "Rollback Vista", revisions=4, max_revisions=3)
    assert next(x for x in api.projects() if x.id == pr.id).purged_revisions == 1
    api.set_project(fresh["pid"])                                                         # il progetto «aperto» non deve cambiare
    view = _view(api, "Rollback Vista")
    st, errs, emitted = _statuses(view), _errors(view), []
    view.projects_changed.connect(lambda: emitted.append(1))
    seen = {}

    def fake_exec(self):
        seen["dlg"] = self
        seen["sel"] = self.selected_revision()
        seen["purged_row"] = self.table.item(self.table.rowCount() - 1, 4).text()
        # download della rev 3
        self.table.selectRow(1)
        self.btn_dl.click()
        # rollback di una revisione eliminata: bottoni disabilitati
        self.table.selectRow(3)
        seen["restore_enabled_purged"] = self.btn_restore.isEnabled()
        # rollback della rev 3
        self.table.selectRow(1)
        self.btn_restore.click()
        seen["rows_after"] = self.table.rowCount()
        seen["sel_after"] = self.selected_revision()
        return QDialog.Accepted
    monkeypatch.setattr(HistoryDialog, "exec", fake_exec)
    saved = []
    dest = tmp_path / "salvata.L5K"
    monkeypatch.setattr(U.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (saved.append((a[2], a[3])) or str(dest), "")))
    msgs.reason = "ripristino della versione buona"
    view.show_history()

    assert seen["sel"] == 4 and seen["purged_row"] == PURGED_LABEL and seen["restore_enabled_purged"] is False
    assert saved[0][0] == "Imp_rev3.L5K"                                                   # <nome del file Master>_revN<ext>
    assert dest.exists() and dest.read_text("utf-8") == l5k(name="Imp", r01a="XIC(A3)OTE(B);")
    assert "Tutti i file (*)" in saved[0][1] and "*.L5K" in saved[0][1]
    assert api.project_id == fresh["pid"]                                                 # progetto condiviso intatto
    assert "NUOVA revisione" in msgs.questions[-1] and "cronologia resta intatta" in msgs.questions[-1]
    assert not errs
    hist = {m.revision: m for m in api.history_for(pr.id)}
    assert max(hist) == 5
    assert seen["rows_after"] == 5 and seen["sel_after"] == 5                              # lo storico aperto si e' aggiornato
    assert emitted and any("nuova rev 5" in s for s in st)
    assert any(e.action == "master_restored" for e in api.audit(limit=20))
    # il contenuto della rev 5 e' quello della rev 3
    a, b = tmp_path / "r3.L5K", tmp_path / "r5.L5K"
    api.download_master_for(pr.id, a, 3)
    api.download_master_for(pr.id, b, 5)
    assert a.read_bytes() == b.read_bytes()

    # motivo troppo corto: avviso, nessuna nuova revisione; rollback di una revisione eliminata: errore del server
    n = len(emitted)
    view.rollback(2, "no")
    assert msgs.warnings and "5 caratteri" in msgs.warnings[-1] and len(emitted) == n
    view.rollback(1, "motivo valido per test")
    assert errs and max(m.revision for m in api.history_for(pr.id)) == 5
    # annullo alla conferma / alla domanda del motivo
    msgs.answer = QMessageBox.No
    view.rollback(4)
    msgs.answer = QMessageBox.Yes
    view.rollback(4, reason="   ")
    assert max(m.revision for m in api.history_for(pr.id)) == 5


def test_export_project_and_cancel(fresh, qapp, tmp_path, monkeypatch):
    Msgs(monkeypatch)
    api = _admin(fresh)
    _make_project(api, tmp_path, "Export Vista", revisions=2)
    view = _view(api, "Export Vista")
    st, errs = _statuses(view), _errors(view)
    asked = []
    monkeypatch.setattr(U.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (asked.append(a[2]) or "", "")))
    view.export_project()                                                                 # annullato
    assert asked and asked[0].startswith("Export_Vista_export_") and asked[0].endswith(".zip") and not st
    dest = tmp_path / "out" / "exp.zip"
    monkeypatch.setattr(U.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(dest), "")))
    view.export_project()
    assert not errs and zipfile.is_zipfile(dest) and any("esportato" in s and "Export Vista" in s for s in st)
    view.export_project(str(tmp_path / "diretto.zip"))
    assert zipfile.is_zipfile(tmp_path / "diretto.zip")


def _delete_dialog(monkeypatch, typed, export, accept=True):
    seen = {}

    def fake_exec(self):
        seen["btn_enabled_before"] = self.btn_delete.isEnabled()
        seen["export_default"] = self.chk_export.isChecked()
        self.chk_export.setChecked(export)
        self.confirm.setText(typed)
        seen["btn_enabled"] = self.btn_delete.isEnabled()
        return QDialog.Accepted if (accept and self.btn_delete.isEnabled()) else QDialog.Rejected
    monkeypatch.setattr(DeleteProjectDialog, "exec", fake_exec)
    return seen


def test_delete_project_wrong_name_cancel_and_success(fresh, qapp, tmp_path, monkeypatch):
    msgs = Msgs(monkeypatch)
    api = _admin(fresh)
    pr = _make_project(api, tmp_path, "Da Eliminare", revisions=2)
    api.audit(project_id=pr.id)
    view = _view(api, "Da Eliminare")
    st, errs, emitted = _statuses(view), _errors(view), []
    view.projects_changed.connect(lambda: emitted.append(1))

    def exists():
        return any(x.id == pr.id for x in api.projects())

    # nome errato nel dialogo: il pulsante resta disabilitato, nulla viene eliminato
    seen = _delete_dialog(monkeypatch, "da eliminare", export=False)
    view.btn_delete.click()
    assert seen["btn_enabled_before"] is False and seen["btn_enabled"] is False and seen["export_default"] is True
    assert exists() and not emitted and not st
    # nome errato passato a mano (hook): avviso lato client, il server non viene chiamato
    view.delete_project(confirm_name="Altro nome")
    assert msgs.warnings and "non coincide" in msgs.warnings[-1] and exists()
    # annullo con il nome giusto ma l'utente non conferma
    _delete_dialog(monkeypatch, "Da Eliminare", export=False, accept=False)
    view.delete_project()
    assert exists()

    # export richiesto ma salvataggio annullato -> NON elimina
    _delete_dialog(monkeypatch, "Da Eliminare", export=True)
    monkeypatch.setattr(U.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: ("", "")))
    view.delete_project()
    assert exists() and not emitted

    # export fallisce -> NON elimina e mostra l'errore
    zpath = tmp_path / "prima.zip"
    monkeypatch.setattr(U.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(zpath), "")))
    real_export = api.export_project

    def broken(pid, dest):
        raise ApiError(500, "disco pieno")
    api.export_project = broken
    view.delete_project()
    assert exists() and errs[-1] == "disco pieno" and not zpath.exists()
    api.export_project = real_export

    # successo: prima l'export, poi l'eliminazione
    order = []
    api.export_project = lambda pid, dest: (order.append("export"), real_export(pid, dest))[1]
    real_delete = api.delete_project
    api.delete_project = lambda pid, name: (order.append("delete"), real_delete(pid, name))[1]
    audit_rows = len(api.audit(project_id=pr.id, limit=500))
    assert audit_rows > 0
    view.delete_project()
    assert order == ["export", "delete"] and not exists() and zipfile.is_zipfile(zpath)
    assert emitted and not errs[1:]
    import re
    m = [re.search(r"(\d+) righe di audit rimosse", x) for x in st if "Da Eliminare" in x]
    assert m and m[0] and int(m[0].group(1)) >= audit_rows, st                              # + la riga dell'export appena fatto
    assert view.proj_combo.findText("Da Eliminare") < 0                                    # sparito dall'elenco
    assert not any(e.project_id == pr.id for e in api.audit(limit=500))                   # audit del progetto rimosso


def test_delete_project_without_export_and_hook_args(fresh, qapp, tmp_path, monkeypatch):
    Msgs(monkeypatch)
    api = _admin(fresh)
    a = _make_project(api, tmp_path, "Elimina Senza Zip")
    b = _make_project(api, tmp_path, "Elimina Con Hook")
    view = _view(api, "Elimina Senza Zip")
    errs = _errors(view)
    calls = []
    real = api.export_project
    api.export_project = lambda *x: (calls.append(x), real(*x))[1]
    _delete_dialog(monkeypatch, "Elimina Senza Zip", export=False)
    view.delete_project()
    assert not calls and not any(x.id == a.id for x in api.projects())
    view = _view(api, "Elimina Con Hook")
    view.delete_project(confirm_name="Elimina Con Hook", export_to=str(tmp_path / "hook.zip"))
    assert calls and zipfile.is_zipfile(tmp_path / "hook.zip") and not any(x.id == b.id for x in api.projects()) and not errs
    view.delete_project()                                                                 # nessun progetto selezionato valido: no-op


# ======================================================================================= finestra principale
@pytest.fixture(scope="module")
def solo(tmp_path_factory):
    """Server con un solo progetto (per lo stato «nessun progetto»)."""
    yield from _live_server(tmp_path_factory)


def _notes(win):
    notes = []
    win.notify_user = lambda title, text: notes.append((title, text))
    return notes


def test_project_deleted_event_switches_to_other_project_and_keeps_local_files(fresh, qapp, tmp_path):
    adm = fresh["admin"]
    keep_p = _make_project(adm, tmp_path, "Resta Aperto", revisions=1)
    gone_p = _make_project(adm, tmp_path, "Verra Eliminato", revisions=2)
    win = _login(fresh, tmp_path, "admin", ADMIN_PW)
    notes = _notes(win)
    _open_project(win, gone_p.id)
    win.checkout()                                                      # file locali dell'utente
    work = Path(win.working_file)
    folder = win.local_folder
    assert work.exists() and (folder / ".aps").is_dir()
    work.write_text("lavoro non ancora unito", "utf-8")
    ws_started = win.ws._want
    assert ws_started

    other = ApiClient(fresh["url"])                                     # altra sessione: elimina il progetto aperto
    other.login("admin", ADMIN_PW)
    other.delete_project(gone_p.id, "Verra Eliminato")
    win.on_event({"type": "project_deleted", "message": "Il progetto «Verra Eliminato» e' stato eliminato da admin"})

    assert win.project is not None and win.project.id != gone_p.id
    assert all(p.id != gone_p.id for p in win.projects)
    assert win.sidebar.project.findData(gone_p.id) < 0 and win.api.project_id == win.project.id
    assert win.ws._want                                                  # collegato al nuovo progetto
    assert notes and notes[0][0] == "Progetto eliminato" and "Verra Eliminato" in notes[0][1]
    assert "file locali non sono stati toccati" in notes[0][1] and str(folder) in notes[0][1]
    assert "Progetto eliminato" in win.dashboard.log.toPlainText()
    assert work.read_text("utf-8") == "lavoro non ancora unito" and (folder / ".aps").is_dir()   # nessun file toccato
    win._heartbeat()                                                    # nessuna eccezione
    # evento per un progetto che NON e' aperto: solo aggiornamento dell'elenco, nessun avviso
    n = len(notes)
    win.on_event({"type": "project_deleted", "project_id": 987654, "message": "altro"})
    assert len(notes) == n and win.project is not None
    assert keep_p.id in [p.id for p in win.projects]
    _close(win)


def test_project_deleted_event_with_no_projects_left(solo, qapp, tmp_path):
    win = _login(solo, tmp_path, "admin", ADMIN_PW)
    notes = _notes(win)
    pid = solo["pid"]
    _open_project(win, pid)
    win.checkout()
    local = Path(win.working_file)
    assert local.exists()
    solo["admin"].delete_project(pid, "Linea 1")
    win.on_event({"type": "project_deleted", "message": "eliminato"})
    assert win.project is None and win.projects == [] and win.api.project_id is None
    assert not win.ws._want and win.sidebar.project.count() == 0
    assert win.dashboard.title.text() == "Nessun progetto"
    assert not win.dashboard.btn_checkout.isEnabled() and not win.dashboard.btn_history.isEnabled()
    assert notes and "file locali non sono stati toccati" in notes[0][1] and local.exists()
    win._heartbeat()
    win.refresh_all()                                                   # senza progetto: nessuna eccezione
    win.on_event({"type": "project_deleted", "message": "di nuovo"})   # evento duplicato: ignorato senza errori
    assert win.project is None
    _close(win)


def test_project_updated_event_reloads_projects(fresh, qapp, tmp_path):
    adm = fresh["admin"]
    p = _make_project(adm, tmp_path, "Aggiornato Da Evento", revisions=3)
    win = _login(fresh, tmp_path, "admin", ADMIN_PW)
    _open_project(win, p.id)
    assert win.project.max_revisions == 10 and win.project.name == "Aggiornato Da Evento"
    adm.update_project(p.id, ProjectUpdate(max_revisions=3, name="Nome Nuovo"))
    win.on_event({"type": "project_updated", "message": "aggiornato"})
    assert win.project.id == p.id and win.project.max_revisions == 3 and win.project.name == "Nome Nuovo"
    assert win.sidebar.project.currentText() == "Nome Nuovo"
    _close(win)


def test_local_prune_after_downloads_never_touches_work(fresh, qapp, tmp_path):
    adm = fresh["admin"]
    p = _make_project(adm, tmp_path, "Pulizia Locale", revisions=1, max_revisions=2)
    _grant(fresh, p.id, dev1="Developer")
    win = _login(fresh, tmp_path, "dev1")
    _open_project(win, p.id)
    assert win.project.max_revisions == 2
    win.checkout()
    f = win.local_folder
    assert win.base_revision == 1 and Path(win.working_file) == f / "Imp_rev1.L5K"
    src = tmp_path / f"src_{p.id}"

    def push(rev):
        _reimport(adm, p.id, src, rev)
        win._auto_sync(rev)                                             # «master_updated» -> nuovo Master sul PC

    def visible():
        return sorted(x.name for x in f.glob("*.L5K"))

    push(2)
    push(3)
    assert visible() == ["Imp_rev1.L5K", "Imp_rev2.L5K", "Imp_rev3.L5K"]       # rev1 e' la base/lavoro: protetta
    push(4)                                                                     # rev2 e' pulita e vecchia: rimossa
    assert visible() == ["Imp_rev1.L5K", "Imp_rev3.L5K", "Imp_rev4.L5K"]
    assert sorted(x.name for x in (f / ".aps").glob("*.L5K")) == visible()
    assert "Pulizia locale: rimosse 1 copie di vecchie revisioni" in win.dashboard.log.toPlainText()
    (f / "Imp_rev3.L5K").write_text("MODIFICHE DELL'UTENTE", "utf-8")             # lavoro dell'utente su una vecchia revisione
    push(5)
    push(6)
    assert visible() == ["Imp_rev1.L5K", "Imp_rev3.L5K", "Imp_rev5.L5K", "Imp_rev6.L5K"]
    assert (f / "Imp_rev3.L5K").read_text("utf-8") == "MODIFICHE DELL'UTENTE"
    assert (f / ".aps" / "Imp_rev3.L5K").read_text("utf-8") != "MODIFICHE DELL'UTENTE"   # riferimento pulito conservato
    (f / "Imp_rev9_miei_appunti.L5K").write_text("note", "utf-8")
    (f / "extra.txt").write_text("x", "utf-8")

    win.checkout()                                                              # nuovo checkout: la base passa a rev6
    assert win.base_revision == 6 and Path(win.working_file) == f / "Imp_rev6.L5K"
    assert visible() == ["Imp_rev3.L5K", "Imp_rev5.L5K", "Imp_rev6.L5K", "Imp_rev9_miei_appunti.L5K"]   # rev1 ora libera
    assert (f / "extra.txt").exists() and (f / "Imp_rev3.L5K").read_text("utf-8") == "MODIFICHE DELL'UTENTE"
    assert Path(win.working_file).exists()
    _close(win)


def test_local_prune_does_nothing_for_viewer_without_references(fresh, qapp, tmp_path):
    adm = fresh["admin"]
    p = _make_project(adm, tmp_path, "Pulizia Viewer", revisions=1, max_revisions=2)
    _grant(fresh, p.id, viewer="Viewer")
    win = _login(fresh, tmp_path, "viewer")
    _open_project(win, p.id)
    win.checkout()
    src = tmp_path / f"src_{p.id}"
    for rev in (2, 3, 4):
        _reimport(adm, p.id, src, rev)
        win._auto_sync(rev)
    assert sorted(x.name for x in win.local_folder.glob("*.L5K")) == [f"Imp_rev{i}.L5K" for i in (1, 2, 3, 4)]
    _close(win)


def test_client_history_rollback_with_purged_revisions(fresh, qapp, tmp_path, monkeypatch):
    msgs = Msgs(monkeypatch)
    adm = fresh["admin"]
    p = _make_project(adm, tmp_path, "Rollback Client", revisions=4, max_revisions=3)
    _grant(fresh, p.id, lead="Lead_Engineer")
    win = _login(fresh, tmp_path, "admin", ADMIN_PW)
    _open_project(win, p.id)
    seen = {}

    def fake_exec(self):
        seen["restore_visible"] = not self.btn_restore.isHidden() or self.btn_restore.isVisibleTo(self)
        seen["sel"], seen["note"] = self.selected_revision(), self.lbl_note.text()
        self.table.selectRow(self.table.rowCount() - 1)                         # rev 1: eliminata dal limite
        seen["dl_purged"], seen["rs_purged"] = self.btn_dl.isEnabled(), self.btn_restore.isEnabled()
        self.table.selectRow(1)                                                  # rev 3
        msgs.reason = "no"                                                       # troppo corto
        self.btn_restore.click()
        seen["warn"] = list(msgs.warnings)
        msgs.reason = "ripristino della versione buona"
        self.btn_restore.click()
        seen["rows"], seen["sel_after"] = self.table.rowCount(), self.selected_revision()
        return QDialog.Accepted
    monkeypatch.setattr(HistoryDialog, "exec", fake_exec)
    win.show_history()
    assert seen["restore_visible"] and seen["sel"] == 4 and "conserva le ultime 3 revisioni" in seen["note"]
    assert seen["dl_purged"] is False and seen["rs_purged"] is False
    assert seen["warn"] and "5 caratteri" in seen["warn"][-1]
    assert seen["rows"] == 5 and seen["sel_after"] == 5
    assert win.project.head_revision == 5 and "Master ripristinato dalla rev 3: nuova rev 5" in win.dashboard.log.toPlainText()
    a, b = tmp_path / "a.L5K", tmp_path / "b.L5K"
    adm.set_project(p.id)
    adm.download_master(a, 3)
    adm.download_master(b, 5)
    assert a.read_bytes() == b.read_bytes()

    # scarico di una revisione: nome proposto <nome>_revN<ext>
    saved = []
    monkeypatch.setattr("client.gui_main.QFileDialog.getSaveFileName",
                        staticmethod(lambda *x, **k: (saved.append(x[2]) or str(tmp_path / "r4.L5K"), "")))
    win._download_revision(4)
    assert saved == ["Imp_rev4.L5K"] and (tmp_path / "r4.L5K").exists()
    win._download_revision(3, str(tmp_path / "diretto.L5K"))
    assert (tmp_path / "diretto.L5K").read_bytes() == a.read_bytes()
    n = len(msgs.warnings)
    win._download_revision(2, str(tmp_path / "eliminata.L5K"))                 # rev 2: il file non e' piu' sul server
    assert len(msgs.warnings) == n + 1 and not (tmp_path / "eliminata.L5K").exists()
    # rollback di una revisione eliminata: errore del server mostrato, nessuna nuova revisione
    win._restore(1, win, "motivo sufficiente")
    assert msgs.warnings[-1] and adm.master_info().revision == 5
    _close(win)

    # un Lead non e' Admin: nessun pulsante di ripristino e _restore e' un no-op
    lead = _login(fresh, tmp_path, "lead")
    _open_project(lead, p.id)
    d = HistoryDialog(adm.history(), lead.project_role, lead)
    assert d.btn_restore.isHidden()
    lead._restore(4, lead, "motivo sufficiente")
    assert adm.master_info().revision == 5
    _close(lead)


# ======================================================================================= console del server (stessa vista)
def test_server_console_view_history_retention_export_delete(qapp, tmp_path, monkeypatch):
    """La pagina «Utenti & Progetti» della GUI del server usa la stessa vista tramite ConsoleApi (in-process)."""
    import socket
    from server.gui.controller import EmbeddedServer, resolve_data_dir
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = EmbeddedServer(resolve_data_dir(tmp_path / "dati"), "127.0.0.1", port, admin_password=ADMIN_PW)
    srv.open()
    try:
        msgs = Msgs(monkeypatch)
        api = srv.console
        api.set_project(None)
        pr = _make_project(api, tmp_path, "Da Console", revisions=4, max_revisions=3)
        view = _view(api, "Da Console")
        errs, st = _errors(view), _statuses(view)
        assert "Conservate: 3 di 4" in view.lbl_retention.text()
        seen = {}

        def fake_exec(self):
            seen["rows"] = self.table.rowCount()
            seen["purged"] = [self.table.item(r, 4).text() == PURGED_LABEL for r in range(self.table.rowCount())]
            self.table.selectRow(1)
            self.btn_restore.click()
            return QDialog.Accepted
        monkeypatch.setattr(HistoryDialog, "exec", fake_exec)
        msgs.reason = "rollback dalla console"
        view.show_history()
        assert seen["rows"] == 4 and seen["purged"] == [False, False, False, True] and not errs
        assert max(m.revision for m in api.history_for(pr.id)) == 5 and api.project_id is None          # progetto condiviso intatto
        view.apply_retention(2, confirm=True)
        assert next(x for x in api.projects() if x.id == pr.id).max_revisions == 2 and not errs
        view.export_project(str(tmp_path / "console.zip"))
        assert zipfile.is_zipfile(tmp_path / "console.zip")
        view.delete_project(confirm_name="Da Console")
        assert not any(x.id == pr.id for x in api.projects()) and not errs
        assert any("righe di audit rimosse" in x for x in st)
    finally:
        srv.close()
        srv.detach_logging()
