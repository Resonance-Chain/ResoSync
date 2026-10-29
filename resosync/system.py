"""Integrazione col sistema: avvio automatico, istanza singola, icona, comando per Steam."""
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

SYSTEM = platform.system()
APP_NAME = "ResoSync"


def app_command() -> list:
    """Come rilanciare questa app (exe di PyInstaller oppure python + main.py)."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        if exe.stem.lower() == "savesync":
            # avviato dalla copia col vecchio nome (vecchie opzioni Steam / avvio automatico):
            # i comandi nuovi puntano a ResoSync
            new = exe.with_name("ResoSync" + exe.suffix)
            if new.exists():
                return [str(new)]
        return [str(exe)]
    main = str(Path(__file__).resolve().parent.parent / "main.py")
    exe = sys.executable
    if SYSTEM == "Windows":
        pyw = Path(exe).with_name("pythonw.exe")  # niente finestra nera del terminale
        if pyw.exists():
            exe = str(pyw)
    return [exe, main]


def quote_cmd(args: list) -> str:
    if SYSTEM == "Windows":
        return subprocess.list2cmdline(args)
    import shlex
    return " ".join(shlex.quote(a) for a in args)


def steam_launch_options(game_id: str) -> str:
    """Da incollare in Steam > Proprietà > Opzioni di avvio.
    Su Linux/Steam Deck --quiet: in modalità gioco niente finestre sopra il gioco."""
    extra = ["--quiet"] if SYSTEM == "Linux" else []
    return quote_cmd(app_command() + extra + ["--launch", game_id]) + " %command%"


def in_game_mode() -> bool:
    """Steam Deck in modalità gioco (gamescope) o nessuno schermo: niente finestre."""
    if SYSTEM != "Linux":
        return False
    env = os.environ
    if env.get("XDG_CURRENT_DESKTOP", "").lower() == "gamescope":
        return True
    if env.get("GAMESCOPE_WAYLAND_DISPLAY") and not env.get("KDE_FULL_SESSION"):
        return True
    return not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


# ---------- servizio in background (Linux / Steam Deck) ----------
SERVICE_NAME = "resosync.service"


def _unit_file() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / \
        "user" / SERVICE_NAME


def _systemctl(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True,
                          timeout=30)


def service_supported() -> bool:
    return SYSTEM == "Linux" and shutil.which("systemctl") is not None


def service_installed() -> bool:
    return _unit_file().exists()


def service_active() -> bool:
    return service_supported() and _systemctl("is-active", SERVICE_NAME).stdout.strip() == "active"


def set_background_service(enabled: bool) -> None:
    """Servizio systemd dell'utente: parte all'accensione e lavora anche in modalità gioco."""
    unit = _unit_file()
    if not enabled:
        if unit.exists():
            _systemctl("disable", "--now", SERVICE_NAME)
            unit.unlink(missing_ok=True)
            _systemctl("daemon-reload")
        return
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(
        "[Unit]\nDescription=ResoSync - sincronizzazione dei salvataggi\n"
        "After=network-online.target\nWants=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={quote_cmd(app_command() + ['--service'])}\n"
        "Restart=on-failure\nRestartSec=30\n\n"
        "[Install]\nWantedBy=default.target\n")
    _systemctl("daemon-reload")
    r = _systemctl("enable", "--now", SERVICE_NAME)
    if r.returncode != 0:
        raise OSError(r.stderr.strip() or "systemctl non ha funzionato")


def pause_service() -> bool:
    """Ferma il servizio mentre la finestra è aperta (due copie sincronizzerebbero insieme).
    Ritorna True se era attivo, così si può riavviare alla chiusura."""
    if service_active():
        _systemctl("stop", SERVICE_NAME)
        return True
    return False


def resume_service() -> None:
    if service_supported() and service_installed():
        _systemctl("start", SERVICE_NAME)


# ---------- avvio automatico ----------
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _autostart_file() -> Path:
    if SYSTEM == "Darwin":
        return Path.home() / "Library" / "LaunchAgents" / "com.resosync.app.plist"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart" / \
        "resosync.desktop"


def set_autostart(enabled: bool) -> None:
    cmd = app_command() + ["--minimized"]
    if SYSTEM == "Windows":
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, quote_cmd(cmd))
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except FileNotFoundError:
                    pass
        return
    f = _autostart_file()
    if not enabled:
        f.unlink(missing_ok=True)
        return
    f.parent.mkdir(parents=True, exist_ok=True)
    if SYSTEM == "Darwin":
        args = "".join(f"<string>{a}</string>" for a in cmd)
        f.write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                     '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                     '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                     '<plist version="1.0"><dict><key>Label</key><string>com.resosync.app'
                     f'</string><key>ProgramArguments</key><array>{args}</array>'
                     '<key>RunAtLoad</key><true/></dict></plist>\n')
    else:
        f.write_text(f"[Desktop Entry]\nType=Application\nName={APP_NAME}\n"
                     f"Exec={quote_cmd(cmd)}\nX-GNOME-Autostart-enabled=true\n")


LEGACY_NAME = "SaveSync"  # nome del progetto fino alla 0.4.0


def migrate_from_savesync() -> list:
    """Avvio automatico e servizio registrati quando il programma si chiamava SaveSync: li
    tolgo e li registro col nome nuovo, sennò all'accensione partirebbero tutti e due."""
    done = []
    if SYSTEM == "Windows":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                                winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as k:
                winreg.QueryValueEx(k, LEGACY_NAME)
                winreg.DeleteValue(k, LEGACY_NAME)
            set_autostart(True)
            done.append("avvio automatico")
        except OSError:
            pass
        return done
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    legacy_autostart = config / "autostart" / "savesync.desktop"
    if legacy_autostart.exists():
        legacy_autostart.unlink()
        set_autostart(True)
        done.append("avvio automatico")
    legacy_unit = config / "systemd" / "user" / "savesync.service"
    if legacy_unit.exists() and service_supported():
        _systemctl("disable", "--now", "savesync.service")
        legacy_unit.unlink(missing_ok=True)
        _systemctl("daemon-reload")
        set_background_service(True)
        done.append("servizio in background")
    return done


def is_autostart_enabled() -> bool:
    if SYSTEM == "Windows":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
                winreg.QueryValueEx(k, APP_NAME)
                return True
        except OSError:
            return False
    return _autostart_file().exists()


# ---------- istanza singola ----------
class SingleInstance:
    """La prima istanza ascolta su 127.0.0.1; se ne parte un'altra (doppio clic sull'exe
    mentre l'app è nella tray) le chiede di mostrare la finestra e si chiude."""

    def __init__(self, app_dir: Path):
        self.port_file = Path(app_dir) / "instance.port"
        self._server = None

    def already_running(self) -> bool:
        try:
            port = int(self.port_file.read_text())
            with socket.create_connection(("127.0.0.1", port), timeout=1) as s:
                s.sendall(b"show\n")
                return s.recv(16).startswith(b"ok")
        except (OSError, ValueError):
            return False

    def listen(self, on_show) -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(4)
        self.port_file.parent.mkdir(parents=True, exist_ok=True)
        self.port_file.write_text(str(self._server.getsockname()[1]))

        def loop():
            while True:
                try:
                    conn, _ = self._server.accept()
                except OSError:
                    return
                with conn:
                    try:
                        if conn.recv(16).startswith(b"show"):
                            conn.sendall(b"ok\n")
                            on_show()
                    except OSError:
                        pass
        threading.Thread(target=loop, daemon=True).start()

    def close(self):
        if self._server:
            self._server.close()
            self.port_file.unlink(missing_ok=True)


# ---------- icona ----------
def icon_image(size: int = 64):
    """Icona disegnata al volo: nuvola bianca con freccia su sfondo blu."""
    from PIL import Image, ImageDraw
    s = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=int(14 * s), fill=(37, 99, 235))
    white = (255, 255, 255)
    for x0, y0, x1, y1 in [(10, 26, 34, 50), (22, 14, 48, 40), (34, 24, 56, 46)]:
        d.ellipse((x0 * s, y0 * s, x1 * s, y1 * s), fill=white)
    d.rectangle((20 * s, 36 * s, 46 * s, 50 * s), fill=white)
    blue = (37, 99, 235)
    d.polygon([(32 * s, 22 * s), (41 * s, 32 * s), (23 * s, 32 * s)], fill=blue)
    d.rectangle((29 * s, 31 * s, 35 * s, 44 * s), fill=blue)
    return img


def open_in_file_manager(path):
    if SYSTEM == "Windows":
        os.startfile(path)
    elif SYSTEM == "Darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])
