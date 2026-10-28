"""Motore di sincronizzazione.

Per ogni file confronta 3 hash:
  L = file locale adesso
  R = file sul cloud adesso (dal manifest remoto)
  B = "base", cioè com'era il file all'ultima sync riuscita su questo PC
Così sappiamo chi ha cambiato cosa:
  solo L cambiato  -> upload
  solo R cambiato  -> download
  entrambi         -> conflitto: vince il più recente, l'altro finisce nelle versioni
Prima di sovrascrivere qualcosa (sul cloud o in locale) se ne tiene sempre una copia.
"""
import fnmatch
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .backends import replace_retry

REGISTRY_KEY = "games.json"
LOCK_TTL = 120  # secondi: se un PC si pianta a metà sync, dopo 2 minuti il lock scade


class CloudBusy(Exception):
    """Un altro PC sta sincronizzando lo stesso gioco."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _stamp() -> str:
    # millisecondi inclusi: due backup nello stesso secondo non devono sovrascriversi
    now = time.time()
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int(now * 1000) % 1000:03d}"


class SyncEngine:
    def __init__(self, settings, backend, app_dir: Path, log=print):
        self.settings = settings
        self.backend = backend
        self.app_dir = Path(app_dir)
        self.log = log
        self.lock = threading.Lock()
        self._hash_cache = {}  # path -> (size, mtime_ns, hash)
        self._registered = set()
        self._unreadable = {}  # game_id -> file presenti ma bloccati in questo giro

    # ---------- chiavi remote ----------
    @staticmethod
    def _k_file(gid, rel):
        return f"{gid}/files/{rel}"

    @staticmethod
    def _k_manifest(gid):
        return f"{gid}/manifest.json"

    @staticmethod
    def _k_versions(gid, rel):
        """Versioni nel formato della 0.2.0 (solo lettura, per i ripristini)."""
        return f"{gid}/versions/{rel}/"

    @staticmethod
    def _k_blobs(gid, rel):
        """Dalla 0.3.0 ogni contenuto è un file immutabile <data>__<pc>__<hash>: due PC non
        possono mai sovrascriversi a vicenda (con le cartelle cloud vince l'ultimo arrivato,
        file per file) e le versioni vecchie restano lì senza copiarle."""
        return f"{gid}/blobs/{rel}/"

    def _device_tag(self):
        return re.sub(r"[^\w.-]+", "_", self.settings.device_name) or "pc"

    # ---------- registro giochi sul cloud ----------
    def cloud_games(self) -> dict:
        data = self.backend.read_bytes(REGISTRY_KEY)
        return json.loads(data) if data else {}

    def register_game(self, game) -> None:
        if (game.id, game.name) in self._registered:
            return
        reg = self.cloud_games()
        if reg.get(game.id, {}).get("name") != game.name:
            reg[game.id] = {"name": game.name}
            self.backend.write_bytes(REGISTRY_KEY, json.dumps(reg, indent=2).encode())
        self._registered.add((game.id, game.name))

    # ---------- lock sul cloud ----------
    # Il cloud non offre un vero lock, quindi: scrivo il mio token, aspetto un attimo e
    # rileggo. Se c'è ancora il mio, il lock è mio. Basta per evitare che due PC (o l'app e
    # il launcher sullo stesso PC) scrivano insieme lo stesso manifest.
    def _read_json(self, key):
        try:
            data = self.backend.read_bytes(key)
            return json.loads(data) if data else None
        except ValueError:
            return None

    def acquire_lock(self, gid, wait=0.0):
        key = f"{gid}/lock.json"
        token = uuid.uuid4().hex
        deadline = time.time() + wait
        while True:
            cur = self._read_json(key)
            if not cur or cur.get("expires", 0) < time.time():
                self.backend.write_bytes(key, json.dumps({
                    "token": token, "device": self.settings.device_name,
                    "expires": time.time() + LOCK_TTL}).encode())
                time.sleep(0.3)
                cur = self._read_json(key)
                if cur and cur.get("token") == token:
                    return token
            if time.time() >= deadline:
                who = (cur or {}).get("device", "?")
                raise CloudBusy(f"{who} sta sincronizzando questo gioco")
            time.sleep(2)

    def release_lock(self, gid, token):
        key = f"{gid}/lock.json"
        cur = self._read_json(key)
        if cur and cur.get("token") == token:
            self.backend.delete(key)

    # ---------- manifest remoto / stato locale ----------
    def load_manifest(self, gid) -> dict:
        data = self.backend.read_bytes(self._k_manifest(gid))
        return json.loads(data) if data else {"files": {}}

    def _save_manifest(self, gid, changes: dict) -> None:
        # Rilegge e applica solo le nostre modifiche: riduce i danni se un altro PC
        # ha scritto il manifest nel frattempo.
        manifest = self.load_manifest(gid)
        manifest["files"].update(changes)
        manifest["updated"] = time.time()
        manifest["updated_by"] = self.settings.device_name
        self.backend.write_bytes(self._k_manifest(gid), json.dumps(manifest, indent=2).encode())

    def _state_file(self, gid) -> Path:
        return self.app_dir / "state" / f"{gid}.json"

    def _load_state(self, gid) -> dict:
        try:
            state = json.loads(self._state_file(gid).read_text())
        except (OSError, ValueError):
            state = {}  # primo avvio o file rovinato: si riparte dal confronto pieno
        state.setdefault("base", {})
        state.setdefault("mtimes", {})  # data del salvataggio sincronizzato, per file
        return state

    def reset_state(self, gid) -> None:
        """Da chiamare quando cambia la cartella di un gioco: la base vecchia non vale più."""
        self._state_file(gid).unlink(missing_ok=True)

    def _save_state(self, gid, state) -> None:
        p = self._state_file(gid)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        replace_retry(tmp, p)

    # ---------- scansione locale ----------
    @staticmethod
    def matches(game, rel: str) -> bool:
        name = rel.rsplit("/", 1)[-1]
        def hit(patterns):
            return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p) for p in patterns)
        return hit(game.patterns or ["*"]) and not hit(game.excludes)

    def _hash(self, path: Path, st) -> str:
        key = str(path)
        cached = self._hash_cache.get(key)
        if cached and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
            return cached[2]
        h = sha256_file(path)
        self._hash_cache[key] = (st.st_size, st.st_mtime_ns, h)
        return h

    def scan_local(self, game) -> dict:
        root = Path(game.path)
        out = {}
        unreadable = self._unreadable[game.id] = set()
        for dirpath, dirs, files in os.walk(root):
            for f in files:
                p = Path(dirpath, f)
                rel = p.relative_to(root).as_posix()
                if not self.matches(game, rel):
                    continue
                try:
                    st = p.stat()
                    out[rel] = {"hash": self._hash(p, st), "mtime": st.st_mtime, "size": st.st_size}
                except OSError as e:  # file bloccato dal gioco mentre scrive: riproviamo dopo
                    # NON è un file cancellato: va escluso dal confronto di questo giro
                    unreadable.add(rel)
                    self.log(f"[{game.name}] salto {rel} per ora: {e}")
        return out

    # ---------- sync ----------
    def sync_game(self, game, wait_lock=0.0) -> dict:
        with self.lock:
            Path(game.path).mkdir(parents=True, exist_ok=True)  # PC nuovo: cartella da creare
            self.register_game(game)
            local = self.scan_local(game)
            skip = self._unreadable.get(game.id, set())
            manifest = self.load_manifest(game.id)
            if game.mode == "backup":
                # solo backup: conta solo se è cambiato qualcosa QUI (quello che fanno gli
                # altri PC sul cloud non tocca la cartella del gioco)
                base = self._load_state(game.id)["base"]
                if all(base.get(r) == e["hash"] for r, e in local.items()):
                    return {"up": 0, "down": 0, "conflicts": 0, "deleted": 0,
                            "errors": len(skip)}
            elif not self._differs(local, manifest, skip):
                # Tutto allineato: niente lock, solo aggiornare la base locale
                state = self._load_state(game.id)
                for r in set(state["base"]) - set(local) - skip:
                    state["base"].pop(r, None)
                    state["mtimes"].pop(r, None)
                for r, e in local.items():
                    if state["base"].get(r) != e["hash"]:
                        state["mtimes"][r] = e["mtime"]
                    state["base"][r] = e["hash"]
                self._save_state(game.id, state)
                stats = {"up": 0, "down": 0, "conflicts": 0, "deleted": 0, "errors": 0}
                stats["errors"] = len(skip)
                return stats
            token = self.acquire_lock(game.id, wait_lock)
            try:
                manifest = self.load_manifest(game.id)  # può essere cambiato mentre aspettavamo
                return self._sync_game(game, local, manifest)
            finally:
                self.release_lock(game.id, token)

    @staticmethod
    def _differs(local, manifest, skip=frozenset()) -> bool:
        remote = {r: e["hash"] for r, e in manifest["files"].items()
                  if not e.get("deleted") and r not in skip}
        return {r: e["hash"] for r, e in local.items()} != remote

    def _sync_game(self, game, local, manifest) -> dict:
        remote = {r: e for r, e in manifest["files"].items() if not e.get("deleted")}
        state = self._load_state(game.id)
        base, mtimes = state["base"], state["mtimes"]
        changes = {}
        skip = self._unreadable.get(game.id, set())
        stats = {"up": 0, "down": 0, "conflicts": 0, "deleted": 0, "errors": len(skip)}
        propagate = self.settings.propagate_deletes
        if propagate and base and not manifest["files"]:
            # Cloud completamente vuoto (cloud nuovo o cartella cloud cambiata): non vuol dire
            # che qualcuno abbia cancellato i salvataggi. Carico, non cancello.
            self.log(f"[{game.name}] cloud vuoto: carico i salvataggi invece di cancellarli")
            propagate = False
        if propagate and base and not local and not skip:
            # Sparito tutto in un colpo (cartella spostata, disco scollegato, gioco
            # reinstallato): quasi certamente non è voluto, quindi non cancello il cloud.
            self.log(f"[{game.name}] la cartella è vuota: non propago le cancellazioni, "
                     f"riscarico dal cloud")
            propagate = False

        ctx = {"game": game, "local": local, "remote": remote, "base": base, "mtimes": mtimes,
               "changes": changes, "stats": stats, "propagate": propagate}
        groups = {}
        for rel in sorted(set(local) | set(remote) | set(base)):
            groups.setdefault(self.group_key(game, rel), []).append(rel)
        for rels in groups.values():
            if any(r in skip for r in rels):
                continue  # un file del salvataggio è bloccato: decido tutto il gruppo dopo
            if len(rels) == 1 or game.mode == "backup":
                for rel in rels:
                    self._sync_one(rel, **ctx)
            else:
                try:
                    self._sync_group(rels, **ctx)
                except Exception as e:
                    stats["errors"] += 1
                    self.log(f"[{game.name}] errore sul salvataggio "
                             f"{self.group_key(game, rels[0])}: {e}")

        if changes:
            self._save_manifest(game.id, changes)
        self._save_state(game.id, state)
        if changes and game.max_cloud_mb > 0:
            try:
                self.enforce_space(game)
            except Exception as e:
                self.log(f"[{game.name}] pulizia spazio non riuscita: {e}")
        self._history(game, stats, changes)
        return stats

    # ---------- cronologia ----------
    def _history(self, game, stats, changes):
        if not any(stats.values()):
            return
        p = self.app_dir / "history" / f"{game.id}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        entry = {"time": time.time(), **stats, "uploaded": sorted(changes)[:50]}
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        lines = p.read_text(encoding="utf-8").splitlines()
        if len(lines) > 500:
            p.write_text("\n".join(lines[-500:]) + "\n", encoding="utf-8")

    def history(self, game, limit=100) -> list:
        p = self.app_dir / "history" / f"{game.id}.jsonl"
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in reversed(lines[-limit:]):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    # ---------- "sto giocando" ----------
    # Ogni PC che ha il gioco aperto tiene sul cloud un file <gioco>/playing/<pc>.json
    # aggiornato ogni 2 minuti: gli altri PC lo vedono e avvisano.
    PRESENCE_TTL = 300

    def set_playing(self, game, playing: bool) -> None:
        key = f"{game.id}/playing/{self._device_tag()}.json"
        if playing:
            self.backend.write_bytes(key, json.dumps({
                "device": self.settings.device_name, "time": time.time()}).encode())
        else:
            self.backend.delete(key)

    def others_playing(self, game) -> list:
        mine = f"{game.id}/playing/{self._device_tag()}.json"
        out = []
        for key in self.backend.list(f"{game.id}/playing/"):
            if key == mine:
                continue
            info = self._read_json(key) or {}
            if time.time() - info.get("time", 0) < self.PRESENCE_TTL:
                out.append(info.get("device", key.rsplit("/", 1)[-1][:-5]))
        return out

    # ---------- spazio sul cloud ----------
    def cloud_usage(self, game) -> dict:
        """{'current': byte dei salvataggi attuali, 'versions': byte delle versioni vecchie,
        'n_versions': quante}"""
        current = {e.get("blob") for e in self.load_manifest(game.id)["files"].values()
                   if not e.get("deleted")}
        sizes = self.backend.sizes(f"{game.id}/blobs/")
        sizes.update(self.backend.sizes(f"{game.id}/versions/"))
        sizes.update(self.backend.sizes(f"{game.id}/files/"))
        cur = sum(v or 0 for k, v in sizes.items() if k in current or "/files/" in k)
        old = {k: v for k, v in sizes.items() if k not in current and "/files/" not in k}
        return {"current": cur, "versions": sum(v or 0 for v in old.values()),
                "n_versions": len(old)}

    def _old_versions(self, game) -> list:
        """Versioni non attuali, dalla più vecchia: [(chiave, byte)]."""
        current = {e.get("blob") for e in self.load_manifest(game.id)["files"].values()
                   if not e.get("deleted")}
        sizes = self.backend.sizes(f"{game.id}/blobs/")
        sizes.update(self.backend.sizes(f"{game.id}/versions/"))
        old = [(k, v or 0) for k, v in sizes.items() if k not in current]
        return sorted(old, key=lambda kv: kv[0].rsplit("/", 1)[-1])  # nome = data

    def enforce_space(self, game) -> int:
        """Cancella le versioni più vecchie finché il gioco sta nel suo limite di spazio.
        I salvataggi attuali non si toccano mai. Ritorna i byte liberati."""
        limit = game.max_cloud_mb * 1024 * 1024
        usage = self.cloud_usage(game)
        total = usage["current"] + usage["versions"]
        freed = 0
        for key, size in self._old_versions(game):
            if total - freed <= limit:
                break
            self.backend.delete(key)
            freed += size
        if freed:
            self.log(f"[{game.name}] limite spazio: liberati {freed // 1024} KB di versioni vecchie")
        return freed

    def cleanup_versions(self, game, keep: int) -> int:
        """Tiene solo le ultime `keep` versioni per file. Ritorna i byte liberati."""
        by_file = {}
        for key, size in self._old_versions(game):
            folder = key.rsplit("/", 1)[0]
            by_file.setdefault(folder, []).append((key, size))
        freed = 0
        for items in by_file.values():
            for key, size in items[:-keep] if keep > 0 else items:
                self.backend.delete(key)
                freed += size
        return freed

    # ---------- istantanee ----------
    def snapshot(self, game, reason: str):
        from .snapshots import SnapshotStore
        if not Path(game.path).is_dir():
            return None
        local = self.scan_local(game)
        return SnapshotStore(self.app_dir, game.id).take(
            game.path, local, reason, self.settings.snapshots_keep)

    # ---------- un file ----------
    def _sync_one(self, rel, game, local, remote, base, mtimes, changes, stats, propagate):
        L = local.get(rel, {}).get("hash")
        R = remote.get(rel, {}).get("hash")
        B = base.get(rel)
        if game.mode == "backup":
            # Il gioco ha già il suo cloud (Steam, Epic, GOG): SaveSync tiene solo le
            # copie con le versioni. Carico quello che cambia qui, non scarico mai.
            if L is not None and L != B and L != R:
                try:
                    self._upload(game, rel, rel in remote, changes, base, mtimes, remote)
                    stats["up"] += 1
                except Exception as e:
                    stats["errors"] += 1
                    self.log(f"[{game.name}] errore su {rel}: {e}")
            elif L is not None:
                base[rel] = L
                mtimes[rel] = local[rel]["mtime"]
            return
        if L == R:
            if L is None:
                base.pop(rel, None)
                mtimes.pop(rel, None)
            else:
                if B != L:
                    mtimes[rel] = local[rel]["mtime"]
                base[rel] = L
            return

        try:
            local_changed, remote_changed = L != B, R != B
            if local_changed and not remote_changed:
                if L is not None:
                    self._upload(game, rel, rel in remote, changes, base, mtimes, remote)
                    stats["up"] += 1
                elif propagate:
                    self._delete_remote(game, rel, changes, base, remote)
                    mtimes.pop(rel, None)
                    stats["deleted"] += 1
                else:  # cancellato in locale ma non propaghiamo: lo rimettiamo
                    self._download(game, rel, remote[rel], base, mtimes); stats["down"] += 1
            elif remote_changed and not local_changed:
                if R is not None and L is not None and \
                        remote[rel].get("mtime", 0) < mtimes.get(rel, 0) - 0.001:
                    # Il cloud propone un salvataggio PIÙ VECCHIO di quello già
                    # sincronizzato: tipico delle cartelle cloud (Proton Drive, OneDrive,
                    # Dropbox) quando un manifest vecchio arriva in ritardo e
                    # sovrascrive quello nuovo. Non si torna indietro: ricarico il mio.
                    self.log(f"[{game.name}] sul cloud c'è una versione più vecchia di "
                             f"{rel}: ricarico quella più recente")
                    self._upload(game, rel, True, changes, base, mtimes, remote)
                    stats["up"] += 1
                elif R is not None:
                    self._download(game, rel, remote[rel], base, mtimes); stats["down"] += 1
                elif propagate:
                    self._delete_local(game, rel, base)
                    mtimes.pop(rel, None)
                    stats["deleted"] += 1
                else:
                    self._upload(game, rel, False, changes, base, mtimes, remote)
                    stats["up"] += 1
            else:
                stats["conflicts"] += 1
                if L is None:
                    self._download(game, rel, remote[rel], base, mtimes); stats["down"] += 1
                elif R is None:
                    self._upload(game, rel, False, changes, base, mtimes, remote)
                    stats["up"] += 1
                elif local[rel]["mtime"] >= remote[rel]["mtime"]:
                    self.log(f"[{game.name}] CONFLITTO su {rel}: vince il locale (più recente), "
                             f"quello del cloud resta nelle versioni")
                    self._upload(game, rel, True, changes, base, mtimes, remote)
                    stats["up"] += 1
                else:
                    self.log(f"[{game.name}] CONFLITTO su {rel}: vince il cloud "
                             f"({remote[rel].get('device')}), il locale va nei backup")
                    self._download(game, rel, remote[rel], base, mtimes); stats["down"] += 1
        except Exception as e:
            stats["errors"] += 1
            self.log(f"[{game.name}] errore su {rel}: {e}")

    # ---------- salvataggi fatti di più file ----------
    @staticmethod
    def group_key(game, rel) -> str:
        """Quali file formano UN salvataggio.
        folder: la sottocartella di primo livello (Cyberpunk: ManualSave-12/sav.dat +
                metadata.json + screenshot.png); i file sciolti restano singoli
        name:   stesso nome, estensioni diverse (Skyrim: Save1.ess + Save1.skse)
        none:   ogni file per conto suo"""
        mode = getattr(game, "group_by", "folder")
        if mode == "folder" and "/" in rel:
            return rel.split("/", 1)[0] + "/"
        if mode == "name":
            folder, _, name = rel.rpartition("/")
            stem = name.split(".", 1)[0]
            return f"{folder}/{stem}" if folder else stem
        return rel

    def _sync_group(self, rels, game, local, remote, base, mtimes, changes, stats, propagate):
        """Come per un file, ma il salvataggio si decide e si applica tutto insieme: mai il
        sav.dat di un PC con il metadata dell'altro."""
        ctx = dict(game=game, local=local, remote=remote, base=base, mtimes=mtimes,
                   changes=changes, stats=stats)
        L = {r: local[r]["hash"] for r in rels if r in local}
        R = {r: remote[r]["hash"] for r in rels if r in remote}
        B = {r: base[r] for r in rels if r in base}
        name = self.group_key(game, rels[0])
        if L == R or (L != B and R == B):
            # tutto uguale, oppure ho cambiato solo io (il cloud è fermo): file per file va bene
            for r in rels:
                self._sync_one(r, propagate=propagate, **ctx)
            return
        l_time = max((local[r]["mtime"] for r in L), default=0)
        r_time = max((remote[r].get("mtime", 0) for r in R), default=0)
        if L == B:  # è cambiato solo il cloud
            known = max((mtimes.get(r, 0) for r in rels), default=0)
            if L and R and r_time < known - 0.001:
                self.log(f"[{game.name}] sul cloud c'è una versione più vecchia del "
                         f"salvataggio {name}: ricarico quella più recente")
                self._push_group(rels, **ctx)
            elif not R and not propagate:
                self._push_group(rels, **ctx)  # sparito dal cloud: lo rimetto
            else:
                self._apply_group(rels, propagate=True, **ctx)
            return
        stats["conflicts"] += 1
        if l_time >= r_time:
            self.log(f"[{game.name}] CONFLITTO sul salvataggio {name}: vince il locale (più "
                     f"recente), quello del cloud resta nelle versioni")
            self._push_group(rels, **ctx)
        else:
            self.log(f"[{game.name}] CONFLITTO sul salvataggio {name}: vince il cloud, il "
                     f"locale va nei backup")
            self._apply_group(rels, propagate=True, **ctx)

    def _push_group(self, rels, game, local, remote, base, mtimes, changes, stats):
        """Il mio salvataggio vince intero: carico i miei file e tolgo dal manifest quelli
        che nel mio non ci sono (restano tra le versioni)."""
        for r in rels:
            if r in local:
                if local[r]["hash"] != remote.get(r, {}).get("hash"):
                    self._upload(game, r, r in remote, changes, base, mtimes, remote)
                    stats["up"] += 1
                else:
                    base[r] = local[r]["hash"]
                    mtimes[r] = local[r]["mtime"]
            elif r in remote:
                self._delete_remote(game, r, changes, base, remote)
                mtimes.pop(r, None)
            else:
                base.pop(r, None)
                mtimes.pop(r, None)

    def _apply_group(self, rels, game, local, remote, base, mtimes, changes, stats, propagate):
        """Il salvataggio del cloud vince intero. Tutto o niente: prima scarico e verifico
        TUTTI i file, poi li sostituisco insieme. Se ne manca anche uno (con le cartelle
        cloud può arrivare in ritardo) non tocco niente e riprovo dopo."""
        todo = [r for r in rels if r in remote and
                local.get(r, {}).get("hash") != remote[r]["hash"]]
        fetched = []
        try:
            for r in todo:
                fetched.append((r, self._fetch(game, r, remote[r])))
        except Exception:
            for _, tmp in fetched:
                tmp.unlink(missing_ok=True)
            raise
        for r, tmp in fetched:
            self._commit(game, r, tmp, remote[r], base, mtimes)
            stats["down"] += 1
        for r in rels:
            if r in local and r not in remote:
                # non fa parte del salvataggio vincente: va nei backup e si toglie
                self._delete_local(game, r, base)
                mtimes.pop(r, None)
            elif r in remote and r not in todo:
                base[r] = remote[r]["hash"]
                mtimes[r] = remote[r].get("mtime", 0)

    def _upload(self, game, rel, overwrite, changes, base, mtimes, remote):
        src = Path(game.path, rel)
        prefix = self._k_blobs(game.id, rel)
        # Fotografia del file: se il gioco lo riscrive mentre carichiamo non mandiamo roba a metà
        with tempfile.TemporaryDirectory() as td:
            snap = Path(td, "snap")
            shutil.copy2(src, snap)
            st = snap.stat()
            h = sha256_file(snap)
            if rel in remote and not remote[rel].get("blob"):
                self._migrate_legacy(game, rel, remote[rel])
            existing = sorted(self.backend.list(prefix))
            same = [k for k in existing if k.endswith("__" + h[:16])]
            if same:
                blob = same[-1]  # stesso contenuto già sul cloud (es. ripristino)
            else:
                # hash corto nel nome: i percorsi Windows hanno un limite di 260 caratteri
                # (il contenuto viene comunque verificato con l'hash intero al download)
                blob = f"{prefix}{_stamp()}__{self._device_tag()}__{h[:16]}"
                self.backend.upload_file(blob, snap)
                existing.append(blob)
        changes[rel] = {"hash": h, "mtime": st.st_mtime, "size": st.st_size, "blob": blob,
                        "device": self.settings.device_name, "time": time.time()}
        self._prune(existing, keep=blob)
        base[rel] = h
        mtimes[rel] = st.st_mtime
        self.log(f"[{game.name}] ↑ {rel}")

    def _fetch(self, game, rel, entry, key=None) -> Path:
        """Scarica in un file temporaneo accanto alla destinazione e verifica l'hash."""
        dst = Path(game.path, rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".savesync-tmp")
        try:
            src = key or (entry or {}).get("blob") or self._k_file(game.id, rel)
            try:
                self.backend.download_file(src, tmp)
            except FileNotFoundError:
                raise IOError("file non ancora arrivato sul cloud, riprovo più tardi")
            if key is None and sha256_file(tmp) != entry["hash"]:
                raise IOError("hash non corrisponde (file sul cloud ancora in caricamento?)")
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        return tmp

    def _commit(self, game, rel, tmp, entry, base, mtimes=None, track=True):
        """Sostituisce il file locale con quello scaricato (facendo prima il backup)."""
        dst = Path(game.path, rel)
        try:
            h = sha256_file(tmp)
            if dst.exists():
                self._backup_local(game, rel)
            replace_retry(tmp, dst)
        finally:
            tmp.unlink(missing_ok=True)
        if entry and entry.get("mtime"):
            os.utime(dst, (entry["mtime"], entry["mtime"]))  # alcuni giochi guardano la data
        if track:
            base[rel] = h
            if mtimes is not None:
                mtimes[rel] = entry.get("mtime", 0)
        self.log(f"[{game.name}] ↓ {rel}")

    def _download(self, game, rel, entry, base, mtimes=None, key=None):
        # Prima scarico e verifico, POI faccio il backup: se il download fallisce non
        # riempio i backup di copie identiche (che spingerebbero fuori la cronologia vera)
        tmp = self._fetch(game, rel, entry, key)
        self._commit(game, rel, tmp, entry, base, mtimes, track=key is None)

    def _delete_remote(self, game, rel, changes, base, remote):
        # il blob resta come versione: basta toglierlo dal manifest
        if not remote.get(rel, {}).get("blob"):
            self._migrate_legacy(game, rel, remote.get(rel, {}))
        changes[rel] = {"deleted": True, "device": self.settings.device_name, "time": time.time()}
        base.pop(rel, None)
        self.log(f"[{game.name}] ✕ cloud {rel}")

    def _delete_local(self, game, rel, base):
        self._backup_local(game, rel)
        Path(game.path, rel).unlink(missing_ok=True)
        base.pop(rel, None)
        self.log(f"[{game.name}] ✕ locale {rel}")

    # ---------- versioni e backup ----------
    def _migrate_legacy(self, game, rel, entry):
        """File caricato dalla 0.2.0 in files/<rel>: lo sposto tra i blob prima di
        sostituirlo, così resta tra le versioni."""
        legacy = self._k_file(game.id, rel)
        h = entry.get("hash")
        if not h:
            return
        device = re.sub(r"[^\w.-]+", "_", entry.get("device", "")) or "pc"
        try:
            self.backend.copy(legacy,
                              f"{self._k_blobs(game.id, rel)}{_stamp()}__{device}__{h[:16]}")
            self.backend.delete(legacy)
        except Exception:
            pass  # file vecchio già sparito: niente da migrare

    def _prune(self, blobs, keep):
        """Tiene il blob attuale + le ultime `max_versions` versioni."""
        blobs = sorted(set(blobs))
        old = [b for b in blobs if b != keep]
        for b in old[:-self.settings.max_versions] if self.settings.max_versions > 0 else []:
            try:
                self.backend.delete(b)
            except Exception:
                pass

    def _backup_local(self, game, rel):
        src = Path(game.path, rel)
        folder = self.app_dir / "backups" / game.id / rel
        folder.mkdir(parents=True, exist_ok=True)
        existing = sorted(folder.iterdir())
        if existing and existing[-1].stat().st_size == src.stat().st_size and \
                sha256_file(existing[-1]) == sha256_file(src):
            return  # identico all'ultimo backup
        dst = folder / _stamp()
        while dst.exists():
            dst = dst.with_name(dst.name + "_")
        shutil.copy2(src, dst)
        old = sorted(folder.iterdir())
        for o in old[:-self.settings.max_versions] if self.settings.max_versions > 0 else []:
            o.unlink()

    def remote_files(self, game) -> list:
        m = self.load_manifest(game.id)
        return sorted(r for r, e in m["files"].items() if not e.get("deleted"))

    def list_versions(self, game, rel) -> list:
        """[(etichetta, origine, chiave/percorso)] dal più recente: versioni cloud + backup locali."""
        out = []
        keys = self.backend.list(self._k_blobs(game.id, rel)) + \
            self.backend.list(self._k_versions(game.id, rel))
        for k in keys:
            stamp, _, device = k.rsplit("/", 1)[-1].partition("__")
            device = device.split("__")[0]
            out.append((stamp, f"cloud ({device})", k))
        local_dir = self.app_dir / "backups" / game.id / rel
        if local_dir.exists():
            for p in local_dir.iterdir():
                out.append((p.name, "backup locale", str(p)))
        return sorted(out, reverse=True)

    # ---------- anteprima di una versione ----------
    IMAGE_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".webp")
    _INFO_KEYS = re.compile(r"name|level|lvl|time|location|area|zone|region|quest|mission|"
                            r"chapter|difficulty|character|class|play|date|money|gold|day",
                            re.IGNORECASE)

    def _read_version(self, origin, ref):
        if origin == "backup locale":
            return Path(ref).read_bytes()
        return self.backend.read_bytes(ref)

    @classmethod
    def _json_info(cls, data: bytes) -> list:
        """Le voci più parlanti di un file JSON di metadati (livello, luogo, tempo...)."""
        try:
            obj = json.loads(data.decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError):
            return []
        out = []

        def walk(o, path=""):
            if len(out) >= 10:
                return
            if isinstance(o, dict):
                for k, v in o.items():
                    walk(v, k)
            elif isinstance(o, list):
                for v in o[:20]:
                    walk(v, path)
            elif path and cls._INFO_KEYS.search(path) and isinstance(o, (str, int, float)) \
                    and not isinstance(o, bool) and str(o).strip() and len(str(o)) < 80:
                out.append((path, o))
        walk(obj)
        return out

    def preview(self, game, rel, origin, ref) -> dict:
        """{'image': byte o None, 'image_from': nome, 'info': [(chiave, valore)], 'size': n}
        Se il file non è un'immagine, cerca nella stessa cartella lo screenshot e i metadati
        salvati nello stesso momento (es. Cyberpunk: sav.dat + screenshot.png + metadata.json)."""
        data = self._read_version(origin, ref) or b""
        out = {"image": None, "image_from": "", "info": [], "size": len(data)}
        low = rel.lower()
        if low.endswith(self.IMAGE_EXT):
            out["image"], out["image_from"] = data, rel
        elif low.endswith(".json"):
            out["info"] = self._json_info(data)
        if out["image"] and out["info"]:
            return out
        stamp = Path(ref).name.split("__")[0][:15]
        folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
        # versioni dei file vicini, caricate entro 10 minuti da questa
        candidates = []
        if origin == "backup locale":
            root = self.app_dir / "backups" / game.id / folder
            for f in (root.iterdir() if root.is_dir() else []):
                if f.is_dir() and f.name != Path(rel).name:
                    for v in f.iterdir():
                        candidates.append((f.name, "backup locale", str(v), v.name[:15]))
        else:
            prefix = f"{game.id}/blobs/{folder}/" if folder else f"{game.id}/blobs/"
            for key in self.backend.list(prefix):
                parts = key[len(prefix):].split("/")
                if len(parts) == 2 and parts[0] != Path(rel).name:
                    candidates.append((parts[0], "cloud", key, parts[1][:15]))

        def distance(c):
            try:
                a = time.mktime(time.strptime(stamp, "%Y%m%d-%H%M%S"))
                b = time.mktime(time.strptime(c[3], "%Y%m%d-%H%M%S"))
                return abs(a - b)
            except ValueError:
                return float("inf")
        for name, org, key, _ in sorted(candidates, key=distance):
            if distance((name, org, key, _)) > 600:
                break
            if not out["image"] and name.lower().endswith(self.IMAGE_EXT):
                out["image"], out["image_from"] = self._read_version(org, key), name
            elif not out["info"] and name.lower().endswith(".json"):
                out["info"] = self._json_info(self._read_version(org, key) or b"")
        return out

    @staticmethod
    def suggest_grouping(folder) -> str:
        """Guarda come il gioco organizza i file e propone cos'è UN salvataggio."""
        root = Path(folder)
        try:
            subdirs = [d for d in root.iterdir() if d.is_dir()]
            files = [f.name for f in root.iterdir() if f.is_file()]
        except OSError:
            return "folder"
        multi = [d for d in subdirs if sum(1 for f in d.iterdir() if f.is_file()) >= 2]
        if len(multi) >= 2:
            return "folder"  # tante cartelle con più file dentro: ognuna è un salvataggio
        stems = {}
        for f in files:
            stems.setdefault(f.split(".", 1)[0], set()).add(f)
        if sum(1 for v in stems.values() if len(v) >= 2) >= 2:
            return "name"  # Save1.ess + Save1.skse, Save2.ess + Save2.skse...
        return "folder"

    def restore_group(self, game, rel, origin, ref) -> list:
        """Rimette TUTTI i file del salvataggio di `rel` com'erano nel momento della versione
        scelta (es. sav.dat + metadata + screenshot). Ritorna i file ripristinati."""
        key = self.group_key(game, rel)
        stamp = Path(ref).name.split("__")[0]

        def secs(st):
            """'20261028-152200-123' -> secondi con i millisecondi (i file dello stesso
            salvataggio vengono caricati a pochi millisecondi l'uno dall'altro)."""
            try:
                t = time.mktime(time.strptime(st[:15], "%Y%m%d-%H%M%S"))
            except ValueError:
                return None
            ms = st[16:19]
            return t + (int(ms) / 1000 if ms.isdigit() else 0)
        target = secs(stamp)
        members = {r for r in set(self.scan_local(game)) | set(self.remote_files(game))
                   if self.group_key(game, r) == key}
        plan = {rel: (origin, ref)}
        for r in members - {rel}:
            best = None
            for _, org, k in self.list_versions(game, r):
                t = secs(Path(k).name.split("__")[0])
                if t is None or target is None or (org == "backup locale") != \
                        (origin == "backup locale"):
                    continue
                if abs(t - target) <= 600 and (best is None or abs(t - target) < best[0]):
                    best = (abs(t - target), org, k)
            if best:
                plan[r] = (best[1], best[2])
        with self.lock:
            self.snapshot(game, f"prima di ripristinare il salvataggio {key}")
        for r, (org, k) in plan.items():
            self.restore_version(game, r, org, k, snapshot=False)
        return sorted(plan)

    def restore_version(self, game, rel, origin, ref, snapshot=True) -> None:
        """Rimette una versione vecchia in locale; alla prossima sync parte verso il cloud."""
        with self.lock:
            if snapshot:
                self.snapshot(game, f"prima di ripristinare {rel}")
            dst = Path(game.path, rel)
            if origin == "backup locale":
                if dst.exists():
                    self._backup_local(game, rel)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ref, dst)
            else:
                self._download(game, rel, None, {}, key=ref)
            # aggiorna la data: è una modifica "nuova" e deve vincere su eventuali conflitti
            os.utime(dst, None)
            self.log(f"[{game.name}] ripristinata versione {Path(ref).name} di {rel}")
