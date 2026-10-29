"""Istantanee locali della cartella salvataggi ("com'era prima di giocare ieri sera").

Ogni istantanea è un elenco file -> hash; i contenuti stanno in objects/<hash> e sono
condivisi tra istantanee, quindi un file uguale in 10 istantanee occupa spazio una volta sola.
"""
import json
import os
import shutil
import threading
import time
from pathlib import Path

from .backends import replace_retry


def _hash_file(path):
    from .engine import sha256_file
    return sha256_file(path)


_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(root: Path) -> threading.RLock:
    """Una sola istantanea alla volta per gioco: la pulizia di una non deve cancellare i
    contenuti che l'altra sta ancora copiando."""
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(root), threading.RLock())


class SnapshotStore:
    def __init__(self, app_dir, game_id):
        self.root = Path(app_dir) / "snapshots" / game_id
        self.objects = self.root / "objects"
        self._lock = _lock_for(self.root)

    def _obj(self, h):
        return self.objects / h[:2] / h

    def list(self) -> list:
        """Istantanee dalla più recente: [{id, time, reason, device, files, size}]."""
        out = []
        for p in self.root.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            files = data.get("files", {})
            out.append({"id": p.stem, "time": data.get("time", 0),
                        "reason": data.get("reason", ""), "files": len(files),
                        "size": sum(f.get("size", 0) for f in files.values())})
        return sorted(out, key=lambda s: (s["time"], s["id"]), reverse=True)

    def load(self, snap_id) -> dict:
        return json.loads((self.root / f"{snap_id}.json").read_text(encoding="utf-8"))

    def take(self, game_path, files: dict, reason: str, keep: int = 10):
        """files = {rel: {"hash", "size", "mtime"}} (da SyncEngine.scan_local).
        Ritorna l'id, o None se identica all'ultima (non serve rifarla)."""
        with self._lock:
            return self._take(game_path, files, reason, keep)

    def _take(self, game_path, files, reason, keep):
        if not files:
            return None
        current = {r: e["hash"] for r, e in files.items()}
        last = self.list()[:1]
        if last and {r: e["hash"] for r, e in self.load(last[0]["id"])["files"].items()} == current:
            return None
        for rel, e in files.items():
            obj = self._obj(e["hash"])
            if obj.exists():
                continue
            obj.parent.mkdir(parents=True, exist_ok=True)
            tmp = obj.with_suffix(".tmp")
            try:
                shutil.copy2(Path(game_path, rel), tmp)
                if _hash_file(tmp) != e["hash"]:  # cambiato mentre copiavo: salto il file
                    tmp.unlink(missing_ok=True)
                    continue
                replace_retry(tmp, obj)
            except OSError:
                # sparito o bloccato dal gioco proprio adesso: l'istantanea si fa lo stesso,
                # senza questo file
                tmp.unlink(missing_ok=True)
                continue
        now = time.time()
        snap_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + \
            f"-{int(now * 1000) % 1000:03d}"
        kept = {r: {"hash": e["hash"], "size": e.get("size", 0), "mtime": e.get("mtime", 0)}
                for r, e in files.items() if self._obj(e["hash"]).exists()}
        self.root.mkdir(parents=True, exist_ok=True)
        while (self.root / f"{snap_id}.json").exists():
            snap_id += "_"  # due istantanee nello stesso millisecondo: mai sovrascrivere
        (self.root / f"{snap_id}.json").write_text(json.dumps(
            {"time": now, "reason": reason, "files": kept}, indent=1), encoding="utf-8")
        self.prune(keep)
        return snap_id

    def prune(self, keep: int) -> None:
        snaps = self.list()
        for s in snaps[max(keep, 1):]:
            (self.root / f"{s['id']}.json").unlink(missing_ok=True)
        self.gc()

    def gc(self) -> None:
        """Cancella i contenuti non più usati da nessuna istantanea."""
        used = set()
        for s in self.list():
            used |= {e["hash"] for e in self.load(s["id"])["files"].values()}
        if not self.objects.exists():
            return
        for sub in self.objects.iterdir():
            for obj in sub.iterdir():
                if obj.name not in used:
                    obj.unlink(missing_ok=True)

    def disk_usage(self) -> int:
        total = 0
        for dirpath, _, files in os.walk(self.objects):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
        return total

    def restore(self, game_path, snap_id) -> int:
        with self._lock:
            return self._restore(game_path, snap_id)

    def _restore(self, game_path, snap_id) -> int:
        """Rimette i file dell'istantanea (con data di adesso, così vincono alla prossima
        sincronizzazione e arrivano agli altri PC). I salvataggi nati dopo restano dove sono.
        Ritorna il numero di file rimessi."""
        data = self.load(snap_id)
        n = 0
        now = time.time()
        for rel, e in data["files"].items():
            dst = Path(game_path, rel)
            if dst.exists() and _hash_file(dst) == e["hash"]:
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".resosync-tmp")
            shutil.copyfile(self._obj(e["hash"]), tmp)
            replace_retry(tmp, dst)
            os.utime(dst, (now, now))
            n += 1
        return n
