# Novità

## 0.4.0

- **Steam Deck e Linux**: versione Linux (`SaveSync-linux-x86_64.tar.gz`) con script di
  installazione, **servizio in background** che lavora anche in modalità gioco, launcher
  silenzioso per le opzioni di avvio di Steam. *Rileva giochi* trova i salvataggi dei giochi
  Windows dentro i prefissi Proton (`steamapps/compatdata/<id>`), anche su microSD, e quelli di
  Heroic. Guida: [STEAMDECK.md](STEAMDECK.md).
- Cambiando cloud (es. da cartella Proton Drive a OneDrive) SaveSync riparte da zero e carica
  tutto: con "propaga cancellazioni" attivo il cloud nuovo e vuoto poteva sembrare "cancellato
  tutto".
- Due istantanee nello stesso millisecondo non si sovrascrivono più.
- **Istantanee della cartella**: quando apri un gioco (e prima di ogni ripristino) SaveSync
  salva una copia completa della cartella dei salvataggi. Tasto destro sul gioco >
  *Istantanee della cartella…* per tornare "a com'era prima di giocare ieri sera". I file
  uguali tra un'istantanea e l'altra occupano spazio una volta sola.
- **Avviso se il gioco è aperto su un altro PC**: notifica appena lo apri; avviando da Steam
  con le opzioni di avvio, SaveSync chiede conferma prima di far partire il gioco.
- **Modalità "solo backup"** per i giochi che hanno già il cloud di Steam/Epic/GOG: SaveSync
  tiene copie e versioni ma non scarica mai nella cartella del gioco. *Rileva giochi* mostra
  quali giochi hanno già il cloud dello store e chiede che modalità usare.
- **Aggiornamento automatico**: SaveSync controlla le Release su GitHub e propone
  "Aggiorna a x.y.z"; un clic e si aggiorna e riapre da solo.
- **Dettagli del gioco** (doppio clic): ultimo salvataggio e da quale PC, spazio in locale e
  sul cloud, istantanee, chi lo sta giocando, cronologia delle sincronizzazioni.
- **Anteprima nel ripristino**: per ogni versione mostra lo screenshot e i dati del
  salvataggio (livello, luogo, tempo di gioco...) quando il gioco li salva.
- **Limite di spazio per gioco** e pulsante *Pulisci versioni vecchie*: i salvataggi attuali
  non vengono mai toccati.

## 0.3.1

- **Giochi avviati da Vortex, Mod Organizer o altri launcher ora vengono riconosciuti.**
  Prima SaveSync cercava solo l'exe indicato dal database, che spesso è il launcher
  (es. `redprelauncher.exe` per Cyberpunk 2077, `start_protected_game.exe` per Elden Ring):
  se il gioco partiva in un altro modo non si accorgeva che stavi giocando.
  Ora conta come "in gioco" qualsiasi exe che parte dalla cartella d'installazione del gioco.
- La cartella d'installazione viene trovata da sola per i giochi di Steam, Epic e GOG, anche
  per quelli già aggiunti con le versioni precedenti.
- Per i giochi con anti-cheat di cui Windows non rivela il percorso dell'exe, il confronto
  avviene sui nomi degli exe presenti nella cartella del gioco.

## 0.3.0

**Aggiorna SaveSync su tutti i PC**: il formato sul cloud cambia. La 0.3.0 legge e converte
da sola i dati della 0.2.0, ma una 0.2.0 rimasta su un altro PC non troverebbe i file nuovi.

Correzioni trovate con una simulazione di due PC su una cartella cloud (Proton Drive,
OneDrive, Dropbox) che consegna i file in ritardo e in ordine sparso:

- **Salvataggio più recente sostituito da uno più vecchio**: se il client cloud consegnava in
  ritardo un manifest vecchio, SaveSync scaricava il salvataggio vecchio sopra quello nuovo.
  Ora un salvataggio non viene mai sostituito da uno più vecchio.
- **PC bloccati in "hash non corrisponde"**: se due PC caricavano lo stesso file quasi insieme,
  il cloud poteva tenere il contenuto di uno e il manifest dell'altro, per sempre. Ora ogni
  versione è un file a sé (`blobs/<file>/<data>__<pc>__<hash>`) e non si sovrascrive mai nulla.
- **File bloccato dal gioco scambiato per cancellato** (frequente su Windows): con "propaga
  cancellazioni" attivo poteva cancellarlo dal cloud.
- **Backup che cancellavano la cronologia**: un download fallito creava a ogni tentativo un
  backup identico, finché le 10 versioni tenute erano tutte uguali.
- **Cartella sparita = cancella tutto**: se la cartella di un gioco si svuota di colpo (disco
  scollegato, gioco reinstallato) le cancellazioni non vengono propagate.
- **Cambio cartella di un gioco**: ora riparte pulito invece di considerare "cancellati" i file.
- **Cartella cloud non montata** (es. Proton Drive chiuso): prima SaveSync creava una cartella
  locale finta e sincronizzava nel vuoto; ora aspetta che torni disponibile.
- La cartella di un gioco creata dalla prima sincronizzazione ora viene osservata subito.
- Impostazioni: rifiutati valori assurdi (controllo ogni 0 secondi, 0 versioni, nome PC vuoto).
- Launcher: se il cloud non è configurato avvia il gioco senza scrivere file a caso.

## 0.2.0

Prima versione per Windows.
