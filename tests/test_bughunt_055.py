"""Caccia ai bug per la 0.5.5."""
from pathlib import Path

import pytest

from resosync.backends import FolderBackend
from resosync.config import Game, Settings
from resosync.crypto import CryptoError, EncryptedBackend
from resosync.engine import SyncEngine

PASS = "passphrase di prova"


# 1. PC nuovo su un cloud cifrato, mentre il client cloud sta ancora scaricando la cartella:
#    crypto.json non è ancora arrivato. Il PC nuovo NON deve crearne uno con un sale nuovo
#    (sovrascriverebbe quello vero e tutto il cloud diventerebbe illeggibile)
def test_missing_crypto_meta_on_non_empty_cloud_is_not_recreated(tmp_path):
    cloud = tmp_path / "cloud"
    s = Settings(device_name="PC", encrypt=True, passphrase=PASS)
    eng = SyncEngine(s, EncryptedBackend(FolderBackend(cloud), PASS), tmp_path / "a",
                     log=lambda m: None)
    g = Game("g", "G", str(tmp_path / "saves"))
    Path(g.path).mkdir()
    Path(g.path, "s.sav").write_bytes(b"progressi")
    eng.sync_game(g)
    meta = (cloud / "crypto.json").read_bytes()
    (cloud / "crypto.json").rename(tmp_path / "in-arrivo")  # non ancora scaricato
    with pytest.raises(CryptoError, match="crypto.json"):
        EncryptedBackend(FolderBackend(cloud), PASS)
    assert not (cloud / "crypto.json").exists()
    (tmp_path / "in-arrivo").rename(cloud / "crypto.json")
    assert (cloud / "crypto.json").read_bytes() == meta
    other = SyncEngine(s, EncryptedBackend(FolderBackend(cloud), PASS), tmp_path / "b",
                       log=lambda m: None)
    g2 = Game("g", "G", str(tmp_path / "saves2"))
    other.sync_game(g2)
    assert Path(g2.path, "s.sav").read_bytes() == b"progressi"


def test_empty_cloud_still_gets_encryption(tmp_path):
    EncryptedBackend(FolderBackend(tmp_path / "nuovo"), PASS)
    assert (tmp_path / "nuovo/crypto.json").exists()


def test_turning_on_encryption_on_plain_cloud_still_works(tmp_path):
    cloud = tmp_path / "cloud"
    eng = SyncEngine(Settings(device_name="PC"), FolderBackend(cloud), tmp_path / "a",
                     log=lambda m: None)
    g = Game("g", "G", str(tmp_path / "saves"))
    Path(g.path).mkdir()
    Path(g.path, "s.sav").write_bytes(b"in chiaro")
    eng.sync_game(g)
    EncryptedBackend(FolderBackend(cloud), PASS)  # nessun errore: si attiva la cifratura
    assert (cloud / "crypto.json").exists()


# 2. ResoSync chiuso di colpo (spegnimento, aggiornamento, crash) a metà della sostituzione
#    di un salvataggio da più file: restano il sav.dat nuovo e il metadata vecchio, più le
#    copie .resosync-old. Al giro dopo NON deve caricare quello slot mescolato come vincente
def test_interrupted_group_apply_is_recovered(tmp_path):
    import json
    a = SyncEngine(Settings(device_name="A"), FolderBackend(tmp_path / "cloud"),
                   tmp_path / "app-A", log=lambda m: None)
    b = SyncEngine(Settings(device_name="B"), FolderBackend(tmp_path / "cloud"),
                   tmp_path / "app-B", log=lambda m: None)
    ga = Game("g", "G", str(tmp_path / "A"), group_by="folder")
    gb = Game("g", "G", str(tmp_path / "B"), group_by="folder")
    for g, v in ((ga, b"v1"), (gb, None)):
        Path(g.path, "Slot").mkdir(parents=True)
    Path(ga.path, "Slot/sav.dat").write_bytes(b"dat v1")
    Path(ga.path, "Slot/meta.json").write_bytes(b"meta v1")
    a.sync_game(ga)
    b.sync_game(gb)
    Path(ga.path, "Slot/sav.dat").write_bytes(b"dat v2")
    Path(ga.path, "Slot/meta.json").write_bytes(b"meta v2")
    a.sync_game(ga)
    # B stava applicando v2 ed è stato chiuso dopo il primo file: com'è rimasto il disco
    slot = Path(gb.path, "Slot")
    for name in ("sav.dat", "meta.json"):
        (slot / (name + ".resosync-old")).write_bytes((slot / name).read_bytes())
    m = json.loads((tmp_path / "cloud/g/manifest.json").read_bytes())["files"]
    (slot / "sav.dat").write_bytes((tmp_path / "cloud" / m["Slot/sav.dat"]["blob"]).read_bytes())
    b.sync_game(gb)
    assert ((slot / "sav.dat").read_bytes(), (slot / "meta.json").read_bytes()) == \
        (b"dat v2", b"meta v2")
    assert not list(slot.glob("*.resosync-old"))
    m = json.loads((tmp_path / "cloud/g/manifest.json").read_bytes())["files"]
    cloud = {r: (tmp_path / "cloud" / e["blob"]).read_bytes() for r, e in m.items()}
    assert cloud == {"Slot/sav.dat": b"dat v2", "Slot/meta.json": b"meta v2"}


def test_interrupted_apply_with_new_file_is_recovered(tmp_path, monkeypatch):
    """Lo slot vincente ha un file in più (shot.png) che qui non c'era: anche quello va tolto
    se la sostituzione è stata interrotta, sennò lo slot resta mescolato."""
    import resosync.engine as em
    a = SyncEngine(Settings(device_name="A"), FolderBackend(tmp_path / "cloud"),
                   tmp_path / "app-A", log=lambda m: None)
    b = SyncEngine(Settings(device_name="B"), FolderBackend(tmp_path / "cloud"),
                   tmp_path / "app-B", log=lambda m: None)
    ga = Game("g", "G", str(tmp_path / "A"), group_by="folder")
    gb = Game("g", "G", str(tmp_path / "B"), group_by="folder")
    Path(ga.path, "Slot").mkdir(parents=True)
    Path(ga.path, "Slot/sav.dat").write_bytes(b"dat v1")
    Path(ga.path, "Slot/meta.json").write_bytes(b"meta v1")
    a.sync_game(ga)
    b.sync_game(gb)
    for name, data in (("sav.dat", b"dat v2"), ("meta.json", b"meta v2"), ("shot.png", b"png")):
        Path(ga.path, "Slot", name).write_bytes(data)
    a.sync_game(ga)

    class Killed(BaseException):
        """ResoSync chiuso di colpo: niente rollback, niente finally."""
    real_commit, real_unlink = em.SyncEngine._commit, Path.unlink
    done = []

    def commit_then_die(self, game, rel, tmp, *args, **kw):
        out = real_commit(self, game, rel, tmp, *args, **kw)
        done.append(rel)
        if rel.endswith("shot.png"):  # scritto il file nuovo, poi si spegne tutto
            monkeypatch.setattr(Path, "unlink", lambda *a, **k: None)  # nessuna pulizia
            raise Killed()
        return out
    monkeypatch.setattr(em.SyncEngine, "_commit", commit_then_die)
    with pytest.raises(Killed):
        b.sync_game(gb)
    monkeypatch.setattr(em.SyncEngine, "_commit", real_commit)
    monkeypatch.setattr(Path, "unlink", real_unlink)
    for p in Path(gb.path).rglob("*.resosync-tmp"):
        p.unlink()
    (tmp_path / "cloud/g/lock.json").unlink()  # il lock di un PC piantato scade da solo
    b.sync_game(gb)
    slot = Path(gb.path, "Slot")
    got = {p.name: p.read_bytes() for p in slot.iterdir()}
    assert got == {"sav.dat": b"dat v2", "meta.json": b"meta v2", "shot.png": b"png"}
    a.sync_game(ga)
    assert {p.name: p.read_bytes() for p in Path(ga.path, "Slot").iterdir()} == got


def test_recovery_waits_for_running_sync(tmp_path):
    """Le copie .resosync-old di una sync IN CORSO (l'altra istanza ha il lock) non vanno
    toccate."""
    from resosync.engine import CloudBusy
    eng = SyncEngine(Settings(device_name="A"), FolderBackend(tmp_path / "cloud"),
                     tmp_path / "app", log=lambda m: None)
    g = Game("g", "G", str(tmp_path / "saves"), group_by="folder")
    Path(g.path, "Slot").mkdir(parents=True)
    Path(g.path, "Slot/sav.dat").write_bytes(b"nuovo, in scrittura")
    Path(g.path, "Slot/sav.dat.resosync-old").write_bytes(b"vecchio")
    other = SyncEngine(Settings(device_name="launcher"), FolderBackend(tmp_path / "cloud"),
                       tmp_path / "app", log=lambda m: None)
    other.register_game(g)
    token = other.acquire_lock("g")
    with pytest.raises(CloudBusy):
        eng.sync_game(g)
    assert Path(g.path, "Slot/sav.dat").read_bytes() == b"nuovo, in scrittura"
    other.release_lock("g", token)


# 3. una sync lunga (salvataggi grandi, connessione lenta) non deve perdere il lock a metà:
#    un altro PC entrerebbe mentre sta ancora scrivendo
def test_lock_is_kept_during_long_sync(tmp_path, monkeypatch):
    import time
    import resosync.engine as em
    from resosync.engine import CloudBusy
    monkeypatch.setattr(em, "LOCK_TTL", 1.5)
    a = SyncEngine(Settings(device_name="A"), FolderBackend(tmp_path / "cloud"),
                   tmp_path / "a", log=lambda m: None)
    b = SyncEngine(Settings(device_name="B"), FolderBackend(tmp_path / "cloud"),
                   tmp_path / "b", log=lambda m: None)
    token = a.acquire_lock("g")
    try:
        time.sleep(3.5)  # più del doppio della durata del lock
        with pytest.raises(CloudBusy):
            b.acquire_lock("g")
    finally:
        a.release_lock("g", token)
    b.release_lock("g", b.acquire_lock("g"))  # liberato: ora B entra
    time.sleep(2)  # e il lock di A non torna (il rinnovo si è fermato)
    b.release_lock("g", b.acquire_lock("g"))


# 4. chiudendo ResoSync (uscita, aggiornamento, spegnimento del Deck) si aspetta la fine
#    della sync in corso invece di interromperla a metà
def test_service_stop_waits_for_running_sync(tmp_path):
    import threading
    import time
    from resosync import service
    from resosync.config import Config
    cfg = Config(app_dir=tmp_path / "app")
    saves = tmp_path / "saves"
    saves.mkdir()
    cfg.games.append(Game("g", "G", str(saves)))
    eng = SyncEngine(cfg.settings, FolderBackend(tmp_path / "cloud"), cfg.app_dir,
                     log=lambda m: None)
    started, finished = threading.Event(), threading.Event()

    def slow_sync(game, wait_lock=0.0):
        with eng.lock:
            started.set()
            time.sleep(2)
            finished.set()
        return {"up": 0, "down": 0, "conflicts": 0, "deleted": 0, "errors": 0}
    eng.sync_game = slow_sync
    svc = service.SyncService(cfg, eng, lambda *a: None)
    svc.start()
    assert started.wait(5)
    svc.stop()
    assert finished.is_set(), "chiuso a metà sync"


# 5. backup locali di "Saves" (file) e di "Saves/x" (file in una cartella con lo stesso nome,
#    in un altro momento): le cartelle dei backup si sovrappongono
def test_local_backups_with_file_and_folder_of_same_name(tmp_path):
    eng = SyncEngine(Settings(device_name="A"), FolderBackend(tmp_path / "cloud"),
                     tmp_path / "app", log=lambda m: None)
    g = Game("g", "G", str(tmp_path / "saves"))
    Path(g.path, "Saves").mkdir(parents=True)
    Path(g.path, "Saves/x").write_bytes(b"vecchio x")
    eng._backup_local(g, "Saves/x")
    Path(g.path, "Saves/x").unlink()
    Path(g.path, "Saves").rmdir()
    Path(g.path, "Saves").write_bytes(b"ora e' un file")
    eng._backup_local(g, "Saves")  # non deve fallire
    versions = eng.list_versions(g, "Saves")
    assert [Path(v[2]).read_bytes() for v in versions] == [b"ora e' un file"]
