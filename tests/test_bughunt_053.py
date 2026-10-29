"""Caccia ai bug per la 0.5.3: nessun modo di perdere un salvataggio o di rovinarlo.
Ogni test riproduce un caso in cui succedeva."""
import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

import resosync.engine as em
from resosync.backends import FolderBackend
from resosync.config import Game, Settings
from resosync.engine import SyncEngine
from resosync.snapshots import SnapshotStore


def pc(tmp_path, name, cloud="cloud", **kw):
    eng = SyncEngine(Settings(device_name=name, **kw), FolderBackend(tmp_path / cloud),
                     tmp_path / f"app-{name}", log=lambda m: None)
    g = Game("g", "G", str(tmp_path / f"saves-{name}"))
    Path(g.path).mkdir(parents=True, exist_ok=True)
    return eng, g


def write(g, rel, data, mtime=None):
    p = Path(g.path, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime:
        os.utime(p, (mtime, mtime))


def read(g, rel):
    return Path(g.path, rel).read_bytes()


def manifest(tmp_path, cloud="cloud"):
    return json.loads((tmp_path / cloud / "g/manifest.json").read_bytes())["files"]


def cloud_content(tmp_path, rel, cloud="cloud"):
    return (tmp_path / cloud / manifest(tmp_path, cloud)[rel]["blob"]).read_bytes()


# 1. il gioco scrive il file proprio mentre lo carico: sul cloud non deve finire un
#    salvataggio a metà (che poi l'altro PC scaricherebbe)
def test_file_written_during_upload_is_not_uploaded_half(tmp_path, monkeypatch):
    a, ga = pc(tmp_path, "A")
    write(ga, "s.sav", b"A" * 1000, 1000)
    real = shutil.copy2

    def copy_while_game_writes(src, dst, *args, **kw):
        out = real(src, dst, *args, **kw)
        if Path(src).name == "s.sav":
            with open(dst, "r+b") as f:  # la copia ha preso metà vecchio e metà nuovo
                f.write(b"B" * 500)
            Path(src).write_bytes(b"B" * 1000)
            os.utime(src, (1100, 1100))
        return out
    monkeypatch.setattr(em.shutil, "copy2", copy_while_game_writes)
    st = a.sync_game(ga)
    monkeypatch.setattr(em.shutil, "copy2", real)
    assert st["up"] == 0 and st["errors"] == 1
    assert not (tmp_path / "cloud/g/manifest.json").exists() or "s.sav" not in manifest(tmp_path)
    a.sync_game(ga)
    assert cloud_content(tmp_path, "s.sav") == b"B" * 1000


# 2. il gioco salva tra la scansione e il caricamento: la decisione è stata presa su un
#    file che non c'è più, si rifà al giro dopo
def test_file_changed_after_scan_is_rechecked(tmp_path, monkeypatch):
    a, ga = pc(tmp_path, "A")
    write(ga, "s.sav", b"vecchio", 1000)
    real = a.scan_local

    def scan_then_game_saves(game):
        out = real(game)
        write(ga, "s.sav", b"nuovo, salvato adesso", 1100)
        return out
    monkeypatch.setattr(a, "scan_local", scan_then_game_saves)
    st = a.sync_game(ga)
    assert st["up"] == 0 and st["errors"] == 1
    monkeypatch.setattr(a, "scan_local", real)
    a.sync_game(ga)
    assert cloud_content(tmp_path, "s.sav") == b"nuovo, salvato adesso"


# 3. sto scaricando il salvataggio dell'altro PC e intanto il gioco salva qui: il
#    salvataggio appena fatto non deve essere sovrascritto
def test_local_save_during_download_is_not_overwritten(tmp_path, monkeypatch):
    a, ga = pc(tmp_path, "A")
    b, gb = pc(tmp_path, "B")
    write(ga, "s.sav", b"uno", 1000)
    a.sync_game(ga)
    b.sync_game(gb)
    write(ga, "s.sav", b"due (da A)", 2000)
    a.sync_game(ga)
    real = b._fetch

    def fetch_while_game_saves(game, rel, entry, key=None):
        tmp = real(game, rel, entry, key)
        write(gb, "s.sav", b"tre (B ha appena salvato)", 3000)
        return tmp
    monkeypatch.setattr(b, "_fetch", fetch_while_game_saves)
    b.sync_game(gb)
    monkeypatch.setattr(b, "_fetch", real)
    assert read(gb, "s.sav") == b"tre (B ha appena salvato)"
    b.sync_game(gb)  # al giro dopo: conflitto, vince il più recente (quello di B)
    a.sync_game(ga)
    assert read(ga, "s.sav") == read(gb, "s.sav") == b"tre (B ha appena salvato)"


def test_local_file_created_during_sync_is_not_deleted(tmp_path, monkeypatch):
    """Cancellazione arrivata dal cloud, ma nel frattempo il gioco ha rifatto il file."""
    a, ga = pc(tmp_path, "A", propagate_deletes=True)
    b, gb = pc(tmp_path, "B", propagate_deletes=True)
    for g in (ga,):
        write(g, "1.sav", b"1", 1000)
        write(g, "2.sav", b"2", 1000)
    a.sync_game(ga)
    b.sync_game(gb)
    Path(ga.path, "1.sav").unlink()
    a.sync_game(ga)
    real = b.scan_local

    def scan_then_game_saves(game):
        out = real(game)
        write(gb, "1.sav", b"salvato adesso", 5000)
        return out
    monkeypatch.setattr(b, "scan_local", scan_then_game_saves)
    b.sync_game(gb)
    assert read(gb, "1.sav") == b"salvato adesso"


# 4. salvataggio fatto di più file: se la sostituzione si blocca a metà (file aperto da
#    antivirus o dal gioco), non deve restare il sav.dat nuovo con il metadata vecchio
def test_group_apply_failure_rolls_back(tmp_path, monkeypatch):
    a, ga = pc(tmp_path, "A")
    b, gb = pc(tmp_path, "B")
    ga.group_by = gb.group_by = "folder"
    write(ga, "Slot1/sav.dat", b"dat v1", 1000)
    write(ga, "Slot1/meta.json", b"meta v1", 1000)
    a.sync_game(ga)
    b.sync_game(gb)
    write(ga, "Slot1/sav.dat", b"dat v2", 2000)
    write(ga, "Slot1/meta.json", b"meta v2", 2000)
    a.sync_game(ga)
    real = em.replace_retry
    calls = []

    def replace_fails_second(src, dst, *args, **kw):
        if str(src).endswith(".resosync-tmp"):
            calls.append(dst)
            if len(calls) == 2:
                raise PermissionError("file aperto da un altro programma")
        return real(src, dst, *args, **kw)
    monkeypatch.setattr(em, "replace_retry", replace_fails_second)
    st = b.sync_game(gb)
    monkeypatch.setattr(em, "replace_retry", real)
    assert st["errors"] == 1
    assert (read(gb, "Slot1/sav.dat"), read(gb, "Slot1/meta.json")) == (b"dat v1", b"meta v1")
    assert not [p for p in Path(gb.path).rglob("*") if p.name.endswith(
        (".resosync-tmp", ".resosync-old"))]
    b.sync_game(gb)
    assert (read(gb, "Slot1/sav.dat"), read(gb, "Slot1/meta.json")) == (b"dat v2", b"meta v2")


# 5. ripristino di una versione dal cloud: se il file sul cloud è rovinato (caricamento a
#    metà) non deve finire nella cartella del gioco
def test_restore_corrupt_cloud_version_refused(tmp_path):
    a, ga = pc(tmp_path, "A")
    write(ga, "s.sav", b"buono", 1000)
    a.sync_game(ga)
    write(ga, "s.sav", b"attuale", 2000)
    a.sync_game(ga)
    old = next(v for v in a.list_versions(ga, "s.sav") if v[1].startswith("cloud")
               and (tmp_path / "cloud" / v[2]).read_bytes() == b"buono")
    (tmp_path / "cloud" / old[2]).write_bytes(b"bu")  # troncato
    with pytest.raises(IOError):
        a.restore_version(ga, "s.sav", old[1], old[2])
    assert read(ga, "s.sav") == b"attuale"


def test_restore_local_backup_is_atomic(tmp_path, monkeypatch):
    """Il backup locale si copia accanto e poi si sostituisce: un errore a metà copia non
    lascia il salvataggio troncato."""
    a, ga = pc(tmp_path, "A")
    b, gb = pc(tmp_path, "B")
    write(ga, "s.sav", b"uno", 1000)
    a.sync_game(ga)
    b.sync_game(gb)
    write(ga, "s.sav", b"due", 2000)
    a.sync_game(ga)
    b.sync_game(gb)  # B ha "uno" nei backup locali
    backup = next(v for v in b.list_versions(gb, "s.sav") if v[1] == "backup locale")

    def broken_copy(src, dst, *args, **kw):
        Path(dst).write_bytes(b"x")
        raise OSError("disco pieno")
    monkeypatch.setattr(em.shutil, "copy2", broken_copy)
    with pytest.raises(OSError):
        b.restore_version(gb, "s.sav", backup[1], backup[2], snapshot=False)
    assert read(gb, "s.sav") == b"due"


# 6. istantanee: un contenuto rovinato sul disco non si rimette, e un file attuale che
#    l'istantanea di sicurezza non è riuscita a salvare non si sovrascrive
def test_snapshot_restore_refuses_corrupt_object(tmp_path):
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "s.sav").write_bytes(b"ieri")
    store = SnapshotStore(tmp_path / "app", "g")
    eng = SyncEngine(Settings(), FolderBackend(tmp_path / "cloud"), tmp_path / "app",
                     log=lambda m: None)
    game = Game("g", "G", str(saves))
    snap = store.take(saves, eng.scan_local(game), "test")
    h = hashlib.sha256(b"ieri").hexdigest()
    store._obj(h).write_bytes(b"rovinato")
    (saves / "s.sav").write_bytes(b"oggi")
    store.take(saves, eng.scan_local(game), "prima di ripristinare")
    with pytest.raises(IOError):
        store.restore(saves, snap)
    assert (saves / "s.sav").read_bytes() == b"oggi"


def test_snapshot_restore_refuses_unprotected_file(tmp_path):
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "s.sav").write_bytes(b"ieri")
    store = SnapshotStore(tmp_path / "app", "g")
    eng = SyncEngine(Settings(), FolderBackend(tmp_path / "cloud"), tmp_path / "app",
                     log=lambda m: None)
    game = Game("g", "G", str(saves))
    snap = store.take(saves, eng.scan_local(game), "test")
    (saves / "s.sav").write_bytes(b"oggi")  # l'istantanea di sicurezza non c'è
    with pytest.raises(IOError):
        store.restore(saves, snap)
    assert (saves / "s.sav").read_bytes() == b"oggi"


# 7. cancellazioni di massa parziali (gioco reinstallato che lascia un solo file, cartella
#    ripulita a metà): non si propagano agli altri PC
def test_partial_mass_deletion_not_propagated(tmp_path):
    a, ga = pc(tmp_path, "A", propagate_deletes=True)
    b, gb = pc(tmp_path, "B", propagate_deletes=True)
    for i in range(6):
        write(ga, f"{i}.sav", b"x%d" % i, 1000)
    write(ga, "config.ini", b"cfg", 1000)
    a.sync_game(ga)
    b.sync_game(gb)
    for i in range(6):
        Path(ga.path, f"{i}.sav").unlink()  # resta solo config.ini
    st = a.sync_game(ga)
    assert st["deleted"] == 0
    b.sync_game(gb)
    assert all(Path(gb.path, f"{i}.sav").exists() for i in range(6))


# 8. orologio sbagliato su un dispositivo (Steam Deck rimasto offline, batteria tampone
#    scarica): un salvataggio nuovo ma con la data "nel passato" non è un ritorno indietro
#    del cloud, e non deve essere scartato
@pytest.mark.parametrize("group", ["none", "folder"])
def test_device_with_clock_behind_does_not_lose_progress(tmp_path, group):
    a, ga = pc(tmp_path, "PC")
    b, gb = pc(tmp_path, "Deck")
    ga.group_by = gb.group_by = group
    write(ga, "Slot/s.sav", b"PC: capitolo 1", 10_000)
    write(ga, "Slot/meta.json", b"m1", 10_000)
    a.sync_game(ga)
    b.sync_game(gb)
    # sul Deck si gioca DOPO, ma il suo orologio è un'ora indietro
    write(gb, "Slot/s.sav", b"Deck: capitolo 2", 10_000 + 600 - 3600)
    write(gb, "Slot/meta.json", b"m2", 10_000 + 600 - 3600)
    b.sync_game(gb)
    a.sync_game(ga)
    assert read(ga, "Slot/s.sav") == b"Deck: capitolo 2"
    assert read(ga, "Slot/meta.json") == b"m2"
    b.sync_game(gb)
    assert read(gb, "Slot/s.sav") == b"Deck: capitolo 2"


def test_stale_manifest_still_detected_with_ancestry(tmp_path):
    """Il controllo nuovo non deve indebolire quello vecchio: un manifest arrivato in
    ritardo con una versione intermedia mai vista resta un ritorno indietro."""
    a, ga = pc(tmp_path, "A")
    b, gb = pc(tmp_path, "B")
    write(ga, "s.sav", b"v1", 1000)
    a.sync_game(ga)
    b.sync_game(gb)
    write(ga, "s.sav", b"v2", 2000)
    a.sync_game(ga)
    stale = (tmp_path / "cloud/g/manifest.json").read_bytes()  # con v2, che B non ha visto
    write(ga, "s.sav", b"v3", 3000)
    a.sync_game(ga)
    b.sync_game(gb)
    assert read(gb, "s.sav") == b"v3"
    (tmp_path / "cloud/g/manifest.json").write_bytes(stale)
    b.sync_game(gb)
    assert read(gb, "s.sav") == b"v3"
    a.sync_game(ga)
    assert read(ga, "s.sav") == b"v3" and cloud_content(tmp_path, "s.sav") == b"v3"


# 9. launcher: il manifest è arrivato dal client cloud ma il salvataggio vero no (è più
#    grande, arriva dopo). Il gioco non deve partire col salvataggio vecchio senza dirlo
def test_launcher_waits_for_save_still_arriving(tmp_path, monkeypatch):
    import sys
    import threading
    import time
    from test_deck import use_app_dir
    from resosync import launcher
    from resosync.config import Config
    use_app_dir(monkeypatch, tmp_path / "app")
    cfg = Config(app_dir=tmp_path / "app")
    cfg.settings.folder_path = str(tmp_path / "cloud")
    cfg.settings.device_name = "Deck"
    saves = tmp_path / "saves"
    saves.mkdir()
    cfg.games.append(Game("g", "Gioco", str(saves)))
    cfg.save()
    (saves / "slot.sav").write_bytes(b"vecchia")
    SyncEngine(cfg.settings, FolderBackend(tmp_path / "cloud"), cfg.app_dir,
               log=lambda m: None).sync_game(cfg.games[0])
    other, og = pc(tmp_path, "PC")
    write(og, "slot.sav", b"nuova dal PC", time.time() + 5)
    other.sync_game(og)
    blob = tmp_path / "cloud" / manifest(tmp_path)["slot.sav"]["blob"]
    parked = tmp_path / "in-arrivo"
    blob.rename(parked)
    threading.Timer(4, lambda: parked.rename(blob)).start()  # il client cloud lo consegna
    seen = tmp_path / "visto-dal-gioco"
    game_cmd = [sys.executable, "-c", f"open({str(seen)!r},'wb').write("
                                      f"open({str(saves / 'slot.sav')!r},'rb').read())"]
    assert launcher.run("g", game_cmd, quiet=True) == 0
    assert seen.read_bytes() == b"nuova dal PC"

