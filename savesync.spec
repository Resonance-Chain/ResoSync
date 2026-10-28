# Build: pyinstaller savesync.spec   ->  dist/SaveSync/SaveSync.exe
# Cartella (onedir) e non file unico: parte più in fretta (conta quando lo lanci da Steam)
# e gli antivirus lo segnalano molto meno.
import sys

from PyInstaller.utils.hooks import collect_submodules

# pystray sceglie il backend a runtime: va indicato a mano (importarlo qui fallirebbe senza schermo)
tray = {"win32": ["pystray._win32"], "darwin": ["pystray._darwin"]}.get(
    sys.platform, ["pystray._xorg", "pystray._appindicator", "pystray._gtk"])
hidden = tray + collect_submodules("keyring.backends") + collect_submodules("watchdog.observers")

a = Analysis(["main.py"], hiddenimports=hidden, excludes=["pytest", "tkinter.test"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="SaveSync", console=False,
          icon="assets/icon.ico", upx=False, version=None)
coll = COLLECT(exe, a.binaries, a.datas, name="SaveSync", upx=False)
