"""Le variabili Tk (StringVar, BooleanVar...) delle finestre chiuse vengono liberate da Python
"quando capita", anche in un thread secondario (sync, login, verifica). Il loro distruttore
parla con Tcl, e con il Tcl multithread di Windows e dello Steam Deck un thread secondario
deve aspettare il thread principale: se il principale non è nel ciclo degli eventi resta
bloccato circa un secondo per variabile, e se il principale sta aspettando proprio quel thread
(alla chiusura aspetta la fine della sync) si bloccano a vicenda.

Qui il distruttore, fuori dal thread principale, mette la variabile in coda; la libera davvero
il thread principale nel suo giro normale degli eventi (install)."""
import threading
import tkinter as tk

_pending = []
_guard = threading.Lock()
_original_del = tk.Variable.__del__


def _variable_del(self):
    if threading.current_thread() is threading.main_thread():
        _original_del(self)
        return
    tkapp = getattr(self, "_tk", None)
    if tkapp is not None:
        with _guard:
            _pending.append((tkapp, self._name, getattr(self, "_tclCommands", None)))


def drain() -> None:
    """Libera in Tcl le variabili messe in coda (solo dal thread principale)."""
    with _guard:
        items = _pending[:]
        _pending.clear()
    for tkapp, name, commands in items:
        try:
            if tkapp.getboolean(tkapp.call("info", "exists", name)):
                tkapp.globalunsetvar(name)
            for c in commands or ():
                tkapp.deletecommand(c)
        except Exception:  # noqa: BLE001 - finestra (interprete) già chiusa: niente da fare
            pass


def install(root, every_ms=200) -> None:
    """Svuota la coda periodicamente nel ciclo degli eventi di `root`."""
    def tick():
        drain()
        try:
            root.after(every_ms, tick)
        except tk.TclError:
            pass  # finestra chiusa
    root.after(every_ms, tick)


tk.Variable.__del__ = _variable_del
