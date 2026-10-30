"""Modalità servizio, senza finestre: per lo Steam Deck in modalità gioco (o per un PC
senza schermo). Si configura una volta dalla finestra, poi gira come servizio di sistema.

Uso:  ResoSync --service
"""
import logging
import logging.handlers
import signal
import threading

from .backends import make_backend
from .config import Config
from .engine import SyncEngine
from .service import SyncService

RETRY = 60  # secondi tra un tentativo e l'altro se il cloud non è raggiungibile (rete spenta)


def setup_log(app_dir) -> logging.Logger:
    app_dir.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("resosync")
    log.setLevel(logging.INFO)
    target = str((app_dir / "resosync.log").resolve())
    if any(getattr(h, "baseFilename", target) != target for h in log.handlers):
        for h in list(log.handlers):  # cartella dati diversa (succede nei test): ricomincio
            log.removeHandler(h)
            h.close()
    if not log.handlers:
        h = logging.handlers.RotatingFileHandler(app_dir / "resosync.log", maxBytes=1_000_000,
                                                 backupCount=3, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S"))
        log.addHandler(h)
        log.addHandler(logging.StreamHandler())  # finisce anche nel journal di systemd
    return log


def run(stop_event=None) -> int:
    cfg = Config.load()
    log = setup_log(cfg.app_dir)
    stop = stop_event or threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *a: stop.set())
        except ValueError:
            pass  # non nel thread principale (test)

    if not cfg.is_configured():
        log.error("Cloud non configurato: apri ResoSync e completa le impostazioni")
        return 2

    def on_event(kind, *args):
        if kind == "log":
            log.info(args[0])
        elif kind == "status" and args[1].startswith(("Errore", "In attesa")):
            log.info(f"[{args[0]}] {args[1]}")
        elif kind == "notify":
            log.warning(f"{args[0]}: {args[1]}")
        elif kind == "confirm_first":
            game = cfg.get_game(args[0])
            log.warning(f"[{game.name if game else args[0]}] prima sincronizzazione con "
                        f"salvataggi diversi qui e sul cloud: apri ResoSync (modalità desktop) "
                        f"per vedere cosa succede e confermare")

    def connect():
        """Backend + motore + servizio; riprova finché il cloud non risponde."""
        while not stop.is_set():
            try:
                backend = make_backend(cfg.settings, cfg.vault)
                engine = SyncEngine(cfg.settings, backend, cfg.app_dir, log=log.info)
                svc = SyncService(cfg, engine, on_event)
                svc.start()
                log.info(f"Servizio avviato ({len(cfg.games)} giochi, "
                         f"PC: {cfg.settings.device_name})")
                return svc
            except Exception as e:  # rete non ancora pronta all'avvio, login scaduto, ...
                log.warning(f"Cloud non raggiungibile ({e}): riprovo tra {RETRY} secondi")
                stop.wait(RETRY)
        return None

    def cloud_key():
        s = cfg.settings
        return (cfg.cloud_identity(), s.encrypt, s.passphrase, s.rclone_path, s.rclone_config)

    service = connect()
    current = cloud_key()
    config_mtime = _mtime(cfg.file)
    while not stop.wait(5):
        # configurazione cambiata dalla finestra: ricarico giochi e impostazioni
        if _mtime(cfg.file) == config_mtime:
            continue
        config_mtime = _mtime(cfg.file)
        try:
            fresh = Config.load(cfg.app_dir)
        except Exception as e:  # noqa: BLE001 - file a metà: riprovo al prossimo giro
            log.warning(f"Configurazione non leggibile ({e}): riprovo")
            config_mtime = 0.0
            continue
        cfg.games[:] = fresh.games
        for k, v in vars(fresh.settings).items():
            setattr(cfg.settings, k, v)
        if cloud_key() != current:
            # cloud diverso: il backend vecchio scriverebbe ancora sul cloud di prima
            current = cloud_key()
            log.info("Cloud cambiato: mi ricollego")
            if service:
                service.stop()
            service = connect()
        elif service:
            service.rewatch()
            service.request_sync()
        log.info("Configurazione ricaricata")
    if service:
        service.stop()
    log.info("Servizio fermato")
    return 0


def _mtime(path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
