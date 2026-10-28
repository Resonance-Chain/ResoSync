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
            if not self._differs(local, manifest, skip):
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
        if propagate and base and not local and not skip:
            # Sparito tutto in un colpo (cartella spostata, disco scollegato, gioco
            # reinstallato): quasi certamente non è voluto, quindi non cancello il cloud.
            self.log(f"[{game.name}] la cartella è vuota: non propago le cancellazioni, "
                     f"riscarico dal cloud")
            propagate = False

        for rel in sorted(set(local) | set(remote) | set(base)):
            if rel in skip:
                continue
            L = local.get(rel, {}).get("hash")
            R = remote.get(rel, {}).get("hash")
            B = base.get(rel)
            if L == R:
                if L is None:
                    base.pop(rel, None)
                    mtimes.pop(rel, None)
                else:
                    if B != L:
                        mtimes[rel] = local[rel]["mtime"]
                    base[rel] = L
                continue

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

        if changes:
            self._save_manifest(game.id, changes)
        self._save_state(game.id, state)
        return stats

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

    def _download(self, game, rel, entry, base, mtimes=None, key=None):
        dst = Path(game.path, rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".savesync-tmp")
        # Prima scarico e verifico, POI faccio il backup: se il download fallisce non
        # riempio i backup di copie identiche (che spingerebbero fuori la cronologia vera)
        try:
            src = key or (entry or {}).get("blob") or self._k_file(game.id, rel)
            try:
                self.backend.download_file(src, tmp)
            except FileNotFoundError:
                raise IOError("file non ancora arrivato sul cloud, riprovo più tardi")
            h = sha256_file(tmp)
            if key is None and h != entry["hash"]:
                raise IOError("hash non corrisponde (file sul cloud ancora in caricamento?)")
            if dst.exists():
                self._backup_local(game, rel)
            replace_retry(tmp, dst)
        finally:
            tmp.unlink(missing_ok=True)
        if entry and entry.get("mtime"):
            os.utime(dst, (entry["mtime"], entry["mtime"]))  # alcuni giochi guardano la data
        if key is None:
            base[rel] = h
            if mtimes is not None:
                mtimes[rel] = entry.get("mtime", 0)
        self.log(f"[{game.name}] ↓ {rel}")

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

    def restore_version(self, game, rel, origin, ref) -> None:
        """Rimette una versione vecchia in locale; alla prossima sync parte verso il cloud."""
        with self.lock:
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
