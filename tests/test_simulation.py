"""Simulazione: due PC che usano una cartella sincronizzata da un client cloud
(Proton Drive, OneDrive, Dropbox...).

Il client cloud è modellato così: ogni PC ha la sua copia della cartella; i file viaggiano
uno alla volta, in ordine casuale e con ritardo; se due PC scrivono lo stesso file vince
l'ultimo (come fanno i client veri, a parte le "copie in conflitto").

Invarianti controllate alla fine:
  1. i due PC convergono sugli stessi file
  2. per ogni file vince il salvataggio più recente (nessun progresso perso)
  3. ogni ultimo salvataggio di ciascun PC resta recuperabile (versioni o backup)
"""
import os
import random
import shutil
import time
from pathlib import Path

import pytest

from resosync import engine as engine_mod
from resosync.backends import FolderBackend
from resosync.config import Game, Settings
from resosync.engine import CloudBusy, SyncEngine


class FakeCloudClient:
    """Server centrale + replica locale per ogni PC, last-writer-wins."""

    def __init__(self, replicas):
        self.replicas = replicas            # nome -> Path
        self.server = {}                    # path -> (bytes|None, seq)
        self.seen = {n: {} for n in replicas}  # nome -> path -> (bytes|None, seq) all'ultimo sync
        self.seq = 0

    def _local(self, name, rel):
        p = self.replicas[name] / rel
        return p.read_bytes() if p.is_file() else None

    def _paths(self, name):
        root = self.replicas[name]
        out = set()
        for dirpath, _, files in os.walk(root):
            for f in files:
                if not f.endswith(".partial"):
                    out.add(Path(dirpath, f).relative_to(root).as_posix())
        return out

    def step(self, rng, name, rel=None):
        """Sincronizza UN file di una replica (push delle modifiche locali o pull dal server)."""
        candidates = sorted(self._paths(name) | set(self.server) | set(self.seen[name]))
        if not candidates:
            return
        rel = rel or rng.choice(candidates)
        local = self._local(name, rel)
        last_data, last_seq = self.seen[name].get(rel, (None, 0))
        srv_data, srv_seq = self.server.get(rel, (None, 0))
        if local != last_data:  # modifica locale -> server (vince l'ultimo)
            self.seq += 1
            self.server[rel] = (local, self.seq)
            self.seen[name][rel] = (local, self.seq)
        elif srv_seq > last_seq:  # novità dal server -> disco
            p = self.replicas[name] / rel
            if srv_data is None:
                p.unlink(missing_ok=True)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(srv_data)
            self.seen[name][rel] = (srv_data, srv_seq)

    def sync_now(self, rng, name):
        """Accesso diretto (rclone, API): la replica è allineata al server in quel momento."""
        for rel in sorted(self._paths(name) | set(self.server) | set(self.seen[name])):
            self.step(rng, name, rel)

    def flush(self, rng):
        for _ in range(3):
            for name in self.replicas:
                for rel in sorted(self._paths(name) | set(self.server) | set(self.seen[name])):
                    self.step(rng, name, rel)


SAVE_PARTS = ("sav.dat", "meta.json", "shot.png")  # un salvataggio "alla Cyberpunk"


def run_simulation(tmp_path, seed, steps=300, files=("slot1.sav", "slot2.sav"),
                   propagate=False, multi=False, group_by="folder", deletes=False,
                   direct=()):
    """multi=True: ogni elemento di `files` è una cartella-salvataggio con 3 file scritti
    insieme dal gioco (stesso contenuto = stessa partita).
    direct: i PC che arrivano al cloud direttamente (rclone, Google Drive/OneDrive via API)
    invece che da una cartella sincronizzata in ritardo."""
    rng = random.Random(seed)
    names = ["A", "B"]
    cloud = FakeCloudClient({n: tmp_path / f"cloud-{n}" for n in names})
    pcs = {}
    for n in names:
        (tmp_path / f"cloud-{n}").mkdir()
        s = Settings(device_name=n, max_versions=1000, propagate_deletes=propagate)
        eng = SyncEngine(s, FolderBackend(tmp_path / f"cloud-{n}"), tmp_path / f"app-{n}",
                         log=lambda m: None)
        game = Game("g", "G", str(tmp_path / f"saves-{n}"), group_by=group_by)
        Path(game.path).mkdir()
        pcs[n] = (eng, game)

    clock = time.time()
    writes = []      # (mtime, pc, file, contenuto)
    seen_by_sync = set()
    counter = 0

    def write(pc, f):
        nonlocal clock, counter
        clock = max(clock + 0.05, time.time())
        counter += 1
        data = f"{pc}-{counter}".encode()
        if not multi:
            Path(pcs[pc][1].path, f).unlink(missing_ok=True)
        for part in (SAVE_PARTS if multi else [None]):
            p = Path(pcs[pc][1].path, f, part) if part else Path(pcs[pc][1].path, f)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            os.utime(p, (clock, clock))
        writes.append((clock, pc, f, data))

    def sync(pc):
        present = {p.read_bytes() for p in Path(pcs[pc][1].path).rglob("*") if p.is_file()}
        if pc in direct:
            cloud.sync_now(rng, pc)
        try:
            pcs[pc][0].sync_game(pcs[pc][1])
            seen_by_sync.update(present)  # solo se la sync è andata: sennò non è uscito dal PC
        except CloudBusy:
            pass
        finally:
            if pc in direct:
                cloud.sync_now(rng, pc)

    for _ in range(steps):
        r = rng.random()
        pc = rng.choice(names)
        if deletes and r < 0.06:
            f = rng.choice(files)
            target = Path(pcs[pc][1].path, f)
            if target.exists():  # il gioco (o tu) cancella uno slot
                if target.is_dir():
                    shutil.rmtree(target)  # salvataggio da più file: va via la cartella
                else:
                    target.unlink()
                clock = max(clock + 0.05, time.time())
                writes.append((clock, pc, f, None))
        elif r < 0.25:
            write(pc, rng.choice(files))
        elif r < 0.5:
            sync(pc)
        else:
            for _ in range(rng.randint(1, 4)):
                cloud.step(rng, rng.choice(names))

    # quiete: niente più partite, si lascia finire cloud e sync
    for _ in range(6):
        cloud.flush(rng)
        for pc in names:
            # un lock rimasto appeso (PC "piantato" a metà) scade dopo LOCK_TTL
            for c in cloud.replicas.values():
                (c / "g" / "lock.json").unlink(missing_ok=True)
            cloud.flush(rng)
            sync(pc)
    cloud.flush(rng)

    def read(pc, f):
        if not multi:
            p = Path(pcs[pc][1].path, f)
            return p.read_bytes() if p.exists() else None
        parts = {Path(pcs[pc][1].path, f, part).read_bytes()
                 if Path(pcs[pc][1].path, f, part).exists() else None for part in SAVE_PARTS}
        # salvataggio coerente = i 3 file della stessa partita; altrimenti lo segnalo
        return parts.pop() if len(parts) == 1 else ("MESCOLATO", tuple(sorted(map(str, parts))))
    final = {pc: {f: read(pc, f) for f in files} for pc in names}
    run_simulation.seen_by_sync = seen_by_sync
    return writes, final, pcs, cloud


def _everything_stored(tmp_path):
    blobs = set()
    for p in tmp_path.rglob("*"):
        if p.is_file() and p.stat().st_size < 64:
            blobs.add(p.read_bytes())
    return blobs


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(engine_mod.time, "sleep", lambda s: None)


@pytest.mark.parametrize("seed", range(int(os.environ.get("SIM_SEEDS", "8"))))
def test_two_pcs_through_laggy_cloud_folder(tmp_path, seed):
    writes, final, pcs, cloud = run_simulation(tmp_path, seed)
    files = {w[2] for w in writes}
    assert final["A"] == final["B"], f"seed {seed}: i PC non convergono: {final}"
    for f in files:
        latest = max((w for w in writes if w[2] == f), key=lambda w: w[0])
        assert final["A"][f] == latest[3], (
            f"seed {seed}: per {f} doveva vincere {latest[3]} (il più recente), "
            f"c'è {final['A'][f]}")
    stored = _everything_stored(tmp_path)
    for pc in ("A", "B"):
        for f in files:
            mine = [w for w in writes if w[1] == pc and w[2] == f]
            if mine:
                assert mine[-1][3] in stored, f"seed {seed}: perso {mine[-1][3]}"


@pytest.mark.parametrize("seed", range(int(os.environ.get("SIM_SEEDS_LONG", "2"))))
def test_long_sessions_three_files(tmp_path, seed):
    files = ("slot1.sav", "slot2.sav", "profilo/settings.dat")
    writes, final, pcs, cloud = run_simulation(tmp_path, 10_000 + seed, steps=600, files=files)
    assert final["A"] == final["B"], f"seed {seed}: i PC non convergono"
    for f in {w[2] for w in writes}:
        latest = max((w for w in writes if w[2] == f), key=lambda w: w[0])
        assert final["A"][f] == latest[3], f"seed {seed}: per {f} doveva vincere {latest[3]}"


@pytest.mark.parametrize("seed", range(int(os.environ.get("SIM_SEEDS_MULTI", "6"))))
def test_multi_file_saves_never_mixed(tmp_path, seed, monkeypatch):
    """Salvataggi da 3 file (sav.dat + meta.json + shot.png): ognuno deve avere sempre i
    3 file della stessa partita, DOPO OGNI SINCRONIZZAZIONE (il gioco potrebbe essere aperto
    in qualsiasi momento) e alla fine la più recente, su entrambi i PC."""
    slots = ("ManualSave-1", "AutoSave-0")
    mixed_after_sync = []
    original = SyncEngine.sync_game

    def checked(self, game, *a, **k):
        try:
            return original(self, game, *a, **k)
        finally:
            for slot in slots:
                vals = {Path(game.path, slot, part).read_bytes()
                        if Path(game.path, slot, part).exists() else None
                        for part in SAVE_PARTS}
                if len(vals) > 1:
                    mixed_after_sync.append((self.settings.device_name, slot, vals))
    monkeypatch.setattr(SyncEngine, "sync_game", checked)
    writes, final, pcs, cloud = run_simulation(tmp_path, 20_000 + seed, steps=300,
                                               files=slots, multi=True)
    assert not mixed_after_sync, f"seed {seed}: salvataggio mescolato: {mixed_after_sync[0]}"
    for pc in ("A", "B"):
        for f, v in final[pc].items():
            assert not (isinstance(v, tuple) and v[0] == "MESCOLATO"), \
                f"seed {seed}: {pc}/{f} ha file di partite diverse: {v}"
    assert final["A"] == final["B"], f"seed {seed}: i PC non convergono: {final}"
    for f in {w[2] for w in writes}:
        latest = max((w for w in writes if w[2] == f), key=lambda w: w[0])
        assert final["A"][f] == latest[3], f"seed {seed}: per {f} doveva vincere {latest[3]}"


@pytest.mark.parametrize("propagate", [False, True])
@pytest.mark.parametrize("seed", range(int(os.environ.get("SIM_SEEDS_DELETE", "4"))))
def test_deletions(tmp_path, seed, propagate):
    """Cancellazioni su dispositivi diversi, con e senza 'propaga cancellazioni'."""
    files = ("slot1.sav", "slot2.sav")
    writes, final, pcs, cloud = run_simulation(tmp_path, 30_000 + seed, steps=300, files=files,
                                               propagate=propagate, deletes=True)
    assert final["A"] == final["B"], f"seed {seed}: i PC non convergono: {final}"
    stored = _everything_stored(tmp_path)
    for f in files:
        actions = sorted((w for w in writes if w[2] == f), key=lambda w: w[0])
        if not actions:
            continue
        # contano i salvataggi che una sincronizzazione ha visto: uno cancellato prima di
        # sincronizzarlo non è mai uscito dal PC
        real_writes = [w for w in actions if w[3] is not None
                       and w[3] in run_simulation.seen_by_sync]
        if actions[-1][3] is not None and actions[-1][3] in run_simulation.seen_by_sync:
            # l'ultima cosa successa è un salvataggio: deve esserci, ovunque
            assert final["A"][f] == actions[-1][3], f"seed {seed}: {f} doveva essere {actions[-1][3]}"
        elif not propagate and real_writes:
            # senza propagazione un file cancellato torna, col contenuto più recente
            assert final["A"][f] == real_writes[-1][3], f"seed {seed}: {f} non è tornato"
        if real_writes:
            assert real_writes[-1][3] in stored, f"seed {seed}: perso {real_writes[-1][3]}"


@pytest.mark.parametrize("propagate", [False, True])
@pytest.mark.parametrize("seed", range(int(os.environ.get("SIM_SEEDS_DELETE_MULTI", "3"))))
def test_multi_file_saves_with_deletions(tmp_path, seed, propagate, monkeypatch):
    """Salvataggi da 3 file con cancellazioni dell'intero slot: mai mescolati dopo una
    sincronizzazione, PC allineati alla fine, l'ultimo salvataggio vince."""
    slots = ("ManualSave-1", "AutoSave-0")
    mixed = []
    original = SyncEngine.sync_game

    def checked(self, game, *a, **k):
        try:
            return original(self, game, *a, **k)
        finally:
            for slot in slots:
                vals = {Path(game.path, slot, part).read_bytes()
                        if Path(game.path, slot, part).exists() else None
                        for part in SAVE_PARTS}
                if len(vals) > 1:
                    mixed.append((self.settings.device_name, slot, vals))
    monkeypatch.setattr(SyncEngine, "sync_game", checked)
    writes, final, pcs, cloud = run_simulation(tmp_path, 40_000 + seed, steps=300, files=slots,
                                               multi=True, deletes=True, propagate=propagate)
    assert not mixed, f"seed {seed}: salvataggio mescolato: {mixed[0]}"
    assert final["A"] == final["B"], f"seed {seed}: i PC non convergono: {final}"
    for f in slots:
        actions = sorted((w for w in writes if w[2] == f), key=lambda w: w[0])
        if actions and actions[-1][3] is not None and actions[-1][3] in run_simulation.seen_by_sync:
            assert final["A"][f] == actions[-1][3], f"seed {seed}: {f} doveva essere {actions[-1][3]}"


@pytest.mark.parametrize("multi", [False, True])
@pytest.mark.parametrize("seed", range(int(os.environ.get("SIM_SEEDS_MIXED", "4"))))
def test_folder_pc_and_direct_deck(tmp_path, seed, multi):
    """Il PC usa la cartella di Proton Drive (in ritardo), il Deck lo stesso cloud via rclone
    (diretto). Stesse garanzie: convergono, vince il più recente, mai salvataggi mescolati."""
    files = ("ManualSave-1", "AutoSave-0") if multi else ("slot1.sav", "slot2.sav")
    writes, final, pcs, cloud = run_simulation(tmp_path, 50_000 + seed, files=files,
                                               multi=multi, direct=("B",))
    for pc in ("A", "B"):
        for f, v in final[pc].items():
            assert not (isinstance(v, tuple) and v[0] == "MESCOLATO"), f"seed {seed}: {v}"
    assert final["A"] == final["B"], f"seed {seed}: i PC non convergono: {final}"
    for f in {w[2] for w in writes}:
        latest = max((w for w in writes if w[2] == f), key=lambda w: w[0])
        assert final["A"][f] == latest[3], f"seed {seed}: per {f} doveva vincere {latest[3]}"

