"""Prova dell'interfaccia: apre davvero ogni finestra con un gioco sincronizzato e raccoglie gli
errori. Serve un display (in locale: xvfb-run -a python -m pytest tests/test_gui_smoke.py)."""
import os
import time
from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")


def _display_ok():
    try:
        r = tk.Tk()
        r.destroy()
        return True
    except tk.TclError:
        return False


pytestmark = pytest.mark.skipif(not _display_ok(), reason="nessun display")


@pytest.fixture
def app(tmp_path, monkeypatch):
    from test_deck import use_app_dir
    from tkinter import messagebox
    from resosync import gui
    from resosync.config import Config, Game
    use_app_dir(monkeypatch, tmp_path / "app")
    monkeypatch.setattr(gui, "pystray", None)  # niente icona nel tray (thread appesi)
    errors, shown = [], []
    for name in ("showinfo", "showwarning"):
        monkeypatch.setattr(messagebox, name, lambda *a, **k: shown.append(a))
    monkeypatch.setattr(messagebox, "showerror", lambda *a, **k: errors.append(("errore", a)))
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    saves = tmp_path / "saves"
    (saves / "Slot1").mkdir(parents=True)
    for part, data in (("sav.dat", b"partita"), ("metadata.json", b'{"level": 7}'),
                       ("screenshot.png", b"\x89PNG finta")):
        (saves / "Slot1" / part).write_bytes(data)
    cfg = Config(app_dir=tmp_path / "app")
    cfg.settings.folder_path = str(tmp_path / "cloud")
    cfg.settings.device_name = "PC"
    cfg.settings.auto_update = False  # niente richieste in rete durante la prova
    cfg.games.append(Game("g", "Gioco", str(saves), group_by="folder"))
    cfg.save()
    a = gui.App(minimized=True, instance=None)
    real_log = a._log

    def log(msg):
        if msg.startswith("Errore interfaccia"):
            errors.append(("interfaccia", msg))
        real_log(msg)
    a._log = log
    a.report_callback_exception = lambda *exc: errors.append(("callback", exc[1]))
    a.errors, a.shown, a.saves = errors, shown, saves
    pump(a, lambda: a.engine is not None, 15)
    assert a.engine, "il servizio non è partito"
    yield a
    if a.service:
        a.service.stop(wait=5)
    a.destroy()


def pump(app, cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end and not cond():
        app.update()
        time.sleep(0.02)
    app.update()
    return cond()


def new_windows(app, before):
    return [w for w in app.winfo_children() if isinstance(w, tk.Toplevel) and w not in before]


def open_dialog(app, action):
    before = set(app.winfo_children())
    app.tree.selection_set("g")
    action()
    pump(app, lambda: new_windows(app, before), 3)
    wins = new_windows(app, before)
    assert wins, f"{action.__name__}: nessuna finestra"
    return wins[0]


def test_every_window_opens_without_errors(app):
    g = app.cfg.get_game("g")
    app.engine.sync_game(g)
    (app.saves / "Slot1" / "sav.dat").write_bytes(b"partita 2")
    app.engine.sync_game(g)
    app._refresh()

    # dettagli e spazio
    w = open_dialog(app, app.show_details)
    pump(app, lambda: False, 1.5)
    w.destroy()

    # modifica gioco: si apre e si salva senza cambiare niente
    w = open_dialog(app, app.edit_game)
    w._save()
    pump(app, lambda: not w.winfo_exists(), 2)
    assert app.cfg.get_game("g").group_by == "folder"

    # ripristino: file, versioni, anteprima
    w = open_dialog(app, app.restore)
    pump(app, lambda: w.files, 5)
    rel = "Slot1/sav.dat"
    w.flist.selection_set(w.files.index(rel))
    w._load_versions()
    pump(app, lambda: w.versions, 5)
    w.vlist.selection_set(len(w.versions) - 1)
    w._preview()
    pump(app, lambda: False, 1.5)
    w.destroy()

    # istantanee
    w = open_dialog(app, app.show_snapshots)
    pump(app, lambda: False, 0.5)
    w.destroy()

    # impostazioni: ogni tipo di cloud e ogni scheda, poi Annulla
    w = open_dialog(app, app.open_settings)
    for backend in ("folder", "gdrive", "onedrive", "s3", "rclone"):
        w.v["backend"].set(backend)
        w._toggle()
        app.update()
    w.destroy()

    # verifica del cloud dal menu
    app.tree.selection_set("g")
    app.verify_cloud()
    pump(app, lambda: any("Verifica del cloud" in str(s) for s in app.shown), 10)
    assert any("integri" in str(s) for s in app.shown)

    # copia opzioni di Steam
    app.copy_steam_options()
    app.update()

    assert not app.errors, app.errors


def test_login_works_with_window_open(app, tmp_path, monkeypatch):
    """Il login a OneDrive / Google parte dalla finestra aperta: il ritorno dal browser (una
    richiesta a 127.0.0.1) deve arrivare anche con l'app che gira. Se non arriva, il messaggio
    dice cosa ha visto il server e dove era fermo ogni thread."""
    import http.server
    import sys
    import threading
    import traceback
    import urllib.parse
    import urllib.request
    from resosync import oauth
    from resosync.vault import Vault
    from test_oauth_http import FakeResp

    monkeypatch.setattr(oauth.requests, "post", lambda url, data, timeout: FakeResp(
        200, {"access_token": "A1", "refresh_token": "R1", "expires_in": 3600}))
    events, t0 = [], time.time()

    def ev(what):
        events.append(f"{time.time() - t0:5.2f}s [{threading.current_thread().name}] {what}")

    class Server(http.server.HTTPServer):
        def server_bind(self):
            ev("server: bind")
            super().server_bind()
            ev(f"server: in ascolto su {self.server_address}")

        def get_request(self):
            conn = super().get_request()
            ev(f"server: connessione accettata da {conn[1]}")
            return conn

        def handle_error(self, request, client_address):
            ev("server: errore " + traceback.format_exc().strip().splitlines()[-1])
    monkeypatch.setattr(oauth.http.server, "HTTPServer", Server)
    client = oauth.OAuthClient("onedrive", "cid", Vault(tmp_path / "vault"))

    def browser(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        target = (q["redirect_uri"][0].replace("localhost", "127.0.0.1") +
                  f"/?code=CODE&state={q['state'][0]}")

        def follow():
            try:
                ev(f"browser: apro {target}")
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                opener.open(target, timeout=10).read()
                ev("browser: risposta ricevuta")
            except Exception as e:  # noqa: BLE001
                ev(f"browser: errore {e!r}")
        threading.Thread(target=follow, name="browser", daemon=True).start()

    def stacks():
        ev("dove sono i thread a metà attesa:")
        names = {t.ident: t.name for t in threading.enumerate()}
        for ident, frame in sys._current_frames().items():
            where = " <- ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}"
                                for f in reversed(traceback.extract_stack(frame)[-4:]))
            events.append(f"      {names.get(ident, ident)}: {where}")
    threading.Timer(5, stacks).start()
    done = {}
    t = threading.Thread(target=lambda: done.setdefault("r", _try(client.login, 10, browser)),
                         name="login")
    t.start()
    pump(app, lambda: not t.is_alive(), 15)  # la finestra resta viva mentre aspetta
    assert done.get("r") is None, f"login non riuscito: {done.get('r')}\n" + "\n".join(events)
    assert client.access_token() == "A1"


def _try(fn, *args):
    try:
        fn(*args)
    except Exception as e:  # noqa: BLE001
        return repr(e)
    return None


def test_first_sync_confirmation_dialog(app, tmp_path):
    """Il servizio chiede conferma della prima sincronizzazione: la finestra la mostra."""
    asked = []
    from tkinter import messagebox
    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    mp.setattr(messagebox, "askyesno", lambda title, text, **k: asked.append(text) or False)
    app._confirm_first("g", {"down": ["a"], "up": [], "conflicts": [
        {"name": "Slot1/", "winner": "local", "local_time": time.time(),
         "remote_time": time.time() - 60, "device": "Deck"}]})
    app.update()
    mp.undo()
    assert asked and "Slot1/" in asked[0]
    assert not app.errors, app.errors


def test_localhost_callback_diagnostics(app):
    """Come il login (server su 127.0.0.1, "browser" che torna con il codice), ma con una
    cronologia di ogni passo: se su qualche sistema non funziona con la finestra aperta, il
    messaggio dice dove si ferma."""
    import http.server
    import socket
    import threading
    import urllib.request
    events, t0 = [], time.time()

    def ev(what):
        events.append(f"{time.time() - t0:5.2f}s {threading.current_thread().name}: {what}")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            ev(f"do_GET {self.path}")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    class Server(http.server.HTTPServer):
        def handle_error(self, request, client_address):
            import traceback
            ev("errore nel server: " + traceback.format_exc().strip().splitlines()[-1])

    ev(f"timeout predefinito dei socket: {socket.getdefaulttimeout()}")
    server = Server(("127.0.0.1", 0), Handler)
    server.timeout = 1
    ev(f"server su porta {server.server_port}")
    got = []

    def browser():
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            ev("browser: apro la connessione")
            got.append(opener.open(f"http://127.0.0.1:{server.server_port}/?code=X",
                                   timeout=8).read())
            ev("browser: risposta ricevuta")
        except Exception as e:  # noqa: BLE001
            ev(f"browser: errore {e!r}")
    done = []

    def serve():
        end = time.time() + 8
        while not got and time.time() < end:
            ev("server: aspetto una richiesta")
            server.handle_request()
        server.server_close()
        done.append(True)
    threading.Thread(target=serve, name="server", daemon=True).start()
    threading.Thread(target=browser, name="browser", daemon=True).start()
    pump(app, lambda: done, 12)
    ev("thread vivi: " + ", ".join(sorted(t.name for t in threading.enumerate())))
    assert got == [b"ok"], "\n".join(events)
