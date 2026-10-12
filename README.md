# Automation Project Server

Collaborazione multi-utente **offline** sui progetti PLC (oggi: file `.L5X` di Studio 5000; il nome è generico per
poter aggiungere altri ambienti, es. CODESYS), con **3-way merge strutturale**, **consenso
esplicito** prima di toccare il Master, **utenti e ruoli** (modello Siemens Project Server) e **audit trail
immutabile**. Client desktop PySide6 con tema scuro industriale.

```
 Client A (PySide6) ─┐                               ┌─ Client B (PySide6)
  login JWT, checkout │   REST (HTTP) + WebSocket    │   notifica + schermata di consenso
                      ▼                               ▼
         ┌─────────────── Automation Project Server (FastAPI) ───────────────┐
         │  auth/        JWT + bcrypt, sessioni, RBAC (@require_role)      │
         │  db/          SQLAlchemy: utenti, ruoli, progetti, permessi,    │
         │               audit log a catena di hash                        │
         │  projects     un Master + un SessionManager per progetto        │
         │  session_manager  proposte, quorum, consenso (digest), Admin    │
         │  merger       3-way merge lxml (Base / Proposta / Master)       │
         └──────────────────────────────────────────────────────────────────┘
```

## Struttura

```
common/roles.py            ruoli (Admin > Lead_Engineer > Developer > Viewer)
common/schemas.py          contratto REST/WS condiviso (pydantic)
server/config.py           impostazioni (variabili APS_*)
server/db/                 base.py (engine, WAL)  models.py (User, RoleRow, Project, ProjectPermission,
                           AuthSession, AuditLog)  audit.py (catena di hash)
server/auth/               security.py (bcrypt, JWT)  models.py (Principal)  rbac.py (require_role, ...)
                           service.py (login, refresh, utenti)  routes.py (/api/auth, /api/users, /api/roles)
server/projects.py         registry dei progetti + /api/projects e permessi
server/merger.py           engine 3-way merge (lxml, diff3 sui Rung)
server/session_manager.py  stati, consenso, Consenso Finale del Lead, Forza Merge, Ripristino
server/storage.py          Master + storico revisioni (scrittura atomica)
server/main.py             API FastAPI + WebSocket      server/__main__.py  avvio CLI
server/gui/                app grafica del server: controller.py (avvio/arresto, cartella dati, console in-process)
                           main_window.py (stato, utenti, cronologia, impostazioni)  setup_dialog.py (primo avvio)
client/theme/              dark.qss (QSS con variabili @TOKEN@)  tokens.py (palette)  widgets.py (LED, badge, avatar, icone)
client/views/              login_view  sidebar  dashboard_view  diff_viewer  audit_view  user_mgmt_view  merge_dialog
common/discovery.py        protocollo di scoperta (UDP)   server/discovery.py  risponditore   client/discovery.py  ricerca
server/service.py          servizio Windows (pywin32): install/start/stop/remove/run   server/service_ctl.py  stato e UAC senza pywin32
install_service.bat / uninstall_service.bat   installano/rimuovono il servizio (chiedono i privilegi di amministratore)
client/localfiles.py       cartella locale del progetto + ricerca dei file candidati al merge (qualsiasi tipo)
common/filekinds.py        tipi di file del Master (L5X, L5K, testo, binario) e filtri 'Tutti i file'
server/textmerge.py        merge testuale per riga (L5K e altri file di testo) + dispatcher per tipo
tools/                     make_icon.py (rigenera app.ico), check_exe_icon.py (verifica icona negli exe)
client/gui_approval.py     schermata Consenso + Diff + Conflitti      client/gui_main.py  finestra principale
run_server.py / run_client.py   entry point per PyInstaller (run_server.py = exe unico del server: GUI, servizio, CLI)
server/launcher.py         smista gli argomenti dell'exe unico: GUI | install/start/stop/... | serve | --run-service    build_server.bat / build_client.bat   creano gli exe
client/api_client.py       client HTTP con refresh automatico del token   client/ws_listener.py  push
tests/                     168 test (merger, tipi di file, nomi file, reimport, consenso, REST+WS, RBAC, audit, GUI offscreen su server reale)
samples/                   L5X di esempio: base, DevA, DevB (con e senza conflitti)
```

## Eseguibili (Windows)

Il logo dell'applicazione (`client/theme/assets/logo.png`, icona `app.ico`) e' usato da server e client: login, barra laterale,
finestre, area di notifica e icona degli exe.

Le icone degli `.exe` (`app.ico`) sono generate da `tools/make_icon.py` in formato BMP a 32 bit per le dimensioni da 16 a
128 px e PNG per 256 px (il formato che Windows legge meglio nelle risorse degli exe). Gli script di build la rigenerano
ogni volta e, a fine build, `tools/check_exe_icon.py` verifica che sia incorporata (`Icona incorporata: OK`).
Se l'icona nuova non compare in Esplora risorse nonostante l'OK, e' la cache delle icone di Windows: esegui
`ie4uinit.exe -show` (lo fa gia' lo script) oppure riavvia Esplora risorse. Gli `.bat` sono salvati con fine riga CRLF
(`.gitattributes`): con LF puro `cmd.exe` puo' interpretare male label e `goto`.


Per distribuire senza installare Python sui PC:

```
build_server.bat   ->  dist\AutomationProjectServer.exe   (UN SOLO exe del server: GUI + servizio Windows + riga di comando)
build_client.bat   ->  dist\AutomationProjectClient.exe   (applicazione grafica del client)
```

Ogni script crea un ambiente isolato (`.venv-build-server` / `.venv-build-client`), installa le dipendenze e lancia
PyInstaller (file singolo, con l'icona `client/theme/assets/app.ico`). Serve Python 3.10+ nel PATH e internet per `pip`.

`AutomationProjectServer.exe` si comporta cosi':

| Come lo lanci | Cosa fa |
|---|---|
| doppio clic / senza argomenti | applicazione grafica |
| `install --data-dir D:\APS [--port 8750] [--name "Linea 1"]`, `start`, `stop`, `restart`, `status`, `remove` | servizio Windows (prompt come amministratore) |
| `run --data-dir D:\APS` | server in primo piano senza installare (prova) |
| `serve [--data-dir D:\APS] [--port 8750] [--seed-project "Linea 1" --master Master.L5K]` | server da riga di comando, senza interfaccia |

L'exe e' "windowed" (nessuna finestra nera al doppio clic): da terminale scrive nella console da cui lo lanci, ma `cmd` non
ne attende la fine, quindi usa `start /wait "" AutomationProjectServer.exe status`. Senza console (UAC, servizio) l'esito va
in `%TEMP%\AutomationProjectServer-last-command.log`.
Gli exe vanno compilati su Windows (PyInstaller non fa cross-compilazione).

## Applicazione grafica del server

`AutomationProjectServer.exe` (oppure `python run_server.py`) e' l'equivalente dell'applicazione di amministrazione
di Siemens Project Server: lavori **direttamente sul server**, senza collegarti da un client.

* **Primo avvio**: scegli la **cartella dei dati**, crea l'amministratore (utente e password) e imposta la porta. Il server
  crea dentro la cartella scelta la sottocartella `AutomationProjectServer` con database, utenti, progetti, revisioni e audit
  trail. Se la cartella contiene gia' dati di un'installazione precedente vengono riutilizzati.
* **Stato del server**: Avvia / Arresta / Riavvia, indirizzo da inserire nei client (con "Copia"), cartella dei dati,
  utenti attivi, progetti, client collegati, attivita' recente e registro.
* **Utenti & Progetti**: crea, modifica, disattiva utenti e reimposta le password; assegna i permessi per progetto;
  crea progetti e inizializza il Master da un .L5X locale; sessioni e ruoli. La gestione funziona anche a server fermo.
  La console non chiede credenziali (l'accesso al computer del server equivale a quello dell'amministratore) ed e'
  registrata nell'audit come "Console server".
* **Cronologia eventi**: audit trail completo con filtri, esportazione CSV e verifica d'integrita'.
* **Impostazioni**: *Cambia cartella...* ferma il server, copia (a scelta) i dati nella nuova posizione, li riapre e
  riavvia; la vecchia cartella non viene cancellata. Qui si cambiano anche interfaccia di rete, porta e avvio automatico.
  Le preferenze sono salvate per utente di Windows (registro di sistema).

## Scoperta automatica del server

Il client **trova da solo i server** nella rete locale: all'apertura del login li cerca (pulsante ↻ per ripetere la
ricerca). Se ne trova **uno** lo seleziona; se ne trova **piu' di uno** mostra l'elenco (nome + indirizzo) e riseleziona
l'ultimo usato. Se non ne trova resta possibile scrivere l'indirizzo a mano (`http://server:8750`).

Come funziona: il server risponde su **UDP 8751** a un pacchetto broadcast del client (nessuna dipendenza esterna) e
comunica nome e porta HTTP. Il nome mostrato si imposta nelle Impostazioni dell'app grafica (default: nome del PC; da riga di
comando `--name`, oppure `APS_SERVER_NAME`; `APS_DISCOVERY_PORT=0` la disattiva). Funziona solo **nella stessa rete locale**
(il broadcast non attraversa router/VPN: li' serve l'indirizzo manuale) e se nel firewall del server e' aperta la UDP 8751
(oltre alla TCP 8750). Un server in ascolto solo su `127.0.0.1` non si annuncia.

## Servizio Windows (avvio automatico con il PC)

Come servizio il server **parte da solo all'accensione** (anche senza nessun utente collegato) e Windows lo riavvia se si
ferma per un errore (dopo 5 s, 5 s, 30 s). Tre modi:

* `install_service.bat` sul PC del server (accanto a `AutomationProjectServer.exe`): chiede i privilegi di
  amministratore, la cartella dati, la porta e il nome; crea l'amministratore se la cartella e' nuova (**password mostrata una
  sola volta**), registra il servizio ad avvio automatico, apre nel firewall TCP 8750 e UDP 8751 e lo avvia.
  `uninstall_service.bat` lo rimuove.
* Dalle **Impostazioni** dell'app grafica: *Installa come servizio...* (usa la cartella dati e la porta correnti; Windows
  chiede l'autorizzazione; se l'operazione non riesce l'app mostra l'esito). E' lo stesso exe: non serve altro.
* Da prompt come amministratore: `start /wait "" AutomationProjectServer.exe install --data-dir D:\APS --port 8750 --name "Linea 1"`,
  poi `start`, `stop`, `restart`, `status`, `remove`; `run` lo esegue in primo piano per provarlo senza installare.

Dettagli: gira come LocalSystem, quindi la cartella dati deve essere un **percorso locale** (non UNC / unita' di rete
mappata). Configurazione in `%ProgramData%\AutomationProjectServer\service.json`; registro in `<cartella dati>\logs\server.log`.
`remove` toglie servizio e regole firewall ma **non i dati**. Mentre il servizio e' attivo l'app grafica resta utilizzabile per
utenti, progetti e cronologia: le sue operazioni passano dal servizio (stessi dati e stesso segreto dei token), la sua pagina
Stato mostra "in esecuzione come servizio" e i pulsanti Avvia/Arresta si usano dalle Impostazioni. Non avviare il server
dell'app grafica e il servizio insieme sulla stessa porta.

## Cartella locale del client

Nella schermata principale del client la riga **Cartella locale** indica dove il progetto e' salvato sul tuo PC; con
*Cambia cartella...* la scegli tu, per ogni progetto (e per ogni server/utente). Al **primo Checkout** il client te la
chiede. I file portano **il nome del file con cui e' stato importato il Master** e, al massimo, il numero di revisione in
fondo: importando `Linea1.L5K` il Checkout salva `Linea1_rev3.L5K` (e' il file su cui lavori), piu' una copia pulita nella
cartella nascosta `.aps` (serve a capire cosa hai modificato). Se rifai il Checkout della stessa revisione e il file ha tue
modifiche, **non viene sovrascritto**. **Richiedi Merge...** elenca i file con la stessa estensione trovati in quella
cartella (e sottocartelle, fino a 2 livelli), non propone le copie del Master scaricate e mai toccate, ordina per data e
preseleziona il piu' recente tra quelli modificati dopo il checkout; per ciascun file indica se e' *Modificato*, *Identico al
Master* o *Non sembra un file L5X*. Con *Altro file...* si puo' scegliere un file anche fuori dalla cartella.

### Master di qualsiasi tipo di file (L5K, L5X, altri IDE)

Il Master puo' essere **qualsiasi file**: nelle finestre di selezione (Master iniziale, «Altro file...», salvataggi) c'e' sempre
il filtro **Tutti i file (\*)**, e per Rockwell **L5K** (export ASCII) e **L5X** (export XML). L'estensione del primo file
caricato resta quella del progetto: i file scaricati (`<nome>_revN.L5K`) e quelli
cercati da *Richiedi Merge* usano la stessa estensione e lo stesso nome; con **Mostra tutti i file** si elenca qualsiasi file della cartella.

| Tipo (riconosciuto dal contenuto) | Merge |
|---|---|
| **L5X** (XML Studio 5000) | strutturale: Rung / Line / Tag / UDT / AOI / Program / Routine |
| **L5K** (ASCII Rockwell) | **testuale per riga** (3-way); le modifiche sono etichettate con `Program[X]/Routine[Y]`; controllo che sia lo stesso `CONTROLLER` |
| altro file di testo (`.st`, `.xml`, ...) | testuale per riga (3-way) |
| file binario | nessun merge: il Master e' versionato, scaricabile e ripristinabile, le richieste di merge sono rifiutate |

Il tipo del file inviato deve coincidere con quello del Master (un L5X in un progetto L5K viene rifiutato).

## Progetti e file base dal client

Anche dal client (non solo dal server) si gestiscono i progetti e il file base (Master):

* **Nuovo progetto** (pulsante **+** accanto all'elenco progetti, solo **Admin**: e' il ruolo che il server richiede per
  creare progetti). Subito dopo il client propone di importare il file base.
* **Importa file base...** (sulla schermata principale, per **Lead** e **Admin**, finche' il Master non e' inizializzato).
  Il selettore file mostra *Tutti i file* in testa; Rockwell L5K / L5X a portata di mano.
* **Reimporta file base...** (Lead e Admin, su un progetto gia' in lavorazione): carica un nuovo file base come **nuova
  revisione** (la cronologia resta intatta, si puo' sempre tornare indietro dallo Storico). Chiede un motivo (minimo 5
  caratteri) e finisce nella cronologia eventi (`master_reimported`). Deve essere dello stesso tipo del Master e, per
  L5X/L5K, dello stesso controller, e diverso dal Master corrente. Le richieste di merge in sospeso vengono rianalizzate e i
  consensi gia' dati si azzerano; il nome del file importato diventa il nuovo nome dei file distribuiti ai client.
  Attenzione: il reimport **non passa dal consenso** degli altri sviluppatori, per questo e' riservato a Lead e Admin e
  tracciato nell'audit.
* **Dal server**: stessa funzione nell'interfaccia del server, pagina **Utenti & Progetti**, tab *Permessi di progetto*: scegli il progetto e premi **Reimporta file base...** (visibile se il Master e' gia' inizializzato; altrimenti c'e' *Carica Master iniziale*). Stessi controlli, stesso motivo obbligatorio e stesso evento `master_reimported`; i client collegati ricevono l'avviso di nuovo Master. Funziona anche con il server installato come servizio.

### Reimport automatico dopo il tuo merge

Quando una tua richiesta di merge viene completata, il client controlla cosa ha trovato il server sul Master rispetto alla
revisione da cui eri partito. **Solo se ci sono modifiche di altri che tu non avevi** (non contano quelle identiche alle tue)
scarica da solo il nuovo Master come tua nuova copia di lavoro (`<nome>_revN.<ext>`, nuova revisione base) e te lo
**segnala**: riga nel registro, messaggio in barra di stato/area di notifica e una finestra non bloccante con il numero di
modifiche altrui trovate e le unita' toccate. Il file precedente resta invariato su disco; un file con modifiche non
unite non viene mai sovrascritto. Se il merge non porta modifiche altrui non succede nulla.

## Avvio da sorgente (Windows / PowerShell)

```powershell
python -m venv .venv ; .\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt

# Server con interfaccia grafica
python run_server.py

# Oppure server da riga di comando (come l'exe con `serve`): crea il progetto e inizializza il Master dalla revisione iniziale
python -m server --seed-project "Linea 1" --master samples\Master_rev1_base.L5X --port 8750
```

Con `serve` (riga di comando), al **primo avvio** viene creato l'utente `admin` con una password generata, **stampata una sola volta** su stderr
(oppure impostala tu con `APS_ADMIN_PASSWORD`); al primo accesso il client chiede di cambiarla. Nell'app grafica la scelgi tu nella configurazione iniziale.

```powershell
# Client (su ogni PC): login -> l'Admin crea gli utenti e assegna i permessi dal pannello "Utenti & Permessi"
python -m client
```

| Variabile | Default | Significato |
|---|---|---|
| `APS_DATA_DIR` | `./data` | database SQLite, `jwt_secret`, Master e revisioni per progetto |
| `APS_DATABASE_URL` | SQLite in `APS_DATA_DIR` | altro database SQLAlchemy (es. PostgreSQL) |
| `APS_JWT_SECRET` | generato e salvato | chiave di firma dei token |
| `APS_ADMIN_USER` / `APS_ADMIN_PASSWORD` | `admin` / generata | amministratore iniziale |
| `APS_ACCESS_TTL_MIN` / `APS_REFRESH_TTL_H` | 15 / 12 | durata access token / refresh token |
| `APS_MAX_LOGIN_FAILURES` / `APS_LOCKOUT_SECONDS` | 5 / 300 | blocco temporaneo dopo password errate |
| `APS_SESSION_TTL` | 90 | secondi senza heartbeat dopo cui un utente non è più "attivo" |
| `APS_HOST` / `APS_PORT` / `APS_MAX_UPLOAD_MB` | `0.0.0.0` / 8750 / 200 | rete e limite upload |

Prova rapida con gli esempi: due Developer e un Lead fanno Checkout; B invia `samples\DevB_con_conflitti.L5X` e viene
approvata da B, A e dal Lead; poi A invia `samples\DevA.L5X` dalla stessa base e risolve i 2 conflitti dalla schermata.
Test: `python -m pytest tests -q` (la GUI gira in modalità `offscreen`, nessun display necessario).

## Ruoli e permessi

| Ruolo | Può |
|---|---|
| **Viewer / Auditor** | scaricare gli L5X, vedere storico revisioni, proposte e cronologia eventi. Nessuna modifica, nessun voto |
| **Developer / Contributor** | tutto quanto sopra + Checkout, inviare Merge Request, dare consenso ai merge degli altri, annullare le proprie richieste |
| **Lead Engineer / Approver** | come Developer + **Consenso Finale** sulle richieste dei Developer (anche senza essere partecipante), rifiutare, inizializzare il Master di un progetto |
| **Admin** | tutto, in ogni progetto: utenti, permessi, progetti, risolvere i conflitti di qualunque richiesta, **Forza Merge**, **Ripristina** una revisione, verifica integrità audit |

* Il ruolo dell'account è il **massimo**; sul progetto si assegna lo stesso ruolo o uno inferiore
  (`ruolo effettivo = min(account, progetto)`). Senza permesso sul progetto non c'è accesso. `Admin` è solo globale.
* Il ruolo è riletto dal database a ogni richiesta: declassare o disattivare un utente ha effetto **immediato**.
* Vincoli di sicurezza: non si può rimuovere l'ultimo Admin né declassare/disattivare se stessi.

## Flusso di consenso

1. Il Developer fa **Checkout** (scarica `<nome del file importato>_revN.<ext>`, la rev N diventa la sua base), modifica offline in Studio 5000.
2. **Richiedi Merge**: il server calcola il 3-way merge (Base = rev del checkout, Proposta = suo file, Master = ultima rev).
3. Hanno diritto di voto l'autore e i Developer/Lead **attivi** (WebSocket o heartbeat entro `APS_SESSION_TTL`); i Viewer no.
   I partecipanti ricevono la notifica e si apre la schermata di consenso.
4. Con conflitti: l'autore (o un Admin) sceglie per ciascuno *mia / Master / entrambe / manuale* e applica le risoluzioni.
5. **Dai Consenso al Merge** / **Rifiuta / Chiedi Modifiche**. Se il progetto ha `require_lead_approval` e l'autore è
   un Developer serve anche il **Consenso Finale** di un Lead/Admin.
6. Ottenuti tutti i consensi, il Master avanza di revisione e tutti scaricano la nuova versione. Ogni consenso è legato al
   *digest del risultato*: se cambia il Master o le risoluzioni, i consensi si azzerano.

Il client non sovrascrive mai la copia di lavoro: dopo un aggiornamento scarica `<nome>_revN.<ext>` nella
cartella del progetto (e la copia pulita in `.aps`) e l'utente rifà il Checkout quando vuole.

## API principali

Autenticazione: `Authorization: Bearer <access_token>`. WebSocket: `/ws?token=<access>&project=<id>`.

| Endpoint | Ruolo minimo |
|---|---|
| `POST /api/auth/login` · `/refresh` · `/logout` · `/change-password`, `GET /api/auth/me` | pubblico / utente |
| `GET /api/auth/sessions[?all=true]`, `DELETE /api/auth/sessions/{id}` | utente (le proprie) / Admin (tutte) |
| `GET /api/roles` | Viewer |
| `GET/POST /api/users`, `PATCH /api/users/{id}` | Admin |
| `GET /api/projects` | Viewer (solo quelli accessibili) |
| `POST /api/projects`, `PATCH /api/projects/{id}`, `GET/PUT /api/projects/{id}/permissions` | Admin |
| `POST /api/projects/{id}/master/init` | Lead |
| `GET /api/projects/{id}/master` · `/master/history` · `/master/download` | Viewer |
| `POST /api/projects/{id}/master/restore` | Admin |
| `POST /api/projects/{id}/master/reimport` (file + motivo) | Lead / Admin |
| `POST /heartbeat`, `GET /developers`, `GET /proposals[/{pid}[/preview]]` | Viewer |
| `POST /proposals` | Developer |
| `POST /proposals/{pid}/resolutions` · `/approve` · `/reject` · `/cancel` | Developer (regole di consenso sopra) |
| `POST /proposals/{pid}/force-merge` | Admin |
| `GET /api/audit` (filtri `project_id`, `action`, `actor`) | Viewer (solo progetti accessibili; l'Admin vede tutto) |
| `GET /api/audit/verify` | Admin |

(Gli ultimi path di progetto sono sotto `/api/projects/{id}/`.) Nel codice: `@require_role(Role.LEAD)` come decoratore,
`min_role(Role.X)` come dipendenza FastAPI, `project_access(Role.X)` per il controllo per progetto.

## Sicurezza e audit

* Password con **bcrypt** (con pre-hash SHA-256), policy ≥ 10 caratteri con lettera e cifra, blocco temporaneo dopo 5 errori.
* **JWT** HS256: access token 15 min, refresh 12 h con **rotazione** e rilevamento del riuso (riuso ⇒ sessione revocata).
  Sessioni elencabili e revocabili singolarmente.
* **Audit trail**: chi ha scaricato (`master_downloaded`), chiesto il merge, dato consenso, approvato/forzato/ripristinato,
  gestito utenti e permessi, con data, progetto e revisione. Ogni riga contiene l'hash della precedente; su SQLite
  `UPDATE`/`DELETE` sono bloccati da trigger e `GET /api/audit/verify` rileva qualunque manomissione.
* Parser XML con risoluzione delle entità disattivata (XXE); upload limitato in dimensione.

## Limiti noti

* **Branch non implementati**: la gestione di branch (Lead) e i branch personali (Developer) citati nelle specifiche non ci
  sono; il modello è Master unico per progetto con richieste di merge. È un'estensione naturale (un `MasterStore` per branch).
* Gli **exe** non includono i driver PostgreSQL (`APS_DATABASE_URL` funziona solo con SQLite); l'exe a file singolo
  può essere segnalato da alcuni antivirus/SmartScreen: in tal caso firmalo o aggiungi un'eccezione. Le build sono state
  provate su Linux con le stesse opzioni, non su Windows.
* **LDAP / Active Directory non implementato**: solo account locali con bcrypt.
* **Nessun TLS** integrato: in rete usare un reverse proxy HTTPS (le password viaggiano nel login).
* L'app grafica del server e' pensata per **una sola istanza per cartella dati**: non avviarne due sulla stessa cartella.
  Chiudendo la finestra il server si ferma (nessuna icona nell'area di notifica): per un servizio sempre attivo usa la
  versione console. La GUI del server non e' stata provata su Windows (solo su Linux in modalita' offscreen).
* L'**installazione come servizio non e' stata provata su Windows** (non ho una macchina Windows): la logica (configurazione,
  avvio/arresto, riavvio se cade, registro, scoperta) e' coperta da test e da un exe Linux, ma la registrazione presso il
  gestore dei servizi (pywin32, `sc`, `netsh`) va verificata alla prima installazione; in caso di problemi guarda
  `<cartella dati>\\logs\\server.log` e il Visualizzatore eventi.
* Le **richieste di merge in sospeso non sono persistite**: al riavvio del server vanno reinviate. Master, revisioni, utenti
  e audit invece sono su disco.
* Il blocco dopo password errate è **in memoria** (si azzera al riavvio) e per singolo processo.
* La **Safety Signature** dei controller GuardLogix non viene ricalcolata dal merge: va verificata in Studio 5000.
* Il merge è stato provato solo su **L5X sintetici** (`tests/sample_l5x.py`): prima dell'uso in produzione provalo su copie di
  progetti reali. Gli elementi che il merge non scompone (diversi da Rung/Line, Tag, UDT, AOI, Program, Routine) sono
  confrontati per intero: se due persone li toccano si ottiene un conflitto.
* Il merge di **L5K e dei file di testo e' per riga**, non strutturale come quello degli L5X: due modifiche in righe
  diverse si uniscono da sole, ma modifiche logicamente collegate (es. un tag e la riga che lo usa) possono produrre un
  file L5K da controllare: la schermata di consenso mostra sempre il risultato prima del voto. Anche l'L5K non e' stato
  provato su export reali di Studio 5000, solo su esempi sintetici.
* Gestione progetti (creazione, permessi) riservata all'Admin; il Lead inizializza il Master e dà il Consenso Finale.
