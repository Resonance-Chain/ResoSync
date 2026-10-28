"""Servizio in background: guarda cartelle e processi dei giochi e lancia le sync quando serve."""
import os
import threading
import time
from pathlib import Path

from .config import process_names
from .engine import CloudBusy

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    HAVE_WATCHDOG = True
except ImportError:  # senza watchdog si va a polling, funziona lo stesso
    HAVE_WATCHDOG = False
    FileSystemEventHandler = object

try:
    import psutil
except ImportError:
    psutil = None

BUSY_RETRY = 15  # secondi prima di riprovare se un altro PC tiene il lock


def running_process_names() -> set:
    if psutil is None:
        return set()
    names = set()
    me = os.getpid()
    for p in psutil.process_iter(["name"]):
        if p.pid == me:
            continue
        n = (p.info.get("name") or "").lower()
        names.add(n[:-4] if n.endswith(".exe") else n)
    return names


class _Handler(FileSystemEventHandler):
    def __init__(self, service, game_id):
        self.service, self.game_id = service, game_id

    def on_any_event(self, event):
        if event.event_type in ("opened", "closed_no_write"):
            return  # semplici letture (anche le nostre), niente da sincronizzare
        path = str(getattr(event, "dest_path", "") or event.src_path)
        if path.endswith(".savesync-tmp"):
            return  # sono i nostri download
        if not event.is_directory:
            self.service.notify(self.game_id)


class SyncService:
    def __init__(self, config, engine, on_event):
        """on_event(kind, *args):
        'status' (game_id, testo) | 'log' (testo) | 'notify' (titolo, testo) | 'error' (exc)"""
        self.config = config
        self.engine = engine
        self.on_event = on_event
        self._pending = {}         # game_id -> istante in cui far partire la sync
        self._forced = set()       # sync da fare anche se il gioco è aperto / auto sync spento
        self._playing = {}         # game_id -> True se il processo del gioco è attivo
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._observer = None
        self._snapshots = {}
        self._thread = threading.Thread(target=self._loop, daemon=True)

    # ---------- API ----------
    def start(self):
        self._watch()
        self._thread.start()
        self.request_sync()  # giro iniziale

    def stop(self):
        self._stop.set()
        if self._observer:
            self._observer.stop()

    def is_playing(self, game_id) -> bool:
        return self._playing.get(game_id, False)

    def notify(self, game_id):
        """Un file è cambiato: aspetta che il gioco finisca di scrivere, poi sincronizza."""
        if self.is_playing(game_id) and self.config.settings.sync_only_after_exit:
            self.on_event("status", game_id, "In gioco: sync all'uscita")
            return
        with self._lock:
            self._pending[game_id] = time.time() + self.config.settings.debounce_seconds
        self.on_event("status", game_id, "Modifiche rilevate…")

    def request_sync(self, game_id=None, delay=0.0):
        ids = [game_id] if game_id else [g.id for g in self.config.games if g.enabled]
        with self._lock:
            for gid in ids:
                self._pending[gid] = time.time() + delay
                self._forced.add(gid)

    def rewatch(self):
        self._watch()

    # ---------- interni ----------
    def _watch(self):
        if not HAVE_WATCHDOG:
            return
        if self._observer:
            self._observer.stop()
        self._observer = Observer()
        for g in self.config.games:
            if g.enabled and Path(g.path).is_dir():
                try:
                    self._observer.schedule(_Handler(self, g.id), g.path, recursive=True)
                except OSError as e:
                    self.on_event("log", f"[{g.name}] impossibile osservare la cartella: {e}")
        self._observer.daemon = True
        self._observer.start()

    def _poll_local_changes(self):
        """Fallback senza watchdog: confronta dimensioni/date dei file."""
        for g in self.config.games:
            if not g.enabled:
                continue
            snap = {}
            for dirpath, _, files in os.walk(g.path):
                for f in files:
                    try:
                        st = os.stat(os.path.join(dirpath, f))
                        snap[os.path.join(dirpath, f)] = (st.st_size, st.st_mtime_ns)
                    except OSError:
                        pass
            if g.id in self._snapshots and self._snapshots[g.id] != snap:
                self.notify(g.id)
            self._snapshots[g.id] = snap

    def _check_processes(self):
        games = [g for g in self.config.games if g.enabled and g.process]
        if not games:
            return
        try:
            running = running_process_names()
        except Exception:
            return
        for g in games:
            now = bool(process_names(g.process) & running)
            was = self._playing.get(g.id, False)
            self._playing[g.id] = now
            if now and not was:
                # Appena avviato: scarico subito l'ultima versione (spesso il gioco carica
                # il salvataggio solo dal menu, quindi si fa ancora in tempo)
                self.on_event("log", f"[{g.name}] gioco avviato")
                self.request_sync(g.id)
            elif was and not now:
                self.on_event("log", f"[{g.name}] gioco chiuso, carico i salvataggi")
                self.request_sync(g.id, delay=2)

    def _loop(self):
        last_full = time.time()
        last_poll = last_proc = 0
        while not self._stop.is_set():
            now = time.time()
            s = self.config.settings
            if s.auto_sync and now - last_full >= s.poll_interval:
                last_full = now
                with self._lock:
                    for g in self.config.games:
                        if g.enabled and not (self.is_playing(g.id) and s.sync_only_after_exit):
                            self._pending.setdefault(g.id, 0)
            if s.auto_sync and not HAVE_WATCHDOG and now - last_poll >= 3:
                last_poll = now
                self._poll_local_changes()
            if now - last_proc >= 3:
                last_proc = now
                self._check_processes()

            with self._lock:
                due = [gid for gid, t in self._pending.items()
                       if t <= now and (s.auto_sync or gid in self._forced)]
                for gid in due:
                    del self._pending[gid]
                    self._forced.discard(gid)
            for gid in due:
                self._run(gid)
            self._stop.wait(0.5)

    def _run(self, gid):
        game = self.config.get_game(gid)
        if not game:
            return
        self.on_event("status", gid, "Sincronizzazione…")
        try:
            st = self.engine.sync_game(game)
        except CloudBusy as e:
            self.on_event("status", gid, f"In attesa: {e}")
            self.request_sync(gid, delay=BUSY_RETRY)
            return
        except Exception as e:
            self.on_event("status", gid, f"Errore: {e}")
            self.on_event("log", f"[{game.name}] sync fallita: {e}")
            self.on_event("error", e)
            return
        parts = [f"↑{st['up']}" if st["up"] else "", f"↓{st['down']}" if st["down"] else "",
                 f"⚠{st['conflicts']} conflitti" if st["conflicts"] else "",
                 f"{st['errors']} errori" if st["errors"] else ""]
        extra = " ".join(p for p in parts if p)
        playing = " (in gioco)" if self.is_playing(gid) else ""
        self.on_event("status", gid, f"OK {time.strftime('%H:%M:%S')} {extra}{playing}".strip())
        if st["conflicts"]:
            self.on_event("notify", f"{game.name}: conflitto",
                          "Salvataggio modificato su due PC. Ho tenuto il più recente, l'altro "
                          "è in Ripristina versione.")
        if st["errors"]:
            # di solito file bloccati dal gioco: riprovo tra poco
            self.request_sync(gid, delay=30)
