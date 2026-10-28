"""Modalità servizio, senza finestre: per lo Steam Deck in modalità gioco (o per un PC
senza schermo). Si configura una volta dalla finestra, poi gira come servizio di sistema.

Uso:  SaveSync --service
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
    log = logging.getLogger("savesync")
    log.setLevel(logging.INFO)
    if not log.handlers:
        h = logging.handlers.RotatingFileHandler(app_dir / "savesync.log", maxBytes=1_000_000,
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
        log.error("Cloud non configurato: apri SaveSync e completa le impostazioni")
        return 2

    def on_event(kind, *args):
        if kind == "log":
            log.info(args[0])
        elif kind == "status" and args[1].startswith(("Errore", "In attesa")):
            log.info(f"[{args[0]}] {args[1]}")
        elif kind == "notify":
            log.warning(f"{args[0]}: {args[1]}")

    service = None
    while not stop.is_set():
        try:
            backend = make_backend(cfg.settings, cfg.vault)
            engine = SyncEngine(cfg.settings, backend, cfg.app_dir, log=log.info)
            service = SyncService(cfg, engine, on_event)
            service.start()
            log.info(f"Servizio avviato ({len(cfg.games)} giochi, PC: {cfg.settings.device_name})")
            break
        except Exception as e:  # rete non ancora pronta all'avvio, login scaduto, ...
            log.warning(f"Cloud non raggiungibile ({e}): riprovo tra {RETRY} secondi")
            stop.wait(RETRY)

    config_mtime = _mtime(cfg.file)
    while not stop.wait(5):
        # configurazione cambiata dalla finestra: ricarico giochi e impostazioni
        if _mtime(cfg.file) != config_mtime:
            config_mtime = _mtime(cfg.file)
            fresh = Config.load(cfg.app_dir)
            cfg.games[:] = fresh.games
            for k, v in vars(fresh.settings).items():
                setattr(cfg.settings, k, v)
            if service:
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
