# Novità

(Fino alla 0.4.0 il progetto si chiamava SaveSync.)

## 0.5.7 (in sviluppo)

- **Google Drive, PC che non si vedono più**: Drive permette due file con lo stesso nome. Se due
  PC creavano lo stesso file nello stesso momento (configurati insieme, prima sync di un
  gioco), ognuno usava la sua copia: non si vedevano più le modifiche a vicenda e il lock era di
  tutti e due. Ora vale sempre la copia più vecchia e il doppione sparisce.
- **Gioco riaggiunto senza connessione**: lo stato delle sync vecchio restava, e i file che
  c'erano allora e oggi mancano sembravano cancellati (con "propaga cancellazioni" sparivano dal
  cloud). Ora si azzera anche offline.

## 0.5.6

- **Verifica del cloud, falsi allarmi**: con Proton Drive / OneDrive un salvataggio appena
  caricato da un altro PC può non essere ancora arrivato. La verifica lo dava per rovinato e
  mandava l'avviso; ora è "in arrivo" e si ricontrolla dopo.
- **Verifica del cloud, servizio bloccato**: con salvataggi grandi la verifica durava minuti e
  in quel tempo ResoSync non si accorgeva dei giochi aperti (niente download prima di giocare,
  niente protezione durante la partita). Ora gira a parte e la sync non la aspetta.
- **Anteprima della prima sync e registro**: per i giochi che salvano nel registro di Windows
  il conflitto non compariva nell'anteprima e veniva deciso senza chiedere.
- **Log**: l'avviso sui file che differiscono solo per le maiuscole si ripeteva ogni 30 secondi.

## 0.5.5

Novità per la sicurezza dei salvataggi:

- **Salvataggi nel registro di Windows**: i giochi che salvano lì (alcuni giochi Unity, molti
  giochi vecchi) ora vengono trovati da *Rileva giochi* e sincronizzati. Le chiavi vengono
  esportate in un file nella cartella del gioco, con versioni, conflitti e istantanee come gli
  altri salvataggi; quando arriva dal cloud o lo ripristini, torna nel registro. Un PC su cui
  il gioco non è mai stato aperto non sovrascrive mai il salvataggio degli altri.
- **Verifica del cloud**: ogni settimana (impostabile) ResoSync controlla che ogni salvataggio
  sul cloud ci sia e sia integro. Se uno è rovinato lo ricarica da una copia buona (il file qui,
  i backup, le istantanee); se non c'è nessuna copia ti avvisa. Anche a mano dal menu del gioco.
- **Anteprima della prima sincronizzazione**: su un PC nuovo (o un cloud nuovo), se ci sono
  salvataggi diversi qui e sul cloud, ResoSync non decide da solo: mostra quale versione
  terrebbe per ognuno e chiede conferma. Prima fa sempre un'istantanea della cartella.

Bug sistemati:

- **Cifratura su un PC nuovo**: se il programma del cloud non aveva ancora scaricato
  crypto.json, ResoSync ne creava uno nuovo che sovrascriveva quello vero, e tutti i
  salvataggi sul cloud diventavano illeggibili. Ora aspetta che arrivi.
- **Chiusura improvvisa a metà sync** (spegnimento, crash, aggiornamento): uno slot da più file
  poteva restare mescolato e al giro dopo veniva caricato sul cloud come vincente. Ora ResoSync
  se ne accorge, rimette lo slot com'era e rifà il lavoro. In più, chiudendo il programma
  aspetta che la sync in corso finisca.
- **Sync lunghe**: il lock sul cloud scadeva dopo 2 minuti anche se la sync stava ancora
  lavorando (salvataggi grandi, rete lenta), e un altro PC poteva entrare a metà. Ora si
  rinnova finché serve.
- **File e cartella con lo stesso nome** (es. `Saves/x` prima, `Saves` poi): la pulizia delle
  versioni di uno cancellava le versioni dell'altro, e i backup locali si bloccavano.
- **Maiuscole e minuscole**: `Slot.sav` e `slot.sav` sul cloud (uno arrivato dal Deck) su
  Windows sono lo stesso file e venivano scaricati a turno uno sopra l'altro. Ora se ne
  sincronizza uno e l'altro viene segnalato.

## 0.5.4

- **rclone**: nuovo tipo di cloud. Con [rclone](https://rclone.org) ResoSync arriva a più di 70
  servizi, **Proton Drive compreso, anche sullo Steam Deck**: il Deck può usare direttamente la
  stessa cartella Proton Drive del PC (vedi [STEAMDECK.md](STEAMDECK.md)). Un errore di rclone
  (rete giù, login scaduto) non viene mai scambiato per "cloud vuoto".
- **Servizio fermo senza avvisare**: un errore imprevisto nel giro del servizio lo faceva
  morire, e ResoSync smetteva di sincronizzare e fare backup senza dire niente. Ora l'errore va
  nel log e il servizio continua.
- **Steam Deck, cambio di cloud**: il servizio in background continuava a usare il cloud
  vecchio dopo che l'avevi cambiato. Ora si ricollega a quello nuovo.
- **Impostazioni**: scegliendo un cloud senza completarlo, la finestra cancellava comunque lo
  stato delle sincronizzazioni. Ora non cambia niente finché la configurazione non è completa.
- **Steam Deck, giochi sulla microSD**: togliendo la scheda, ResoSync ricreava la cartella dei
  salvataggi in memoria e ci riscaricava tutto, e la scheda reinserita non si montava più al
  suo posto. Ora aspetta che la scheda torni.
- **Ripristina versione con il gioco aperto**: il gioco sovrascriveva la versione ripristinata
  al primo salvataggio automatico. Ora chiede di chiuderlo prima, come per le istantanee.
- **rclone**: con il file di configurazione protetto da password restava appeso fino al
  timeout (ora lo spiega subito), e i salvataggi grandi su connessioni lente venivano
  interrotti ogni volta (ora il tempo cresce con la dimensione).

## 0.5.3

Caccia ai bug con un solo obiettivo: **nessun modo di perdere un salvataggio o di rovinarlo**.

- **Il gioco salva mentre ResoSync lavora**: se il file cambiava durante il caricamento,
  sul cloud poteva finire un salvataggio a metà (che poi l'altro PC scaricava). E se il gioco
  salvava mentre arrivava la versione dell'altro PC, il salvataggio appena fatto veniva
  sovrascritto. Ora ResoSync se ne accorge, non tocca niente e riprova al giro dopo.
- **Salvataggi da più file** (Cyberpunk e simili): se la sostituzione di un file si bloccava a
  metà (antivirus, client cloud), restava il sav.dat nuovo con il metadata vecchio. Ora si
  torna indietro e lo slot resta com'era, intero.
- **Orologio sbagliato** (Steam Deck rimasto offline, ora sballata): un salvataggio nuovo con
  la data "nel passato" veniva scambiato per un ritorno indietro del cloud e scartato. Ora ogni
  caricamento ricorda da quali versioni discende, e se discende dalla tua è più nuovo,
  qualunque cosa dica l'orologio.
- **Avvio dal launcher / Steam**: con le cartelle cloud il manifest arriva spesso prima del
  salvataggio vero, e il gioco partiva con il salvataggio vecchio senza dire niente. Ora
  ResoSync aspetta fino a 30 secondi che arrivi, e se non arriva te lo chiede.
- **Cancellazioni di massa**: la protezione scattava solo se la cartella si svuotava del tutto.
  Ora anche se sparisce più di metà dei salvataggi in un colpo (gioco reinstallato che lascia
  solo il config) le cancellazioni non si propagano agli altri PC.
- **Ripristino di una versione**: una versione rovinata sul cloud finiva nella cartella del
  gioco, e un errore a metà copia di un backup locale lasciava il salvataggio troncato. Ora la
  versione si verifica prima e la copia è sicura.
- **Ripristino di un'istantanea**: ora controlla tutto prima di toccare qualcosa. Un contenuto
  rovinato non si rimette, e un file attuale si sovrascrive solo se ne esiste una copia.
- **Modifiche "invisibili"**: un programma che riscriveva il salvataggio con la stessa
  dimensione e la stessa data non veniva notato. Ora su Linux / Steam Deck sì.

## 0.5.2

Seconda caccia ai bug, soprattutto sulle **cancellazioni** (finora mai simulate):

- **Salvataggio cancellato dal disco con "propaga cancellazioni" attivo**: se un altro
  dispositivo aveva cancellato uno slot e tu poi ci avevi salvato sopra, il vecchio "cancellato"
  arrivato in ritardo dal cloud eliminava il tuo salvataggio più recente. Ora una cancellazione
  più vecchia del tuo salvataggio non vale.
- **Ricompariva un salvataggio vecchio**: cancellando uno slot (senza propagazione) mentre il
  cloud mostrava per errore una versione più vecchia, veniva rimessa quella vecchia. Ora viene
  rimessa la versione più recente che il dispositivo conosceva.
- **Slot da più file cancellati** (es. una cartella di Cyberpunk): ora rimessi, cancellati o
  sostituiti sempre interi, mai a metà.
- **Steam Deck**: passando alla modalità gioco con la finestra di ResoSync aperta, il servizio
  restava fermo e in modalità gioco non si sincronizzava più niente fino al riavvio. Ora riparte
  da solo.
- **Proton Drive / OneDrive su Windows**: se il loro programma teneva aperto un file per un
  attimo, la lettura falliva con un errore. Ora riprova.
- **Istantanee**: un file sparito o bloccato durante la copia faceva fallire l'istantanea
  intera; ora viene saltato solo quel file. Due istantanee nello stesso momento non si
  intralciano più.
- **config.json rovinato** (es. disco pieno): ResoSync non partiva più. Ora usa l'ultima copia
  buona e mette da parte quella rovinata.

## 0.5.1

Correzioni, ognuna con il suo test:

- **Slot indipendenti nella stessa cartella** (es. `Saves/slot1.sav`, `Saves/slot2.sav`): la
  0.5.0 li trattava come un unico salvataggio, quindi modificando slot diversi su due
  dispositivi uno dei due finiva solo nei backup. Ora "una sottocartella = un salvataggio" si
  usa solo quando la struttura lo dimostra (es. Cyberpunk); altrimenti ogni file per conto suo.
- **Versioni appena caricate da un altro dispositivo** potevano essere cancellate dal limite di
  spazio o dalla pulizia, se questo dispositivo non vedeva ancora il manifest nuovo (cartelle
  cloud in ritardo). Ora le versioni delle ultime 2 ore non si cancellano mai.
- **Fusi orari diversi** (es. Steam Deck rimasto su UTC): l'ordine delle versioni era sbagliato
  di qualche ora. Ora i nomi delle versioni sono in UTC.
- **Mentre giochi non carico nulla**: dopo un errore (es. file bloccato dal gioco) i tentativi
  automatici caricavano comunque durante la partita. Ora aspettano che il gioco si chiuda.
- L'istantanea all'avvio del gioco (lenta la prima volta) ritardava il download dell'ultimo
  salvataggio: ora gira in parallelo.
- **Steam Deck**: il rilevamento nei prefissi Proton ora ignora maiuscole e minuscole (Windows
  non le distingue, Linux sì: alcuni giochi non venivano trovati). Anche i filtri dei file
  (`*.SAV`) ora ignorano maiuscole e minuscole.
- File temporanei rimasti da un download interrotto non vengono più caricati come salvataggi.
- Riselezionando la vecchia cartella cloud `SaveSync` dopo il cambio di nome non si crea più un
  cloud nuovo e vuoto dentro di essa.
- Avviato dalla copia `SaveSync.exe`, ResoSync ora registra avvio automatico e opzioni di avvio
  col nome nuovo.

## 0.5.0

- **SaveSync ora si chiama ResoSync.** Aggiornando non si perde niente: i dati restano nella
  cartella `.savesync` se esiste già, le password vengono spostate da sole sotto il nome nuovo,
  l'avvio automatico e il servizio del Deck vengono convertiti al primo avvio, e le opzioni di
  avvio di Steam che puntano a `SaveSync.exe` (o `savesync-app/SaveSync` sul Deck) continuano
  a funzionare.
- **Un salvataggio si sincronizza tutto insieme.** Per i giochi che salvano più file per ogni
  salvataggio (Cyberpunk: `ManualSave-12/` con `sav.dat`, `metadata.json`, `screenshot.png`;
  Skyrim: `Save1.ess` + `Save1.skse`) SaveSync decide e applica il salvataggio intero: in caso
  di conflitto vince il salvataggio più recente *completo*, e se il cloud non ha ancora
  consegnato tutti i file aspetta invece di applicarne metà. Mai lo screenshot di una partita
  con i dati di un'altra.
- Scelta per gioco ("Un salvataggio è: una sottocartella / i file con lo stesso nome / ogni file
  da solo"), suggerita in automatico guardando la cartella.
- **Ripristina tutto il salvataggio**: da *Ripristina versione* rimette insieme tutti i file
  dello stesso momento.
- Il formato sul cloud non cambia: funziona insieme alla 0.4.0 sugli altri PC.

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
