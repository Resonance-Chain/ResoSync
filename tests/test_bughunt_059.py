"""Caccia ai bug per la 0.5.9."""
from pathlib import Path

import pytest

from resosync.backends import FolderBackend
from resosync.config import Game, Settings
from resosync.engine import SyncEngine


def pc(tmp_path, name, **kw):
    eng = SyncEngine(Settings(device_name=name, **kw), FolderBackend(tmp_path / "cloud"),
                     tmp_path / f"app-{name}", log=lambda m: None)
    g = Game("g", "G", str(tmp_path / f"saves-{name}"))
    Path(g.path).mkdir(parents=True, exist_ok=True)
    return eng, g


# 1. restringere i filtri di un gioco (es. escludere *.bak) non vuol dire cancellare quei file:
#    con "propaga cancellazioni" sparivano dagli altri PC
def test_newly_excluded_files_are_not_deletions(tmp_path):
    a, ga = pc(tmp_path, "A", propagate_deletes=True)
    b, gb = pc(tmp_path, "B", propagate_deletes=True)
    for name in ("1.sav", "2.sav", "3.sav", "vecchio.bak"):
        Path(ga.path, name).write_bytes(name.encode())
    a.sync_game(ga)
    b.sync_game(gb)
    ga.excludes = ga.excludes + ["*.bak"]
    st = a.sync_game(ga)
    assert st["deleted"] == 0
    b.sync_game(gb)
    assert Path(gb.path, "vecchio.bak").exists(), "cancellato sull'altro PC"
    assert Path(ga.path, "vecchio.bak").read_bytes() == b"vecchio.bak"


def test_pcs_with_different_filters_do_not_delete_each_others_files(tmp_path):
    """Il PC A sincronizza tutto, il B solo *.sav: B non deve scaricare (a ogni giro) né
    cancellare dal cloud i file che per lui sono fuori dai filtri."""
    a, ga = pc(tmp_path, "A", propagate_deletes=True)
    b, gb = pc(tmp_path, "B", propagate_deletes=True)
    gb.patterns = ["*.sav"]
    Path(ga.path, "slot.sav").write_bytes(b"s")
    Path(ga.path, "impostazioni.ini").write_bytes(b"i")
    a.sync_game(ga)
    for _ in range(3):
        st = b.sync_game(gb)
    assert st["deleted"] == 0 and st["down"] == 0
    assert not Path(gb.path, "impostazioni.ini").exists()
    a.sync_game(ga)
    assert Path(ga.path, "impostazioni.ini").read_bytes() == b"i"
