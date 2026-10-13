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
tests/                     ~450 test (merger, tipi di file, nomi file, reimport, consenso, REST+WS, RBAC, audit, GUI offscreen su server reale)
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
| **PLCopen XML** (CODESYS e simili) | strutturale: POU / DUT / variabili / azioni / transizioni / metodi / configurazioni; ST e IL per riga dentro la POU, LD/FBD/SFC atomici |
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
| `APS_DEFAULT_MAX_REVISIONS` | 10 | revisioni del Master conservate per i nuovi progetti (l'Admin la cambia per progetto, 2-200) |

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
| `GET/POST /api/users`, `PATCH /api/users/{id}` (anche `unlock`), `DELETE /api/users/{id}`, `PUT /api/auth/policy` | Admin |
| `GET /api/notifications` · `/count`, `POST /api/notifications/read` | utente (le proprie) |
| `GET /api/client/update`, `/download`; `POST/DELETE /api/client/update` | utente / Admin |
| `GET /api/projects` | Viewer (solo quelli accessibili) |
| `POST /api/projects`, `PATCH /api/projects/{id}` (anche `max_revisions`), `GET/PUT /api/projects/{id}/permissions` | Admin |
| `DELETE /api/projects/{id}?confirm=<nome esatto>` (elimina progetto, file e audit del progetto) | Admin |
| `GET /api/projects/{id}/export` (.zip: revisioni conservate, history.json, audit.csv, README.txt) | Admin |
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

## Revisioni conservate, eliminazione, esportazione e backup

* **Limite di revisioni** (`max_revisions`, predefinito 10, da 2 a 200, solo Admin): dopo ogni nuova revisione il server cancella
  dal disco i file delle piu' vecchie, tenendo sempre la testa e le basi delle richieste di merge ancora attive. Nello storico
  restano i metadati (`purged=true`). Una richiesta con base eliminata riceve 409 ("fai il Checkout dell'ultimo Master"); il
  ripristino di una revisione eliminata e' rifiutato (422) e il download risponde 410. Ridurre il limite pota subito.
* **Eliminazione di un progetto** (Admin, conferma con il nome esatto): cancella il progetto, la sua cartella dati e **tutte le sue
  righe di audit**; la catena di hash viene ricalcolata (`AuditService.purge_project`, unica eccezione all'immutabilita') e
  **non resta nessuna riga di audit** del progetto. L'unica memoria e' una riga nel registro applicativo del server (log).
  I client collegati ricevono l'evento `project_deleted`.
* **Esportazione** (`GET /api/projects/{id}/export`): archivio .zip con le revisioni conservate (`<nome>_revN<ext>`), `history.json`,
  `audit.csv` e un `README.txt` che elenca le revisioni eliminate dal limite. Consigliata prima di eliminare un progetto.
* **Backup dei dati del server**: dalla GUI (Impostazioni > BACKUP > "Crea backup...") oppure `python -m server --backup FILE.zip`.
  Funziona a server acceso (il database e' copiato con l'API di backup di SQLite). L'archivio contiene database, progetti e il
  **segreto JWT**: conservalo in un posto protetto. Ripristino a mano: server fermo, scompatta l'archivio nella cartella dati
  (devono comparire `aps.db` e `projects/`), riavvia. Non copre un database PostgreSQL esterno (usa `pg_dump`).

### Dal client e dalla console del server (Utenti & Permessi > Permessi di progetto, solo Admin)

Gruppo **Impostazioni progetto**, identico nel client e nell'applicazione del server:
**Modifica progetto...** (nome e descrizione), **Storico e rollback...** (scarica una revisione, "Ripristina come nuova
revisione" con motivo; le revisioni eliminate dal limite sono in grigio e non si possono scaricare ne' ripristinare),
**Esporta progetto (.zip)...**, **Revisioni da conservare sul server** + **Applica** (se riduci il limite chiede conferma:
le revisioni piu' vecchie vengono eliminate in modo definitivo) ed **Elimina progetto...** (dialogo che elenca cosa sparira',
propone l'esportazione preventiva e abilita il pulsante solo digitando il nome esatto). Quando un progetto viene eliminato i
client collegati ricevono un avviso non bloccante e i loro **file locali non vengono toccati**.

**Pulizia sul PC dei client**: dopo ogni download il client tiene al massimo le ultime N copie `<nome>_revN<ext>` (N = limite del
progetto). Non cancella mai il file di lavoro, la sua base, file con contenuto diverso dalla propria copia pulita in `.aps`
(potrebbero contenere lavoro tuo) ne' file che non rispettano esattamente il nome `<nome>_rev<numero><ext>`.

## Guida integrata e aspetto

Il client ha una **Guida** (voce di menu o tasto **F1**) con 20 argomenti in italiano e ricerca (ruoli, cartella locale, merge,
consenso, conflitti, tipi di file, progetti, storico e rollback, revisioni conservate, eliminazione ed esportazione, audit,
server, risoluzione dei problemi, scorciatoie). L'aspetto e' stato rifatto (tema condiviso con l'app del server): schermata di
accesso a due colonne, schede a gradiente, sidebar a gruppi, tabelle e campi rifiniti, stati vuoti. Aggiungendo una funzione
nuova ricordati di documentarla in `client/views/help_view.py` e, se genera un evento di audit, in `audit_view.ACTIONS`.

## Novità: confronto, blocco, discussione, HTTPS, PLCopen, branch

* **Confronto tra revisioni, etichette, blocco**: dallo storico si confrontano due revisioni (strutturale per L5X/PLCopen, per righe per il testo). Lead/Admin
  etichettano e *bloccano* una revisione: le revisioni bloccate non vengono mai eliminate dal limite e non contano nel limite; sblocca solo l'Admin.
* **Presa in carico (blocco esclusivo)** del Master (`lock.json` per progetto, scadenza 8 h, `APS_LOCK_TTL_H`); per i file binari c'è il *check-in*.
  L'Admin può forzare il rilascio. Audit: `lock_acquired/released/forced`, `master_checkin`, `revision_labeled/locked/unlocked`.
* **Discussione** sulle richieste di merge (scheda «Discussione»): commenti, `@menzioni`, modifica entro 15 minuti, eliminazione logica, eventi WS in tempo reale.
  Audit: `comment_added/edited/deleted`.
* **HTTPS integrato**: certificato autofirmato generato dal server (SAN con i nomi del PC, 825 giorni) o certificato/chiave PEM propri
  (`--tls`, `--no-tls`, `--tls-cert`, `--tls-key`, Impostazioni del server). La ricerca automatica annuncia lo schema; il client
  memorizza l'impronta SHA-256 al primo collegamento (*trust on first use*, file `trusted_servers.json`) e avvisa se cambia. Le notifiche usano `wss`.
  Per un servizio Windows già installato, cambiare le impostazioni TLS richiede di reinstallarlo. Audit: `tls_certificate_generated`, `tls_settings_changed`. Richiede `cryptography`.
* **PLCopen XML (CODESYS)**: merge strutturale (vedi tabella dei tipi). Il progetto deve avere lo stesso `contentHeader@name`; DOCTYPE/entità esterne sono rifiutati.
* **Branch**: linee alternative del Master dentro un progetto, con revisioni, presa in carico, richieste di merge e commenti propri
  (`data/projects/<progetto>/branches/<branch>/`). *Team* (Lead/Admin): scrivono tutti i Developer; «Salva su branch» (`POST .../branches/{slug}/save`) unisce 3-way le modifiche con quelle già nel branch e consolida subito col solo consenso dell'autore (conflitti: la richiesta resta da risolvere); «Richiedi Merge» resta per chiedere il consenso. *Personale* (Developer o superiore, max 5 aperti
  per utente e progetto): scrive solo il proprietario con salvataggi diretti (`POST .../branches/{slug}/commit`); le richieste nel branch hanno il solo consenso dell'autore.
  «Aggiorna dal principale» (`/sync`) porta nel branch le novità con un 3-way; «Unisci nel principale» (`/merge`) crea una richiesta nel principale con consenso normale e a
  merge fatto il branch diventa `merged` (sola lettura, come `closed`). Le basi dei branch aperti non sono eliminate dal limite di revisioni. Tutte le chiamate di progetto accettano `?branch=<slug>`, anche il WebSocket;
  gli eventi portano `branch`. Export e backup includono i branch; eliminando il progetto spariscono anche i branch. Nel client: selettore nella barra laterale,
  banner del branch, «Branch...», «Salva su branch...», cartelle locali `<cartella>/branches/<nome>/`. Audit: `branch_created/synced/merged/closed/deleted/committed`.

* **Eliminazione utenti** (solo Admin, `DELETE /api/users/{id}`, pulsante «Elimina utente...» in Utenti & Permessi del client e della console del server): sessioni e permessi di progetto vengono rimossi; audit, commenti e branch restano col nome; non si elimina se stessi né l'ultimo Admin attivo. Audit: `user_deleted`.

* **Richieste di merge persistenti**: le richieste aperte (principale e branch) sono salvate in `pending.json` di ogni progetto/branch; al riavvio il server le ricarica, le rianalizza sulla testa corrente e mantiene i consensi solo se il risultato del merge è identico.
* **Blocco account persistente e scadenza password opzionale**: dopo N errori (`APS_MAX_LOGIN_FAILURES`) l'account è bloccato nel database (resiste al riavvio; l'Admin lo sblocca con `PATCH /api/users/{id} {"unlock": true}` o dal pulsante «Sblocca»). L'Admin può impostare la scadenza delle password (`GET/PUT /api/auth/policy`, `password_max_age_days`, 0 = mai): alla scadenza il primo accesso impone il cambio. Audit: `account_locked`, `password_expired`, `account_policy_changed`.
* **Anteprima conflitti (dry-run)**: `POST /api/projects/{id}/proposals/dry-run` e `POST .../branches/{slug}/dry-run` (`mode=merge|sync`, `file` facoltativo) eseguono lo stesso 3-way senza creare richieste e restituiscono conflitti e modifiche. Nel client: «Verifica conflitti» nelle finestre dei file da unire e «Anteprima conflitti» nell'elenco branch.
* **Notifiche persistenti** (`GET /api/notifications`, `/count`, `POST /read`): nuova richiesta di merge da votare, richiesta rifiutata / unita, branch aggiornato, menzioni. Restano anche se l'utente è offline; il client le mostra con la campanella (numero non lette) e l'evento WebSocket `notification`. **Email opzionale** se sono impostati `APS_SMTP_HOST`, `APS_SMTP_PORT`, `APS_SMTP_USER`, `APS_SMTP_PASSWORD`, `APS_SMTP_FROM`, `APS_SMTP_SECURITY` (`starttls` | `ssl` | `none`) e l'utente ha un indirizzo email.
* **Avviso di nuova revisione**: se un collega salva nel branch su cui lavori, il client propone di scaricarla e mostra «disponibile la rev N» sotto la cartella locale.
* **Risoluzione dei conflitti**: oltre alla scelta per elemento (proposta / Master / entrambe / manuale), pulsanti «Tutti: la proposta», «Tutti: il Master», «Entrambe (dove possibile)».
* **Aggiornamento automatico del client**: l'Admin pubblica `AutomationProjectClient.exe` dalle Impostazioni del server (o `POST /api/client/update`, multipart `file`, `version`, `notes`); all'accesso il client confronta la propria versione (`common/version.py`) con quella pubblicata (`GET /api/client/update?current=`), scarica, verifica lo SHA-256 e si sostituisce con uno script `.bat` riavviandosi. Dai sorgenti segnala soltanto. Audit: `client_update_published/removed`. **Non provato su Windows** (solo la logica e lo script generato).

## Sicurezza e audit

* Password con **bcrypt** (con pre-hash SHA-256), policy ≥ 10 caratteri con lettera e cifra, blocco temporaneo dopo 5 errori.
* **JWT** HS256: access token 15 min, refresh 12 h con **rotazione** e rilevamento del riuso (riuso ⇒ sessione revocata).
  Sessioni elencabili e revocabili singolarmente.
* **Audit trail**: chi ha scaricato (`master_downloaded`), chiesto il merge, dato consenso, approvato/forzato/ripristinato,
  gestito utenti e permessi, con data, progetto e revisione. Ogni riga contiene l'hash della precedente; su SQLite
  `UPDATE`/`DELETE` sono bloccati da trigger e `GET /api/audit/verify` rileva qualunque manomissione.
* Parser XML con risoluzione delle entità disattivata (XXE); upload limitato in dimensione.

## Limiti noti

* Gli **exe** non includono i driver PostgreSQL (`APS_DATABASE_URL` funziona solo con SQLite); l'exe a file singolo
  può essere segnalato da alcuni antivirus/SmartScreen: in tal caso firmalo o aggiungi un'eccezione. Le build sono state
  provate su Linux con le stesse opzioni, non su Windows.
* **LDAP / Active Directory non implementato**: solo account locali con bcrypt.
* **TLS** autofirmato: il client si fida al primo collegamento (impronta memorizzata); per una PKI aziendale usa un certificato proprio. Non provato su Windows (Schannel/servizio).
* **PLCopen/CODESYS** provato su esempi sintetici e sul formato standard, non su export reali CODESYS (metodi, struttura del progetto).
* Le **prese in carico** restano finché non scadono (8 h) o vengono rilasciate; le richieste di merge in sospeso sono persistite al riavvio (vedi sopra), ma le sessioni dei client senza account non lo sono.
* Race rarissima sulla creazione simultanea dello stesso nome di branch (500 invece di 409).
* L'app grafica del server e' pensata per **una sola istanza per cartella dati**: non avviarne due sulla stessa cartella.
  Chiudendo la finestra il server si ferma (nessuna icona nell'area di notifica): per un servizio sempre attivo usa la
  versione console. La GUI del server non e' stata provata su Windows (solo su Linux in modalita' offscreen).
* L'**installazione come servizio non e' stata provata su Windows** (non ho una macchina Windows): la logica (configurazione,
  avvio/arresto, riavvio se cade, registro, scoperta) e' coperta da test e da un exe Linux, ma la registrazione presso il
  gestore dei servizi (pywin32, `sc`, `netsh`) va verificata alla prima installazione; in caso di problemi guarda
  `<cartella dati>\\logs\\server.log` e il Visualizzatore eventi.
* Le revisioni piu' vecchie del limite sono **eliminate in modo definitivo** dal server: per tenerle, esporta il progetto o
  alza il limite prima che vengano potate. Il backup (`--backup`) copia solo il database SQLite della cartella dati.
* Le **notifiche** agli utenti collegati arrivano in tempo reale solo se il WebSocket è attivo; le email partono in un thread senza coda di ritentativi (un errore SMTP è solo registrato nel log).
* La **Safety Signature** dei controller GuardLogix non viene ricalcolata dal merge: va verificata in Studio 5000.
* Il merge è stato provato solo su **L5X sintetici** (`tests/sample_l5x.py`): prima dell'uso in produzione provalo su copie di
  progetti reali. Gli elementi che il merge non scompone (diversi da Rung/Line, Tag, UDT, AOI, Program, Routine) sono
  confrontati per intero: se due persone li toccano si ottiene un conflitto.
* Il merge di **L5K e dei file di testo e' per riga**, non strutturale come quello degli L5X: due modifiche in righe
  diverse si uniscono da sole, ma modifiche logicamente collegate (es. un tag e la riga che lo usa) possono produrre un
  file L5K da controllare: la schermata di consenso mostra sempre il risultato prima del voto. Anche l'L5K non e' stato
  provato su export reali di Studio 5000, solo su esempi sintetici.
* Gestione progetti (creazione, permessi) riservata all'Admin; il Lead inizializza il Master e dà il Consenso Finale.
