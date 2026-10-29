"""Seconda caccia ai bug dopo la 0.5.1."""
import os
import subprocess
import threading
from pathlib import Path

from resosync import system
from resosync.backends import FolderBackend
from resosync.config import Config, Game, Settings
from resosync.engine import SyncEngine
from resosync.snapshots import SnapshotStore


def eng_game(tmp_path):
    eng = SyncEngine(Settings(device_name="A"), FolderBackend(tmp_path / "cloud"),
                     tmp_path / "app", log=lambda m: None)
    g = Game("g", "G", str(tmp_path / "saves"))
    Path(g.path).mkdir(parents=True, exist_ok=True)
    return eng, g


# J. un file che sparisce (o è bloccato) durante l'istantanea non deve farla fallire tutta
def test_snapshot_survives_vanishing_file(tmp_path, monkeypatch):
    eng, g = eng_game(tmp_path)
    for n in ("a.sav", "b.sav", "c.sav"):
        Path(g.path, n).write_text(n)
    files = eng.scan_local(g)
    Path(g.path, "b.sav").unlink()  # il gioco lo cancella mentre copio
    sid = SnapshotStore(eng.app_dir, "g").take(g.path, files, "test")
    assert sid is not None
    snap = SnapshotStore(eng.app_dir, "g").load(sid)
    assert set(snap["files"]) == {"a.sav", "c.sav"}


# N. due istantanee nello stesso momento (avvio gioco + ripristino) non devono cancellarsi
#    a vicenda i contenuti
def test_concurrent_snapshots_keep_their_objects(tmp_path, monkeypatch):
    import resosync.snapshots as snapmod
    eng, g = eng_game(tmp_path)
    for i in range(30):
        Path(g.path, f"s{i}.sav").write_bytes(os.urandom(2000))
    store = SnapshotStore(eng.app_dir, "g")
    real_copy = snapmod.shutil.copy2

    def slow_copy(src, dst, *a, **k):  # copia lenta: l'altra istantanea fa in tempo a pulire
        threading.Event().wait(0.01)
        return real_copy(src, dst, *a, **k)
    monkeypatch.setattr(snapmod.shutil, "copy2", slow_copy)
    files1 = eng.scan_local(g)
    errors = []

    def take(files, reason):
        try:
            SnapshotStore(eng.app_dir, "g").take(g.path, files, reason, keep=1)
        except Exception as e:
            errors.append(e)
    t = threading.Thread(target=take, args=(files1, "uno"))
    t.start()
    Path(g.path, "s0.sav").write_bytes(b"cambiato")
    take(eng.scan_local(g), "due")
    t.join()
    assert not errors
    for s in store.list():
        for e in store.load(s["id"])["files"].values():
            assert store._obj(e["hash"]).exists(), "istantanea con contenuti mancanti"


# K. Proton Drive / OneDrive tengono aperto un file per un attimo mentre lo caricano
def test_folder_backend_retries_locked_reads(tmp_path, monkeypatch):
    b = FolderBackend(tmp_path / "c")
    b.write_bytes("g/manifest.json", b"{}")
    calls = {"n": 0}
    real = Path.read_bytes

    def flaky(self):
        if self.name == "manifest.json" and calls["n"] < 2:
            calls["n"] += 1
            raise PermissionError("file in uso da un altro processo")
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", flaky)
    assert b.read_bytes("g/manifest.json") == b"{}"


# L. Steam Deck: se la sessione desktop viene chiusa (passaggio alla modalità gioco) con la
#    finestra ancora aperta, il servizio in pausa deve ripartire da solo
def test_paused_service_resumes_when_window_dies(monkeypatch):
    calls, runs = [], []
    monkeypatch.setattr(system, "SYSTEM", "Linux")
    monkeypatch.setattr(system.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(system, "_systemctl", lambda *a: calls.append(a) or
                        subprocess.CompletedProcess(a, 0, "active\n", ""))
    monkeypatch.setattr(system.subprocess, "run", lambda args, **k: runs.append(args) or
                        subprocess.CompletedProcess(args, 0, "", ""))
    assert system.pause_service() is True
    assert ("stop", "resosync.service") in calls
    watcher = [r for r in runs if r and r[0] == "systemd-run"]
    assert watcher, "nessun guardiano che faccia ripartire il servizio"
    script = " ".join(watcher[0])
    assert str(os.getpid()) in script and "start resosync.service" in script


# M. config.json rovinato (es. disco pieno): si riparte dall'ultima copia buona
def test_corrupt_config_falls_back_to_backup(tmp_path):
    cfg = Config(app_dir=tmp_path)
    cfg.settings.folder_path = str(tmp_path / "cloud")
    cfg.games.append(Game("g", "G", str(tmp_path / "s")))
    cfg.save()
    cfg.save()  # la seconda volta resta una copia della prima
    (tmp_path / "config.json").write_text('{"settings": {"folder_pa')  # troncato
    again = Config.load(tmp_path)
    assert again.get_game("g") and again.settings.folder_path == str(tmp_path / "cloud")
    assert any(p.name.startswith("config.json.rovinato") for p in tmp_path.iterdir())
