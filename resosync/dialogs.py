"""Finestre secondarie: dettagli del gioco, istantanee, anteprima delle versioni."""
import io
import os
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from . import gamedb
from .snapshots import SnapshotStore


def run_bg(widget, fn, done, on_error=None):
    """Esegue fn in un thread (le richieste al cloud non devono bloccare la finestra)
    e chiama done(risultato) nel thread dell'interfaccia."""
    box = {}

    def work():
        try:
            box["r"] = fn()
        except Exception as e:  # noqa: BLE001 - lo mostriamo all'utente
            box["e"] = e

    def poll():
        try:
            if not widget.winfo_exists():
                return
        except tk.TclError:
            return
        if t.is_alive():
            widget.after(100, poll)
        elif "e" in box:
            if on_error:
                on_error(box["e"])
            else:
                messagebox.showerror("ResoSync", str(box["e"]), parent=widget)
        else:
            done(box["r"])
    t = threading.Thread(target=work, daemon=True)
    t.start()
    poll()


def human_size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def fmt_time(t) -> str:
    return time.strftime("%d/%m/%Y %H:%M", time.localtime(t)) if t else "-"


def folder_size(path) -> int:
    total = 0
    for dirpath, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


class PreviewPanel(ttk.LabelFrame):
    """Screenshot + dati del salvataggio (livello, luogo, tempo di gioco...)."""

    def __init__(self, parent):
        super().__init__(parent, text="Anteprima", padding=6)
        self.image_label = ttk.Label(self, text="Seleziona una versione", anchor="center")
        self.image_label.pack(fill="x")
        self.info = ttk.Label(self, text="", justify="left", anchor="w", wraplength=330)
        self.info.pack(fill="x", pady=(6, 0))
        self._photo = None

    def clear(self, text=""):
        self._photo = None
        self.image_label.configure(image="", text=text)
        self.info.configure(text="")

    def show(self, data: dict):
        self._photo = None
        if data.get("image"):
            try:
                from PIL import Image, ImageTk
                img = Image.open(io.BytesIO(data["image"]))
                img.thumbnail((340, 200))
                self._photo = ImageTk.PhotoImage(img)
                self.image_label.configure(image=self._photo, text="")
            except Exception:
                self.image_label.configure(image="", text="(immagine non leggibile)")
        else:
            self.image_label.configure(image="", text="(nessuno screenshot per questa versione)")
        lines = [f"Dimensione: {human_size(data.get('size'))}"]
        if data.get("image_from"):
            lines.append(f"Immagine: {data['image_from']}")
        for k, v in data.get("info", []):
            if isinstance(v, float):
                v = f"{v:.0f}" if v == int(v) else f"{v:.1f}"
            lines.append(f"{k}: {v}")
        self.info.configure(text="\n".join(lines))


class StatusDialog(tk.Toplevel):
    """Dettagli di un gioco: stato, spazio, cronologia, modalità."""

    def __init__(self, app, game):
        super().__init__(app)
        self.app, self.game = app, game
        self.title(f"Dettagli - {game.name}")
        self.transient(app)
        self.geometry("720x560")

        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        self.rows = {}
        fields = [("folder", "Cartella salvataggi"), ("install", "Cartella d'installazione"),
                  ("mode", "Modalità"), ("store", "Cloud dello store"),
                  ("last", "Ultimo salvataggio sul cloud"), ("local", "In locale"),
                  ("cloud", "Sul cloud"), ("snaps", "Istantanee"), ("others", "Aperto su")]
        for i, (key, label) in enumerate(fields):
            ttk.Label(top, text=label + ":", foreground="gray").grid(row=i, column=0, sticky="nw",
                                                                     padx=(0, 10))
            self.rows[key] = ttk.Label(top, text="…", wraplength=520, justify="left")
            self.rows[key].grid(row=i, column=1, sticky="w")

        btns = ttk.Frame(self, padding=(10, 0))
        btns.pack(fill="x")
        ttk.Button(btns, text="Pulisci versioni vecchie…", command=self._cleanup).pack(side="left")
        ttk.Button(btns, text="Istantanee…",
                   command=lambda: SnapshotDialog(app, game)).pack(side="left", padx=4)
        ttk.Button(btns, text="Aggiorna", command=self._load).pack(side="left")

        hist = ttk.LabelFrame(self, text="Cronologia sincronizzazioni", padding=4)
        hist.pack(fill="both", expand=True, padx=10, pady=10)
        self.tree = ttk.Treeview(hist, columns=("when", "what", "files"), show="headings")
        for col, title, w in [("when", "Quando", 130), ("what", "Cosa", 200),
                              ("files", "File caricati", 330)]:
            self.tree.heading(col, text=title)
            self.tree.column(col, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True)
        ttk.Button(self, text="Chiudi", command=self.destroy).pack(anchor="e", padx=10,
                                                                   pady=(0, 10))
        self._load()

    def _load(self):
        g, eng = self.game, self.app.engine
        self.rows["folder"].configure(text=g.path)
        self.rows["install"].configure(text=g.install_dir or "(non impostata)")
        self.rows["mode"].configure(text="Solo backup (non scarica mai nella cartella del gioco)"
                                    if g.mode == "backup" else "Sincronizzazione tra i PC")
        stores = gamedb.store_cloud(gamedb.load(self.app.cfg.app_dir), g.name)
        self.rows["store"].configure(text=", ".join(stores) if stores else "nessuno (o non noto)")
        store = SnapshotStore(self.app.cfg.app_dir, g.id)
        snaps = store.list()
        self.rows["snaps"].configure(
            text=f"{len(snaps)}, ultima {fmt_time(snaps[0]['time']) if snaps else '-'}, "
                 f"{human_size(store.disk_usage())} su questo PC")
        for item in self.tree.get_children():
            self.tree.delete(item)
        for h in eng.history(g) if eng else []:
            what = " ".join(p for p in [f"↑{h['up']}" if h.get("up") else "",
                                        f"↓{h['down']}" if h.get("down") else "",
                                        f"⚠{h['conflicts']} conflitti" if h.get("conflicts") else "",
                                        f"{h['errors']} errori" if h.get("errors") else ""] if p)
            self.tree.insert("", "end", values=(fmt_time(h["time"]), what,
                                                ", ".join(h.get("uploaded", []))[:200]))
        if not eng:
            return

        def remote():
            m = eng.load_manifest(g.id)
            files = [e for e in m["files"].values() if not e.get("deleted")]
            last = max(files, key=lambda e: e.get("time", 0), default=None)
            local = eng.scan_local(g)
            return {"last": last, "usage": eng.cloud_usage(g), "others": eng.others_playing(g),
                    "local": (len(local), sum(e["size"] for e in local.values()))}

        def show(r):
            self.rows["local"].configure(text=f"{r['local'][0]} file, {human_size(r['local'][1])}")
            last = r["last"]
            self.rows["last"].configure(
                text=f"{fmt_time(last.get('time'))} da {last.get('device', '?')}" if last else "-")
            u = r["usage"]
            limit = f", limite {self.game.max_cloud_mb} MB" if self.game.max_cloud_mb else ""
            self.rows["cloud"].configure(
                text=f"salvataggi attuali {human_size(u['current'])} + {u['n_versions']} "
                     f"versioni vecchie {human_size(u['versions'])}{limit}")
            self.rows["others"].configure(text=", ".join(r["others"]) or "nessun altro PC")
        for key in ("last", "cloud", "others", "local"):
            self.rows[key].configure(text="carico…")
        run_bg(self, remote, show, on_error=lambda e: self.rows["cloud"].configure(
            text=f"cloud non raggiungibile: {e}"))

    def _cleanup(self):
        keep = simpledialog.askinteger(
            "Pulisci versioni vecchie", "Quante versioni vecchie tenere per ogni file?\n"
            "(i salvataggi attuali non vengono mai toccati)", parent=self,
            minvalue=0, maxvalue=1000, initialvalue=3)
        if keep is None:
            return
        run_bg(self, lambda: self.app.engine.cleanup_versions(self.game, keep),
               lambda freed: (messagebox.showinfo(
                   "ResoSync", f"Liberati {human_size(freed)} sul cloud.", parent=self),
                   self._load()))


class SnapshotDialog(tk.Toplevel):
    """Istantanee della cartella salvataggi: tornare a com'era prima di una sessione."""

    def __init__(self, app, game):
        super().__init__(app)
        self.app, self.game = app, game
        self.store = SnapshotStore(app.cfg.app_dir, game.id)
        self.title(f"Istantanee - {game.name}")
        self.transient(app)
        self.geometry("640x380")
        ttk.Label(self, padding=(10, 10, 10, 0), wraplength=600, justify="left", text=(
            "Copie complete della cartella salvataggi, fatte in automatico quando apri il gioco "
            "e prima di ogni ripristino. Ripristinando, i file tornano com'erano in quel "
            "momento (e poi vanno agli altri PC); i salvataggi creati dopo restano dove sono."
        )).pack(anchor="w")
        self.tree = ttk.Treeview(self, columns=("when", "reason", "files", "size"),
                                 show="headings", selectmode="browse")
        for col, title, w in [("when", "Quando", 140), ("reason", "Perché", 260),
                              ("files", "File", 60), ("size", "Dimensione", 90)]:
            self.tree.heading(col, text=title)
            self.tree.column(col, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=10, pady=10)
        btns = ttk.Frame(self, padding=(10, 0, 10, 10))
        btns.pack(fill="x")
        ttk.Button(btns, text="Crea istantanea adesso", command=self._take).pack(side="left")
        ttk.Button(btns, text="Chiudi", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Ripristina", command=self._restore).pack(side="right", padx=4)
        self._fill()

    def _fill(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for s in self.store.list():
            self.tree.insert("", "end", iid=s["id"], values=(
                fmt_time(s["time"]), s["reason"], s["files"], human_size(s["size"])))
        if not self.tree.get_children():
            self.tree.insert("", "end", values=("", "(nessuna istantanea ancora)", "", ""))

    def _take(self):
        run_bg(self, lambda: self.app.engine.snapshot(self.game, "manuale"),
               lambda sid: (self._fill(), None if sid else messagebox.showinfo(
                   "ResoSync", "Nessuna modifica dall'ultima istantanea.", parent=self)))

    def _restore(self):
        sel = self.tree.selection()
        if not sel or not self.store.list():
            return
        snap_id = sel[0]
        if self.app.service and self.app.service.is_playing(self.game.id):
            messagebox.showwarning("ResoSync", "Chiudi prima il gioco.", parent=self)
            return
        if not messagebox.askyesno("Ripristina istantanea", "Rimettere i salvataggi com'erano "
                                   f"il {self.tree.set(snap_id, 'when')}?\n\nPrima ne faccio "
                                   "comunque un'altra istantanea, così puoi tornare indietro.",
                                   parent=self):
            return

        def work():
            with self.app.engine.lock:
                self.app.engine.snapshot(self.game, "prima di ripristinare un'istantanea")
                return self.store.restore(self.game.path, snap_id)

        def done(n):
            self.app._log(f"[{self.game.name}] istantanea ripristinata ({n} file)")
            if self.app.service:
                self.app.service.request_sync(self.game.id)
            self._fill()
            messagebox.showinfo("ResoSync", f"Fatto: {n} file rimessi.", parent=self)
        run_bg(self, work, done)
