"""Simula due PC che condividono lo stesso cloud (cartella)."""
import os, sys, time, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from resosync.config import Settings, Game
from resosync.backends import FolderBackend
from resosync.engine import SyncEngine

def make(tmp, name, cloud):
    s = Settings(backend="folder", folder_path=str(cloud), device_name=name, max_versions=3)
    eng = SyncEngine(s, FolderBackend(cloud), Path(tmp, name, "app"), log=lambda m: print("  ", m))
    game = Game(id="elden-ring", name="Elden Ring", path=str(Path(tmp, name, "saves")))
    return eng, game

def test_two_pcs():
    with tempfile.TemporaryDirectory() as tmp:
        cloud = Path(tmp, "cloud")
        a, ga = make(tmp, "PC-A", cloud)
        b, gb = make(tmp, "PC-B", cloud)
        Path(ga.path).mkdir(parents=True)
        Path(ga.path, "slot1.sav").write_text("A1")
        Path(ga.path, "sub").mkdir()
        Path(ga.path, "sub", "slot2.sav").write_text("A2")
        Path(ga.path, "junk.tmp").write_text("x")

        assert a.sync_game(ga)["up"] == 2
        st = b.sync_game(gb)          # PC nuovo: scarica tutto
        assert st["down"] == 2 and Path(gb.path, "sub", "slot2.sav").read_text() == "A2"
        assert not Path(gb.path, "junk.tmp").exists()
        assert a.sync_game(ga) == {"up": 0, "down": 0, "conflicts": 0, "deleted": 0, "errors": 0}

        # B gioca e salva -> A riceve
        time.sleep(0.01); Path(gb.path, "slot1.sav").write_text("B1")
        assert b.sync_game(gb)["up"] == 1
        assert a.sync_game(ga)["down"] == 1 and Path(ga.path, "slot1.sav").read_text() == "B1"

        # conflitto: entrambi modificano, B è più recente
        Path(ga.path, "slot1.sav").write_text("A-conf"); a.sync_game  # no sync di A
        time.sleep(0.05); Path(gb.path, "slot1.sav").write_text("B-conf")
        b.sync_game(gb)
        os.utime(Path(ga.path, "slot1.sav"), (time.time() - 100, time.time() - 100))
        st = a.sync_game(ga)
        assert st["conflicts"] == 1 and Path(ga.path, "slot1.sav").read_text() == "B-conf"

        # versioni + ripristino
        vers = a.list_versions(ga, "slot1.sav")
        assert any(o == "backup locale" for _, o, _ in vers)
        assert any(o.startswith("cloud") for _, o, _ in vers)
        loc = next(v for v in vers if v[1] == "backup locale" and Path(v[2]).read_text() == "A-conf")
        a.restore_version(ga, "slot1.sav", loc[1], loc[2])
        assert a.sync_game(ga)["up"] == 1
        b.sync_game(gb)
        assert Path(gb.path, "slot1.sav").read_text() == "A-conf"

        # cancellazione locale senza propagazione -> il file torna
        Path(gb.path, "sub", "slot2.sav").unlink()
        b.sync_game(gb)
        assert Path(gb.path, "sub", "slot2.sav").read_text() == "A2"
        print("tutto ok")

if __name__ == "__main__":
    test_two_pcs()
