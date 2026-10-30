"""Servizio in background: guarda cartelle e processi dei giochi e lancia le sync quando serve."""
import os
import re
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

BUSY_RETRY = 15   # secondi prima di riprovare se un altro PC tiene il lock
ERROR_RETRY = 30  # secondi prima di riprovare dopo un errore (es. file bloccato dal gioco)


def wine_exe(cmdline) -> tuple:
    """Giochi Windows sotto Proton/Wine (Steam Deck, Linux): il processo si chiama wine o ha
    il nome tagliato a 15 caratteri, ma nella riga di comando c'è il percorso dell'exe
    (Z:\\home\\deck\\...\\Gioco.exe oppure /home/deck/.../Gioco.exe).
    Ritorna (nome senza .exe, percorso Linux o "") oppure None."""
    for arg in cmdline or []:
        if not arg.lower().endswith(".exe"):
            continue
        name = re.split(r"[\\/]", arg)[-1].lower()[:-4]
        if re.match(r"^[zZ]:\\", arg):
            path = "/" + arg[3:].replace("\\", "/")
        elif arg.startswith("/"):
            path = arg
        else:
            path = ""  # C:\\... dentro il prefisso: basta il nome
        return name, os.path.normcase(os.path.normpath(path)) if path else ""
    return None


def running_processes(with_paths=True) -> list:
    """[(nome senza .exe in minuscolo, percorso dell'exe normalizzato o "")]"""
    if psutil is None:
        return []
    out = []
    me = os.getpid()
    # Escludo me stesso e i miei "antenati": avviando da Steam con le opzioni di avvio,
    # il processo padre (reaper/proton) ha nella riga di comando il percorso del gioco e
    # vive finché vivo io: contarlo farebbe aspettare il launcher per sempre.
    skip = {me}
    try:
        skip |= {a.pid for a in psutil.Process(me).parents()}
    except Exception:
        pass
    unix = os.name != "nt"
    attrs = ["name", "exe"] if with_paths else ["name"]
    if unix:
        attrs.append("cmdline")
    for p in psutil.process_iter(attrs):
        if p.pid in skip:
            continue
        n = (p.info.get("name") or "").lower()
        exe = p.info.get("exe") or "" if with_paths else ""
        out.append((n[:-4] if n.endswith(".exe") else n,
                    os.path.normcase(os.path.normpath(exe)) if exe else ""))
        if unix:
            w = wine_exe(p.info.get("cmdline"))
            if w:
                out.append((w[0], w[1] if with_paths else ""))
    return out


def running_process_names() -> set:
    return {n for n, _ in running_processes(with_paths=False)}


# exe che si trovano in tante cartelle di giochi: non bastano a dire "questo gioco è aperto"
_GENERIC_EXE = re.compile(r"crash|report|unins|setup|install|redist|vc_?redist|dxsetup|"
                          r"prereq|helper|update|launcherpatcher|easyanticheat|battleye|"
                          r"be_service|eac_?launcher|cefprocess|webhelper|ue4prereq|dotnet",
                          re.IGNORECASE)
_exe_cache = {}  # cartella -> (istante, {nomi exe})


def install_exe_names(folder: str, max_files=5000, ttl=600) -> set:
    """Nomi degli exe nella cartella d'installazione (per i processi di cui Windows non
    rivela il percorso, es. giochi con anti-cheat protetto)."""
    now = time.time()
    hit = _exe_cache.get(folder)
    if hit and now - hit[0] < ttl:
        return hit[1]
    names, seen = set(), 0
    for dirpath, dirs, files in os.walk(folder):
        for f in files:
            seen += 1
            low = f.lower()
            if low.endswith(".exe") and not _GENERIC_EXE.search(low):
                names.add(low[:-4])
        if seen > max_files:
            break
    _exe_cache[folder] = (now, names)
    return names


def is_game_running(game, procs) -> bool:
    """Il gioco è aperto se c'è un processo col nome indicato, oppure un qualsiasi exe che
    parte dalla sua cartella d'installazione (così funziona con Vortex, Mod Organizer,
    launcher che si chiudono subito, exe con nomi diversi da quelli del database)."""
    names = process_names(game.process)
    folder = os.path.normcase(os.path.normpath(game.install_dir)) if game.install_dir else ""
    for name, exe in procs:
        if name in names:
            return True
        if folder and exe and exe.startswith(folder + os.sep):
            return True
    if folder:  # percorso non disponibile (processo protetto): confronto i nomi
        installed = install_exe_names(game.install_dir)
        return any(not exe and name in installed for name, exe in procs)
    return False


class _Handler(FileSystemEventHandler):
    def __init__(self, service, game_id):
        self.service, self.game_id = service, game_id

    def on_any_event(self, event):
        if event.event_type in ("opened", "closed_no_write"):
            return  # semplici letture (anche le nostre), niente da sincronizzare
        path = str(getattr(event, "dest_path", "") or event.src_path)
        if path.endswith(".resosync-tmp"):
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
        self._forced = set()       # sync da fare anche con la sincronizzazione automatica spenta
        self._allow_playing = set()  # sync da fare anche se il gioco è aperto
        self._playing = {}         # game_id -> True se il processo del gioco è attivo
        self._heartbeat = {}       # game_id -> ultimo aggiornamento di "sto giocando"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._observer = None
        self._snapshots = {}
        self._watched = set()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    # ---------- API ----------
    def start(self):
        self._watch()
        self._thread.start()
        self.request_sync()  # giro iniziale

    def stop(self, wait=30.0):
        """Ferma il servizio. Una sync in corso si lascia finire (fino a `wait` secondi):
        uscita, aggiornamento o spegnimento non devono interromperla a metà."""
        self._stop.set()
        if self.engine.lock.acquire(timeout=wait):
            self.engine.lock.release()
        for gid, playing in list(self._playing.items()):
            game = self.config.get_game(gid)
            if playing and game:
                self._presence(game, False)
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

    def request_sync(self, game_id=None, delay=0.0, while_playing=False):
        """while_playing=True: anche se il gioco è aperto (sync all'avvio del gioco, o chiesta
        a mano). Le altre (tentativi dopo un errore, ...) aspettano che il gioco si chiuda."""
        ids = [game_id] if game_id else [g.id for g in self.config.games if g.enabled]
        with self._lock:
            for gid in ids:
                self._pending[gid] = time.time() + delay
                self._forced.add(gid)
                if while_playing:
                    self._allow_playing.add(gid)

    def rewatch(self):
        self._watch()

    # ---------- interni ----------
    def _watch(self):
        if not HAVE_WATCHDOG:
            return
        if self._observer:
            self._observer.stop()
        self._observer = Observer()
        self._watched = set()
        for g in self.config.games:
            if g.enabled and Path(g.path).is_dir():
                try:
                    self._observer.schedule(_Handler(self, g.id), g.path, recursive=True)
                    self._watched.add(g.id)
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
        games = [g for g in self.config.games if g.enabled and (g.process or g.install_dir)]
        if not games:
            return
        try:
            procs = running_processes(with_paths=any(g.install_dir for g in games))
        except Exception:
            return
        for g in games:
            now = is_game_running(g, procs)
            was = self._playing.get(g.id, False)
            self._playing[g.id] = now
            if now and not was:
                self.on_event("log", f"[{g.name}] gioco avviato")
                self._game_started(g)
                # scarico subito l'ultima versione (spesso il gioco carica il salvataggio
                # solo dal menu, quindi si fa ancora in tempo)
                self.request_sync(g.id, while_playing=True)
            elif was and not now:
                self.on_event("log", f"[{g.name}] gioco chiuso, carico i salvataggi")
                self._presence(g, False)
                self.request_sync(g.id, delay=2)
            elif now and time.time() - self._heartbeat.get(g.id, 0) > 120:
                self._presence(g, True)

    def _game_started(self, g):
        # 1. istantanea della cartella: "com'era prima di giocare". In parallelo: la prima
        # volta può copiare GB e non deve ritardare il download dell'ultimo salvataggio
        def snap():
            try:
                if self.engine.snapshot(g, "avvio del gioco"):
                    self.on_event("log", f"[{g.name}] istantanea della cartella salvata")
            except Exception as e:
                self.on_event("log", f"[{g.name}] istantanea non riuscita: {e}")
        threading.Thread(target=snap, daemon=True).start()
        # 2. qualcun altro sta giocando allo stesso gioco?
        try:
            others = self.engine.others_playing(g)
        except Exception:
            others = []
        if others:
            who = ", ".join(others)
            self.on_event("log", f"[{g.name}] ATTENZIONE: aperto anche su {who}")
            self.on_event("notify", f"{g.name} è aperto anche su {who}",
                          "Se salvate nello stesso slot su tutti e due, alla fine vince il "
                          "salvataggio più recente (l'altro resta in Ripristina versione).")
        self._presence(g, True)

    def _presence(self, g, playing):
        try:
            self.engine.set_playing(g, playing)
            self._heartbeat[g.id] = time.time() if playing else 0
        except Exception:
            pass  # cloud irraggiungibile: riprovo al prossimo giro

    def _loop(self):
        self._last = {"full": time.time(), "poll": 0, "proc": 0}
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001
                # mai far morire il thread: smetterebbe di sincronizzare senza dire niente
                try:
                    self.on_event("log", f"Errore imprevisto nel servizio (continuo): {e!r}")
                except Exception:
                    pass
            self._stop.wait(0.5)

    def _tick(self):
        now = time.time()
        s = self.config.settings
        last = self._last
        if s.auto_sync and now - last["full"] >= s.poll_interval:
            last["full"] = now
            with self._lock:
                for g in self.config.games:
                    if g.enabled and not (self.is_playing(g.id) and s.sync_only_after_exit):
                        self._pending.setdefault(g.id, 0)
        if s.auto_sync and not HAVE_WATCHDOG and now - last["poll"] >= 3:
            last["poll"] = now
            self._poll_local_changes()
        if now - last["proc"] >= 3:
            last["proc"] = now
            self._check_processes()

        with self._lock:
            due = [gid for gid, t in self._pending.items()
                   if t <= now and (s.auto_sync or gid in self._forced)
                   and not (self.is_playing(gid) and s.sync_only_after_exit
                            and gid not in self._allow_playing)]
            # le altre restano in attesa: partono quando il gioco si chiude
            for gid in due:
                del self._pending[gid]
                self._forced.discard(gid)
                self._allow_playing.discard(gid)
        for gid in due:
            if self._stop.is_set():
                break  # si sta chiudendo: non ne comincio un'altra
            self._run(gid)

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
        if HAVE_WATCHDOG and gid not in self._watched and Path(game.path).is_dir():
            self._watch()  # la cartella è appena stata creata dalla prima sync
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
            self.request_sync(gid, delay=ERROR_RETRY)
