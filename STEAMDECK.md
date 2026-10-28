# ResoSync su Steam Deck

Obiettivo: i salvataggi passano da soli tra PC e Steam Deck, **anche in modalità gioco**.
Tempo: circa 20 minuti la prima volta.

## Prima di iniziare: quale cloud

PC e Deck devono usare **lo stesso cloud**. La cartella di Proton Drive non va bene sul Deck:
Proton Drive su Linux non ha ancora un programma che sincronizza le cartelle. Usa uno di questi,
che ResoSync raggiunge direttamente senza installare niente:

- **OneDrive** (consigliato sul Deck): l'accesso non scade.
- **Google Drive**: va bene, ma leggi la nota sul punto 3.

> **Il cloud di Steam fa già metà del lavoro.** Per i giochi Steam con salvataggio su cloud
> (Cyberpunk, Elden Ring, Hades...) Steam sincronizza già PC e Deck da solo. ResoSync serve
> per i giochi senza cloud di Steam, per quelli di Epic/GOG (Heroic), per gli emulatori e come
> rete di sicurezza con versioni e istantanee. Per i giochi con il cloud di Steam scegli
> "Solo backup" quando ResoSync te lo chiede.

## 1. Sul PC: passa al nuovo cloud

1. Aggiorna ResoSync sul PC all'ultima versione.
2. **Impostazioni > Cloud**: scegli OneDrive (o Google Drive), inserisci il Client ID (vedi
   [SETUP.md](SETUP.md), punto 3 B o C), premi **Accedi…**, poi **Salva**.
3. ResoSync carica i salvataggi del PC sul nuovo cloud. I dati vecchi restano nella cartella
   Proton Drive come copia: puoi cancellarli quando vuoi.

Usa **lo stesso Client ID** sul Deck: non serve crearne un altro.

## 2. Sul Deck: installa

1. Passa alla **modalità desktop**: tasto Steam > Spegni > Passa al desktop.
2. Apri il browser e scarica **`ResoSync-linux-x86_64.tar.gz`** dall'ultima Release su GitHub.
3. Apri la cartella Download (Dolphin), tasto destro sul file > **Estrai > Estrai l'archivio
   qui**.
4. Apri **Konsole** (dal menu) e incolla:

   ```bash
   bash ~/Downloads/ResoSync/install-steamdeck.sh
   ```

   ResoSync si installa nella tua home (sopravvive agli aggiornamenti di SteamOS), compare nel
   menu e sul desktop, e si apre.

## 3. Sul Deck: configura

1. **Impostazioni > Cloud**: lo stesso cloud e lo stesso Client ID del PC > **Accedi…** (si apre
   il browser) > **Salva**.
   - *Google Drive*: se l'app Google è in modalità "test", l'accesso scade ogni 7 giorni e sul
     Deck dovresti tornare in modalità desktop per rifarlo. Per evitarlo: Google Cloud Console >
     Schermata consenso OAuth > **Pubblica app**.
2. **Impostazioni > Comportamento > Nome di questo PC**: es. `Steam Deck`.
3. **Impostazioni > Sistema**: spunta **"Servizio in background"**. Da adesso ResoSync parte con
   il Deck e lavora anche in modalità gioco, senza finestre.
4. **Rileva giochi**: ResoSync trova da solo i salvataggi dei giochi installati, anche quelli
   Windows che girano con Proton (dentro `steamapps/compatdata`), quelli sulla microSD e quelli
   di Heroic. Selezionali e **Aggiungi selezionati**: i nomi coincidono con quelli del PC, quindi
   si agganciano da soli.

## 4. Opzioni di avvio di Steam (consigliato)

Per sincronizzare esattamente prima e dopo ogni partita:

1. In ResoSync: tasto destro sul gioco > **Copia opzioni di avvio Steam**.
2. Nel client Steam (in modalità desktop): tasto destro sul gioco > Proprietà > **Opzioni di
   avvio** > incolla. Diventa una cosa tipo:

   ```
   "/home/deck/.local/share/resosync-app/ResoSync" --quiet --launch cyberpunk-2077 %command%
   ```

   `--quiet` = nessuna finestra sopra il gioco. Se qualcosa va storto il gioco parte comunque.

## 5. Torna in modalità gioco

Fatto. Gioca normalmente: il servizio sincronizza quando chiudi un gioco e ogni minuto
controlla le novità dal PC.

## Controllare che funzioni

- In modalità desktop apri ResoSync: la colonna *Stato* e il doppio clic su un gioco
  (cronologia) mostrano cosa è successo. Mentre la finestra è aperta il servizio è in pausa,
  e riparte quando la chiudi.
- Log del servizio: `~/.resosync/resosync.log`. Log delle opzioni di avvio:
  `~/.resosync/launcher.log`.
- In Konsole: `systemctl --user status resosync` mostra se il servizio è attivo.

## Aggiornare / disinstallare

- **Aggiornare**: scarica il nuovo `.tar.gz`, estrai e rilancia lo script (punto 2.4).
  Impostazioni e giochi restano.
- **Disinstallare**:

  ```bash
  bash ~/.local/share/resosync-app/install-steamdeck.sh --uninstall
  ```

  I tuoi dati restano in `~/.resosync`.
