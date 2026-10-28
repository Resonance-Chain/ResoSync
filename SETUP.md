# Guida: da zero a salvataggi sincronizzati

Tempo totale: circa 20 minuti la prima volta, 5 minuti per ogni PC in più.

## 1. Ottenere il programma per Windows

Il programma viene compilato in automatico da GitHub (gratis) ogni volta che il codice cambia.

1. Apri il repository su GitHub, scheda **Actions**.
2. Clicca sull'ultima esecuzione di **Test e build** con la spunta verde.
3. In fondo alla pagina, sotto **Artifacts**, scarica **SaveSync-windows** (uno zip).
4. Dentro trovi:
   - `SaveSync-Setup-x.y.z.exe`: l'installer (consigliato)
   - `SaveSync-portable.zip`: la versione senza installazione

> Nuova versione? Su GitHub *Releases > Draft a new release*,
> crea un tag tipo `v0.3.0` e pubblica: in 10 minuti la pipeline ci allega installer e zip.

## 2. Installare

1. Avvia `SaveSync-Setup-x.y.z.exe`.
2. Windows mostrerà **"Windows ha protetto il PC"** (SmartScreen): succede con tutti i programmi
   non firmati con un certificato a pagamento. Clicca **Ulteriori informazioni > Esegui comunque**.
3. Lascia spuntato **"Avvia SaveSync all'accensione del PC"**: così lavora sempre in background.

## 3. Scegliere dove salvare (una volta sola)

Al primo avvio si aprono le Impostazioni. Hai quattro strade. **Se non sai quale scegliere, usa la A.**

### A. Cartella dentro Google Drive / OneDrive / Dropbox (consigliata per iniziare)

1. Assicurati di avere il programma del cloud installato e collegato:
   - OneDrive: già presente in Windows 10/11
   - Google Drive: [Drive per desktop](https://www.google.com/drive/download/)
   - Dropbox: [app desktop](https://www.dropbox.com/install)
2. In SaveSync: **Cartella** > **Sfoglia…** > scegli la cartella del cloud
   (es. `C:\Users\Tu\OneDrive`). SaveSync crea dentro una sottocartella `SaveSync`.
3. **Salva**.

**Google Drive per desktop**: nelle sue preferenze imposta la cartella in modalità
**"Mirroring"** (non "Streaming"), sennò i file non sono davvero sul disco.

### B. OneDrive diretto (senza programma OneDrive)

Serve registrare un'"app" gratuita su Microsoft (5 minuti):

1. Vai su <https://portal.azure.com/> e accedi con il tuo account Microsoft.
2. Cerca **Registrazioni app** > **Nuova registrazione**.
3. Nome: `SaveSync`. Tipi di account: **"Account in qualsiasi directory organizzativa e account
   Microsoft personali"**.
4. URI di reindirizzamento: **Client pubblico/nativo (mobile e desktop)**, valore `http://localhost`.
5. **Registra**, poi copia l'**ID applicazione (client)**.
6. In SaveSync: **OneDrive (accesso diretto)** > incolla il Client ID > **Accedi…** > accedi nel
   browser > **Salva**.

### C. Google Drive diretto (senza programma Google Drive)

1. Vai su <https://console.cloud.google.com/> e crea un progetto `SaveSync`.
2. **API e servizi > Libreria**: cerca **Google Drive API** e abilitala.
3. **API e servizi > Schermata consenso OAuth**: tipo **Esterno**, compila nome app e email.
   In **Utenti di test** aggiungi la tua email Gmail.
4. **API e servizi > Credenziali > Crea credenziali > ID client OAuth**, tipo **App desktop**.
5. Copia **Client ID** e **Client secret**.
6. In SaveSync: **Google Drive (accesso diretto)** > incolla i due valori > **Accedi…** > **Salva**.

Google mostrerà "App non verificata": è la tua app in modalità test, clicca **Continua**.
In modalità test Google fa scadere l'accesso ogni 7 giorni: se SaveSync segnala "Serve il login",
premi di nuovo **Accedi…**. Per evitarlo: Schermata consenso > **Pubblica app**.

### D. S3 (Cloudflare R2, Backblaze B2, AWS)

Per chi lo conosce già: bucket, endpoint, access key e secret key. Cloudflare R2 ha 10 GB gratis.

## 4. Aggiungere i giochi

1. **Rileva giochi**: la prima volta scarica il database (qualche secondo).
2. Seleziona i giochi che vuoi (Ctrl+clic per più di uno) > **Aggiungi selezionati**.
3. Partono subito le prime sincronizzazioni (colonna *Stato*).

Gioco non trovato? **Aggiungi gioco** e scegli la cartella dei salvataggi a mano
(cercala su [PCGamingWiki](https://www.pcgamingwiki.com/), sezione "Save game data location").

### Controlla il processo del gioco

Per ogni gioco, doppio clic > **Processo del gioco**. Serve perché SaveSync sappia quando giochi:

1. Avvia il gioco.
2. In SaveSync: doppio clic sul gioco > **Scegli…** > trova l'exe del gioco nella lista > **Salva**.

Esempio: per Elden Ring il database suggerisce `start_protected_game.exe`, ma quello giusto è
`eldenring.exe`.

## 5. Steam (consigliato)

Così ogni volta che avvii un gioco da Steam i salvataggi vengono scaricati prima e caricati dopo,
anche se SaveSync è chiuso:

1. In SaveSync: tasto destro sul gioco > **Copia opzioni di avvio Steam**.
2. In Steam: tasto destro sul gioco > **Proprietà** > **Generale** > **Opzioni di avvio** > incolla.

Se il gioco ha già delle opzioni di avvio, mettile **dopo** `%command%`.

## 6. Il secondo PC

1. Installa SaveSync (punto 2).
2. Stesso cloud del primo PC (punto 3). Con le opzioni A, B e C usa lo stesso account.
   Con la cifratura attiva, usa **la stessa passphrase**.
3. **Rileva giochi** e aggiungi gli stessi giochi. I nomi coincidono e si agganciano da soli.
   Per i giochi aggiunti a mano: **Aggiungi gioco** e scegli il nome dal menu a tendina (sono i
   giochi già presenti sul cloud).

> ⚠️ **Prima sincronizzazione sul secondo PC**: se ha già dei salvataggi suoi dello stesso gioco,
> vince il più recente. L'altro non si perde (è in *Ripristina versione*), ma se vuoi andare sul
> sicuro fai prima una copia della cartella.

## 7. Cifratura (facoltativa)

**Impostazioni > Sicurezza > Cifra i salvataggi**, scegli una passphrase lunga e salvala in un
password manager. Va attivata su tutti i PC con la stessa passphrase. Se la perdi, i dati sul
cloud non si recuperano (quelli sui PC restano).

## Problemi?

| Sintomo | Cosa fare |
|---|---|
| Stato "Errore: …" | leggi il riquadro *Attività*; spesso è il gioco che tiene il file aperto, SaveSync riprova da solo |
| "Serve il login" | Impostazioni > Cloud > **Accedi…** |
| "In attesa: PC-X sta sincronizzando" | normale, riprova da solo; se dura più di 2 minuti il lock scade |
| Un salvataggio è sparito o è vecchio | tasto destro > **Ripristina versione** |
| Non vedo la finestra | icona di SaveSync nella barra in basso a destra (freccetta ^) |

File utili, in `%USERPROFILE%\.savesync`:
- `launcher.log`: cosa è successo con le opzioni di avvio di Steam
- `backups\`: copie locali dei salvataggi sovrascritti
- `config.json`: configurazione (senza password)
