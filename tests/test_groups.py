"""Punto 1: un salvataggio fatto di più file si decide e si applica tutto insieme."""
import os
from pathlib import Path

from savesync.backends import FolderBackend
from savesync.config import Game, Settings
from savesync.engine import SyncEngine

PARTS = ("sav.dat", "metadata.json", "screenshot.png")


def pc(tmp_path, name, group_by="folder", cloud="cloud"):
    eng = SyncEngine(Settings(device_name=name), FolderBackend(tmp_path / cloud),
                     tmp_path / f"app-{name}", log=lambda m: None)
    g = Game("g", "G", str(tmp_path / f"saves-{name}"), group_by=group_by)
    Path(g.path).mkdir(parents=True, exist_ok=True)
    return eng, g


def save(g, slot, token, mtime):
    for part in PARTS:
        p = Path(g.path, slot, part)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{token}:{part}")
        os.utime(p, (mtime, mtime))


def contents(g, slot):
    return {Path(g.path, slot, p).read_text().split(":")[0] if Path(g.path, slot, p).exists()
            else None for p in PARTS}


def test_group_key():
    g = Game("g", "G", ".", group_by="folder")
    assert SyncEngine.group_key(g, "ManualSave-1/sav.dat") == "ManualSave-1/"
    assert SyncEngine.group_key(g, "settings.ini") == "settings.ini"
    g.group_by = "name"
    assert SyncEngine.group_key(g, "Saves/Save1.ess") == SyncEngine.group_key(g, "Saves/Save1.skse")
    g.group_by = "none"
    assert SyncEngine.group_key(g, "ManualSave-1/sav.dat") == "ManualSave-1/sav.dat"


def test_conflict_whole_save_wins(tmp_path):
    a, ga = pc(tmp_path, "A")
    b, gb = pc(tmp_path, "B")
    save(ga, "AutoSave-0", "base", 1000)
    a.sync_game(ga)
    b.sync_game(gb)
    save(ga, "AutoSave-0", "A", 2000)
    save(gb, "AutoSave-0", "B", 3000)  # B più recente
    a.sync_game(ga)
    st = b.sync_game(gb)
    assert st["conflicts"] == 1
    a.sync_game(ga)
    assert contents(ga, "AutoSave-0") == {"B"} and contents(gb, "AutoSave-0") == {"B"}
    # il salvataggio perdente resta recuperabile intero
    backups = tmp_path / "app-A" / "backups" / "g" / "AutoSave-0"
    assert {p.read_text().split(":")[0] for p in backups.rglob("*") if p.is_file()} == {"A"}


def test_partial_arrival_applies_nothing(tmp_path):
    """Cartella cloud in ritardo: manifest nuovo arrivato, uno dei 3 file no.
    Non si applica mezzo salvataggio; quando arriva tutto, si applica tutto."""
    a, ga = pc(tmp_path, "A")
    b, gb = pc(tmp_path, "B")
    save(ga, "ManualSave-1", "v1", 1000)
    a.sync_game(ga)
    b.sync_game(gb)
    save(ga, "ManualSave-1", "v2", 2000)
    a.sync_game(ga)
    blob_dir = tmp_path / "cloud/g/blobs/ManualSave-1/metadata.json"
    newest = sorted(blob_dir.iterdir())[-1]
    hidden = newest.read_bytes()
    newest.unlink()  # "non ancora arrivato"
    st = b.sync_game(gb)
    assert st["errors"] == 1 and st["down"] == 0
    assert contents(gb, "ManualSave-1") == {"v1"}  # intatto, non mescolato
    assert not list(Path(gb.path).rglob("*.savesync-tmp"))
    newest.write_bytes(hidden)  # arrivato
    st = b.sync_game(gb)
    assert st["down"] == 3 and contents(gb, "ManualSave-1") == {"v2"}


def test_per_file_mode_unchanged(tmp_path):
    a, ga = pc(tmp_path, "A", group_by="none")
    b, gb = pc(tmp_path, "B", group_by="none")
    save(ga, "S", "v1", 1000)
    a.sync_game(ga)
    assert b.sync_game(gb)["down"] == 3


def test_suggest_grouping(tmp_path):
    cp = tmp_path / "cp"
    for slot in ("ManualSave-1", "AutoSave-0"):
        (cp / slot).mkdir(parents=True)
        for part in PARTS:
            (cp / slot / part).write_text("x")
    assert SyncEngine.suggest_grouping(cp) == "folder"
    sk = tmp_path / "skyrim"
    sk.mkdir()
    for n in ("Save1", "Save2"):
        (sk / f"{n}.ess").write_text("x")
        (sk / f"{n}.skse").write_text("x")
    assert SyncEngine.suggest_grouping(sk) == "name"
    er = tmp_path / "er"
    er.mkdir()
    (er / "ER0000.sl2").write_text("x")
    assert SyncEngine.suggest_grouping(er) == "folder"  # file singolo: nessuna differenza


def test_restore_whole_save(tmp_path):
    a, ga = pc(tmp_path, "A")
    save(ga, "ManualSave-1", "buono", 1000)
    a.sync_game(ga)
    save(ga, "ManualSave-1", "rotto", 2000)
    a.sync_game(ga)
    v = next(v for v in a.list_versions(ga, "ManualSave-1/sav.dat")
             if v[1].startswith("cloud") and
             Path(tmp_path, "cloud", v[2]).read_text().startswith("buono"))
    restored = a.restore_group(ga, "ManualSave-1/sav.dat", v[1], v[2])
    assert restored == sorted(f"ManualSave-1/{p}" for p in PARTS)
    assert contents(ga, "ManualSave-1") == {"buono"}
