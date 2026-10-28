"""Un test per ogni bug trovato dopo la 0.2.0."""
import json
import os
from pathlib import Path

import pytest

from savesync.backends import FolderBackend
from savesync.config import Game, Settings
from savesync.engine import SyncEngine


def pc(tmp_path, name, cloud="cloud", **settings):
    s = Settings(device_name=name, **settings)
    logs = []
    eng = SyncEngine(s, FolderBackend(tmp_path / cloud), tmp_path / f"app-{name}",
                     log=logs.append)
    game = Game("g", "G", str(tmp_path / f"saves-{name}"))
    Path(game.path).mkdir(parents=True, exist_ok=True)
    return eng, game, logs


def write(game, rel, data, mtime):
    p = Path(game.path, rel)
    p.write_bytes(data)
    os.utime(p, (mtime, mtime))


def test_stale_manifest_never_downgrades_save(tmp_path):
    """Un manifest vecchio arrivato in ritardo (cartella cloud) non deve far tornare
    indietro un salvataggio: si ricarica quello più recente."""
    a, ga, logs = pc(tmp_path, "A")
    write(ga, "s.sav", b"vecchio", 1000)
    a.sync_game(ga)
    stale = (tmp_path / "cloud/g/manifest.json").read_bytes()
    write(ga, "s.sav", b"nuovo", 2000)
    a.sync_game(ga)
    (tmp_path / "cloud/g/manifest.json").write_bytes(stale)  # il cloud "torna indietro"
    st = a.sync_game(ga)
    assert Path(ga.path, "s.sav").read_bytes() == b"nuovo"
    assert st["up"] == 1 and any("più vecchia" in m for m in logs)
    m = json.loads((tmp_path / "cloud/g/manifest.json").read_bytes())
    assert Path(m["files"]["s.sav"]["blob"]).name.endswith(m["files"]["s.sav"]["hash"][:16])


def test_newer_remote_save_still_downloads(tmp_path):
    a, ga, _ = pc(tmp_path, "A")
    b, gb, _ = pc(tmp_path, "B")
    write(ga, "s.sav", b"uno", 1000)
    a.sync_game(ga)
    b.sync_game(gb)
    write(gb, "s.sav", b"due", 2000)
    b.sync_game(gb)
    assert a.sync_game(ga)["down"] == 1
    assert Path(ga.path, "s.sav").read_bytes() == b"due"


def test_locked_file_is_not_treated_as_deleted(tmp_path, monkeypatch):
    """Su Windows il gioco può tenere il file bloccato: non è una cancellazione."""
    a, ga, _ = pc(tmp_path, "A", propagate_deletes=True)
    write(ga, "s.sav", b"dati", 1000)
    write(ga, "altro.sav", b"x", 1000)
    a.sync_game(ga)
    import savesync.engine as em
    real = em.sha256_file
    monkeypatch.setattr(em, "sha256_file", lambda p: (_ for _ in ()).throw(
        PermissionError("in uso")) if Path(p).name == "s.sav" else real(p))
    a._hash_cache.clear()
    write(ga, "s.sav", b"dati2", 1100)  # cambia, ma non si riesce a leggere
    st = a.sync_game(ga)
    assert st["deleted"] == 0 and st["errors"] == 1
    m = json.loads((tmp_path / "cloud/g/manifest.json").read_bytes())
    assert not m["files"]["s.sav"].get("deleted")


def test_failed_download_does_not_flood_backups(tmp_path):
    a, ga, _ = pc(tmp_path, "A", max_versions=3)
    b, gb, _ = pc(tmp_path, "B", max_versions=3)
    for i, data in enumerate([b"v1", b"v2", b"v3"]):  # B ha una cronologia di backup
        write(ga, "s.sav", data, 1000 + i)
        a.sync_game(ga)
        b.sync_game(gb)
    before = sorted(p.read_bytes() for p in (tmp_path / "app-B/backups/g/s.sav").iterdir())
    write(ga, "s.sav", b"v4", 2000)
    a.sync_game(ga)
    blob = next((tmp_path / "cloud/g/blobs/s.sav").glob("*" + json.loads(
        (tmp_path / "cloud/g/manifest.json").read_bytes())["files"]["s.sav"]["hash"][:16]))
    blob.write_bytes(b"rotto")  # file sul cloud rovinato / a metà
    for _ in range(5):
        assert b.sync_game(gb)["errors"] == 1
    after = sorted(p.read_bytes() for p in (tmp_path / "app-B/backups/g/s.sav").iterdir())
    assert after == before


def test_mass_disappearance_does_not_wipe_cloud(tmp_path):
    a, ga, logs = pc(tmp_path, "A", propagate_deletes=True)
    for i in range(3):
        write(ga, f"{i}.sav", b"x%d" % i, 1000)
    a.sync_game(ga)
    for i in range(3):
        Path(ga.path, f"{i}.sav").unlink()
    st = a.sync_game(ga)
    assert st["deleted"] == 0 and st["down"] == 3


def test_single_delete_still_propagates_when_enabled(tmp_path):
    a, ga, _ = pc(tmp_path, "A", propagate_deletes=True)
    write(ga, "1.sav", b"1", 1000)
    write(ga, "2.sav", b"2", 1000)
    a.sync_game(ga)
    Path(ga.path, "1.sav").unlink()
    assert a.sync_game(ga)["deleted"] == 1


def test_reset_state_when_folder_changes(tmp_path):
    a, ga, _ = pc(tmp_path, "A", propagate_deletes=True)
    write(ga, "1.sav", b"1", 1000)
    a.sync_game(ga)
    moved = Game("g", "G", str(tmp_path / "nuova-cartella"))
    a.reset_state("g")
    st = a.sync_game(moved)
    assert st["deleted"] == 0 and Path(moved.path, "1.sav").read_bytes() == b"1"


def test_upgrade_from_020_layout(tmp_path):
    """Cloud scritto dalla 0.2.0 (files/<rel> sovrascritto + versions/): va letto e
    migrato senza perdere niente."""
    cloud = tmp_path / "cloud"
    (cloud / "g/files").mkdir(parents=True)
    (cloud / "g/versions/s.sav").mkdir(parents=True)
    (cloud / "g/files/s.sav").write_bytes(b"attuale")
    (cloud / "g/versions/s.sav/20260101-000000-000__PC-vecchio").write_bytes(b"vecchia")
    import hashlib
    h = hashlib.sha256(b"attuale").hexdigest()
    (cloud / "g/manifest.json").write_text(json.dumps({"files": {"s.sav": {
        "hash": h, "mtime": 1000, "size": 7, "device": "PC-vecchio", "time": 1000}}}))
    b, gb, _ = pc(tmp_path, "B")
    assert b.sync_game(gb)["down"] == 1
    assert Path(gb.path, "s.sav").read_bytes() == b"attuale"
    write(gb, "s.sav", b"nuovo", 2000)
    b.sync_game(gb)
    assert not (cloud / "g/files/s.sav").exists()  # migrato nei blob
    contents = sorted(Path(cloud, k).read_bytes() for k in b.backend.list("g/blobs/s.sav/"))
    assert contents == [b"attuale", b"nuovo"]
    versions = b.list_versions(gb, "s.sav")
    assert any(o.startswith("cloud") for _, o, _ in versions)
    assert len([v for v in versions if v[1].startswith("cloud")]) == 3  # 2 blob + 1 vecchia


def test_versions_are_pruned(tmp_path):
    a, ga, _ = pc(tmp_path, "A", max_versions=2)
    for i in range(6):
        write(ga, "s.sav", b"v%d" % i, 1000 + i)
        a.sync_game(ga)
    blobs = sorted((tmp_path / "cloud/g/blobs/s.sav").iterdir())
    assert len(blobs) == 3  # attuale + 2 versioni
    assert blobs[-1].read_bytes() == b"v5"


def test_folder_backend_refuses_missing_cloud(tmp_path):
    with pytest.raises(FileNotFoundError):
        FolderBackend(tmp_path / "Proton Drive non montato" / "SaveSync")
    with pytest.raises(ValueError):
        FolderBackend("")
    FolderBackend(tmp_path / "SaveSync")  # la sottocartella sì, la crea


def test_restore_cloud_version_roundtrip(tmp_path):
    a, ga, _ = pc(tmp_path, "A")
    b, gb, _ = pc(tmp_path, "B")
    write(ga, "s.sav", b"buono", 1000)
    a.sync_game(ga)
    write(ga, "s.sav", b"corrotto", 2000)
    a.sync_game(ga)
    b.sync_game(gb)
    v = next(v for v in b.list_versions(gb, "s.sav")
             if v[1].startswith("cloud") and Path(tmp_path, "cloud", v[2]).read_bytes() == b"buono")
    b.restore_version(gb, "s.sav", v[1], v[2])
    assert b.sync_game(gb)["up"] == 1
    assert a.sync_game(ga)["down"] == 1
    assert Path(ga.path, "s.sav").read_bytes() == b"buono"


def test_switching_cloud_never_deletes_local_saves(tmp_path):
    """Dalla cartella Proton Drive a Google Drive/OneDrive (cloud nuovo e vuoto) con
    'propaga cancellazioni' attivo: i salvataggi locali NON devono sparire."""
    from savesync.config import Config
    cfg = Config(app_dir=tmp_path / "app")
    cfg.settings.folder_path = str(tmp_path / "proton" / "SaveSync")
    cfg.settings.propagate_deletes = True
    (tmp_path / "proton").mkdir()
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "slot.sav").write_text("progressi")
    game = Game("g", "G", str(saves))
    SyncEngine(cfg.settings, FolderBackend(cfg.settings.folder_path), cfg.app_dir,
               log=lambda m: None).sync_game(game)
    before = cfg.cloud_identity()
    cfg.settings.folder_path = str(tmp_path / "nuovo-cloud")
    assert cfg.cloud_identity() != before
    cfg.reset_all_states()  # quello che fa la finestra Impostazioni
    eng = SyncEngine(cfg.settings, FolderBackend(cfg.settings.folder_path), cfg.app_dir,
                     log=lambda m: None)
    st = eng.sync_game(game)
    assert (saves / "slot.sav").read_text() == "progressi"
    assert st["up"] == 1 and st["deleted"] == 0


def test_empty_cloud_never_propagates_deletes(tmp_path):
    """Anche senza passare dalla finestra (config cambiata a mano): cloud vuoto = carico."""
    a, ga, _ = pc(tmp_path, "A", propagate_deletes=True)
    write(ga, "slot.sav", b"progressi", 1000)
    a.sync_game(ga)
    a2 = SyncEngine(a.settings, FolderBackend(tmp_path / "altro-cloud"), a.app_dir,
                    log=lambda m: None)
    st = a2.sync_game(ga)
    assert st["deleted"] == 0 and st["up"] == 1
    assert Path(ga.path, "slot.sav").read_bytes() == b"progressi"
