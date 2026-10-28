# Novità

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
