"""Interfaccia grafica (Tkinter, inclusa in Python) + icona nella barra di sistema."""
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import gamedb, system
from .backends import make_backend
from .config import Config, Game, slugify
from .engine import SyncEngine
from .oauth import NeedsLogin, OAuthClient
from .service import HAVE_WATCHDOG, SyncService, psutil

try:
    import pystray
except Exception:  # su alcuni Linux senza tray manca il backend
    pystray = None

GUIDE_URL = "https://github.com/mtkennerly/ludusavi-manifest"


def _hint(parent, text, **grid):
    lbl = ttk.Label(parent, text=text, foreground="gray", wraplength=grid.pop("wrap", 460),
                    justify="left")
    lbl.grid(sticky="w", **grid)
    return lbl


class App(tk.Tk):
    def __init__(self, minimized=False, instance=None):
        super().__init__()
        self.title("SaveSync")
        self.geometry("960x580")
        self.minsize(760, 440)
        self.cfg = Config.load()
        self.events = queue.Queue()
        self.engine = None
        self.service = None
        self.status = {}
        self.tray = None
        self.instance = instance
        self._told_tray = False
        try:
            from PIL import ImageTk
            self._icon = ImageTk.PhotoImage(system.icon_image(64))
            self.iconphoto(True, self._icon)
        except Exception:
            pass

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.report_callback_exception = lambda *a: self._log(f"Errore interfaccia: {a[1]}")
        self.after(150, self._process_events)
        self._start_tray()
        if instance:
            instance.listen(lambda: self.events.put(("show", ())))

        if minimized:
            self.withdraw() if self.tray else self.iconify()
        if self.cfg.is_configured():
            self._start_service()
        else:
            self.after(300, self._first_run)

    # ---------- layout ----------
    def _build(self):
        bar = ttk.Frame(self, padding=(8, 8, 8, 4))
        bar.pack(fill="x")
        left = [("Aggiungi gioco", self.add_game), ("Rileva giochi", self.detect_games), None,
                ("Sincronizza", self.sync_selected), ("Sincronizza tutti", self.sync_all), None,
                ("Avvia gioco", self.launch_game), ("Ripristina versione", self.restore)]
        for item in left:
            if item is None:
                ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
            else:
                ttk.Button(bar, text=item[0], command=item[1]).pack(side="left", padx=2)
        ttk.Button(bar, text="Impostazioni", command=self.open_settings).pack(side="right")

        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=8, pady=4)

        frame = ttk.Frame(paned)
        self.tree = ttk.Treeview(frame, columns=("name", "path", "process", "status"),
                                 show="headings", selectmode="browse")
        for col, title, w in [("name", "Gioco", 170), ("path", "Cartella salvataggi", 360),
                              ("process", "Processo", 130), ("status", "Stato", 220)]:
            self.tree.heading(col, text=title)
            self.tree.column(col, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.edit_game())
        self.tree.bind("<Button-3>", self._context_menu)
        self.tree.bind("<Delete>", lambda e: self.remove_game())
        paned.add(frame, weight=3)

        self.menu = tk.Menu(self, tearoff=0)
        for label, cmd in [("Modifica…", self.edit_game), ("Sincronizza ora", self.sync_selected),
                           ("Avvia gioco con sync", self.launch_game),
                           ("Copia opzioni di avvio Steam", self.copy_steam_options),
                           ("Ripristina versione…", self.restore),
                           ("Apri cartella salvataggi", self.open_folder), (None, None),
                           ("Rimuovi", self.remove_game)]:
            if label is None:
                self.menu.add_separator()
            else:
                self.menu.add_command(label=label, command=cmd)

        logf = ttk.LabelFrame(paned, text="Attività", padding=4)
        self.logbox = tk.Text(logf, height=8, state="disabled", wrap="word",
                              font=("Consolas", 9))
        sb = ttk.Scrollbar(logf, command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.logbox.pack(fill="both", expand=True)
        paned.add(logf, weight=1)

        self.statusbar = ttk.Label(self, anchor="w", padding=(8, 2))
        self.statusbar.pack(fill="x")
        self._refresh()

    def _context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self.menu.tk_popup(event.x_root, event.y_root)

    def _refresh(self):
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for g in self.cfg.games:
            st = self.status.get(g.id, "In attesa" if g.enabled else "Disattivato")
            self.tree.insert("", "end", iid=g.id, values=(g.name, g.path, g.process, st))
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])
        s = self.cfg.settings
        where = {"folder": s.folder_path, "s3": f"s3://{s.s3_bucket}/{s.s3_prefix}",
                 "gdrive": "Google Drive", "onedrive": "OneDrive"}.get(s.backend, "?")
        bits = [f"Cloud: {where or '-'}" + (" (cifrato)" if s.encrypt else ""),
                f"PC: {s.device_name}",
                "sync automatica" if s.auto_sync else "sync manuale",
                "watcher attivo" if HAVE_WATCHDOG else "polling"]
        self.statusbar.config(text="   |   ".join(bits))

    # ---------- tray ----------
    def _start_tray(self):
        if pystray is None:
            return
        try:
            put = lambda kind: (lambda icon, item: self.events.put((kind, ())))
            menu = pystray.Menu(pystray.MenuItem("Apri SaveSync", put("show"), default=True),
                                pystray.MenuItem("Sincronizza tutto", put("sync_all")),
                                pystray.Menu.SEPARATOR,
                                pystray.MenuItem("Esci", put("quit")))
            self.tray = pystray.Icon("SaveSync", system.icon_image(64), "SaveSync", menu)
            self.tray.run_detached()
            if type(self.tray).__module__.endswith("_xorg"):
                self.after(2500, self._check_tray)
        except Exception as e:
            self.tray = None
            self._log(f"Icona nella barra non disponibile: {e}")

    def _check_tray(self):
        """Linux senza area di notifica: niente tray, sennò chiudendo la finestra sparirebbe."""
        if self.tray is not None and not getattr(self.tray, "_systray_manager", None):
            try:
                self.tray.stop()
            except Exception:
                pass
            self.tray = None
            self._show()
            self._log("Area di notifica non disponibile: chiudere la finestra chiude l'app")

    def _notify(self, title, msg):
        if self.tray is not None and getattr(self.tray, "HAS_NOTIFICATION", False):
            try:
                self.tray.notify(msg, title)
                return
            except Exception:
                pass
        self._log(f"{title}: {msg}")

    def _show(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    # ---------- eventi dai thread ----------
    def _emit(self, kind, *args):
        self.events.put((kind, args))

    def _process_events(self):
        try:
            while True:
                kind, args = self.events.get_nowait()
                if kind == "log":
                    self._log(args[0])
                elif kind == "status":
                    gid, text = args
                    self.status[gid] = text
                    if self.tree.exists(gid):
                        self.tree.set(gid, "status", text)
                elif kind == "notify":
                    self._notify(*args)
                elif kind == "error":
                    if isinstance(args[0], NeedsLogin):
                        self.statusbar.config(text=f"Serve il login: {args[0]}")
                elif kind == "ready":
                    self._service_ready(args[0])
                elif kind == "start_error":
                    self._log(f"Cloud non raggiungibile: {args[0]}")
                    self.statusbar.config(text=f"Cloud non raggiungibile: {args[0]}")
                    err = args[0]
                    transient = isinstance(err, OSError) and not isinstance(err, PermissionError)
                    if transient:  # rete giù: riprovo tra un minuto
                        self.after(60000, self._start_service)
                    elif not isinstance(err, NeedsLogin):  # passphrase sbagliata, ecc.
                        messagebox.showerror("SaveSync", f"Impossibile usare il cloud:\n{err}")
                elif kind == "show":
                    self._show()
                elif kind == "sync_all":
                    self.sync_all()
                elif kind == "quit":
                    self._quit()
                    return
        except queue.Empty:
            pass
        self.after(150, self._process_events)

    def _log(self, msg):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", f"{time.strftime('%H:%M:%S')}  {msg}\n")
        lines = int(self.logbox.index("end-1c").split(".")[0])
        if lines > 2000:
            self.logbox.delete("1.0", f"{lines - 2000}.0")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    # ---------- servizio ----------
    def _start_service(self):
        """Il backend può fare richieste di rete (o derivare la chiave): lo creo in un thread."""
        if self.service:
            self.service.stop()
            self.service = None
        self.engine = None
        self.statusbar.config(text="Connessione al cloud…")
        settings, app_dir, vault = self.cfg.settings, self.cfg.app_dir, self.cfg.vault

        def work():
            try:
                backend = make_backend(settings, vault)
                engine = SyncEngine(settings, backend, app_dir, log=lambda m: self._emit("log", m))
                self._emit("ready", engine)
            except Exception as e:
                self._emit("start_error", e)
        threading.Thread(target=work, daemon=True).start()

    def _service_ready(self, engine):
        if self.service:
            self.service.stop()
        self.engine = engine
        self.service = SyncService(self.cfg, engine, self._emit)
        self.service.start()
        self._log("Servizio di sincronizzazione avviato")
        self._refresh()

    def _first_run(self):
        self._show()
        messagebox.showinfo("Benvenuto in SaveSync",
                            "Per iniziare scegli dove salvare i dati nel cloud.\n\n"
                            "Il modo più semplice: una cartella dentro Google Drive, OneDrive o "
                            "Dropbox (il loro programma la manda online).\n\n"
                            "Poi usa 'Rileva giochi' per trovare i salvataggi in automatico.")
        self.open_settings()

    def _on_close(self):
        if self.tray is not None and self.cfg.settings.close_to_tray:
            self.withdraw()
            if not self._told_tray:
                self._told_tray = True
                self._notify("SaveSync è ancora attivo",
                             "Continua a sincronizzare dalla barra di sistema. Per chiuderlo: "
                             "tasto destro sull'icona > Esci.")
        else:
            self._quit()

    def _quit(self):
        if self.service:
            self.service.stop()
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        if self.instance:
            self.instance.close()
        self.destroy()

    # ---------- azioni ----------
    def _selected(self, quiet=False):
        sel = self.tree.selection()
        if not sel:
            if not quiet:
                messagebox.showinfo("SaveSync", "Seleziona prima un gioco dalla lista.")
            return None
        return self.cfg.get_game(sel[0])

    def _require_engine(self):
        if not self.engine:
            if not self.cfg.is_configured():
                self.open_settings()
            else:
                messagebox.showinfo("SaveSync", "Connessione al cloud non ancora pronta: "
                                    "controlla le impostazioni o riprova tra poco.")
            return False
        return True

    def _cloud_names(self):
        try:
            return [v["name"] for v in self.engine.cloud_games().values()] if self.engine else []
        except Exception:
            return []

    def add_game(self):
        if self._require_engine():
            GameDialog(self, None, self._cloud_names(), self._save_game)

    def detect_games(self):
        if self._require_engine():
            DetectDialog(self)

    def edit_game(self):
        g = self._selected()
        if g:
            GameDialog(self, g, self._cloud_names(), self._save_game)

    def _save_game(self, old, game, quiet=False):
        if old is None and self.cfg.get_game(game.id):
            if not quiet:
                messagebox.showerror("SaveSync", f"C'è già un gioco con ID '{game.id}'.")
            return False
        if old:
            self.cfg.games[self.cfg.games.index(old)] = game
        else:
            self.cfg.games.append(game)
        if self.engine and (old is None or os.path.normcase(os.path.normpath(old.path)) !=
                            os.path.normcase(os.path.normpath(game.path))):
            # Cartella nuova (o gioco riaggiunto): lo stato della vecchia non vale più,
            # sennò una cartella vuota sembrerebbe "ho cancellato tutto"
            self.engine.reset_state(game.id)
        self.cfg.save()
        if self.service:
            self.service.rewatch()
            self.service.request_sync(game.id)
        self._refresh()
        return True

    def remove_game(self):
        g = self._selected()
        if g and messagebox.askyesno("Rimuovi", f"Smettere di sincronizzare '{g.name}'?\n\n"
                                     "I file in locale e sul cloud NON vengono cancellati."):
            self.cfg.games.remove(g)
            self.cfg.save()
            if self.service:
                self.service.rewatch()
            self._refresh()

    def sync_selected(self):
        g = self._selected()
        if g and self.service:
            self.service.request_sync(g.id)

    def sync_all(self):
        if self.service:
            self.service.request_sync()

    def open_folder(self):
        g = self._selected()
        if g and Path(g.path).exists():
            system.open_in_file_manager(g.path)

    def restore(self):
        g = self._selected()
        if g and self._require_engine():
            RestoreDialog(self, g)

    def launch_game(self):
        g = self._selected()
        if not g:
            return
        if not g.launch:
            messagebox.showinfo("Avvia gioco", f"Imposta prima il comando di avvio di {g.name} "
                                "(Modifica > Comando di avvio).\n\nPer i giochi Steam puoi anche "
                                "usare 'Copia opzioni di avvio Steam' dal menu col tasto destro.")
            return
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        subprocess.Popen(system.app_command() + ["--launch", g.id], creationflags=flags)

    def copy_steam_options(self):
        g = self._selected()
        if not g:
            return
        opts = system.steam_launch_options(g.id)
        self.clipboard_clear()
        self.clipboard_append(opts)
        messagebox.showinfo("Opzioni di avvio Steam",
                            "Copiato negli appunti:\n\n" + opts + "\n\nIn Steam: tasto destro sul "
                            "gioco > Proprietà > Generale > Opzioni di avvio, e incolla.\n"
                            "Da quel momento, ogni volta che avvii il gioco da Steam, SaveSync "
                            "scarica i salvataggi prima e li carica quando chiudi.")

    def open_settings(self):
        SettingsDialog(self)


class GameDialog(tk.Toplevel):
    def __init__(self, app, game, cloud_names, on_save):
        super().__init__(app)
        self.app, self.game, self.on_save = app, game, on_save
        self.title("Modifica gioco" if game else "Aggiungi gioco")
        self.transient(app)
        self.resizable(False, False)
        f = ttk.Frame(self, padding=12)
        f.pack(fill="both")

        self.name = tk.StringVar(value=game.name if game else "")
        self.path = tk.StringVar(value=game.path if game else "")
        self.patterns = tk.StringVar(value=", ".join(game.patterns) if game else "*")
        default_ex = Game("x", "x", "").excludes
        self.excludes = tk.StringVar(value=", ".join(game.excludes if game else default_ex))
        self.process = tk.StringVar(value=game.process if game else "")
        self.launch = tk.StringVar(value=game.launch if game else "")
        self.enabled = tk.BooleanVar(value=game.enabled if game else True)

        ttk.Label(f, text="Nome del gioco").grid(row=0, column=0, sticky="w")
        cb = ttk.Combobox(f, textvariable=self.name, values=cloud_names, width=52)
        cb.grid(row=0, column=1, columnspan=2, sticky="we", pady=3)
        if game:
            cb.configure(state="disabled")  # il nome determina l'ID sul cloud
        _hint(f, "Usa lo stesso nome su tutti i PC (il menu mostra i giochi già sul cloud).",
              row=1, column=1, columnspan=2)

        ttk.Label(f, text="Cartella salvataggi").grid(row=2, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.path, width=54).grid(row=2, column=1, sticky="we", pady=3)
        bf = ttk.Frame(f)
        bf.grid(row=2, column=2, padx=(4, 0))
        ttk.Button(bf, text="Sfoglia…", command=self._browse).pack(side="left")

        ttk.Label(f, text="File da includere").grid(row=3, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.patterns).grid(row=3, column=1, columnspan=2, sticky="we",
                                                      pady=3)
        ttk.Label(f, text="File da escludere").grid(row=4, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.excludes).grid(row=4, column=1, columnspan=2, sticky="we",
                                                      pady=3)
        _hint(f, "Pattern separati da virgola, es: *.sav, slot*, profiles/*", row=5, column=1,
              columnspan=2)

        ttk.Label(f, text="Processo del gioco").grid(row=6, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(f, textvariable=self.process).grid(row=6, column=1, sticky="we", pady=(8, 3))
        ttk.Button(f, text="Scegli…", command=self._pick_process).grid(row=6, column=2,
                                                                       sticky="w", padx=(4, 0),
                                                                       pady=(8, 3))
        _hint(f, "Es. eldenring.exe. Mentre il gioco è aperto non carico nulla (i file potrebbero "
                 "essere a metà): sincronizzo appena lo apri e appena lo chiudi.",
              row=7, column=1, columnspan=2)
        ttk.Label(f, text="Comando di avvio").grid(row=8, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.launch).grid(row=8, column=1, columnspan=2, sticky="we",
                                                    pady=3)
        _hint(f, "Facoltativo, per 'Avvia gioco'. Es. steam://rungameid/1245620 oppure il "
                 "percorso dell'exe.", row=9, column=1, columnspan=2)

        ttk.Checkbutton(f, text="Sincronizzazione attiva", variable=self.enabled).grid(
            row=10, column=1, sticky="w", pady=6)

        btns = ttk.Frame(f)
        btns.grid(row=11, column=0, columnspan=3, sticky="e", pady=(8, 0))
        ttk.Button(btns, text="Annulla", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Salva", command=self._save).pack(side="right", padx=4)
        self.grab_set()

    def _browse(self):
        d = filedialog.askdirectory(parent=self, initialdir=self.path.get() or str(Path.home()))
        if d:
            self.path.set(os.path.normpath(d))
            if not self.name.get():
                self.name.set(Path(d).name)

    def _pick_process(self):
        if psutil is None:
            messagebox.showinfo("SaveSync", "Installa psutil per questa funzione.", parent=self)
            return
        names = sorted({(p.info.get("name") or "") for p in psutil.process_iter(["name"])} - {""},
                       key=str.lower)
        win = tk.Toplevel(self)
        win.title("Processi attivi")
        win.transient(self)
        ttk.Label(win, text="Avvia il gioco, poi scegli il suo processo:", padding=8).pack(
            anchor="w")
        lb = tk.Listbox(win, width=50, height=20)
        lb.pack(fill="both", expand=True, padx=8)
        for n in names:
            lb.insert("end", n)

        def pick(_=None):
            if lb.curselection():
                self.process.set(names[lb.curselection()[0]])
                win.destroy()
        lb.bind("<Double-1>", pick)
        ttk.Button(win, text="Usa questo", command=pick).pack(pady=8)

    def _save(self):
        name, path = self.name.get().strip(), self.path.get().strip()
        if not name or not path:
            messagebox.showerror("SaveSync", "Servono nome e cartella.", parent=self)
            return
        split = lambda s: [x.strip() for x in s.split(",") if x.strip()]
        game = Game(id=self.game.id if self.game else slugify(name), name=name, path=path,
                    patterns=split(self.patterns.get()) or ["*"],
                    excludes=split(self.excludes.get()), enabled=self.enabled.get(),
                    process=self.process.get().strip(), launch=self.launch.get().strip())
        if not Path(path).exists() and not messagebox.askyesno(
                "Cartella inesistente", "La cartella non esiste: crearla e scaricarci i salvataggi "
                "dal cloud?", parent=self):
            return
        if self.on_save(self.game, game):
            self.destroy()


class DetectDialog(tk.Toplevel):
    """Trova i giochi installati usando il database di Ludusavi."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Rileva giochi")
        self.transient(app)
        self.geometry("900x500")
        self.results = []

        top = ttk.Frame(self, padding=(8, 8, 8, 0))
        top.pack(fill="x")
        ttk.Label(top, text="Cerca:").pack(side="left")
        self.query = tk.StringVar()
        self.query.trace_add("write", lambda *a: self._fill())
        ttk.Entry(top, textvariable=self.query, width=30).pack(side="left", padx=4)
        ttk.Button(top, text="Aggiorna database", command=lambda: self._load(True)).pack(
            side="right")
        self.info = ttk.Label(top, text="", foreground="gray")
        self.info.pack(side="left", padx=10)

        frame = ttk.Frame(self, padding=8)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=("name", "folder", "files", "when"),
                                 show="headings", selectmode="extended")
        for col, title, w in [("name", "Gioco", 220), ("folder", "Cartella trovata", 440),
                              ("files", "File", 50), ("when", "Ultima modifica", 120)]:
            self.tree.heading(col, text=title)
            self.tree.column(col, width=w, anchor="w")
        sb = ttk.Scrollbar(frame, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)

        bottom = ttk.Frame(self, padding=(8, 0, 8, 8))
        bottom.pack(fill="x")
        ttk.Label(bottom, text=f"Dati: Ludusavi manifest ({GUIDE_URL}). Seleziona con Ctrl/Shift.",
                  foreground="gray").pack(side="left")
        ttk.Button(bottom, text="Chiudi", command=self.destroy).pack(side="right")
        ttk.Button(bottom, text="Aggiungi selezionati", command=self._add).pack(side="right",
                                                                                padx=4)
        self._load(False)

    def _load(self, force):
        self.info.config(text="Carico…")
        q = queue.Queue()

        def work():
            try:
                db = gamedb.update(self.app.cfg.app_dir, force=force,
                                   progress=lambda m: q.put(("msg", m)))
            except Exception as e:
                db = gamedb.load(self.app.cfg.app_dir)  # offline: uso la cache se c'è
                if not db:
                    q.put(("err", e))
                    return
                q.put(("msg", f"Database non aggiornato ({e}), uso la copia salvata"))
            q.put(("msg", "Cerco i salvataggi su questo PC…"))
            q.put(("done", gamedb.detect(db)))

        def poll():
            if not self.winfo_exists():
                return
            try:
                while True:
                    kind, val = q.get_nowait()
                    if kind == "msg":
                        self.info.config(text=val)
                    elif kind == "err":
                        self.info.config(text=f"Errore: {val}")
                        return
                    elif kind == "done":
                        self.results = val
                        self._fill()
                        return
            except queue.Empty:
                pass
            self.after(100, poll)
        threading.Thread(target=work, daemon=True).start()
        poll()

    def _fill(self):
        self.tree.delete(*self.tree.get_children())
        have_ids = {g.id for g in self.app.cfg.games}
        have_paths = {os.path.normcase(os.path.normpath(g.path)) for g in self.app.cfg.games}
        q = self.query.get().lower().strip()
        shown = 0
        for i, d in enumerate(self.results):
            if slugify(d.name) in have_ids or os.path.normcase(d.folder) in have_paths:
                continue
            if q and q not in d.name.lower():
                continue
            when = time.strftime("%d/%m/%Y", time.localtime(d.mtime)) if d.mtime else ""
            self.tree.insert("", "end", iid=str(i), values=(d.name, d.folder, d.files, when))
            shown += 1
        self.info.config(text=f"{shown} giochi con salvataggi trovati (non ancora aggiunti)")

    def _add(self):
        added = 0
        for iid in self.tree.selection():
            d = self.results[int(iid)]
            launch = f"steam://rungameid/{d.steam_id}" if d.steam_id else ""
            game = Game(id=slugify(d.name), name=d.name, path=d.folder, patterns=d.patterns,
                        process=d.process, launch=launch)
            if self.app._save_game(None, game, quiet=True):
                added += 1
        self._fill()
        if added:
            self.app._log(f"Aggiunti {added} giochi dal rilevamento automatico")


class RestoreDialog(tk.Toplevel):
    def __init__(self, app, game):
        super().__init__(app)
        self.app, self.game = app, game
        self.title(f"Ripristina versione - {game.name}")
        self.transient(app)
        self.geometry("760x420")
        self.files, self.versions = [], []

        pw = ttk.PanedWindow(self, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=8, pady=8)
        lf = ttk.LabelFrame(pw, text="File", padding=4)
        self.flist = tk.Listbox(lf, exportselection=False)
        self.flist.pack(fill="both", expand=True)
        self.flist.bind("<<ListboxSelect>>", self._load_versions)
        pw.add(lf, weight=1)

        rf = ttk.LabelFrame(pw, text="Versioni (più recenti in alto)", padding=4)
        self.vlist = tk.Listbox(rf, exportselection=False)
        self.vlist.pack(fill="both", expand=True)
        pw.add(rf, weight=1)

        bf = ttk.Frame(self, padding=(8, 0, 8, 8))
        bf.pack(fill="x")
        ttk.Label(bf, text="Il file attuale viene salvato nei backup prima del ripristino.",
                  foreground="gray").pack(side="left")
        ttk.Button(bf, text="Chiudi", command=self.destroy).pack(side="right")
        ttk.Button(bf, text="Ripristina", command=self._restore).pack(side="right", padx=4)
        self._bg(lambda: sorted(set(app.engine.remote_files(game)) |
                                set(app.engine.scan_local(game))), self._show_files)

    def _bg(self, fn, done):
        """Le richieste al cloud non devono bloccare la finestra."""
        box = {}

        def work():
            try:
                box["r"] = fn()
            except Exception as e:
                box["e"] = e

        def poll():
            if not self.winfo_exists():
                return
            if t.is_alive():
                self.after(100, poll)
            elif "e" in box:
                messagebox.showerror("SaveSync", str(box["e"]), parent=self)
            else:
                done(box["r"])
        t = threading.Thread(target=work, daemon=True)
        t.start()
        poll()

    def _show_files(self, files):
        self.files = files
        for f in files:
            self.flist.insert("end", f)

    def _rel(self):
        sel = self.flist.curselection()
        return self.files[sel[0]] if sel else None

    def _load_versions(self, _=None):
        rel = self._rel()
        self.vlist.delete(0, "end")
        self.versions = []
        if rel:
            self.vlist.insert("end", "Carico…")
            self._bg(lambda: self.app.engine.list_versions(self.game, rel), self._show_versions)

    def _show_versions(self, versions):
        self.versions = versions
        self.vlist.delete(0, "end")
        for stamp, origin, _ in versions:
            try:
                pretty = time.strftime("%d/%m/%Y %H:%M:%S",
                                       time.strptime(stamp[:15], "%Y%m%d-%H%M%S"))
            except ValueError:
                pretty = stamp
            self.vlist.insert("end", f"{pretty}   [{origin}]")
        if not versions:
            self.vlist.insert("end", "(nessuna versione precedente)")

    def _restore(self):
        rel = self._rel()
        sel = self.vlist.curselection()
        if not rel or not sel or not self.versions:
            return
        _, origin, ref = self.versions[sel[0]]
        if not messagebox.askyesno("Ripristina", f"Ripristinare questa versione di {rel}?\n"
                                   "Verrà poi caricata sul cloud.", parent=self):
            return

        def work():
            try:
                self.app.engine.restore_version(self.game, rel, origin, ref)
                self.app.service.request_sync(self.game.id)
            except Exception as e:
                self.app._emit("log", f"Ripristino fallito: {e}")
        threading.Thread(target=work, daemon=True).start()
        self.destroy()


class SettingsDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Impostazioni")
        self.transient(app)
        self.resizable(False, False)
        s = app.cfg.settings
        self.v = {k: (tk.BooleanVar if isinstance(val, bool) else tk.StringVar)(value=val)
                  for k, val in vars(s).items()}
        self.v["autostart"].set(system.is_autostart_enabled())
        self.passphrase2 = tk.StringVar(value=s.passphrase)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        nb.add(self._tab_cloud(nb), text="Cloud")
        nb.add(self._tab_security(nb), text="Sicurezza")
        nb.add(self._tab_behaviour(nb), text="Comportamento")
        nb.add(self._tab_system(nb), text="Sistema")

        btns = ttk.Frame(self, padding=(8, 0, 8, 8))
        btns.pack(fill="x")
        ttk.Button(btns, text="Annulla", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Salva", command=self._save).pack(side="right", padx=4)
        self._toggle()
        self.grab_set()

    # ---- schede ----
    def _tab_cloud(self, nb):
        f = ttk.Frame(nb, padding=12)
        self.frames = {}

        def section(row, value, title, hint):
            ttk.Radiobutton(f, text=title, variable=self.v["backend"], value=value,
                            command=self._toggle).grid(row=row, column=0, sticky="w",
                                                       pady=(8 if row else 0, 0))
            box = ttk.Frame(f, padding=(22, 2, 0, 0))
            box.grid(row=row + 1, column=0, sticky="we")
            if hint:
                _hint(box, hint, row=99, column=0, columnspan=3)
            self.frames[value] = box
            return box

        box = section(0, "folder", "Cartella (Google Drive / OneDrive / Dropbox / NAS)",
                      "La più semplice: ci pensa il programma del cloud a caricarla.")
        ttk.Entry(box, textvariable=self.v["folder_path"], width=52).grid(row=0, column=0)
        ttk.Button(box, text="Sfoglia…", command=self._browse).grid(row=0, column=1, padx=4)

        box = section(2, "gdrive", "Google Drive (accesso diretto)",
                      "Serve un 'OAuth client ID' di tipo App desktop creato su Google Cloud "
                      "Console (gratis, vedi README). I file stanno in una cartella nascosta "
                      "dell'app, il resto del Drive non viene toccato.")
        self._labeled(box, 0, "Client ID", "gdrive_client_id")
        self._labeled(box, 1, "Client secret", "gdrive_client_secret", secret=True)
        self.gd_status = self._login_row(box, 2, "gdrive")

        box = section(4, "onedrive", "OneDrive (accesso diretto)",
                      "Serve l'Application (client) ID di un'app registrata su portal.azure.com "
                      "(gratis, vedi README). I file stanno in App/SaveSync sul tuo OneDrive.")
        self._labeled(box, 0, "Client ID", "onedrive_client_id")
        self.od_status = self._login_row(box, 1, "onedrive")

        box = section(6, "s3", "Storage S3 (AWS, Cloudflare R2, Backblaze B2, MinIO…)", "")
        for i, (key, label) in enumerate([("s3_bucket", "Bucket"), ("s3_prefix", "Prefisso"),
                                          ("s3_endpoint", "Endpoint (vuoto = AWS)"),
                                          ("s3_region", "Regione"),
                                          ("s3_access_key", "Access key"),
                                          ("s3_secret_key", "Secret key")]):
            self._labeled(box, i, label, key, secret=key == "s3_secret_key")
        return f

    def _tab_security(self, nb):
        f = ttk.Frame(nb, padding=12)
        ttk.Checkbutton(f, text="Cifra i salvataggi prima di mandarli sul cloud",
                        variable=self.v["encrypt"], command=self._toggle).grid(
            row=0, column=0, columnspan=2, sticky="w")
        _hint(f, "AES-256. Il provider vede solo dati illeggibili. Usa la STESSA passphrase su "
                 "tutti i PC. Se la perdi i salvataggi sul cloud non si recuperano (quelli sui "
                 "PC restano).", row=1, column=0, columnspan=2, pady=(0, 8))
        self.pass_entries = []
        for i, (label, var) in enumerate([("Passphrase", self.v["passphrase"]),
                                          ("Ripeti passphrase", self.passphrase2)]):
            ttk.Label(f, text=label).grid(row=2 + i, column=0, sticky="w")
            e = ttk.Entry(f, textvariable=var, show="•", width=36)
            e.grid(row=2 + i, column=1, sticky="w", pady=2)
            self.pass_entries.append(e)
        store = ("nel portachiavi di sistema (Gestione credenziali di Windows)"
                 if self.app.cfg.vault.secure else
                 "in un file locale: il portachiavi di sistema non è disponibile")
        _hint(f, f"Password e chiavi sono salvate {store}, non in config.json.",
              row=5, column=0, columnspan=2, pady=(12, 0))
        return f

    def _tab_behaviour(self, nb):
        f = ttk.Frame(nb, padding=12)
        rows = [("device_name", "Nome di questo PC"),
                ("poll_interval", "Controlla il cloud ogni (secondi)"),
                ("debounce_seconds", "Attendi dopo una modifica (secondi)"),
                ("max_versions", "Versioni da tenere per file")]
        for i, (key, label) in enumerate(rows):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w")
            ttk.Entry(f, textvariable=self.v[key], width=24).grid(row=i, column=1, sticky="w",
                                                                  pady=2)
        checks = [("auto_sync", "Sincronizzazione automatica"),
                  ("sync_only_after_exit", "Mentre un gioco è aperto, carica solo quando si chiude"),
                  ("propagate_deletes", "Propaga le cancellazioni (sconsigliato)")]
        for i, (key, label) in enumerate(checks, start=len(rows)):
            ttk.Checkbutton(f, text=label, variable=self.v[key]).grid(row=i, column=0,
                                                                      columnspan=2, sticky="w",
                                                                      pady=(6 if i == 4 else 0, 0))
        return f

    def _tab_system(self, nb):
        f = ttk.Frame(nb, padding=12)
        ttk.Checkbutton(f, text="Avvia SaveSync all'accensione del PC (ridotto a icona)",
                        variable=self.v["autostart"]).grid(row=0, column=0, sticky="w")
        tray_ok = self.app.tray is not None
        cb = ttk.Checkbutton(f, text="Chiudendo la finestra resta attivo nella barra di sistema",
                             variable=self.v["close_to_tray"])
        cb.grid(row=1, column=0, sticky="w")
        if not tray_ok:
            cb.configure(state="disabled")
            _hint(f, "Barra di sistema non disponibile su questo sistema.", row=2, column=0)
        ttk.Label(f, text="Cartella dati dell'app:").grid(row=3, column=0, sticky="w",
                                                         pady=(14, 0))
        row = ttk.Frame(f)
        row.grid(row=4, column=0, sticky="w")
        ttk.Label(row, text=str(self.app.cfg.app_dir)).pack(side="left")
        ttk.Button(row, text="Apri", command=lambda: system.open_in_file_manager(
            str(self.app.cfg.app_dir))).pack(side="left", padx=6)
        return f

    # ---- helper ----
    def _labeled(self, box, row, label, key, secret=False):
        ttk.Label(box, text=label).grid(row=row, column=0, sticky="w")
        ttk.Entry(box, textvariable=self.v[key], width=44, show="•" if secret else "").grid(
            row=row, column=1, sticky="w", pady=1)

    def _login_row(self, box, row, provider):
        status = ttk.Label(box, text="")
        ttk.Button(box, text="Accedi…", command=lambda: self._login(provider, status)).grid(
            row=row, column=0, sticky="w", pady=4)
        status.grid(row=row, column=1, sticky="w")
        self._update_login_status(provider, status)
        return status

    def _oauth(self, provider):
        if provider == "gdrive":
            return OAuthClient("gdrive", self.v["gdrive_client_id"].get().strip(),
                               self.app.cfg.vault, self.v["gdrive_client_secret"].get().strip())
        return OAuthClient("onedrive", self.v["onedrive_client_id"].get().strip(),
                           self.app.cfg.vault)

    def _update_login_status(self, provider, label):
        client = self._oauth(provider)
        ok = client.logged_in and client._load().get("client_id") == client.client_id
        label.config(text="Accesso effettuato" if ok else "Non collegato",
                     foreground="green" if ok else "gray")

    def _login(self, provider, label):
        client = self._oauth(provider)
        if not client.client_id:
            messagebox.showerror("Accedi", "Inserisci prima il Client ID.", parent=self)
            return
        label.config(text="Completa l'accesso nel browser…", foreground="black")
        result = {}

        def work():
            try:
                client.login()
                result["ok"] = True
            except Exception as e:
                result["err"] = e
        t = threading.Thread(target=work, daemon=True)
        t.start()

        def poll():
            if not self.winfo_exists():
                return
            if t.is_alive():
                self.after(300, poll)
            elif "err" in result:
                label.config(text=f"Errore: {result['err']}"[:80], foreground="red")
            else:
                self._update_login_status(provider, label)
        poll()

    def _toggle(self):
        current = self.v["backend"].get()

        def walk(w, state):
            for c in w.winfo_children():
                try:
                    c.configure(state=state)
                except tk.TclError:
                    pass
                walk(c, state)
        for name, box in self.frames.items():
            walk(box, "normal" if name == current else "disabled")
        for e in self.pass_entries:
            e.configure(state="normal" if self.v["encrypt"].get() else "disabled")

    def _browse(self):
        d = filedialog.askdirectory(parent=self)
        if d:
            p = Path(d)
            self.v["folder_path"].set(os.path.normpath(p if p.name == "SaveSync"
                                                       else p / "SaveSync"))

    def _save(self):
        s = self.app.cfg.settings
        new = {}
        try:
            for k, var in self.v.items():
                cur = getattr(s, k)
                val = var.get()
                if isinstance(cur, int) and not isinstance(cur, bool):
                    val = int(val)
                elif isinstance(val, str):
                    val = val.strip() if k != "passphrase" else val
                new[k] = val
        except ValueError:
            messagebox.showerror("Impostazioni", "I campi numerici devono contenere numeri.",
                                 parent=self)
            return
        limits = {"poll_interval": (10, "controllo del cloud: almeno 10 secondi"),
                  "debounce_seconds": (1, "attesa dopo una modifica: almeno 1 secondo"),
                  "max_versions": (1, "versioni da tenere: almeno 1")}
        for k, (minimum, msg) in limits.items():
            if new[k] < minimum:
                messagebox.showerror("Impostazioni", msg.capitalize() + ".", parent=self)
                return
        if not new["device_name"]:
            messagebox.showerror("Impostazioni", "Dai un nome a questo PC.", parent=self)
            return
        if new["encrypt"]:
            if len(new["passphrase"]) < 8:
                messagebox.showerror("Impostazioni", "Passphrase troppo corta (almeno 8 "
                                     "caratteri).", parent=self)
                return
            if new["passphrase"] != self.passphrase2.get():
                messagebox.showerror("Impostazioni", "Le due passphrase non coincidono.",
                                     parent=self)
                return
        for k, val in new.items():
            setattr(s, k, val)
        if not self.app.cfg.is_configured():
            messagebox.showerror("Impostazioni", "Completa la configurazione del cloud scelto.",
                                 parent=self)
            return
        try:
            if new["autostart"] != system.is_autostart_enabled():
                system.set_autostart(new["autostart"])
        except OSError as e:
            messagebox.showwarning("Impostazioni", f"Avvio automatico non impostato: {e}",
                                   parent=self)
        self.app.cfg.save()
        self.destroy()
        self.app._start_service()


def main(minimized=False):
    from .config import APP_DIR
    instance = system.SingleInstance(APP_DIR)
    if instance.already_running():
        return  # l'altra istanza si è mostrata
    App(minimized=minimized, instance=instance).mainloop()
    # Uscita netta: thread di watchdog/tray non devono tenere vivo il processo
    os._exit(0)
