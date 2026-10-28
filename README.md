# ResoSync

Sincronizza i salvataggi dei videogiochi tra più PC passando dal cloud.
Trova da solo i giochi installati, si accorge quando un gioco salva, carica i salvataggi
quando chiudi il gioco e li scarica quando lo riapri su un altro PC.

> Prima volta? Segui la guida passo passo: [SETUP.md](SETUP.md).
> Steam Deck / Linux: [STEAMDECK.md](STEAMDECK.md).

## Installazione su Windows

**Modo facile**: scarica `ResoSync-Setup-x.y.z.exe` dalla pagina Releases (o dagli artifact di
GitHub Actions) e installalo. Non servono permessi di amministratore.

**Compilarlo da solo**: installa Python 3.12 da python.org (spunta "Add to PATH"), poi doppio
clic su `build_windows.bat`. Trovi il programma in `dist\ResoSync\ResoSync.exe` e, se hai
[Inno Setup 6](https://jrsoftware.org/isdl.php), anche l'installer in `dist\`.

**Senza compilare**:

```bash
pip install -r requirements.txt
python main.py
```

## Primo avvio

1. **Impostazioni > Cloud**: scegli dove salvare (vedi sotto).
2. **Rileva giochi**: ResoSync controlla il database open di
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
5. Copia Client ID e Client secret in ResoSync e premi **Accedi…**.

I file finiscono nella cartella dati nascosta dell'app: ResoSync non può vedere né toccare il
resto del tuo Drive.

### OneDrive: creare il Client ID (5 minuti, gratis)

1. Vai su <https://portal.azure.com/> > **Registrazioni app** > *Nuova registrazione*.
2. Tipi di account: **Account in qualsiasi directory organizzativa e account Microsoft personali**.
3. URI di reindirizzamento: piattaforma **Client pubblico/nativo (mobile e desktop)**, valore `http://localhost`.
4. Copia l'**ID applicazione (client)** in ResoSync e premi **Accedi…**.

I file finiscono in `App/ResoSync` sul tuo OneDrive: l'app non vede il resto.

## Giochi aperti, Steam e launcher

- Se un gioco ha il **processo** impostato (es. `eldenring.exe`), ResoSync se ne accorge:
  quando lo apri scarica l'ultima versione, mentre giochi non carica nulla (i file potrebbero
  essere a metà) e quando lo chiudi carica tutto.
- **Steam**: tasto destro sul gioco in ResoSync > *Copia opzioni di avvio Steam*, poi in Steam
  *Proprietà > Opzioni di avvio* incolla. Diventa una cosa tipo:
  `"C:\...\ResoSync.exe" --launch elden-ring %command%`.
  Così ogni avvio da Steam fa: scarica salvataggi, gioca, carica salvataggi. Funziona anche
  senza tenere aperta l'app.
- **Avvia gioco**: stesso giro, usando il *Comando di avvio* del gioco (es.
  `steam://rungameid/1245620` o il percorso dell'exe).

Nota: per alcuni giochi il database indica il launcher (es. `start_protected_game.exe` per Elden
Ring) invece del gioco vero (`eldenring.exe`). Con *Modifica > Scegli…* prendi il processo giusto
dalla lista di quelli aperti.

## Funzioni extra

- **Istantanee** della cartella salvataggi a ogni avvio del gioco (deduplicate, ripristino con
  un clic).
- **Avviso** se lo stesso gioco è aperto su un altro PC.
- **Solo backup** per i giochi che hanno già il cloud dello store.
- **Dettagli** per gioco: spazio, cronologia, ultimo salvataggio.
- **Anteprima** delle versioni (screenshot e metadati del salvataggio).
- **Limite di spazio** per gioco e pulizia delle versioni vecchie.
- **Aggiornamento automatico** dalle Release di GitHub (se il repository è privato serve un
  token in Impostazioni > Sistema).

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
- **Versioni**: ogni salvataggio caricato è un file a sé che non viene mai sovrascritto
  (default: l'attuale + 10 precedenti per file), più copie locali in
  `%USERPROFILE%\.resosync\backups`. Da *Ripristina versione* torni indietro.
- **Mai all'indietro**: se il cloud propone un salvataggio più vecchio di quello già
  sincronizzato (succede con le cartelle cloud quando un file arriva in ritardo), ResoSync non
  lo scarica e ricarica il suo.
- **Lock**: prima di scrivere, un PC prende un lock sul cloud (scade dopo 2 minuti se il PC si
  pianta). Gli altri aspettano e riprovano. Con l'opzione "Cartella" il lock vale solo quando il
  programma del cloud ha già sincronizzato, per questo valgono anche le due regole sopra.
- **Cancellazioni**: di default non vengono propagate (un file cancellato per sbaglio torna).
  Anche se le attivi, una cartella che si svuota di colpo non cancella niente dal cloud.

Struttura sul cloud:

```
games.json                              elenco giochi
crypto.json                             solo con la cifratura (sale + verifica passphrase)
<gioco>/manifest.json                   per ogni file: hash, data, PC, blob attuale
<gioco>/blobs/<file>/<data>__<pc>__<hash>   contenuti (attuale + versioni), mai sovrascritti
<gioco>/lock.json                       lock temporaneo durante una sync
<gioco>/files/, <gioco>/versions/       formato della 0.2.0, convertito alla prima modifica
```

Dati locali (configurazione, stato, backup, log): `%USERPROFILE%\.resosync`, oppure `.savesync`
se venivi da SaveSync, il vecchio nome del progetto.
Si può cambiare con la variabile d'ambiente `RESOSYNC_HOME`.

## Sviluppo

```bash
pip install -r requirements.txt pytest
python -m pytest -v tests
```

La pipeline `.github/workflows/build.yml` esegue i test su Windows e Linux, compila l'exe,
verifica che parta e crea installer + zip portabile. Pubblicando una Release (es. `v1.0.0`) installer e zip vengono allegati da soli.
