"""Guida integrata del client: elenco argomenti con ricerca a sinistra, contenuto tipografato a destra.

Il contenuto vive in ``TOPICS`` (lista di ``HelpTopic``: id, titolo, gruppo, html): e' una struttura dati pura, testabile
senza interfaccia. Gli helper (``who``, ``tip``, ``warn``, ``kbd``, ``ui``, ``link``) producono HTML nel sottoinsieme
supportato da ``QTextBrowser`` con i colori del tema; ``search_topics`` e' la ricerca (pura) usata dal campo di ricerca.

Per ogni funzione la guida indica con dei badge di ruolo chi puo' usarla (Viewer / Developer / Lead / Admin).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import cached_property
from typing import Iterable, Optional

from PySide6.QtCore import QSize, QUrl, Qt, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut, QTextCharFormat, QTextCursor, QTextDocument
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QSplitter,
                               QTextBrowser, QTextEdit, QVBoxLayout, QWidget)

from client.theme.tokens import FONT_MONO, FONT_UI, T
from client.theme.widgets import EmptyState, icon
from client.views.common import eyebrow, heading, subtitle

APP_NAME = "Automation Project Server"
CLIENT_NAME = "Automation Project Client"
APP_VERSION = "2.0"


# ======================================================================================= helper HTML
def _mix(fg: str, bg: str, a: float) -> str:
    """Colore ``fg`` con opacita' ``a`` sopra ``bg`` (QTextBrowser non supporta rgba nell'HTML)."""
    f, b = QColor(fg), QColor(bg)
    return QColor(round(f.red() * a + b.red() * (1 - a)), round(f.green() * a + b.green() * (1 - a)),
                  round(f.blue() * a + b.blue() * (1 - a))).name()


_ROLES = (("Viewer", T["MUTED"]), ("Developer", T["ACCENT"]), ("Lead", T["ORANGE"]), ("Admin", T["RED"]))


def role_pill(name: str) -> str:
    color = dict(_ROLES)[name]
    return (f'<span style="background-color:{_mix(color, T["BG_1"], 0.16)}; color:{color}; font-weight:600; '
            f'font-size:12px;">&nbsp;{name}&nbsp;</span>')


def who(min_role: str = "Viewer", only: Optional[Iterable[str]] = None, note: str = "") -> str:
    """Riga «Chi puo' usarlo» con i badge dei ruoli abilitati (``min_role`` e superiori, oppure ``only``)."""
    names = [n for n, _c in _ROLES]
    allowed = list(only) if only else names[names.index(min_role):]
    pills = "&nbsp;".join(role_pill(n) for n in allowed)
    extra = f' <span style="color:{T["MUTED"]};">{note}</span>' if note else ""
    return (f'<p style="margin-top:2px; margin-bottom:10px;"><span style="color:{T["MUTED"]}; font-size:12px;">'
            f'CHI PUÒ USARLO&nbsp;&nbsp;</span>{pills}{extra}</p>')


def kbd(text: str) -> str:
    return (f'<span style="background-color:{T["SURFACE_2"]}; color:{T["TEXT"]}; font-family:{FONT_MONO}; '
            f'font-size:12px;">&nbsp;{text}&nbsp;</span>')


def ui(text: str) -> str:
    """Nome di un pulsante / voce di menu dell'interfaccia."""
    return f'<span style="color:{T["ACCENT"]}; font-weight:600;">«{text}»</span>'


def mono(text: str) -> str:
    return f'<span style="font-family:{FONT_MONO}; color:{T["SYN_MEMBER"]};">{text}</span>'


def link(topic_id: str, text: str) -> str:
    return f'<a href="topic:{topic_id}">{text}</a>'


def _callout(color: str, label: str, body: str) -> str:
    return (f'<table width="100%" cellspacing="0" cellpadding="0" style="margin-top:12px; margin-bottom:12px;"><tr>'
            f'<td width="4" bgcolor="{color}"></td>'
            f'<td bgcolor="{_mix(color, T["BG_1"], 0.10)}" style="padding:10px 14px;">'
            f'<span style="color:{color}; font-weight:700;">{label}</span><br>{body}</td></tr></table>')


def tip(body: str) -> str:
    return _callout(T["ACCENT"], "Suggerimento", body)


def warn(body: str) -> str:
    return _callout(T["ORANGE"], "Attenzione", body)


def example(body: str) -> str:
    return _callout(T["GREEN"], "Esempio", body)


def table(head: list[str], rows: list[list[str]], widths: Optional[list[int]] = None, center_from: int = 99) -> str:
    """Tabella con intestazione: celle HTML libere (badge, ui, ecc.). Dalla colonna ``center_from`` il titolo e' centrato."""
    th = "" if not any(head) else "".join(
        f'<th align="{"center" if i >= center_from else "left"}" style="background-color:{T["SURFACE_2"]}; color:{T["MUTED"]}; '
        f'padding:7px 10px; font-size:12px;"' + (f' width="{widths[i]}%"' if widths else "") + f'>{h}</th>'
        for i, h in enumerate(head))
    body = ""
    for r_i, row in enumerate(rows):
        bg = T["BG_1"] if r_i % 2 == 0 else T["SURFACE_1"]
        body += "<tr>" + "".join(f'<td bgcolor="{bg}" style="padding:7px 10px;" valign="top">{c}</td>' for c in row) + "</tr>"
    return (f'<table width="100%" cellspacing="0" cellpadding="0" style="margin-top:8px; margin-bottom:12px;">'
            f'{"<tr>" + th + "</tr>" if th else ""}{body}</table>')


YES = f'<span style="color:{T["GREEN"]}; font-weight:700;">&#10004;</span>'
NO = f'<span style="color:{T["DISABLED"]};">&#8212;</span>'


def steps(items: list[str]) -> str:
    return "<ol>" + "".join(f"<li style='margin-bottom:5px;'>{i}</li>" for i in items) + "</ol>"


def bullets(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li style='margin-bottom:4px;'>{i}</li>" for i in items) + "</ul>"


# ======================================================================================= argomenti
@dataclass(frozen=True)
class HelpTopic:
    id: str
    title: str
    group: str
    html: str                  # corpo (senza <html>), formattato con gli helper sopra
    keywords: str = ""         # sinonimi cercabili che non compaiono nel testo

    @cached_property
    def text(self) -> str:
        """Testo semplice (per la ricerca)."""
        return plain_text(self.html)


_TAG = re.compile(r"<[^>]+>")


def plain_text(html: str) -> str:
    t = _TAG.sub(" ", html)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&#10004;", "sì"), ("&#8212;", "no"),
                 ("&rarr;", "->"), ("&middot;", "·")):
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()


def _t_intro() -> str:
    return f"""
<h1>Introduzione e concetti</h1>
<p class="lead">{APP_NAME} permette a più persone di lavorare <b>offline</b> sullo stesso progetto PLC senza
sovrascriversi: ognuno modifica la propria copia nel suo software di programmazione (per esempio Studio 5000) e il server
<b>unisce</b> le modifiche in un'unica versione condivisa, ma solo dopo il <b>consenso</b> dei colleghi.</p>
{who("Viewer", note="— ogni ruolo vede il progetto; cosa può fare cambia, vedi " + link("ruoli", "Ruoli e permessi"))}
<h2>Le cinque parole da conoscere</h2>
{table(["Termine", "Che cosa significa"], [
    ["<b>Master</b>", "La versione <b>ufficiale</b> del progetto, custodita sul server. È l'unico file che tutti considerano "
                      "«quello giusto». Nessuno lo modifica direttamente: cambia solo attraverso un merge approvato."],
    ["<b>Revisione</b>", "Ogni volta che il Master cambia nasce una nuova revisione numerata (rev 1, rev 2, rev 3...). Le "
                         "revisioni non si sovrascrivono: lo storico permette di scaricarle e, per l'Admin, di tornare indietro."],
    ["<b>Richiesta di merge</b>", "La proposta di un Developer di aggiungere le proprie modifiche al Master. Il server confronta "
                                  "tre versioni: la <b>Base</b> (la revisione da cui sei partito), la <b>Proposta</b> (il tuo file) "
                                  "e il <b>Master</b> attuale."],
    ["<b>Consenso</b>", "L'approvazione esplicita dei colleghi coinvolti. Senza il loro consenso il Master non viene toccato."],
    ["<b>Conflitto</b>", "Due persone hanno modificato <i>lo stesso elemento</i> in modo diverso (per esempio la stessa rung). "
                         "Il programma non sceglie da solo: lo fa una persona, vedi " + link("conflitti", "Conflitti e come risolverli") + "."],
], [24, 76])}
{example("Marco e Anna fanno il Checkout della <b>rev 4</b>. Marco modifica la rung 3 della <i>MainRoutine</i>, Anna aggiunge "
         "una UDT. Marco chiede il merge e, ottenuto il consenso, il Master diventa <b>rev 5</b>. Quando Anna chiede il suo "
         "merge, il server unisce le sue modifiche a quelle di Marco: erano in punti diversi, quindi nessun conflitto. Se Anna "
         "avesse toccato anche la rung 3, avrebbe dovuto scegliere quale versione tenere.")}
<h2>Il flusso di lavoro in breve</h2>
{steps([f"{ui('Checkout ultimo Master')}: scarichi la revisione corrente nella tua cartella locale.",
        "Lavori offline sul file, con il tuo software di programmazione, e lo salvi nella stessa cartella.",
        f"{ui('Richiedi Merge...')}: scegli il file e descrivi le modifiche.",
        "I colleghi controllano le differenze e danno il consenso (un Lead può dare il Consenso Finale).",
        "Il Master avanza di revisione e tutti ricevono automaticamente la nuova copia."])}
{tip("Il tuo file di lavoro non viene mai sovrascritto dal programma: le nuove revisioni arrivano come file separati. "
     "Dettagli in " + link("cartella-locale", "Cartella locale e file di lavoro") + ".")}
"""


def _t_primi_passi() -> str:
    return f"""
<h1>Primi passi</h1>
{who("Viewer")}
<h2>Connessione e ricerca automatica del server</h2>
<p>All'apertura del programma la schermata di accesso <b>cerca da sola i server</b> nella rete locale (il pulsante
<b>&#8635;</b> accanto al campo SERVER ripete la ricerca).</p>
{bullets(["Se ne trova <b>uno</b>, lo seleziona e verifica che risponda.",
          "Se ne trova <b>più di uno</b>, li mostra in un elenco (nome e indirizzo): clicca quello giusto. Viene riselezionato l'ultimo usato.",
          "Se non ne trova, scrivi l'indirizzo a mano, per esempio " + mono("http://nomepc:8750") + ".",
          "Il pallino colorato sotto il campo indica se il server è raggiungibile (verde) oppure no (rosso)."])}
{warn("La ricerca automatica funziona solo <b>nella stessa rete locale</b>: non attraversa router o VPN. In quel caso usa "
      "l'indirizzo manuale. Se il server non compare nonostante sia nella stessa rete, controlla il firewall: vedi "
      + link("problemi", "Risoluzione dei problemi") + ".")}
<h2>Accesso</h2>
{steps(["Inserisci <b>Utente</b> e <b>Password</b> assegnati dall'amministratore (l'icona a forma di occhio mostra la password).",
        "Lascia attiva <b>Ricorda server e utente</b> per ritrovarli al prossimo avvio (la password non viene salvata).",
        f"Premi {ui('Accedi')} oppure Invio."])}
<p>Dopo alcuni tentativi con password errata l'account viene <b>bloccato per qualche minuto</b>.</p>
<h2>Cambio password</h2>
<p>Al <b>primo accesso</b>, o dopo un reset fatto dall'Admin, il programma ti chiede di scegliere una nuova password prima di
continuare. In qualsiasi momento puoi cambiarla con l'icona a forma di <b>chiave</b> nell'area utente in basso a sinistra.
La password deve avere almeno <b>10 caratteri</b>, con almeno una lettera e una cifra.</p>
<h2>Scegliere il progetto</h2>
<p>Sotto il logo, il menu <b>PROGETTO</b> elenca i progetti a cui hai accesso. Se non ne vedi nessuno, chiedi a un
amministratore di assegnarti i permessi. L'Admin vede accanto al menu il pulsante <b>+</b> per creare un
{link("progetti", "nuovo progetto")}.</p>
"""


def _roles_table() -> str:
    rows = [
        ["Vedere i progetti assegnati, lo storico delle revisioni e le richieste", YES, YES, YES, YES],
        ["Scaricare il Master (e le revisioni dallo storico)", YES, YES, YES, YES],
        ["Consultare la cronologia eventi", YES, YES, YES, YES],
        ["Checkout con file di lavoro e Richiedi Merge", NO, YES, YES, YES],
        ["Dare il consenso o rifiutare una richiesta", NO, YES, YES, YES],
        ["Annullare una richiesta (le proprie)", NO, YES, YES, YES],
        ["Consenso Finale del Lead", NO, NO, YES, YES],
        ["Importare / reimportare il file base", NO, NO, YES, YES],
        ["Risolvere i conflitti di qualunque richiesta, Forza Merge", NO, NO, NO, YES],
        ["Rollback (Ripristina come nuova revisione)", NO, NO, NO, YES],
        ["Creare, modificare, esportare ed eliminare progetti; revisioni da conservare", NO, NO, NO, YES],
        ["Utenti, permessi, sessioni e verifica integrità audit", NO, NO, NO, YES],
    ]
    return table(["Funzione", role_pill("Viewer"), role_pill("Developer"), role_pill("Lead"), role_pill("Admin")],
                 [[r[0]] + [f'<p align="center">{c}</p>' for c in r[1:]] for r in rows], [52, 12, 12, 12, 12], center_from=1)


def _t_ruoli() -> str:
    return f"""
<h1>Ruoli e permessi</h1>
{who("Viewer", note="— la tabella vale per tutti")}
<p>Ogni utente ha un <b>ruolo sull'account</b>; per ogni progetto l'Admin assegna un <b>ruolo sul progetto</b>. Quello che
puoi fare in un progetto è sempre il <b>minimo tra i due</b>.</p>
{_roles_table()}
<h2>Il ruolo effettivo su un progetto</h2>
<p>Regola: <b>ruolo effettivo = minimo tra ruolo dell'account e ruolo sul progetto</b>. Sul progetto si può assegnare lo stesso
ruolo dell'account o uno inferiore, mai uno superiore. Senza un permesso sul progetto non c'è nessun accesso.</p>
{example("Giulia ha l'account <b>Lead</b>, ma sul progetto «Linea 2» l'Admin le ha dato il ruolo <b>Viewer</b>. Su «Linea 2» "
         "Giulia può solo consultare e scaricare; su un progetto dove è Lead può dare il Consenso Finale.")}
{bullets([f"<b>Viewer</b>: sola lettura. Al posto di {ui('Checkout ultimo Master')} vede {ui('Scarica ultimo Master')} e non ha il pulsante {ui('Richiedi Merge...')}.",
          "<b>Developer</b>: lavora sul file, invia richieste di merge, dà il consenso a quelle dei colleghi.",
          "<b>Lead</b>: come Developer, più il <b>Consenso Finale</b>, il rifiuto di qualunque richiesta, l'importazione e la reimportazione del file base.",
          "<b>Admin</b>: tutto, in ogni progetto. Il ruolo Admin esiste solo a livello di account."])}
<p>Il ruolo viene riletto dal server a ogni richiesta: se un Admin ti declassa o ti disattiva, la modifica ha
<b>effetto immediato</b>. Non si può rimuovere l'ultimo Admin né declassare o disattivare se stessi.</p>
"""


def _t_cartella() -> str:
    return f"""
<h1>Cartella locale e file di lavoro</h1>
{who("Viewer")}
<p>Nella schermata principale la riga <b>Cartella locale</b> indica dove il progetto è salvato sul tuo PC. La scegli tu, per
ogni progetto, con {ui('Cambia cartella...')}; {ui('Apri')} la mostra in Esplora risorse. Finché non ne scegli una, il
programma ne propone una (la riga lo segnala con «proposta»). Al primo Checkout te la chiede.</p>
<h2>Come si chiamano i file</h2>
<p>I file portano <b>il nome del file con cui il Master è stato importato</b> e, in fondo, il numero di revisione:
{mono("&lt;nome&gt;_revN&lt;estensione&gt;")}. Importando {mono("Linea1.L5K")}, il Checkout della rev 3 salva
{mono("Linea1_rev3.L5K")}: è il file su cui lavori.</p>
<h2>La cartella nascosta .aps</h2>
<p>Dentro la cartella locale c'è una sottocartella nascosta {mono(".aps")} con una <b>copia pulita</b> di ogni revisione
scaricata. Serve al programma per capire <b>che cosa hai modificato tu</b> (confrontando il tuo file con la copia pulita): non
modificarla né cancellarla a mano.</p>
{warn("<b>Regola d'oro: il file di lavoro non viene mai sovrascritto.</b> Se rifai il Checkout della stessa revisione e il tuo "
      "file ha modifiche non ancora unite, il programma lo lascia com'è. Le nuove revisioni arrivano sempre come file "
      + mono("&lt;nome&gt;_revN") + " distinti.")}
<p>La pulizia delle vecchie copie è descritta in {link("retention", "Revisioni conservate e pulizia")}.</p>
"""


def _t_checkout() -> str:
    return f"""
<h1>Checkout</h1>
{who("Viewer", note="— il Viewer scarica soltanto")}
<p>Il Checkout scarica nella cartella locale l'<b>ultima revisione del Master</b> e la fissa come la tua <b>base</b>: tutte le
modifiche che farai verranno confrontate con quella revisione.</p>
{steps([f"Scegli il progetto e premi {ui('Checkout ultimo Master')} (il Viewer vede {ui('Scarica ultimo Master')}).",
        "La prima volta il programma chiede in quale cartella salvare.",
        f"Compaiono il file {mono('&lt;nome&gt;_revN&lt;estensione&gt;')} e la copia pulita in {mono('.aps')}.",
        "Apri il file nel tuo software di programmazione, modificalo e salvalo nella stessa cartella."])}
<p>Quando il Master avanza (per un merge approvato di un collega), il programma scarica da solo la nuova revisione come file
separato, senza toccare il tuo lavoro. Rifare il Checkout della stessa revisione non cancella le tue modifiche.</p>
{tip("Scarica il Master all'inizio di ogni sessione di lavoro: partire da una base recente riduce i conflitti.")}
"""


def _t_merge() -> str:
    return f"""
<h1>Lavorare e richiedere un merge</h1>
{who("Developer")}
<p>Quando hai finito le tue modifiche e le hai salvate nella cartella locale, premi {ui('Richiedi Merge...')}.</p>
<h2>Scegliere il file</h2>
<p>La finestra elenca i file della cartella locale (e delle sottocartelle, fino a 2 livelli) con <b>la stessa estensione del
Master</b>, dal più recente. Il Master scaricato e le sue copie non compaiono. Per ogni file indica lo stato:</p>
{table(["Stato", "Significato"], [
    [f'<span style="color:{T["GREEN"]};">Modificato</span>', "Il file è cambiato dopo il Checkout: è quello da unire. Viene preselezionato il più recente tra questi."],
    [f'<span style="color:{T["MUTED"]};">Identico al Master</span>', "Non ci sono modifiche da unire."],
    [f'<span style="color:{T["ORANGE"]};">Da verificare</span>', "Non è possibile stabilire se sia cambiato."],
    [f'<span style="color:{T["RED"]};">Non sembra un file L5X</span>', "Il contenuto non è del tipo del Master del progetto: non può essere inviato."],
], [30, 70])}
{bullets([f"{ui('Mostra tutti i file')}: elenca qualsiasi file della cartella, non solo quelli con l'estensione del Master (utile per altri tipi di file).",
          f"{ui('Altro file...')}: sceglie un file anche fuori dalla cartella locale.",
          f"{ui('Aggiorna')}: rilegge la cartella dopo aver salvato un file nuovo.",
          "Un doppio clic su un file lo seleziona e invia la richiesta."])}
<h2>Il messaggio</h2>
<p>Nel campo <b>Descrizione delle modifiche</b> scrivi in una riga che cosa hai cambiato (per esempio
«Aggiunta logica del nastro N3»). Compare nella richiesta, nello storico e nella cronologia eventi: i colleghi la leggono
prima di votare.</p>
<h2>Dopo l'invio</h2>
<p>Il server calcola il merge e si apre la <b>schermata di consenso</b> con le modifiche, gli eventuali conflitti e i
partecipanti. Con {ui('Salva anteprima...')} puoi salvare su disco il file <i>risultante</i> dal merge e controllarlo nel tuo
software prima di votare. Vedi {link("consenso", "Il consenso")}.</p>
{warn("Il file deve essere dello <b>stesso tipo</b> del Master (un L5X in un progetto L5K viene rifiutato) e, per L5X e L5K, "
      "dello stesso controller. Se la revisione da cui sei partito non è più conservata dal server, la richiesta viene "
      "rifiutata: vedi " + link("problemi", "Risoluzione dei problemi") + ".")}
"""


def _t_consenso() -> str:
    return f"""
<h1>Il consenso</h1>
{who("Developer", note="— il Consenso Finale richiede Lead o Admin")}
<p>Nessuna modifica entra nel Master senza consenso. Ogni consenso riguarda il <b>risultato esatto</b> del merge che hai
visto: se cambiano il Master o le risoluzioni dei conflitti, i consensi già dati si <b>azzerano</b> e vanno rinnovati.</p>
<h2>Chi deve votare (quorum)</h2>
{bullets(["<b>L'autore</b> della richiesta e tutti i <b>Developer e Lead connessi</b> nel momento dell'invio.",
          "Chi si disconnette dopo l'invio <b>esce dal quorum</b> e non blocca più gli altri.",
          "I <b>Viewer</b> non votano.",
          "Se il progetto richiede il <b>Consenso Finale del Lead</b> e l'autore è un Developer, serve anche l'approvazione di un "
          "Lead o Admin diverso dall'autore, anche se non è connesso."])}
<h2>Che cosa si può fare</h2>
{table(["Azione", "Chi", "Note"], [
    [ui("Dai Consenso al Merge"), role_pill("Developer") + " " + role_pill("Lead") + " " + role_pill("Admin"),
     "Per i partecipanti richiesti. Se hai il compito di Consenso Finale il pulsante diventa «Dai Consenso Finale (Lead)». Non è possibile finché restano conflitti da risolvere."],
    [ui("Rifiuta / Chiedi Modifiche"), role_pill("Developer") + " " + role_pill("Lead") + " " + role_pill("Admin"),
     "Chiede obbligatoriamente un <b>commento</b> con il motivo, visibile a tutti. La richiesta passa a «Rifiutata»."],
    [ui("Annulla richiesta"), "l'autore, oppure " + role_pill("Admin"), "Chiude la richiesta senza toccare il Master."],
    [ui("Forza Merge..."), role_pill("Admin"),
     "Applica il merge senza attendere i consensi. Chiede un motivo (minimo 5 caratteri) che finisce nella cronologia eventi; disponibile solo senza conflitti aperti."],
], [30, 25, 45])}
<p>I partecipanti compaiono nell'intestazione con un anello colorato: <span style="color:{T['GREEN']};">verde</span> consenso
dato, <span style="color:{T['ORANGE']};">arancione</span> in attesa, <span style="color:{T['RED']};">rosso</span> rifiutato. Il
commento di chi ha votato appare accanto al suo nome.</p>
<p>Quando tutti i consensi richiesti sono arrivati, il Master avanza di una revisione e tutti i client scaricano la nuova copia.</p>
{tip("Prima di approvare usa la scheda <b>Modifiche</b>: mostra il confronto riga per riga (a due colonne o inline) tra la base e "
     "il risultato.")}
"""


def _t_conflitti() -> str:
    return f"""
<h1>Conflitti e come risolverli</h1>
{who("Developer", note="— li risolve l'autore della richiesta; un Admin può farlo al posto suo")}
<p>Si ha un conflitto quando la tua richiesta e il Master hanno cambiato <b>lo stesso elemento</b> in modi diversi dopo la
revisione di partenza. La scheda <b>Conflitti</b> della schermata di consenso mostra, per ciascun conflitto, tre pannelli:
<b>Base</b> | <b>Proposta</b> | <b>Master</b>.</p>
<h2>Le quattro scelte</h2>
{table(["Scelta", "Effetto"], [
    [ui("Tieni la proposta"), "Vale la tua versione dell'elemento; le modifiche del Master su di esso vengono scartate."],
    [ui("Tieni il Master"), "Vale la versione già presente nel Master; la tua modifica su quell'elemento viene scartata."],
    [ui("Tieni entrambe (Master poi proposta)"), "Disponibile per rung e righe: si mantengono entrambe le versioni, prima quella del Master e poi la tua."],
    [ui("Modifica manualmente"), "Scrivi tu il risultato nel riquadro (una rung o riga per linea; per un elemento XML incolla l'XML completo)."],
], [34, 66])}
{steps([f"Seleziona un conflitto nell'elenco a sinistra (quelli da risolvere sono segnati «DA RISOLVERE»).",
        "Scegli come risolverlo.",
        f"Quando hai scelto per tutti, premi {ui('Applica risoluzioni')}.",
        "Il server ricalcola il merge: i consensi eventualmente già dati si azzerano e si riparte dal voto."])}
{warn("Finché ci sono conflitti non risolti nessuno può dare il consenso. Scegli con attenzione: «Tieni il Master» e «Tieni la "
      "proposta» scartano del lavoro di qualcuno. Per i casi dubbi usa il commento per accordarti con il collega.")}
"""


def _t_tipi() -> str:
    return f"""
<h1>Tipi di file supportati</h1>
{who("Viewer", note="— lo stesso per tutti i ruoli")}
<p>Il Master di un progetto può essere <b>qualsiasi file</b>. Il tipo viene riconosciuto dal <b>contenuto</b> (non dal
nome) e decide come funziona il merge:</p>
{table(["Tipo", "Merge"], [
    ["<b>L5X</b> (export XML di Studio 5000)", "<b>Strutturale</b>: Rung, Line, Tag, UDT, AOI, Program e Routine vengono confrontati uno per uno, quindi modifiche in punti diversi si uniscono da sole."],
    ["<b>L5K</b> (export ASCII Rockwell)", "<b>Per righe</b> (3-way): le modifiche sono etichettate con Program/Routine; viene controllato che sia lo stesso controller."],
    ["<b>PLCopen XML</b> (CODESYS e simili, IEEE 61131-10)", "<b>Strutturale</b>: POU, tipi di dato, variabili, azioni, transizioni, metodi e configurazioni si confrontano uno per uno (vedi " + link('plcopen', 'PLCopen XML') + ")."],
    ["<b>File di testo</b> (.st, .xml, .project di altri ambienti...)", "<b>Per righe</b>, come l'L5K."],
    ["<b>File binario</b>", "<b>Nessun merge</b>: il file è solo versionato. Si può scaricare ogni revisione e, per l'Admin, ripristinarla; le richieste di merge vengono rifiutate."],
], [34, 66])}
<p>L'estensione del primo file caricato resta quella del progetto: i file scaricati e quelli cercati da {ui('Richiedi Merge...')}
usano lo stesso nome e la stessa estensione. Il file inviato deve essere dello stesso tipo del Master.</p>
{warn("Il merge per righe (L5K e testo) non conosce la struttura del programma: due modifiche in righe diverse si uniscono, ma "
      "modifiche logicamente collegate (un tag e la rung che lo usa) possono dare un file da controllare. La schermata di "
      "consenso mostra sempre il risultato prima del voto: usa " + ui("Salva anteprima...") + " e aprilo nel tuo software.")}
{warn("La <b>Safety Signature</b> dei controller GuardLogix <b>non viene ricalcolata</b> dal merge: va verificata in Studio 5000 "
      "dopo ogni merge. Il merge è stato provato su file di esempio: prima dell'uso su impianti reali provalo su copie.")}
"""


def _t_reimport_auto() -> str:
    return f"""
<h1>Reimport automatico dopo il merge</h1>
{who("Developer", note="— avviene da solo per chi ha chiesto il merge")}
<p>Quando una tua richiesta di merge viene completata, il programma controlla che cosa ha trovato il server nel Master
rispetto alla revisione da cui eri partito.</p>
{bullets(["<b>Solo se</b> sul Master c'erano modifiche di altri che tu non avevi (non contano quelle identiche alle tue), il "
          "programma scarica da solo il nuovo Master come tua nuova copia di lavoro " + mono("&lt;nome&gt;_revN&lt;estensione&gt;") +
          " e la imposta come nuova base.",
          "Te lo <b>segnala</b>: riga nel registro Eventi, messaggio nella barra di stato (e nell'area di notifica, se disponibile) "
          "e una finestra <b>non bloccante</b> con il numero di modifiche altrui trovate e gli elementi toccati.",
          "Se il merge non porta modifiche altrui, non succede nulla."])}
{tip("Il file che stavi usando <b>resta invariato su disco</b>: puoi riaprirlo in ogni momento. Un file con modifiche non ancora "
     "unite non viene mai sovrascritto.")}
"""


def _t_progetti() -> str:
    return f"""
<h1>Progetti e file base</h1>
{who("Lead", note="— creare un progetto è riservato all'Admin")}
{table(["Funzione", "Dove", "Chi"], [
    [ui("Nuovo progetto"), "pulsante <b>+</b> accanto al menu PROGETTO (o in Utenti &amp; Permessi)", role_pill("Admin")],
    [ui("Importa file base..."), "schermata principale, finché il Master non è inizializzato", role_pill("Lead") + " " + role_pill("Admin")],
    [ui("Reimporta file base..."), "schermata principale, su un progetto già in lavorazione", role_pill("Lead") + " " + role_pill("Admin")],
    [ui("Modifica progetto..."), "Utenti &amp; Permessi &gt; Permessi di progetto", role_pill("Admin")],
], [30, 46, 24])}
<h2>Creare un progetto</h2>
<p>L'Admin crea il progetto con {ui('Nuovo progetto')} e subito dopo il programma propone di importare il file base. Poi, in
<b>Utenti &amp; Permessi &gt; Permessi di progetto</b>, assegna a ogni utente il suo ruolo sul progetto.</p>
<h2>Importa file base</h2>
<p>Finché il Master non è inizializzato la schermata principale lo segnala e mostra {ui('Importa file base...')}. Scegli il file
di partenza (L5K, L5X o qualsiasi altro file: nel selettore c'è sempre il filtro «Tutti i file»). Il file diventa la rev 1 e il
suo nome diventa il nome dei file distribuiti ai client.</p>
<h2>Reimporta file base</h2>
<p>{ui('Reimporta file base...')} sostituisce il Master di un progetto in corso con un file base aggiornato, creando una
<b>nuova revisione</b>: la cronologia resta <b>intatta</b> e si può sempre tornare indietro dallo storico.</p>
{bullets(["Chiede un <b>motivo</b> (minimo 5 caratteri), che finisce nella cronologia eventi.",
          "Il file deve essere dello stesso tipo del Master, per L5X/L5K dello stesso controller, e diverso dal Master corrente.",
          "Le richieste di merge in sospeso vengono rianalizzate e i consensi già dati si azzerano."])}
{warn("Il reimport <b>non passa dal consenso</b> degli altri sviluppatori: per questo è riservato a Lead e Admin ed è sempre "
      "tracciato nella cronologia eventi.")}
<h2>Modifica progetto</h2>
<p>{ui('Modifica progetto...')} (Admin) permette di cambiare il nome del progetto e le sue impostazioni, per esempio la
richiesta del Consenso Finale del Lead. Il limite di revisioni è in
{link("retention", "Revisioni conservate e pulizia")}.</p>
"""


def _t_storico() -> str:
    return f"""
<h1>Storico, scarico e rollback</h1>
{who("Viewer", note="— il rollback è riservato all'Admin")}
<p>Il pulsante {ui('Storico')} della schermata principale elenca le revisioni del Master con numero, data, autore e
messaggio. L'Admin ci arriva anche da <b>Utenti &amp; Permessi &gt; Permessi di progetto</b> con {ui('Storico e rollback...')}.</p>
{table(["Funzione", "Chi", "Che cosa fa"], [
    [ui("Scarica revisione"), role_pill("Viewer") + " " + role_pill("Developer") + " " + role_pill("Lead") + " " + role_pill("Admin"),
     "Salva sul PC la revisione scelta, nel punto che indichi. Non cancella né sposta nulla."],
    [ui("Ripristina come nuova revisione..."), role_pill("Admin"),
     "<b>Rollback</b>: il contenuto della revisione scelta diventa una <b>nuova</b> revisione del Master. Chiede un motivo (minimo 5 caratteri)."],
], [30, 30, 40])}
<p>Il rollback <b>non cancella niente</b>: se il Master è alla rev 7 e ripristini la rev 4, nasce la rev 8 con il contenuto
della rev 4, e le rev 5, 6 e 7 restano nello storico. L'operazione è registrata nella cronologia eventi.</p>
{warn("Si possono ripristinare e scaricare solo le revisioni <b>ancora conservate</b> dal server: quelle eliminate dal limite "
      "compaiono nello storico come «eliminata». Vedi " + link("retention", "Revisioni conservate e pulizia") + ".")}
"""


def _t_retention() -> str:
    return f"""
<h1>Revisioni conservate e pulizia</h1>
{who(only=["Admin"], note="— decide il limite; la pulizia locale è automatica per tutti")}
<h2>Sul server</h2>
<p>Il server conserva <b>al massimo N revisioni per progetto</b> (default <b>10</b>). Il valore lo decide l'Admin in
<b>Utenti &amp; Permessi &gt; Permessi di progetto</b>: imposta il numero in <b>Revisioni da conservare sul server</b> e premi
{ui('Applica')}.</p>
{bullets(["Le revisioni più vecchie oltre il limite vengono <b>eliminate definitivamente</b> dal disco del server.",
          "Restano però <b>nello storico</b> con lo stato «eliminata»: si vede che sono esistite, ma non si possono più scaricare né ripristinare.",
          "Il server non elimina l'ultima revisione né le basi delle richieste di merge ancora aperte (servono al confronto).",
          "Ridurre il limite elimina subito le revisioni in eccesso: l'operazione è registrata nella cronologia eventi."])}
{warn("L'eliminazione è <b>definitiva</b>. Se ti servono copie di sicurezza usa " + ui("Esporta progetto (.zip)...") + " prima di "
      "abbassare il limite: vedi " + link("esporta", "Esportare un progetto") + ".")}
<h2>Sul tuo PC</h2>
<p>Dopo ogni download del Master il client tiene in cartella locale <b>al massimo le ultime N copie</b>
{mono("&lt;nome&gt;_revN")} (N è lo stesso limite del progetto), insieme alle loro copie pulite in {mono(".aps")}.</p>
{tip("La pulizia locale <b>non cancella mai</b> il tuo file di lavoro né la sua base, e non tocca i file modificati o non "
     "riconosciuti: nel dubbio lascia tutto com'è. Se compare nel registro «Pulizia locale: rimosse N copie», sono state "
     "tolte solo copie identiche all'originale scaricato.")}
"""


def _t_elimina() -> str:
    return f"""
<h1>Eliminare un progetto</h1>
{who(only=["Admin"])}
<p>In <b>Utenti &amp; Permessi &gt; Permessi di progetto</b> il pulsante {ui('Elimina progetto...')} rimuove un progetto dal
server. È un'operazione <b>irreversibile</b>, per questo:</p>
{bullets(["devi <b>digitare il nome del progetto</b> per confermare;",
          "vengono eliminati i <b>file</b> (Master e tutte le revisioni), i <b>permessi</b> degli utenti e <b>l'audit del progetto</b>;",
          "nella cronologia eventi <b>non resta nessuna traccia</b> del progetto eliminato;",
          "i client collegati vengono avvisati e il progetto sparisce dai loro elenchi."])}
{tip("Prima di eliminare puoi <b>esportare il progetto in un file .zip</b> con le revisioni e l'audit: vedi "
     + link("esporta", "Esportare un progetto") + ".")}
<p>I <b>file locali</b> sui PC dei client <b>non vengono toccati</b>: chi aveva scaricato il progetto conserva la propria cartella
e i propri file di lavoro. Se non vuoi eliminare ma solo smettere di usare il progetto, puoi <b>archiviarlo</b> con
{ui('Archivia')}: resta consultabile ma non accetta nuove richieste di merge.</p>
"""


def _t_esporta() -> str:
    return f"""
<h1>Esportare un progetto (.zip)</h1>
{who(only=["Admin"])}
<p>In <b>Utenti &amp; Permessi &gt; Permessi di progetto</b>, {ui('Esporta progetto (.zip)...')} salva un archivio con:</p>
{bullets(["le <b>revisioni del Master</b> ancora conservate (file " + mono("&lt;nome&gt;_revN&lt;estensione&gt;") + ");",
          mono("history.json") + ": i metadati di tutte le revisioni (autore, data, messaggio, SHA-256 e se sono state eliminate);",
          mono("audit.csv") + ": gli eventi della cronologia relativi al progetto;",
          mono("README.txt") + ": un riepilogo, con l'elenco delle revisioni eventualmente eliminate dal limite."])}
<p>L'esportazione è registrata nella cronologia eventi. È il modo giusto per archiviare un progetto concluso o per avere una
copia di sicurezza prima di {link("elimina-progetto", "eliminarlo")}.</p>
"""


def _t_audit() -> str:
    return f"""
<h1>Cronologia eventi e audit</h1>
{who("Viewer", note="— la verifica d'integrità è dell'Admin")}
<p>La voce <b>Cronologia eventi</b> mostra il registro di tutto ciò che succede: accessi, checkout e download del Master,
richieste di merge, consensi, rifiuti, merge completati o forzati, ripristini, gestione utenti e permessi, esportazioni,
eliminazioni. Ogni riga riporta data, utente, azione, progetto, oggetto, dettagli e un'impronta (hash).</p>
<h2>Perché è affidabile</h2>
<p>Il registro è <b>immutabile</b>: ogni voce contiene l'hash SHA-256 della precedente, formando una <b>catena</b>, e il
database rifiuta modifiche e cancellazioni. Se qualcuno alterasse una riga, la catena si spezzerebbe.</p>
{table(["Funzione", "Chi", "Note"], [
    ["Filtri: progetto corrente / tutti i progetti visibili, azione, utente", role_pill("Viewer") + " ... " + role_pill("Admin"),
     "Un utente vede solo i progetti a cui ha accesso; l'Admin vede tutto."],
    [ui("Esporta CSV"), role_pill("Viewer") + " ... " + role_pill("Admin"), "Salva le voci mostrate in un file CSV (separatore «;», apribile con Excel)."],
    [ui("Verifica integrità"), role_pill("Admin"), "Ricontrolla l'intera catena di hash e indica se è coerente o qual è la prima voce non valida."],
], [44, 24, 32])}
"""


def _t_notifiche() -> str:
    return f"""
<h1>Notifiche e stato</h1>
{who("Viewer")}
<h2>L'indicatore di connessione (LED)</h2>
<p>In basso a sinistra, sopra l'area utente, un LED mostra lo stato della connessione in tempo reale con il server:</p>
{table(["LED", "Significato"], [
    [f'<span style="color:{T["GREEN"]}; font-weight:700;">&#9679; verde</span>', "<b>Server connesso</b>: ricevi gli aggiornamenti in tempo reale."],
    [f'<span style="color:{T["ORANGE"]}; font-weight:700;">&#9679; arancione</span>', "<b>Riconnessione...</b>: il programma sta ritentando."],
    [f'<span style="color:{T["RED"]}; font-weight:700;">&#9679; rosso</span>', "<b>Server offline</b>: controlla che il server sia acceso e raggiungibile."],
], [26, 74])}
<h2>Che cosa ti viene notificato</h2>
{bullets(["Una <b>nuova richiesta di merge</b> in cui devi votare: si apre da sola la schermata di consenso.",
          "Richieste rifiutate, merge completati e aggiornamenti del Master: messaggio nell'<b>area di notifica</b> di Windows (se disponibile), nella barra di stato in basso e nel pannello <b>Eventi</b> della schermata principale.",
          "Il <b>reimport automatico</b> dopo un tuo merge: finestra non bloccante, vedi " + link("reimport-auto", "Reimport automatico dopo il merge") + "."])}
<p>Le finestre di consenso e di avviso sono <b>non bloccanti</b>: puoi continuare a lavorare nella finestra principale e tornare
a votare quando vuoi. Il pannello <b>Programmatori</b> mostra chi è connesso e da quale revisione è partito.</p>
"""


def _t_plcopen() -> str:
    return f"""
<h1>PLCopen XML (CODESYS e simili)</h1>
{who("Viewer", note="— lo stesso per tutti i ruoli")}
<p>I file <b>PLCopen XML</b> (standard IEC 61131-10, esportati da CODESYS e da altri ambienti) vengono riconosciuti dal contenuto e uniti in modo
<b>strutturale</b>, non per righe.</p>
{bullets(["Si confrontano uno per uno: <b>POU</b> (programmi, function block, funzioni), <b>tipi di dato</b>, <b>variabili</b>, <b>azioni</b>, <b>transizioni</b>, <b>metodi</b> e <b>configurazioni/risorse</b>.",
          "Il codice in <b>ST</b> e <b>IL</b> si unisce per righe dentro la singola POU: due persone in punti diversi della stessa POU non confliggono.",
          "<b>LD</b>, <b>FBD</b> e <b>SFC</b> (grafici) sono confrontati per intero: se due persone toccano la stessa rete grafica si ha un conflitto da risolvere.",
          "Il progetto deve avere lo stesso nome (<i>contentHeader</i>) del Master; i file con DOCTYPE/entità esterne vengono rifiutati."])}
{warn("Il merge è stato provato su file di esempio e sul formato standard: le estensioni proprietarie di CODESYS (metodi, struttura del progetto) vanno verificate su un export reale. Controlla sempre il risultato nell'anteprima prima del voto.")}
"""


def _t_https() -> str:
    return f"""
<h1>Connessione cifrata (HTTPS)</h1>
{who("Viewer", note="— attivazione e certificato: solo Admin sul server")}
<p>Il server può parlare in <b>HTTPS</b> (e <b>wss</b> per le notifiche) con un certificato <b>autofirmato</b> generato da lui (RSA o EC, valido 825 giorni, con i nomi
del PC), oppure con un certificato e una chiave tuoi (file PEM).</p>
{bullets(["<b>Admin</b>: nelle Impostazioni del server attiva HTTPS e, se vuoi, scegli certificato e chiave. Da riga di comando: " + mono("--tls") + ", " + mono("--no-tls") + ", " + mono("--tls-cert") + ", " + mono("--tls-key") + ". Se il server è un servizio Windows, cambiare le impostazioni richiede di reinstallarlo.",
          "<b>Utente</b>: la ricerca automatica indica se il server è cifrato. Al <b>primo collegamento</b> a un certificato autofirmato vedi l'impronta (SHA-256) e confermi: da quel momento il client la ricorda e ti avvisa se cambia.",
          "Se l'impronta cambia (certificato rigenerato) il client rifiuta il collegamento finché non verifichi con l'Admin e accetti la nuova."])}
{tip("Confronta l'impronta mostrata con quella indicata dall'Admin nelle Impostazioni del server prima di accettare.")}
"""


def _t_confronto() -> str:
    return f"""
<h1>Confronto tra revisioni, etichette e blocco</h1>
{who("Viewer", note="— etichette e blocco: Lead e Admin; sblocco: Admin")}
<p>Nello {link("storico", "Storico")} puoi:</p>
{bullets([ui("Confronta...") + ": scegli due revisioni e vedi cosa è cambiato (per L5X/PLCopen in modo strutturale, per il testo riga per riga).",
          ui("Etichetta...") + ": dai un nome a una revisione (per esempio «Collaudo FAT»). Lead e Admin.",
          ui("Blocca") + ": una revisione bloccata <b>non viene mai eliminata</b> dal limite di revisioni e non conta nel limite. Lead e Admin bloccano; solo l'<b>Admin</b> sblocca."])}
"""


def _t_presa() -> str:
    return f"""
<h1>Presa in carico (blocco esclusivo)</h1>
{who("Developer")}
<p>Per avvisare i colleghi che stai lavorando al Master puoi <b>prenderlo in carico</b>: la barra in alto mostra chi lo ha e da quando.
Il blocco scade da solo dopo <b>8 ore</b> (rinnovabile) e viene rilasciato dalla stessa barra.</p>
{bullets(["Per i <b>file binari</b>, che non si possono unire, la presa in carico è obbligatoria: carichi la nuova versione con il <b>check-in</b>.",
          "L'Admin può <b>forzare</b> il rilascio del blocco di un altro utente.",
          "Prese in carico, rilasci, forzature e check-in sono registrati nella cronologia eventi."])}
"""


def _t_discussione() -> str:
    return f"""
<h1>Discussione sulle richieste di merge</h1>
{who("Developer", note="— i Viewer leggono")}
<p>Nella finestra di consenso, la scheda <b>Discussione</b> permette di scrivere commenti sulla richiesta prima di approvarla o rifiutarla.</p>
{bullets(["Cita un collega con <b>@nome</b>: riceve una notifica.",
          "Puoi <b>modificare</b> un tuo commento entro 15 minuti; puoi eliminarlo (resta il segno «eliminato»; l'Admin può eliminare quelli degli altri).",
          "Nella dashboard il badge «💬 N» indica quante risposte ha una richiesta.",
          "I commenti arrivano in tempo reale a chi ha la richiesta aperta e sono registrati nella cronologia eventi."])}
"""


def _t_branch() -> str:
    return f"""
<h1>Branch</h1>
{who("Developer", note="— i branch di team li crea un Lead; i Viewer li leggono soltanto")}
<p>Un <b>branch</b> è una linea di lavoro alternativa del Master dentro lo stesso progetto, con le sue revisioni, la sua
presa in carico, le sue richieste di merge e i suoi commenti. Il Master normale si chiama <b>principale</b> e non cambia
finché non unisci un branch. Si sceglie dal menu <b>BRANCH</b> nella barra laterale; un banner colorato indica dove stai lavorando.</p>
{table(["Tipo", "Chi lo crea", "Come si lavora"], [
    ["Personale", role_pill("Developer") + " " + role_pill("Lead") + " " + role_pill("Admin"),
     "Scrive solo il proprietario (o un Admin) con " + ui("Salva su branch...") + ". Massimo 5 aperti per utente e progetto. Le richieste di aggiornamento hanno il solo tuo consenso, automatico se non ci sono conflitti."],
    ["Di team", role_pill("Lead") + " " + role_pill("Admin"),
     "Come il principale: richieste di merge e consenso degli altri sviluppatori."],
], [18, 30, 52])}
{bullets(["Un branch nasce dalla <b>testa del principale</b> (o da una revisione conservata a tua scelta): la sua rev 1 è una copia di quella revisione.",
          ui("Aggiorna dal principale") + ": porta nel branch le novità del principale con un merge a tre vie; se ci sono conflitti li risolvi come al solito.",
          ui("Unisci nel principale...") + ": crea una richiesta di merge nel principale, con il consenso normale. A merge avvenuto il branch diventa «unito» e resta in sola lettura.",
          "<b>Chiudi</b> rende il branch di sola lettura; <b>Elimina</b> lo rimuove con tutti i suoi file (proprietario, Admin, e i Lead per i branch di team).",
          "Nell'elenco vedi quante revisioni il branch ha <b>dietro</b> (novità del principale) e <b>avanti</b>.",
          "Sul tuo PC ogni branch ha la sua cartella " + mono("&lt;cartella locale&gt;/branches/&lt;nome&gt;/") + " con la propria " + mono(".aps") + "."])}
{warn("Se il branch cambia dopo che hai chiesto l'unione, la richiesta viene annullata: rifalla. I file binari si salvano e si versionano nei branch personali ma non si possono unire con il merge.")}
<p>Le revisioni del principale da cui partono i branch aperti non vengono eliminate dal limite di revisioni
(vedi {link("retention", "Revisioni conservate e pulizia")}). Le operazioni sono nella {link("audit", "cronologia eventi")}.</p>
"""


def _t_server() -> str:
    return f"""
<h1>Server</h1>
{who(only=["Admin"], note="— sezione per chi gestisce il PC su cui gira il server")}
<h2>L'applicazione grafica del server</h2>
<p>{APP_NAME} (file {mono("AutomationProjectServer.exe")}, oppure {mono("python run_server.py")}) ha una propria finestra con
<b>Stato del server</b> (Avvia / Arresta / Riavvia, indirizzo da dare ai client, utenti attivi, client collegati),
<b>Utenti &amp; Progetti</b>, <b>Cronologia eventi</b> e <b>Impostazioni</b>. Al <b>primo avvio</b> scegli la cartella dei dati,
crei l'amministratore e imposti la porta (default 8750).</p>
<h2>Cartella dei dati</h2>
<p>Dentro la cartella scelta il server crea la sottocartella {mono("AutomationProjectServer")} con database, utenti, progetti,
revisioni e audit. Con {ui('Cambia cartella...')} nelle Impostazioni il server si ferma, copia (a scelta) i dati nella nuova
posizione, li riapre e riparte; la vecchia cartella non viene cancellata.</p>
<h2>Backup dei dati</h2>
<p>Nelle <b>Impostazioni</b>, {ui('Crea backup...')} produce un archivio .zip con database, utenti, progetti, revisioni e audit; si
può fare anche a server acceso. L'archivio contiene anche il <b>segreto di firma dei token</b>: conservalo in un posto protetto.
Per ripristinarlo, ferma il server e scompatta l'archivio nella cartella dei dati.</p>
<h2>Servizio Windows</h2>
<p>Come servizio il server parte da solo con il PC, anche senza nessun utente collegato, e Windows lo riavvia se si ferma per
un errore. Si installa con {ui('Installa come servizio...')} nelle Impostazioni, oppure con {mono("install_service.bat")}: la
cartella dei dati deve essere un <b>percorso locale</b> (non di rete).</p>
<h2>Ricerca automatica e firewall</h2>
<p>Il server risponde ai client che lo cercano su <b>UDP 8751</b> e accetta le connessioni su <b>TCP 8750</b>: entrambe devono
essere aperte nel firewall del PC del server ({mono("install_service.bat")} lo fa per te). Il nome mostrato nell'elenco dei client si
imposta nelle Impostazioni (di default è il nome del PC). Un server in ascolto solo su {mono("127.0.0.1")} non si annuncia in rete.</p>
<p>Il traffico può essere cifrato con <b>HTTPS</b> integrato (vedi {link("https", "Connessione cifrata (HTTPS)")}). Senza HTTPS il traffico, password comprese, <b>non è cifrato</b>: in quel caso usa il programma solo su reti fidate.</p>
"""


def _t_problemi() -> str:
    return f"""
<h1>Risoluzione dei problemi</h1>
{who("Viewer")}
<h2>Non trovo il server</h2>
{bullets(["La ricerca automatica funziona solo nella <b>stessa rete locale</b>: con una VPN o tra reti diverse scrivi l'indirizzo a mano (" + mono("nomepc:8750") + ", con " + mono("https://") + " se il server è cifrato).",
          "Sul PC del server devono essere aperte nel firewall la <b>UDP 8751</b> (ricerca) e la <b>TCP 8750</b> (connessione).",
          "Verifica che il server sia acceso: nella sua applicazione la pagina Stato deve indicare «Server in esecuzione».",
          "Premi <b>&#8635;</b> per ripetere la ricerca; il pallino sotto il campo SERVER diventa verde quando il server risponde."])}
<h2>401 / «sessione scaduta»</h2>
<p>Il programma rinnova da solo la sessione finché lavori. Se compare «sessione scaduta» (errore 401), la sessione è terminata:
è rimasta inattiva troppo a lungo, un Admin l'ha revocata, il tuo account è stato disattivato o il server è stato reinstallato.
Torni alla schermata di accesso: rifai il login. I file locali non vengono toccati.</p>
<h2>«La revisione base non è più conservata»</h2>
<p>Il server tiene solo le ultime N revisioni (vedi {link("retention", "Revisioni conservate e pulizia")}): se sei partito da
una revisione più vecchia, il merge non è più calcolabile. Soluzione: {ui('Checkout ultimo Master')}, rifai le tue modifiche
sul nuovo file e invia di nuovo la richiesta. Il tuo vecchio file non viene cancellato.</p>
<h2>Il file non si può unire</h2>
{bullets(["«<b>Non sembra un file L5X</b>» (o L5K): il contenuto non è del tipo del Master. Usa il file esportato dal software giusto.",
          "<b>Tipo diverso dal Master</b>: un L5X in un progetto L5K (o viceversa) viene rifiutato; per L5X e L5K deve essere lo stesso controller.",
          "<b>Identico al Master</b>: non ci sono modifiche da unire.",
          "<b>File binario</b>: non ha merge, solo versionamento: vedi " + link("tipi-file", "Tipi di file supportati") + ".",
          "Non vedi il tuo file nell'elenco: salvalo nella cartella locale e premi " + ui("Aggiorna") + ", spunta " + ui("Mostra tutti i file") + " oppure usa " + ui("Altro file...") + "."])}
<h2>Non posso dare il consenso</h2>
{bullets(["Sei <b>Viewer</b> sul progetto: i Viewer non votano.",
          "Non eri connesso quando la richiesta è stata inviata, quindi non sei tra i partecipanti richiesti (un Lead può comunque dare il Consenso Finale).",
          "Ci sono ancora <b>conflitti da risolvere</b>: vanno risolti prima del consenso."])}
<h2>Antivirus e firewall</h2>
<p>L'eseguibile a file singolo può essere segnalato da alcuni antivirus o da SmartScreen: aggiungi un'eccezione, o fai firmare
il programma. Al primo avvio del server Windows può chiedere di autorizzare l'accesso alla rete: consentilo per le reti
private.</p>
"""


def _t_scorciatoie() -> str:
    return f"""
<h1>Scorciatoie da tastiera e Informazioni</h1>
{who("Viewer")}
<h2>Scorciatoie</h2>
{table(["Tasti", "Che cosa fa", "Dove"], [
    [kbd("F1"), "Apre la Guida", "ovunque nel client, dopo l'accesso"],
    [kbd("Ctrl") + "+" + kbd("F"), "Porta il cursore nella ricerca", "Guida"],
    [kbd("Invio"), "Passa dal campo Utente alla Password, poi accede", "schermata di accesso"],
    [kbd("Esc"), "Chiude la finestra di dialogo aperta", "finestre di dialogo"],
    ["doppio clic", "Seleziona un file e invia la richiesta di merge", "finestra " + ui("Richiedi Merge...")],
], [18, 52, 30])}
<h2>Informazioni</h2>
{table(["", ""], [
    ["<b>Applicazione</b>", f"{APP_NAME} &middot; client {CLIENT_NAME}"],
    ["<b>Versione</b>", APP_VERSION],
    ["<b>Uso</b>", "Collaborazione multi-utente offline sui progetti PLC con merge a tre vie, consenso, ruoli e audit immutabile."],
], [24, 76])}
"""


GROUPS = ("Per iniziare", "Lavorare sul progetto", "Progetti e revisioni", "Controllo e sistema", "Aiuto")


def build_topics() -> list[HelpTopic]:
    g0, g1, g2, g3, g4 = GROUPS
    return [
        HelpTopic("intro", "Introduzione e concetti", g0, _t_intro(), "master revisione richiesta merge consenso conflitto base"),
        HelpTopic("primi-passi", "Primi passi", g0, _t_primi_passi(), "connessione login accesso password server ricerca udp"),
        HelpTopic("ruoli", "Ruoli e permessi", g0, _t_ruoli(), "admin lead developer viewer rbac permessi"),
        HelpTopic("cartella-locale", "Cartella locale e file di lavoro", g1, _t_cartella(), "aps revN nome file sovrascrivere"),
        HelpTopic("checkout", "Checkout", g1, _t_checkout(), "scaricare master base"),
        HelpTopic("richiedi-merge", "Lavorare e richiedere un merge", g1, _t_merge(), "invia file anteprima messaggio"),
        HelpTopic("consenso", "Il consenso", g1, _t_consenso(), "quorum lead approva rifiuta commenti forza merge"),
        HelpTopic("conflitti", "Conflitti e come risolverli", g1, _t_conflitti(), "tieni mio master entrambi personalizzato"),
        HelpTopic("tipi-file", "Tipi di file supportati", g1, _t_tipi(), "l5x l5k testo binario formato"),
        HelpTopic("plcopen", "PLCopen XML (CODESYS)", g1, _t_plcopen(), "codesys iec 61131 pou xml strutturale st il"),
        HelpTopic("presa-in-carico", "Presa in carico (blocco)", g1, _t_presa(), "lock blocco esclusivo check-in binario"),
        HelpTopic("discussione", "Discussione sulle richieste", g1, _t_discussione(), "commenti menzioni chat"),
        HelpTopic("reimport-auto", "Reimport automatico dopo il merge", g1, _t_reimport_auto(), "notifica nuova copia"),
        HelpTopic("progetti", "Progetti e file base", g2, _t_progetti(), "nuovo progetto importa reimporta"),
        HelpTopic("storico", "Storico, scarico e rollback", g2, _t_storico(), "ripristina revisione tornare indietro"),
        HelpTopic("confronto", "Confronto, etichette e blocco revisioni", g2, _t_confronto(), "confronta diff etichetta bloccata protetta"),
        HelpTopic("branch", "Branch", g2, _t_branch(), "branch ramo personale team sync unisci aggiorna principale"),
        HelpTopic("retention", "Revisioni conservate e pulizia", g2, _t_retention(), "limite eliminata pulizia vecchie copie"),
        HelpTopic("elimina-progetto", "Eliminare un progetto", g2, _t_elimina(), "cancellare rimuovere archivia"),
        HelpTopic("esporta", "Esportare un progetto (.zip)", g2, _t_esporta(), "zip archivio backup progetto"),
        HelpTopic("https", "Connessione cifrata (HTTPS)", g3, _t_https(), "tls certificato autofirmato impronta wss"),
        HelpTopic("audit", "Cronologia eventi e audit", g3, _t_audit(), "hash catena integrita csv registro"),
        HelpTopic("notifiche", "Notifiche e stato", g3, _t_notifiche(), "led tray connessione avvisi"),
        HelpTopic("server", "Server (solo per Admin)", g3, _t_server(), "servizio windows backup udp 8751 firewall cartella dati"),
        HelpTopic("problemi", "Risoluzione dei problemi", g4, _t_problemi(), "errore 401 sessione scaduta antivirus non trovo"),
        HelpTopic("scorciatoie", "Scorciatoie da tastiera e Informazioni", g4, _t_scorciatoie(), "f1 tasti versione info"),
    ]


TOPICS: list[HelpTopic] = build_topics()


# ======================================================================================= ricerca (pura)
def norm(text: str) -> str:
    """Minuscolo senza accenti, per confronti tolleranti («e'» / «è»)."""
    t = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def query_terms(query: str) -> list[str]:
    return [w for w in re.split(r"\s+", norm(query).strip()) if w]


def search_topics(query: str, topics: Optional[list[HelpTopic]] = None) -> list[HelpTopic]:
    """Argomenti che contengono *tutte* le parole cercate (titolo, parole chiave o testo); prima i titoli."""
    topics = TOPICS if topics is None else topics
    terms = query_terms(query)
    if not terms:
        return list(topics)
    scored: list[tuple[int, int, HelpTopic]] = []
    for i, t in enumerate(topics):
        title, body = norm(t.title), norm(t.text + " " + t.keywords)
        if all(w in title or w in body for w in terms):
            score = sum(2 if w in title else 1 for w in terms)
            scored.append((-score, i, t))
    return [t for _s, _i, t in sorted(scored, key=lambda x: (x[0], x[1]))]


def topic_by_id(tid: str, topics: Optional[list[HelpTopic]] = None) -> Optional[HelpTopic]:
    return next((t for t in (TOPICS if topics is None else topics) if t.id == tid), None)


# ======================================================================================= stile del documento
def document_css() -> str:
    return f"""
body {{ color:{T['TEXT']}; font-family:{FONT_UI}; font-size:13px; }}
h1 {{ color:{T['TEXT']}; font-size:26px; font-weight:700; margin-top:4px; margin-bottom:8px; }}
h2 {{ color:{T['ACCENT']}; font-size:16px; font-weight:700; margin-top:22px; margin-bottom:6px; }}
h3 {{ color:{T['TEXT']}; font-size:14px; font-weight:700; margin-top:14px; margin-bottom:4px; }}
p {{ margin-top:4px; margin-bottom:8px; line-height:150%; }}
p.lead {{ font-size:14px; color:{T['TEXT_DIM']}; line-height:155%; }}
li {{ line-height:150%; }}
ul, ol {{ margin-top:4px; margin-bottom:8px; }}
a {{ color:{T['ACCENT']}; text-decoration:none; }}
b {{ color:{T['TEXT']}; font-weight:700; }}
i {{ color:{T['TEXT_DIM']}; }}
"""


# ======================================================================================= vista
class HelpView(QWidget):
    """Pagina «Guida»: ricerca e argomenti a sinistra, contenuto a destra, link interni tra argomenti."""

    topic_changed = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None, topics: Optional[list[HelpTopic]] = None):
        super().__init__(parent)
        self.topics: list[HelpTopic] = list(TOPICS if topics is None else topics)
        self._by_id = {t.id: t for t in self.topics}
        self._terms: list[str] = []
        self._current: Optional[str] = None

        # ---- colonna sinistra: ricerca + elenco
        left = QFrame()
        left.setObjectName("Panel")
        left.setMinimumWidth(250)
        left.setMaximumWidth(320)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 14, 0, 8)
        ll.setSpacing(8)
        head = QVBoxLayout()
        head.setContentsMargins(14, 0, 14, 0)
        head.setSpacing(8)
        head.addWidget(eyebrow("Argomenti"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Cerca nella guida...")
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icon("search", T["MUTED"], 16), QLineEdit.LeadingPosition)
        head.addWidget(self.search)
        self.lbl_count = QLabel("")
        self.lbl_count.setProperty("caption", True)
        head.addWidget(self.lbl_count)
        ll.addLayout(head)
        self.topic_list = QListWidget()
        self.topic_list.setObjectName("TopicList")
        self.topic_list.setFocusPolicy(Qt.StrongFocus)
        self.topic_list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.topic_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.topic_list.setTextElideMode(Qt.ElideRight)
        self.topic_list.setWordWrap(False)
        ll.addWidget(self.topic_list, 1)

        # ---- colonna destra: contenuto
        self.browser = QTextBrowser()
        self.browser.setObjectName("HelpBrowser")
        self.browser.setOpenLinks(False)
        self.browser.setOpenExternalLinks(False)
        self.browser.document().setDefaultStyleSheet(document_css())
        self.browser.document().setDocumentMargin(26)
        f = QFont()
        f.setFamilies([n.strip().strip('"') for n in FONT_UI.split(",")])
        f.setPixelSize(13)
        self.browser.setFont(f)
        self.empty = EmptyState("search", "Nessun argomento trovato",
                                "Prova con altre parole: la ricerca guarda titoli e testi della guida.")
        self.empty.hide()
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.browser, 1)
        rl.addWidget(self.empty, 1)

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 24)
        root.setSpacing(12)
        root.addWidget(heading("Guida"))
        root.addWidget(subtitle("Come lavorare con il server di progetto: concetti, ruoli, flusso di merge e consenso. "
                                "Premi F1 in qualsiasi momento per tornare qui."))
        body = QHBoxLayout()
        body.setSpacing(16)
        body.addWidget(left)
        body.addWidget(right, 1)
        root.addLayout(body, 1)

        self.search.textChanged.connect(self.set_filter)
        self.topic_list.currentItemChanged.connect(self._item_changed)
        self.browser.anchorClicked.connect(self._anchor)
        self._find_sc = QShortcut(QKeySequence(QKeySequence.Find), self)
        self._find_sc.activated.connect(self.focus_search)
        self._fill_list(self.topics)
        if self.topics:
            self.show_topic(self.topics[0].id)

    # ------------------------------------------------------------------ API
    def topic_ids(self) -> list[str]:
        """Argomenti attualmente elencati (dopo l'eventuale filtro)."""
        return [self.topic_list.item(r).data(Qt.UserRole) for r in range(self.topic_list.count())
                if self.topic_list.item(r).data(Qt.UserRole)]

    def current_topic(self) -> Optional[str]:
        return self._current

    def focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    def show_topic(self, tid: str) -> bool:
        """Apre un argomento (se e' filtrato via, azzera la ricerca)."""
        t = self._by_id.get(tid)
        if t is None:
            return False
        if tid not in self.topic_ids():
            self.search.clear()
        if not self._select_row(tid):
            return False
        self._render(t)
        return True

    def set_filter(self, query: str) -> None:
        """Filtra l'elenco con la ricerca, seleziona il primo risultato ed evidenzia le parole nel testo."""
        self._terms = query_terms(query)
        found = search_topics(query, self.topics)
        self._fill_list(found, flat=bool(self._terms))
        n = len(found)
        self.lbl_count.setText("" if not self._terms else (f"{n} argomenti trovati" if n != 1 else "1 argomento trovato"))
        self.empty.setVisible(n == 0)
        self.browser.setVisible(n > 0)
        if n:
            keep = self._current if self._current in {t.id for t in found} else found[0].id
            self._select_row(keep)
            self._render(self._by_id[keep])

    # ------------------------------------------------------------------ interno
    def _select_row(self, tid: str) -> bool:
        self.topic_list.blockSignals(True)
        try:
            for r in range(self.topic_list.count()):
                if self.topic_list.item(r).data(Qt.UserRole) == tid:
                    self.topic_list.setCurrentRow(r)
                    return True
            return False
        finally:
            self.topic_list.blockSignals(False)

    def _fill_list(self, topics: list[HelpTopic], flat: bool = False) -> None:
        self.topic_list.blockSignals(True)
        self.topic_list.clear()
        last = None
        for t in topics:
            if not flat and t.group != last:
                last = t.group
                head = QListWidgetItem(t.group.upper())
                head.setFlags(Qt.NoItemFlags)
                head.setSizeHint(QSize(0, 22 if last == self.topics[0].group else 38))
                head.setTextAlignment(Qt.AlignLeft | Qt.AlignBottom)
                hf = head.font()
                hf.setPixelSize(10)
                hf.setBold(True)
                hf.setLetterSpacing(QFont.AbsoluteSpacing, 1.0)
                head.setFont(hf)
                head.setForeground(QColor(T["MUTED"]))
                self.topic_list.addItem(head)
            it = QListWidgetItem(t.title)
            it.setData(Qt.UserRole, t.id)
            self.topic_list.addItem(it)
        self.topic_list.blockSignals(False)

    def _item_changed(self, cur: Optional[QListWidgetItem], _prev=None) -> None:     # noqa: ANN001
        tid = cur.data(Qt.UserRole) if cur else None
        if tid and tid != self._current:
            self._render(self._by_id[tid])

    def _render(self, t: HelpTopic) -> None:
        self._current = t.id
        self.browser.setHtml(t.html)
        self._highlight()
        self.browser.verticalScrollBar().setValue(0)
        self.topic_changed.emit(t.id)

    def _highlight(self) -> None:
        """Evidenzia nel testo tutte le occorrenze delle parole cercate e porta la vista sulla prima."""
        if not self._terms:
            self.browser.setExtraSelections([])
            return
        doc: QTextDocument = self.browser.document()
        sels: list[QTextEdit.ExtraSelection] = []
        first: Optional[QTextCursor] = None
        for term in self._terms:
            cur = QTextCursor(doc)
            while True:
                cur = doc.find(term, cur)
                if cur.isNull():
                    break
                sel = QTextEdit.ExtraSelection()
                sel.cursor = cur
                fmt = QTextCharFormat()
                fmt.setBackground(QColor(T["CHG_STRONG"]))
                fmt.setForeground(QColor("#FFFFFF"))
                sel.format = fmt
                sels.append(sel)
                if first is None or cur.position() < first.position():
                    first = QTextCursor(cur)
        self.browser.setExtraSelections(sels)
        if first is not None:
            first.clearSelection()
            self.browser.setTextCursor(first)
            self.browser.ensureCursorVisible()

    def _anchor(self, url: QUrl) -> None:
        s = url.toString()
        if s.startswith("topic:"):
            self.show_topic(s.split(":", 1)[1])
