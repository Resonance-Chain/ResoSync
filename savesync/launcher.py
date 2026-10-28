"""Modalità launcher: sync -> gioco -> sync.

Uso:   SaveSync.exe --launch elden-ring                 (usa il comando salvato nel gioco)
       SaveSync.exe --launch elden-ring %command%       (opzioni di avvio di Steam)
"""
import os
import shlex
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from .backends import make_backend
from .config import Config, process_names
from .engine import SyncEngine
from .service import is_game_running, running_processes


class Splash:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("SaveSync")
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)
        f = ttk.Frame(self.root, padding=16)
        f.pack()
        self.label = ttk.Label(f, text="", width=48)
        self.label.pack(anchor="w")
        bar = ttk.Progressbar(f, mode="indeterminate", length=340)
        bar.pack(pady=(10, 0))
        bar.start(12)
        try:
            from PIL import ImageTk
            from .system import icon_image
            self._icon = ImageTk.PhotoImage(icon_image(32))
            self.root.iconphoto(True, self._icon)
        except Exception:
            pass

    def say(self, text):
        self.label.config(text=text)
        self.root.deiconify()
        self.root.update()

    def hide(self):
        self.root.withdraw()
        self.root.update()

    def run(self, fn):
        """Esegue fn in un thread lasciando viva la finestra. Ritorna (risultato, eccezione)."""
        box = {}

        def work():
            try:
                box["result"] = fn()
            except Exception as e:  # noqa: BLE001 - lo mostriamo all'utente
                box["error"] = e
        t = threading.Thread(target=work, daemon=True)
        t.start()
        while t.is_alive():
            self.root.update()
            time.sleep(0.05)
        return box.get("result"), box.get("error")

    def sleep(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.root.update()
            time.sleep(0.2)


def _log(cfg, msg):
    try:
        cfg.app_dir.mkdir(parents=True, exist_ok=True)
        with open(cfg.app_dir / "launcher.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except OSError:
        pass


def _start_game(game, command):
    if command:
        return subprocess.Popen(command)
    launch = game.launch.strip()
    if "://" in launch.split(" ")[0]:  # steam://rungameid/..., com.epicgames.launcher://...
        if sys.platform == "win32":
            os.startfile(launch)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", launch])
        return None
    args = shlex.split(launch, posix=sys.platform != "win32")
    exe = args[0].strip('"') if args else ""
    # molti giochi cercano i loro file nella cartella dell'exe: la uso come cartella di lavoro
    cwd = os.path.dirname(exe) if os.path.isfile(exe) else None
    return subprocess.Popen(launch if sys.platform == "win32" else args, cwd=cwd)


def _proc_name(proc):
    try:
        import psutil
        n = psutil.Process(proc.pid).name().lower()
        return n[:-4] if n.endswith(".exe") else n
    except Exception:
        return ""


def _wait_game(ui, game, proc):
    started = time.time()
    names = process_names(game.process)
    can_watch = bool(names or game.install_dir)
    running = lambda: is_game_running(game, running_processes(bool(game.install_dir)))
    is_the_game = False
    if proc is not None:
        is_the_game = _proc_name(proc) in names
        while proc.poll() is None:
            ui.sleep(1)
    if not can_watch:
        if proc is None:
            ui.root.deiconify()
            messagebox.showinfo("SaveSync", f"Premi OK quando hai finito di giocare a "
                                f"{game.name}: poi carico i salvataggi.", parent=ui.root)
        return
    # Il primo processo era solo un launcher? Aspetta che compaia il gioco vero
    if proc is None or (not is_the_game and time.time() - started < 60):
        deadline = time.time() + 45
        while time.time() < deadline and not running():
            ui.sleep(2)
    while running():
        ui.sleep(3)


def run(game_ref: str, command: list) -> int:
    cfg = Config.load()
    ui = Splash()
    game = cfg.find_game(game_ref)
    if game and not cfg.is_configured():
        messagebox.showwarning("SaveSync", "Cloud non configurato: avvio il gioco senza "
                               "sincronizzare. Apri SaveSync e completa le impostazioni.")
        game = None
    if not game:
        messagebox.showerror("SaveSync", f"Gioco '{game_ref}' non trovato nella configurazione.")
        if command:  # non impedire di giocare per colpa nostra
            return subprocess.call(command)
        return 1
    if not command and not game.launch:
        messagebox.showerror("SaveSync", f"Nessun comando di avvio per {game.name}: impostalo "
                             "nella scheda del gioco o usa le opzioni di avvio di Steam.")
        return 1

    log = lambda m: _log(cfg, m)
    holder = {}

    def sync(wait):
        if "engine" not in holder:
            holder["engine"] = SyncEngine(cfg.settings, make_backend(cfg.settings, cfg.vault),
                                          cfg.app_dir, log=log)
        return holder["engine"].sync_game(game, wait_lock=wait)

    ui.say(f"Scarico i salvataggi di {game.name}…")
    _, err = ui.run(lambda: sync(60))
    if err:
        log(f"[{game.name}] sync prima del gioco fallita: {err}")
        if not messagebox.askyesno("SaveSync", f"Non riesco a scaricare i salvataggi:\n{err}\n\n"
                                   "Avviare comunque il gioco?", parent=ui.root):
            return 1

    eng = holder.get("engine")
    if eng is not None:
        others, _ = ui.run(lambda: eng.others_playing(game))
        if others and not messagebox.askyesno(
                "SaveSync", f"{game.name} risulta aperto anche su {', '.join(others)}.\n\n"
                "Se salvate nello stesso slot, alla fine vince il salvataggio più recente "
                "(l'altro resta in Ripristina versione).\n\nAvviare comunque?", parent=ui.root):
            return 1
        ui.say(f"Salvo un'istantanea di {game.name}…")
        _, err = ui.run(lambda: eng.snapshot(game, "avvio del gioco"))
        if err:
            log(f"[{game.name}] istantanea non riuscita: {err}")
        ui.run(lambda: eng.set_playing(game, True))

    ui.hide()
    log(f"[{game.name}] avvio")
    try:
        proc = _start_game(game, command)
    except OSError as e:
        messagebox.showerror("SaveSync", f"Impossibile avviare il gioco:\n{e}", parent=ui.root)
        return 1
    _wait_game(ui, game, proc)
    log(f"[{game.name}] chiuso")
    if eng is not None:
        ui.run(lambda: eng.set_playing(game, False))

    ui.say(f"Carico i salvataggi di {game.name}…")
    for attempt in range(4):  # su Windows i file possono restare bloccati qualche secondo
        stats, err = ui.run(lambda: sync(120))
        if not err and not stats["errors"]:
            break
        ui.sleep(3)
    if err or stats["errors"]:
        messagebox.showwarning("SaveSync", "Non sono riuscito a caricare tutti i salvataggi"
                               f"{': ' + str(err) if err else ''}.\nRiproverà l'app in "
                               "background.", parent=ui.root)
    ui.root.destroy()
    return proc.returncode if proc is not None and proc.returncode is not None else 0
