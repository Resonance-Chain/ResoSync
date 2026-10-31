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


# 2. una sottocartella che non si riesce a leggere (errore della microSD, permessi) veniva
#    saltata in silenzio: i suoi file sembravano cancellati
def test_unreadable_subfolder_is_not_a_deletion(tmp_path, monkeypatch):
    import os
    import resosync.engine as em
    a, ga = pc(tmp_path, "A", propagate_deletes=True)
    b, gb = pc(tmp_path, "B", propagate_deletes=True)
    for slot in ("Slot1", "Slot2", "Slot3"):
        Path(ga.path, slot).mkdir()
        Path(ga.path, slot, "sav.dat").write_bytes(slot.encode())
    a.sync_game(ga)
    b.sync_game(gb)
    real_walk = os.walk

    def walk_with_bad_folder(top, onerror=None, **kw):
        for dirpath, dirs, files in real_walk(top, **kw):
            if Path(dirpath).name == "Slot2":
                if onerror:
                    onerror(PermissionError(13, "Permission denied", dirpath))
                continue
            yield dirpath, dirs, files
    monkeypatch.setattr(em.os, "walk", walk_with_bad_folder)
    Path(ga.path, "Slot1/sav.dat").write_bytes(b"Slot1 aggiornato")
    st = a.sync_game(ga)
    assert st["deleted"] == 0 and st["errors"] >= 1  # Slot2 saltato per ora, non cancellato
    assert st["up"] == 1  # il resto si sincronizza lo stesso
    monkeypatch.setattr(em.os, "walk", real_walk)
    b.sync_game(gb)
    assert Path(gb.path, "Slot2/sav.dat").exists()
    assert Path(gb.path, "Slot1/sav.dat").read_bytes() == b"Slot1 aggiornato"


def test_unreadable_folder_without_saves_is_ignored(tmp_path, monkeypatch):
    """Su Windows Documenti contiene collegamenti che non si possono leggere ("My Music"...):
    se lì dentro non c'è niente di sincronizzato, la sync va avanti normalmente."""
    import os
    import resosync.engine as em
    a, ga = pc(tmp_path, "A", propagate_deletes=True)
    Path(ga.path, "My Music").mkdir()
    Path(ga.path, "slot.sav").write_bytes(b"s")
    real_walk = os.walk

    def walk(top, onerror=None, **kw):
        for dirpath, dirs, files in real_walk(top, **kw):
            if Path(dirpath).name == "My Music":
                if onerror:
                    onerror(PermissionError(13, "Accesso negato", dirpath))
                continue
            yield dirpath, dirs, files
    monkeypatch.setattr(em.os, "walk", walk)
    st = a.sync_game(ga)
    assert st["up"] == 1 and st["errors"] == 0


# 3. modificare un gioco che salva nel registro (anche solo il nome) faceva sparire le sue chiavi
#    del registro: da lì il registro non veniva più salvato, senza nessun avviso
def test_editing_game_keeps_registry_keys(tmp_path, monkeypatch):
    import types
    gui = pytest.importorskip("resosync.gui")

    class Var:
        def __init__(self, v):
            self.v = v

        def get(self):
            return self.v
    key = "HKEY_CURRENT_USER/Software/Studio/Gioco"
    old = Game("g", "Gioco", str(tmp_path), registry=[key], group_by="folder")
    saved = []
    dlg = types.SimpleNamespace(
        game=old, name=Var("Gioco (rinominato)"), path=Var(str(tmp_path)),
        patterns=Var("*"), excludes=Var(""), enabled=Var(True), process=Var(""),
        launch=Var(""), install_dir=Var(""), mode=Var("sync"), max_mb=Var("0"),
        group_by=Var("Una sottocartella"), group_labels={"folder": "Una sottocartella"},
        on_save=lambda o, n: saved.append(n) or True, destroy=lambda: None)
    gui.GameDialog._save(dlg)
    assert saved and saved[0].registry == [key]
