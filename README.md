# SaveSync

Sincronizza i salvataggi dei videogiochi tra più PC passando dal cloud.
Trova da solo i giochi installati, si accorge quando un gioco salva, carica i salvataggi
quando chiudi il gioco e li scarica quando lo riapri su un altro PC.

> Prima volta? Segui la guida passo passo: [SETUP.md](SETUP.md)

## Installazione su Windows

**Modo facile**: scarica `SaveSync-Setup-x.y.z.exe` dalla pagina Releases (o dagli artifact di
GitHub Actions) e installalo. Non servono permessi di amministratore.

**Compilarlo da solo**: installa Python 3.12 da python.org (spunta "Add to PATH"), poi doppio
clic su `build_windows.bat`. Trovi il programma in `dist\SaveSync\SaveSync.exe` e, se hai
[Inno Setup 6](https://jrsoftware.org/isdl.php), anche l'installer in `dist\`.

**Senza compilare**:

```bash
pip install -r requirements.txt
python main.py
```

## Primo avvio

1. **Impostazioni > Cloud**: scegli dove salvare (vedi sotto).
2. **Rileva giochi**: SaveSync controlla il database open di
   [Ludusavi](https://github.com/mtkennerly/ludusavi-manifest) (oltre 20.000 giochi) e ti mostra quelli
   che hanno salvataggi su questo PC. Selezionali e premi *Aggiungi selezionati*.
3. Ripeti sugli altri PC: i giochi hanno lo stesso nome, quindi si agganciano da soli.

Per i giochi che non sono nel database: **Aggiungi gioco** e indichi la cartella a mano.

## Dove salvare

| Opzione | Configurazione | Note |
|---|---|---|
| **Cartella** | scegli una cartella dentro Google Drive, OneDrive o Dropbox | la più semplice, serve il loro programma installato |
| **Google Drive** | Client ID + secret (vedi sotto) | accesso diretto, nessun programma extra |
| **OneDrive** | Client ID (vedi sotto) | accesso diretto, nessun programma extra |
| **S3** | bucket + chiavi | AWS, Cloudflare R2, Backblaze B2 (R2 e B2 hanno piani gratuiti) |

### Google Drive: creare il Client ID (5 minuti, gratis)

1. Vai su <https://console.cloud.google.com/>, crea un progetto.
2. *API e servizi > Libreria*: abilita **Google Drive API**.
3. *Schermata consenso OAuth*: tipo "Esterno", aggiungi il tuo indirizzo tra gli **utenti di test**.
4. *Credenziali > Crea credenziali > ID client OAuth*, tipo **App desktop**.
5. Copia Client ID e Client secret in SaveSync e premi **Accedi…**.

I file finiscono nella cartella dati nascosta dell'app: SaveSync non può vedere né toccare il
resto del tuo Drive.

### OneDrive: creare il Client ID (5 minuti, gratis)

1. Vai su <https://portal.azure.com/> > **Registrazioni app** > *Nuova registrazione*.
2. Tipi di account: **Account in qualsiasi directory organizzativa e account Microsoft personali**.
3. URI di reindirizzamento: piattaforma **Client pubblico/nativo (mobile e desktop)**, valore `http://localhost`.
4. Copia l'**ID applicazione (client)** in SaveSync e premi **Accedi…**.

I file finiscono in `App/SaveSync` sul tuo OneDrive: l'app non vede il resto.

## Giochi aperti, Steam e launcher

- Se un gioco ha il **processo** impostato (es. `eldenring.exe`), SaveSync se ne accorge:
  quando lo apri scarica l'ultima versione, mentre giochi non carica nulla (i file potrebbero
  essere a metà) e quando lo chiudi carica tutto.
- **Steam**: tasto destro sul gioco in SaveSync > *Copia opzioni di avvio Steam*, poi in Steam
  *Proprietà > Opzioni di avvio* incolla. Diventa una cosa tipo:
  `"C:\...\SaveSync.exe" --launch elden-ring %command%`.
  Così ogni avvio da Steam fa: scarica salvataggi, gioca, carica salvataggi. Funziona anche
  senza tenere aperta l'app.
- **Avvia gioco**: stesso giro, usando il *Comando di avvio* del gioco (es.
  `steam://rungameid/1245620` o il percorso dell'exe).

Nota: per alcuni giochi il database indica il launcher (es. `start_protected_game.exe` per Elden
Ring) invece del gioco vero (`eldenring.exe`). Con *Modifica > Scegli…* prendi il processo giusto
dalla lista di quelli aperti.

## Sicurezza

- **Cifratura** (Impostazioni > Sicurezza): AES-256-GCM con chiave derivata dalla passphrase
  (scrypt). Il provider vede solo dati illeggibili. Usa la stessa passphrase su tutti i PC; se la
  perdi, i dati sul cloud non si recuperano (quelli sui PC restano). I nomi dei file restano visibili.
- **Password e chiavi** (S3, client secret, passphrase) stanno nel portachiavi di sistema
  (Gestione credenziali di Windows), non in `config.json`. I token di Google/Microsoft sono
  in un file cifrato con una chiave tenuta nel portachiavi.

## Come funziona

- **Rilevamento**: `watchdog` riceve gli eventi del filesystem; dopo qualche secondo di calma
  parte la sync. In più un controllo completo ogni 60 secondi per le novità dagli altri PC.
- **Confronto a 3 vie**: hash locale, hash sul cloud, hash dell'ultima sync. Così si capisce chi
  ha cambiato cosa senza fidarsi solo delle date.
- **Conflitti** (modificato su due PC): vince il più recente, l'altro non si perde mai.
- **Versioni**: prima di sovrascrivere si tiene una copia (default 10 per file), sul cloud e in
  locale (`%USERPROFILE%\.savesync\backups`). Da *Ripristina versione* torni indietro.
- **Lock**: prima di scrivere, un PC prende un lock sul cloud (scade dopo 2 minuti se il PC si
  pianta). Gli altri aspettano e riprovano. Con l'opzione "Cartella" il lock vale solo quando il
  programma del cloud ha già sincronizzato, quindi è meno affidabile delle API dirette.
- **Cancellazioni**: di default non vengono propagate (un file cancellato per sbaglio torna).

Struttura sul cloud:

```
games.json                      elenco giochi
crypto.json                     solo se la cifratura è attiva (sale + verifica passphrase)
<gioco>/manifest.json           hash, data, PC di ogni file
<gioco>/files/...               salvataggi attuali
<gioco>/versions/<file>/...     versioni precedenti
<gioco>/lock.json               lock temporaneo durante una sync
```

Dati locali: `%USERPROFILE%\.savesync` (configurazione, stato, backup, log del launcher).
Si può cambiare con la variabile d'ambiente `SAVESYNC_HOME`.

## Sviluppo

```bash
pip install -r requirements.txt pytest
python -m pytest -v tests
```

La pipeline `.github/workflows/build.yml` esegue i test su Windows, Linux e macOS, compila l'exe,
verifica che parta e crea installer + zip portabile. Pubblicando una Release (es. `v1.0.0`) installer e zip vengono allegati da soli.
