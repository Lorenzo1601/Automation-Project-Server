"""Test della Guida integrata (HelpView) e del tema grafico condiviso (client + GUI del server)."""
import os
import re
from datetime import datetime, timezone

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, Qt                                         # noqa: E402
from PySide6.QtTest import QTest                                              # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QWidget                   # noqa: E402

from client.theme import apply_dark_theme, build_stylesheet                   # noqa: E402
from client.theme import widgets as W                                         # noqa: E402
from client.theme.tokens import T, TOKENS                                     # noqa: E402
from client.views import help_view as H                                       # noqa: E402
from client.views.sidebar import CLIENT_NAV, Sidebar                          # noqa: E402
from common.roles import Role                                                 # noqa: E402
from common.schemas import ParticipantOut, ProposalState, ProposalSummary     # noqa: E402
from tests.test_client import PW, _live_server, _window                       # noqa: E402,F401


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    apply_dark_theme(app)
    return app


@pytest.fixture(autouse=True)
def _close_widgets(qapp):
    yield
    for w in QApplication.topLevelWidgets():
        w.close()
        w.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    yield from _live_server(tmp_path_factory)


def sync(fn, ok=None, err=None):
    try:
        res = fn()
    except Exception as exc:                                                  # noqa: BLE001
        if err:
            err(exc)
        return
    if ok:
        ok(res)


# ======================================================================================= contenuto della guida
REQUIRED_TOPICS = {"intro", "primi-passi", "ruoli", "cartella-locale", "checkout", "richiedi-merge", "consenso",
                   "conflitti", "tipi-file", "reimport-auto", "progetti", "storico", "retention", "elimina-progetto",
                   "esporta", "audit", "notifiche", "server", "problemi", "scorciatoie"}


def test_help_topics_have_unique_ids_and_html():
    ids = [t.id for t in H.TOPICS]
    assert len(ids) == len(set(ids)) and REQUIRED_TOPICS <= set(ids)
    for t in H.TOPICS:
        assert t.title.strip() and t.group in H.GROUPS and len(t.html.strip()) > 200, t.id
        assert "<h1>" in t.html and t.text                                   # titolo e testo semplice per la ricerca


def test_help_internal_links_point_to_existing_topics():
    ids = {t.id for t in H.TOPICS}
    for t in H.TOPICS:
        for target in re.findall(r'href="topic:([^"]+)"', t.html):
            assert target in ids, (t.id, target)


def test_help_mentions_exact_button_names_and_facts():
    corpus = " ".join(t.text for t in H.TOPICS)
    for name in ("Modifica progetto...", "Revisioni da conservare sul server", "Applica", "Storico e rollback...",
                 "Esporta progetto (.zip)...", "Elimina progetto...", "Crea backup...", "Importa file base...",
                 "Reimporta file base...", "Nuovo progetto", "Mostra tutti i file", "Richiedi Merge..."):
        assert name in corpus, name
    for fact in ("default 10", "nessuna traccia", ".aps", "UDP 8751", "minimo tra", "eliminata", "F1"):
        assert fact in corpus, fact


def test_help_has_role_badges_and_roles_table():
    roles = H.topic_by_id("ruoli").html
    for r in ("Viewer", "Developer", "Lead", "Admin"):
        assert r in roles
    assert "minimo tra" in H.topic_by_id("ruoli").text
    assert all("CHI PU" in t.html for t in H.TOPICS)                          # ogni argomento indica chi puo' usare la funzione


def test_search_topics_is_accent_insensitive_and_ranks_titles_first():
    assert [t.id for t in H.search_topics("")] == [t.id for t in H.TOPICS]
    hits = [t.id for t in H.search_topics("rollback")]
    assert "storico" in hits and len(hits) < len(H.TOPICS)
    assert H.search_topics("zzzqqq") == []
    assert {t.id for t in H.search_topics("PERCHE")} == {t.id for t in H.search_topics("perché")}
    assert H.search_topics("consenso")[0].id in ("consenso", "intro")        # il titolo pesa piu' del testo
    assert {"server", "problemi"} <= {t.id for t in H.search_topics("firewall udp")}


# ======================================================================================= vista
def test_help_view_builds_and_filters(qapp):
    v = H.HelpView()
    v.resize(1100, 700)
    v.show()
    qapp.processEvents()
    assert v.topic_ids() == [t.id for t in H.TOPICS] and v.current_topic() == "intro"
    assert "Introduzione" in v.browser.toPlainText()
    v.search.setText("firewall")
    got = v.topic_ids()
    assert got and len(got) < len(H.TOPICS) and "server" in got
    assert v.current_topic() in got and v.browser.extraSelections()          # risultato selezionato e parole evidenziate
    assert "trovati" in v.lbl_count.text() or "trovato" in v.lbl_count.text()
    v.search.setText("zzzqqq")
    assert v.topic_ids() == [] and not v.empty.isHidden() and v.browser.isHidden()
    v.search.setText("")
    assert v.topic_ids() == [t.id for t in H.TOPICS] and v.empty.isHidden() and not v.browser.isHidden()


def test_help_view_internal_links_and_selection(qapp):
    v = H.HelpView()
    seen = []
    v.topic_changed.connect(seen.append)
    assert v.show_topic("ruoli") and v.current_topic() == "ruoli" and seen[-1] == "ruoli"
    from PySide6.QtCore import QUrl
    v.browser.anchorClicked.emit(QUrl("topic:consenso"))                      # clic su un link interno
    assert v.current_topic() == "consenso"
    assert not v.show_topic("inesistente")
    row = next(r for r in range(v.topic_list.count()) if v.topic_list.item(r).data(Qt.UserRole) == "checkout")
    v.topic_list.setCurrentRow(row)                                           # selezione dall'elenco
    assert v.current_topic() == "checkout" and "Checkout" in v.browser.toPlainText()
    v.search.setText("rollback")
    assert v.show_topic("intro") and v.search.text() == ""                    # argomento filtrato via: la ricerca si azzera


# ======================================================================================= navigazione / F1
def test_help_nav_entry_only_in_client(qapp):
    from server.gui.main_window import SERVER_NAV
    assert "help" in [k for k, _i, _t in CLIENT_NAV] and "help" not in [k for k, _i, _t in SERVER_NAV]
    assert CLIENT_NAV[-1] == ("help", "help", "Guida")
    sb = Sidebar()
    assert "help" in sb.buttons and sb.buttons["help"].text() == "Guida"
    sb.set_user(type("U", (), {"username": "v", "display_name": "V", "role": "Viewer"})())
    assert not sb.buttons["help"].isHidden() and sb.buttons["users"].isHidden()      # la Guida e' per tutti i ruoli
    assert W.icon("help").isNull() is False


def test_f1_opens_help_in_main_window(live, qapp, tmp_path):
    win = _window(live, tmp_path, "dev1", heartbeat_ms=60_000)
    win.show()
    win._do_login(live["url"], "dev1", PW)
    assert win.pages.currentWidget() is win.dashboard
    win.activateWindow()
    qapp.processEvents()
    QTest.keyClick(win, Qt.Key_F1)
    qapp.processEvents()
    assert win.pages.currentWidget() is win.help and win.sidebar.buttons["help"].isChecked()
    win.sidebar.select("dashboard")
    win._nav("dashboard")
    win.show_help("conflitti")                                                 # anche da codice, su un argomento
    assert win.pages.currentWidget() is win.help and win.help.current_topic() == "conflitti"
    win.ws.stop()
    win.close()


# ======================================================================================= tema
def test_stylesheet_uses_only_supported_properties_and_known_tokens():
    css = build_stylesheet()
    assert "@" not in css
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)                           # i commenti non contano
    for bad in ("box-shadow", "transition", "text-shadow", "transform", "cursor:", "flex", "grid-"):
        assert bad not in css, bad
    for k in ("SURFACE_TOP", "SIDEBAR_TOP", "FIELD_BG", "ACCENT_HOVER", "TEXT_DIM", "DISABLED"):
        assert TOKENS[k] in css, k
    assert T["ACCENT"] == "#00E5FF" and T["BG_0"] == "#121417"


def test_all_icons_render(qapp):
    for name in W._ICONS:
        pm = W.icon_pixmap(name, T["ACCENT"], 20)
        assert not pm.isNull() and pm.devicePixelRatio() == 2, name
        assert not W.icon(name).isNull()


def test_badges_are_italian_and_have_colors(qapp):
    for kind in W._BADGES:
        b = W.StatusBadge(kind)
        assert b.text() and not re.search(r"Pending|Awaiting|Detected|Approved|Cancelled|Rejected|Force", b.text()), kind
        assert W.badge_color(kind).startswith("#")
        b.grab()


def test_empty_state_and_hero_panel_paint(qapp):
    e = W.EmptyState("inbox", "Titolo", "Testo di spiegazione")
    e.resize(400, 260)
    assert e.title.text() == "Titolo" and not e.icon_label.pixmap().isNull()
    assert not e.grab().isNull()
    h = W.HeroPanel()
    h.resize(700, 600)
    img = h.grab().toImage()
    assert img.width() == 700 and img.pixelColor(10, 10) != img.pixelColor(690, 590)     # gradiente reale, non piatto
    e.set_texts("", "")
    assert e.title.isHidden() and e.text.isHidden()


def test_common_helpers(qapp):
    from client.views.common import chip, eyebrow, subtitle, tint
    assert tint("#FF5252", 40) == "rgba(255, 82, 82, 40)"
    c = chip("Serve consenso Lead", T["ORANGE"])
    assert "rgba(255, 171, 64" in c.styleSheet()
    assert eyebrow("progetto").text() == "PROGETTO" and eyebrow("x").property("eyebrow") is True
    assert subtitle("t").property("subtitle") is True


def test_login_hero_and_found_servers_list(qapp):
    from client.discovery import ServerInfo
    from client.views.login_view import LoginView
    found = [ServerInfo("Linea 1", "10.0.0.5", 8750), ServerInfo("Officina", "10.0.0.6", 8750)]
    v = LoginView(run=sync, discover=lambda: found)
    v.resize(1200, 760)
    v.show()
    v.search_servers()
    qapp.processEvents()
    assert any(isinstance(w, W.HeroPanel) for w in v.findChildren(QWidget))
    assert v.found.count() == 2 and not v.found.isHidden()                    # elenco visibile con 2 server
    it = v.found.item(1)
    v.found.itemClicked.emit(it)
    assert v.server_url() == "http://10.0.0.6:8750" and v.found.currentRow() == 1
    one = LoginView(run=sync, discover=lambda: found[:1])
    one.search_servers()
    assert one.found.isHidden() and "Trovato" in one.lbl_server.text()      # un solo server: scelto da solo
    assert not v.grab().isNull()


def _summary(state=ProposalState.AWAITING_CONSENT, **kw):
    now = datetime.now(timezone.utc)
    base = dict(id="p1", author="dev1", author_session="u1", base_revision=1, head_revision=2, state=state,
                state_label="x", created_at=now, updated_at=now, message="Aggiunta logica", n_changes=3,
                touched=["Program[Main]/Routine[Main]"],
                participants=[ParticipantOut(session_id="u1", developer="dev1", decision="approve"),
                              ParticipantOut(session_id="u2", developer="dev2")])
    base.update(kw)
    return ProposalSummary(**base)


def test_dashboard_empty_state_and_state_stripes(qapp):
    from client.views.dashboard_view import DashboardView, ProposalCard
    d = DashboardView()
    d.resize(1100, 700)
    d.show()
    d.set_identity("u9", Role.DEVELOPER)
    assert not d.empty.isHidden() and isinstance(d.empty, W.EmptyState)      # nessuna richiesta: stato vuoto
    d.set_proposals([_summary(), _summary(state=ProposalState.CONFLICT, id="p2", n_conflicts=2, n_unresolved=2)])
    qapp.processEvents()
    assert d.empty.isHidden() and len(d.visible_proposals()) == 2
    cards = d.findChildren(ProposalCard)
    assert {c._stripe.name().upper() for c in cards} == {T["ORANGE"].upper(), T["RED"].upper()}   # colore = stato
    assert all(c.property("clickable") is True for c in cards)
    assert d.btn_active.property("segpos") == "left" and d.btn_all.property("segpos") == "right"
    d.set_developers([])
    assert d.lbl_online.text() == ""
    assert not d.grab().isNull()


def test_server_gui_still_renders_with_shared_theme(qapp, tmp_path):
    """La sidebar e il QSS sono condivisi con la GUI del server: deve costruirsi e disegnarsi senza errori."""
    from tests.test_server_gui import make_window
    w = make_window(tmp_path, autostart=False)
    w.start()
    w.resize(1200, 760)
    w.show()
    qapp.processEvents()
    assert "help" not in w.sidebar.buttons and set(w.sidebar.buttons) == {"status", "users", "audit", "settings"}
    for key in ("status", "users", "audit", "settings"):
        w._nav(key)
        w.sidebar.select(key)
        qapp.processEvents()
        assert not w.grab().isNull()
    assert [lab.text() for lab in w.sidebar.group_labels.values()] == ["SERVER", "SISTEMA"]
    w.close()


def test_audit_view_labels_new_server_actions():
    from client.views.audit_view import ACTION_COLORS, ACTIONS
    for a in ("project_deleted", "retention_changed", "revisions_pruned", "project_exported", "backup_created",
              "master_reimported"):
        assert a in ACTIONS and ACTIONS[a] != a, a
    assert set(ACTION_COLORS) <= set(ACTIONS)
