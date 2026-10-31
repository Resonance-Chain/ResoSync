# Guida: da zero a salvataggi sincronizzati

Tempo totale: circa 20 minuti la prima volta, 5 minuti per ogni PC in più.

## 1. Ottenere il programma per Windows

1. Apri la pagina **Releases** del repository su GitHub
   (<https://github.com/Resonance-Chain/ResoSync/releases>).
2. Dall'ultima versione scarica:
   - `ResoSync-Setup-x.y.z.exe`: l'installer (consigliato)
   - `ResoSync-portable.zip`: la versione senza installazione

Steam Deck / Linux: `ResoSync-linux-x86_64.tar.gz`, guida in [STEAMDECK.md](STEAMDECK.md).

> Per chi gestisce il repository: *Actions > Test e build > Run workflow*, scrivi la versione
> (es. `0.6.0`) e avvia. Dopo i test (circa 40 minuti) compare la Release con installer e zip.

## 2. Installare

1. Avvia `ResoSync-Setup-x.y.z.exe`.
2. Windows mostrerà **"Windows ha protetto il PC"** (SmartScreen): succede con tutti i programmi
   non firmati con un certificato a pagamento. Clicca **Ulteriori informazioni > Esegui comunque**.
3. Lascia spuntato **"Avvia ResoSync all'accensione del PC"**: così lavora sempre in background.

## 3. Scegliere dove salvare (una volta sola)

Al primo avvio si aprono le Impostazioni. Hai cinque strade. **Se non sai quale scegliere, usa la A.**
Se userai anche lo Steam Deck, leggi prima [STEAMDECK.md](STEAMDECK.md): sul Deck non tutte vanno
bene allo stesso modo.

### A. Cartella dentro Proton Drive / Google Drive / OneDrive / Dropbox (consigliata per iniziare)

1. Assicurati di avere il programma del cloud installato e collegato:
   - OneDrive: già presente in Windows 10/11
   - Proton Drive: [app per Windows](https://proton.me/drive/download)
   - Google Drive: [Drive per desktop](https://www.google.com/drive/download/)
   - Dropbox: [app desktop](https://www.dropbox.com/install)
2. In ResoSync: **Cartella** > **Sfoglia…** > scegli la cartella del cloud
   (es. `C:\Users\Tu\OneDrive`). ResoSync crea dentro una sottocartella `ResoSync`.
3. **Salva**.

**Google Drive per desktop**: nelle sue preferenze imposta la cartella in modalità
**"Mirroring"** (non "Streaming"), sennò i file non sono davvero sul disco.

### B. OneDrive diretto (senza programma OneDrive)

Serve registrare un'"app" gratuita su Microsoft (5 minuti):

1. Vai su <https://portal.azure.com/> e accedi con il tuo account Microsoft.
2. Cerca **Registrazioni app** > **Nuova registrazione**.
3. Nome: `ResoSync`. Tipi di account: **"Account in qualsiasi directory organizzativa e account
   Microsoft personali"**.
4. URI di reindirizzamento: **Client pubblico/nativo (mobile e desktop)**, valore `http://localhost`.
5. **Registra**, poi copia l'**ID applicazione (client)**.
6. In ResoSync: **OneDrive (accesso diretto)** > incolla il Client ID > **Accedi…** > accedi nel
   browser > **Salva**.

### C. Google Drive diretto (senza programma Google Drive)

1. Vai su <https://console.cloud.google.com/> e crea un progetto `ResoSync`.
2. **API e servizi > Libreria**: cerca **Google Drive API** e abilitala.
3. **API e servizi > Schermata consenso OAuth**: tipo **Esterno**, compila nome app e email.
   In **Utenti di test** aggiungi la tua email Gmail.
4. **API e servizi > Credenziali > Crea credenziali > ID client OAuth**, tipo **App desktop**.
5. Copia **Client ID** e **Client secret**.
6. In ResoSync: **Google Drive (accesso diretto)** > incolla i due valori > **Accedi…** > **Salva**.

Google mostrerà "App non verificata": è la tua app in modalità test, clicca **Continua**.
In modalità test Google fa scadere l'accesso ogni 7 giorni: se ResoSync segnala "Serve il login",
premi di nuovo **Accedi…**. Per evitarlo: Schermata consenso > **Pubblica app**.

### D. S3 (Cloudflare R2, Backblaze B2, AWS)

Per chi lo conosce già: bucket, endpoint, access key e secret key. Cloudflare R2 ha 10 GB gratis.

### E. rclone (Proton Drive senza programma, pCloud, Mega, SFTP… oltre 70 servizi)

1. Installa [rclone](https://rclone.org/downloads/) (su Windows: `winget install Rclone.Rclone`).
2. Da un terminale: `rclone config` > `n` (nuovo remote), scegli il servizio e segui le domande.
3. In ResoSync: **rclone** > **Cerca** > scegli il remote e aggiungi la cartella, es.
   `proton:ResoSync` > **Salva**.

Può puntare alla stessa cartella che un altro PC usa con l'opzione A: i dati sono gli stessi.

## 4. Aggiungere i giochi

1. **Rileva giochi**: la prima volta scarica il database (qualche secondo).
2. Seleziona i giochi che vuoi (Ctrl+clic per più di uno) > **Aggiungi selezionati**.
3. Partono subito le prime sincronizzazioni (colonna *Stato*).

Gioco non trovato? **Aggiungi gioco** e scegli la cartella dei salvataggi a mano
(cercala su [PCGamingWiki](https://www.pcgamingwiki.com/), sezione "Save game data location").

### Come ResoSync capisce che stai giocando

Serve per non toccare i salvataggi durante la partita e sincronizzarli all'avvio e alla chiusura.
ResoSync guarda la **cartella d'installazione** del gioco: qualsiasi exe che parte da lì conta
come "in gioco", anche se lo avvii da **Vortex**, Mod Organizer o un altro launcher.

Per i giochi di Steam, Epic e GOG la cartella viene trovata da sola (nella colonna *Processo*
compare tra parentesi quadre, es. `[Cyberpunk 2077]`). Per gli altri giochi:
doppio clic sul gioco > **Cartella d'installazione** > **Sfoglia…** > la cartella dove è
installato > **Salva**. In alternativa, con il gioco aperto: **Processo del gioco** > **Scegli…**.

Per controllare: avvia il gioco e guarda il riquadro *Attività*, deve comparire "gioco avviato".

## 5. Steam (consigliato)

Così ogni volta che avvii un gioco da Steam i salvataggi vengono scaricati prima e caricati dopo,
anche se ResoSync è chiuso:

1. In ResoSync: tasto destro sul gioco > **Copia opzioni di avvio Steam**.
2. In Steam: tasto destro sul gioco > **Proprietà** > **Generale** > **Opzioni di avvio** > incolla.

Se il gioco ha già delle opzioni di avvio, mettile **dopo** `%command%`.

## 6. Il secondo PC

1. Installa ResoSync (punto 2).
2. Stesso cloud del primo PC (punto 3). Con le opzioni A, B e C usa lo stesso account.
   Con la cifratura attiva, usa **la stessa passphrase**.
3. **Rileva giochi** e aggiungi gli stessi giochi. I nomi coincidono e si agganciano da soli.
   Per i giochi aggiunti a mano: **Aggiungi gioco** e scegli il nome dal menu a tendina (sono i
   giochi già presenti sul cloud).

> **Prima sincronizzazione sul secondo PC**: se ha già dei salvataggi suoi dello stesso gioco,
> diversi da quelli sul cloud, ResoSync non decide da solo: mostra per ognuno quale versione
> terrebbe (la più recente) e chiede conferma. Prima fa comunque un'istantanea della cartella,
> e la versione scartata resta in *Ripristina versione*.

## 7. Cifratura (facoltativa)

**Impostazioni > Sicurezza > Cifra i salvataggi**, scegli una passphrase lunga e salvala in un
password manager. Va attivata su tutti i PC con la stessa passphrase. Se la perdi, i dati sul
cloud non si recuperano (quelli sui PC restano).

## 8. Strumenti utili

- **Doppio clic su un gioco**: dettagli, spazio usato, cronologia, chi lo sta giocando.
- **Tasto destro > Istantanee della cartella**: copie complete fatte ogni volta che apri il
  gioco. Se una sessione rovina tutto (mod, autosave sbagliato), torni a com'era prima.
- **Tasto destro > Ripristina versione**: versioni dei singoli file, con anteprima dello
  screenshot e dei dati del salvataggio.
- **Salvataggi fatti di più file** (es. Cyberpunk): nella scheda del gioco, "Un salvataggio
  è" dice a ResoSync quali file vanno sempre insieme. Viene proposto in automatico; in
  *Ripristina versione* c'è anche "Ripristina tutto il salvataggio".
- **Aggiornamenti**: quando esce una versione nuova compare "Aggiorna a x.y.z" in basso a
  destra. Aggiorna tutti i PC.
- **Giochi con il cloud di Steam/Epic/GOG**: puoi metterli in *Solo backup* (doppio clic >
  Modifica): ResoSync tiene le versioni senza fare a gara con il cloud dello store.
- **Tasto destro > Verifica il cloud**: controlla che ogni salvataggio sul cloud sia integro e
  ripara quelli rovinati da una copia buona. Lo fa anche da solo una volta a settimana
  (Impostazioni > Comportamento).
- **Giochi che salvano nel registro di Windows** (alcuni giochi Unity, molti giochi vecchi):
  *Rileva giochi* li trova e li sincronizza come gli altri. Nella cartella del gioco compare
  `.resosync-registry`: è il registro esportato, lascialo dov'è.

## Problemi?

| Sintomo | Cosa fare |
|---|---|
| Stato "Errore: …" | leggi il riquadro *Attività*; spesso è il gioco che tiene il file aperto, ResoSync riprova da solo |
| "Serve il login" | Impostazioni > Cloud > **Accedi…** |
| "In attesa: PC-X sta sincronizzando" | normale, riprova da solo; se dura più di 2 minuti il lock scade |
| Un salvataggio è sparito o è vecchio | tasto destro > **Ripristina versione**, oppure **Istantanee della cartella** |
| Stato "Prima sincronizzazione: da confermare" | premi **Sincronizza** sul gioco: compare cosa succede e chiede conferma |
| Avviso "salvataggi rovinati sul cloud" | nessuna copia buona per ripararli: se il file è ancora giusto su un altro PC, sincronizza da lì |
| "manca ancora crypto.json" | cloud cifrato non ancora scaricato del tutto dal programma del cloud: aspetta e riprova |
| Non vedo la finestra | icona di ResoSync nella barra in basso a destra (freccetta ^) |

File utili, in `%USERPROFILE%\.resosync`:
- `launcher.log`: cosa è successo con le opzioni di avvio di Steam
- `backups\`: copie locali dei salvataggi sovrascritti
- `config.json`: configurazione (senza password)
