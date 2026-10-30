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
import calendar
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .backends import replace_retry

REGISTRY_KEY = "games.json"
REGISTRY_FILE = ".resosync-registry/registry.json"  # registro di Windows esportato (registry.py)
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
    """Data per i nomi di versioni e backup: in UTC (la Z finale), così l'ordine è giusto
    anche tra dispositivi con fusi orari diversi (es. uno Steam Deck rimasto su UTC).
    Millisecondi inclusi: due versioni nello stesso secondo non devono sovrascriversi."""
    now = time.time()
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime(now)) + f"-{int(now * 1000) % 1000:03d}Z"


def stamp_epoch(name: str):
    """'20261029-101500-123Z__PC__hash' -> secondi (None se non è una data). I nomi senza Z
    sono delle versioni fino alla 0.5.0, scritti in ora locale."""
    st = name.split("__")[0]
    try:
        base = time.strptime(st[:15], "%Y%m%d-%H%M%S")
    except ValueError:
        return None
    ms = st[16:19]
    frac = int(ms) / 1000 if ms.isdigit() else 0
    return (calendar.timegm(base) if st[19:20] == "Z" else time.mktime(base)) + frac


def _by_date(names):
    """Ordina dalla più vecchia; i nomi senza data vanno in fondo (non si cancellano per primi)."""
    return sorted(names, key=lambda n: (stamp_epoch(str(n).replace("\\", "/").rsplit("/", 1)[-1])
                                        or float("inf"), str(n)))


# Una versione appena caricata da un altro dispositivo può non comparire ancora nel manifest
# che vediamo (le cartelle cloud consegnano i file in ritardo): per un po' non si cancella.
GRACE = 2 * 3600
ANCESTRY = 20  # quante versioni precedenti ricorda ogni caricamento (hash corti)
# Dove Linux monta dischi e schede SD (Steam Deck: /run/media/deck/<scheda>)
REMOVABLE_ROOTS = ("/run/media", "/media", "/mnt")


def _volume_missing(path) -> bool:
    """La cartella sta su un disco rimovibile che adesso non c'è? Su Windows una lettera di
    unità assente fa già fallire la creazione; su Linux no, e la cartella finirebbe sul
    disco interno (o in RAM) al posto della scheda."""
    p = os.path.abspath(str(path))
    for root in REMOVABLE_ROOTS:
        root = os.path.abspath(root)
        if p.startswith(root + os.sep):
            parts = p[len(root) + 1:].split(os.sep)
            # /run/media/<utente>/<scheda>, /media/<utente>/<scheda>, /mnt/<scheda>
            depth = 1 if os.path.basename(root) == "mnt" else 2
            mount = os.path.join(root, *parts[:depth])
            return len(parts) > depth and not os.path.isdir(mount)
    return False


TEMP_SUFFIXES = (".resosync-tmp", ".resosync-old", ".resosync-new", ".savesync-tmp",
                 ".partial")


def _young(name) -> bool:
    t = stamp_epoch(str(name).replace("\\", "/").rsplit("/", 1)[-1])
    return t is not None and t > time.time() - GRACE


class SyncEngine:
    def __init__(self, settings, backend, app_dir: Path, log=print, registry=None):
        self.settings = settings
        self.backend = backend
        self.app_dir = Path(app_dir)
        self.log = log
        self.lock = threading.Lock()
        self._hash_cache = {}  # path -> ((size, mtime_ns, ctime_ns), hash)
        self._registered = set()
        self._unreadable = {}  # game_id -> file presenti ma bloccati in questo giro
        from .registry import RegistrySync
        self.registry = RegistrySync(self.app_dir, registry, log=lambda m: self.log(m))
        self._keepers = {}  # (game_id, token) -> Event che ferma il rinnovo del lock
        self._scan = None  # durante una sync: i file locali come li ha visti la scansione
        self._base = {}  # durante una sync: la base (com'era ogni file all'ultima sync)

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

    def _list_direct(self, prefix):
        """Le chiavi subito dentro `prefix`, non quelle nelle sottocartelle: le versioni di
        "Saves" (blobs/Saves/) non comprendono quelle di "Saves/x" (blobs/Saves/x/)."""
        return [k for k in self.backend.list(prefix) if "/" not in k[len(prefix):]]

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
                    self._keep_lock(gid, token)
                    return token
            if time.time() >= deadline:
                who = (cur or {}).get("device", "?")
                raise CloudBusy(f"{who} sta sincronizzando questo gioco")
            time.sleep(2)

    def _keep_lock(self, gid, token):
        """Rinnova il lock finché la sync lavora: una sync lunga (salvataggi grandi, rete lenta)
        non deve vederlo scadere a metà e un altro PC entrare mentre scrive."""
        key = f"{gid}/lock.json"
        stop, guard = threading.Event(), threading.Lock()
        self._keepers[(gid, token)] = (stop, guard)

        def loop():
            while not stop.wait(LOCK_TTL / 3):
                try:
                    with guard:  # mai un rinnovo dopo il rilascio (ricreerebbe il lock)
                        if stop.is_set():
                            return
                        renew()
                except Exception:
                    pass  # rete giù per un attimo: riprovo al prossimo giro

        def renew():
            cur = self._read_json(key)
            if not cur or cur.get("token") != token:
                stop.set()  # l'abbiamo perso (es. PC sospeso): non lo riprendo
                return
            self.backend.write_bytes(key, json.dumps({
                "token": token, "device": self.settings.device_name,
                "expires": time.time() + LOCK_TTL}).encode())
        threading.Thread(target=loop, daemon=True).start()

    def release_lock(self, gid, token):
        keeper = self._keepers.pop((gid, token), None)
        if keeper:
            keeper[0].set()
            with keeper[1]:
                pass  # aspetta un rinnovo eventualmente in corso
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
        # senza distinguere maiuscole: su Windows "*.SAV" prende "slot.sav", e i giochi
        # Windows sotto Proton (Steam Deck) non sono abituati a un sistema che le distingue
        if getattr(game, "registry", None) and rel == REGISTRY_FILE:
            return True  # il registro esportato va sempre, qualunque siano i filtri
        rel = rel.lower()
        name = rel.rsplit("/", 1)[-1]

        def hit(patterns):
            return any(fnmatch.fnmatchcase(rel, p.lower()) or fnmatch.fnmatchcase(name, p.lower())
                       for p in patterns)
        return hit(game.patterns or ["*"]) and not hit(game.excludes)

    def _hash(self, path: Path, st) -> str:
        # anche il ctime: su Linux (Steam Deck) cambia a ogni scrittura, anche quando un
        # programma rimette la data di modifica di prima
        key, sig = str(path), (st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        cached = self._hash_cache.get(key)
        if cached and cached[0] == sig:
            return cached[1]
        h = sha256_file(path)
        self._hash_cache[key] = (sig, h)
        return h

    @staticmethod
    def _interrupted(game) -> bool:
        return any(f.endswith((".resosync-old", ".resosync-new"))
                   for _, _, files in os.walk(game.path) for f in files)

    def _recover_interrupted(self, game):
        """Copie .resosync-old rimaste = una sostituzione di un salvataggio da più file è stata
        interrotta (spegnimento, crash): lo slot può essere mescolato. Rimetto i file com'erano
        prima, così torna intero e la sync rifà il lavoro da capo."""
        for dirpath, _, files in os.walk(game.path):
            for f in files:
                mark = Path(dirpath, f)
                if f.endswith(".resosync-old"):
                    dst = mark.with_name(f[:-len(".resosync-old")])
                    replace_retry(mark, dst)
                elif f.endswith(".resosync-new"):  # file arrivato con lo slot interrotto
                    dst = mark.with_name(f[:-len(".resosync-new")])
                    dst.unlink(missing_ok=True)
                    mark.unlink()
                else:
                    continue
                self._hash_cache.pop(str(dst), None)
                self.log(f"[{game.name}] sostituzione interrotta: rimesso "
                         f"{dst.relative_to(game.path).as_posix()} com'era")

    def scan_local(self, game) -> dict:
        root = Path(game.path)
        out = {}
        unreadable = self._unreadable[game.id] = set()
        for dirpath, dirs, files in os.walk(root):
            for f in files:
                if f.endswith(TEMP_SUFFIXES):
                    continue  # rimasugli di un download o di una copia interrotti
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
    def _registry_step(self, game):
        """Salvataggi nel registro di Windows <-> file esportato nella cartella del gioco."""
        if not getattr(game, "registry", None):
            return
        try:
            if self.registry.reconcile(game):
                # file riscritto: la data può essere quella (vecchia) del registro e la
                # dimensione uguale, quindi la cache degli hash non se ne accorgerebbe
                self._hash_cache.pop(str(self.registry.file_for(game)), None)
        except Exception as e:  # registro non leggibile: la sync dei file va avanti lo stesso
            self.log(f"[{game.name}] registro di Windows non sincronizzato: {e}")

    def sync_game(self, game, wait_lock=0.0) -> dict:
        with self.lock:
            if game.registry and not Path(game.path).is_dir() and \
                    not _volume_missing(game.path):
                Path(game.path).mkdir(parents=True, exist_ok=True)  # gioco solo-registro
            if Path(game.path).is_dir():
                self._registry_step(game)  # il gioco ha salvato nel registro: va nel file
            try:
                return self._sync_locked(game, wait_lock)
            finally:
                self._registry_step(game)  # arrivato dal cloud: torna nel registro

    def _sync_locked(self, game, wait_lock) -> dict:
        if not Path(game.path).is_dir() and _volume_missing(game.path):
            raise FileNotFoundError(f"la cartella di {game.name} è su un disco o una scheda "
                                    f"SD che adesso non c'è: inseriscila e riprovo")
        Path(game.path).mkdir(parents=True, exist_ok=True)  # PC nuovo: cartella da creare
        if self._interrupted(game):
            # con il lock: le copie potrebbero essere di un'altra sync in corso adesso (il
            # launcher e l'app sullo stesso PC), non di una interrotta
            token = self.acquire_lock(game.id, wait_lock)
            try:
                self._recover_interrupted(game)
            finally:
                self.release_lock(game.id, token)
        self.register_game(game)
        local = self.scan_local(game)
        if local and not self._state_file(game.id).exists():
            # prima sync di questo gioco su questo PC (o su un cloud nuovo): è il momento
            # in cui ogni differenza diventa un conflitto. Prima una fotografia della
            # cartella, così si torna indietro con un clic
            try:
                self.snapshot(game, "prima della prima sincronizzazione")
            except Exception as e:
                self.log(f"[{game.name}] istantanea non riuscita: {e}")
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
        self._scan = local
        try:
            return self._sync_game_inner(game, local, manifest)
        finally:
            self._scan, self._base = None, {}

    def _descends(self, rel, entry) -> bool:
        """La versione sul cloud è stata fatta partendo dalla mia (o dopo averla vista)? Allora
        è sicuramente più nuova, qualunque cosa dica l'orologio di chi l'ha caricata."""
        mine = self._base.get(rel)
        return bool(mine) and mine[:16] in entry.get("anc", ())

    def _is_rollback(self, rel, entry, mtimes) -> bool:
        """La versione sul cloud è PIÙ VECCHIA di quella che ho già sincronizzato (manifest
        vecchio arrivato in ritardo)? Si guarda la data, ma se quella versione discende dalla
        mia non è un ritorno indietro: uno Steam Deck con l'orologio indietro non deve vedersi
        scartare i salvataggi."""
        if self._descends(rel, entry):
            return False
        return entry.get("mtime", 0) < mtimes.get(rel, 0) - 0.001

    def _group_rollback(self, rels, R, B, remote, mtimes) -> bool:
        if any(self._descends(r, remote[r]) for r in R if R[r] != B.get(r)):
            return False
        known = max((mtimes.get(r, 0) for r in rels), default=0)
        return max((remote[r].get("mtime", 0) for r in R), default=0) < known - 0.001

    def _sync_game_inner(self, game, local, manifest) -> dict:
        remote = {r: e for r, e in manifest["files"].items() if not e.get("deleted")}
        state = self._load_state(game.id)
        base, mtimes = state["base"], state["mtimes"]
        self._base = base
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
        gone = [r for r in base if r not in local and r not in skip and r in remote]
        if propagate and len(gone) >= 3 and len(gone) * 2 > len(base):
            # Sparita più di metà dei salvataggi in un colpo (gioco reinstallato che lascia
            # solo il config, cartella ripulita da un programma): non lo propago agli altri PC
            self.log(f"[{game.name}] spariti {len(gone)} file su {len(base)} tutti insieme: "
                     f"non propago le cancellazioni, li riscarico")
            propagate = False
        tombs = [r for r, e in manifest["files"].items() if e.get("deleted") and r in local
                 and base.get(r) == local[r]["hash"]]
        if propagate and len(tombs) >= 3 and len(tombs) * 2 > len(local):
            # stesso discorso, al contrario: un altro PC chiede di cancellare quasi tutto
            self.log(f"[{game.name}] il cloud chiede di cancellare {len(tombs)} file su "
                     f"{len(local)}: non lo faccio, li ricarico")
            propagate = False

        # cancellazioni segnate nel manifest: servono a riconoscere quelle vecchie arrivate
        # in ritardo (vedi _stale_delete)
        self._tomb = {r: e for r, e in manifest["files"].items() if e.get("deleted")}
        ctx = {"game": game, "local": local, "remote": remote, "base": base, "mtimes": mtimes,
               "changes": changes, "stats": stats, "propagate": propagate}
        groups = {}
        for rel in sorted(set(local) | set(remote) | set(base)):
            groups.setdefault(self.group_key(game, rel), []).append(rel)
        collide = self._case_collisions(game, local, remote)
        stats["errors"] += len(collide)
        for rels in groups.values():
            if any(r in skip or r in collide for r in rels):
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

    @staticmethod
    def _case_insensitive(path) -> bool:
        return os.name == "nt" or sys.platform == "darwin"

    def _case_collisions(self, game, local, remote) -> set:
        """Su Windows "Slot.sav" e "slot.sav" sono lo stesso file: se sul cloud ci sono tutti e
        due (uno da Linux / Steam Deck) se ne sincronizza uno solo, quello che c'è qui (o il più
        recente), invece di scaricarli a turno uno sopra l'altro."""
        if not self._case_insensitive(game.path):
            return set()
        by_low = {}
        for r in set(local) | set(remote):
            by_low.setdefault(r.lower(), []).append(r)
        out = set()
        for rels in by_low.values():
            if len(rels) < 2:
                continue
            keep = next((r for r in rels if r in local), None) or \
                max(rels, key=lambda r: remote[r].get("mtime", 0))
            others = sorted(set(rels) - {keep})
            out.update(others)
            self.log(f"[{game.name}] sul cloud ci sono {keep} e {', '.join(others)}, che qui "
                     f"sono lo stesso file (Windows non distingue maiuscole e minuscole): "
                     f"sincronizzo solo {keep}")
        return out

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
        old = {k: v or 0 for k, v in sizes.items() if k not in current and not _young(k)}
        return [(k, old[k]) for k in _by_date(old)]

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

    # ---------- verifica del cloud ----------
    def verify_cloud(self, game, repair=True) -> dict:
        """Scarica ogni salvataggio attuale dal cloud e ne controlla l'hash. Se manca o è
        rovinato lo ricarica da una copia con lo stesso contenuto (il file qui, i backup
        locali, le istantanee, un'altra copia sul cloud).
        {"ok": n, "repaired": [file], "broken": [(file, motivo)], "arriving": [file]}
        I controlli si fanno senza lock (con salvataggi grandi durano minuti e la sync non
        deve aspettare); il lock serve solo per riparare."""
        report = {"ok": 0, "repaired": [], "broken": [], "arriving": []}
        todo = []
        with tempfile.TemporaryDirectory() as td:
            for rel, e in sorted(self.load_manifest(game.id)["files"].items()):
                if e.get("deleted"):
                    continue
                problem = self._check_blob(game, rel, e, Path(td, "check"))
                if not problem:
                    report["ok"] += 1
                elif _young(e.get("blob") or "") or time.time() - e.get("time", 0) < GRACE:
                    # appena caricato da un altro dispositivo: con le cartelle cloud può non
                    # essere ancora arrivato (o essere a metà). Si riguarda la prossima volta
                    report["arriving"].append(rel)
                else:
                    todo.append((rel, e, problem))
            if todo:
                self._repair(game, todo, report, repair, Path(td))
        done = self._verified()
        done[game.id] = time.time()
        p = self.app_dir / "verified.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(done))
        return report

    def _verified(self) -> dict:
        try:
            return json.loads((self.app_dir / "verified.json").read_text())
        except (OSError, ValueError):
            return {}

    def verify_due(self, game, days) -> bool:
        """È ora di verificare il cloud di questo gioco? (solo se è già sincronizzato qui)"""
        return days > 0 and self._state_file(game.id).exists() and \
            time.time() - self._verified().get(game.id, 0) >= days * 86400

    def _repair(self, game, todo, report, repair, td):
        with self.lock:
            token = self.acquire_lock(game.id, 30)
            try:
                current = self.load_manifest(game.id)["files"]
                changes, rotten = {}, []
                for rel, e, problem in todo:
                    if current.get(rel, {}).get("blob") != e.get("blob"):
                        continue  # cambiato nel frattempo: c'è già una versione nuova
                    source = self._find_copy(game, rel, e["hash"], td / "copy") \
                        if repair else None
                    if source is None:
                        report["broken"].append((rel, problem))
                        self.log(f"[{game.name}] VERIFICA: {rel} sul cloud {problem} e non "
                                 f"ne ho una copia uguale")
                        continue
                    blob = f"{self._k_blobs(game.id, rel)}{_stamp()}__{self._device_tag()}__" \
                           f"{e['hash'][:16]}"
                    self.backend.upload_file(blob, source)
                    changes[rel] = dict(e, blob=blob)
                    report["repaired"].append(rel)
                    if e.get("blob") and problem != "manca":
                        rotten.append(e["blob"])
                    self.log(f"[{game.name}] VERIFICA: {rel} sul cloud {problem}: ricaricato")
                if changes:
                    self._save_manifest(game.id, changes)
                for key in rotten:  # la copia rovinata non deve restare tra le versioni
                    try:
                        self.backend.delete(key)
                    except Exception:
                        pass
            finally:
                self.release_lock(game.id, token)

    def _check_blob(self, game, rel, entry, tmp):
        """None se il salvataggio sul cloud è integro, sennò cosa non va."""
        key = entry.get("blob") or self._k_file(game.id, rel)
        try:
            self.backend.download_file(key, tmp)
        except FileNotFoundError:
            return "manca"
        except Exception as e:  # cifrato con un'altra chiave, rovinato, ...
            return f"non è leggibile ({e})"
        try:
            return None if sha256_file(tmp) == entry["hash"] else "è rovinato"
        finally:
            tmp.unlink(missing_ok=True)

    def _find_copy(self, game, rel, h, tmp):
        """Un file con contenuto `h`, da qualsiasi parte: quello del gioco, un backup locale,
        un'istantanea, un'altra copia sul cloud. Ritorna il percorso o None."""
        from .snapshots import SnapshotStore
        candidates = [Path(game.path, rel)]
        backups = self.app_dir / "backups" / game.id / rel
        if backups.is_dir():
            candidates += sorted((f for f in backups.iterdir() if f.is_file()), reverse=True)
        candidates.append(SnapshotStore(self.app_dir, game.id)._obj(h))
        for c in candidates:
            try:
                if c.is_file() and sha256_file(c) == h:
                    return c
            except OSError:
                continue
        for key in self._list_direct(self._k_blobs(game.id, rel)):
            if key.endswith("__" + h[:16]):
                try:
                    self.backend.download_file(key, tmp)
                    if sha256_file(tmp) == h:
                        return tmp
                except Exception:
                    continue
        return None

    # ---------- prima sincronizzazione ----------
    def first_sync_plan(self, game):
        """Prima sync di un gioco su questo PC (o su un cloud nuovo): cosa succederebbe, senza
        toccare niente. None se non c'è niente di rischioso (solo file nuovi da una parte o
        dall'altra) o se il gioco è già stato sincronizzato qui.
        {"down": [...], "up": [...], "conflicts": [{"name", "winner", "local_time",
        "remote_time", "device"}]}: stesse regole della sync vera (vince il più recente)."""
        if game.mode == "backup" or self._state_file(game.id).exists():
            return None
        local = self.scan_local(game) if Path(game.path).is_dir() else {}
        remote = {r: e for r, e in self.load_manifest(game.id)["files"].items()
                  if not e.get("deleted")}
        groups = {}
        for rel in sorted(set(local) | set(remote)):
            groups.setdefault(self.group_key(game, rel), []).append(rel)
        plan = {"down": [], "up": [], "conflicts": []}
        for key, rels in groups.items():
            L = {r: local[r]["hash"] for r in rels if r in local}
            R = {r: remote[r]["hash"] for r in rels if r in remote}
            if L == R:
                continue
            if not L:
                plan["down"].append(key)
            elif not R:
                plan["up"].append(key)
            else:
                l_time = max(local[r]["mtime"] for r in L)
                r_time = max(remote[r].get("mtime", 0) for r in R)
                newest = max(R, key=lambda r: remote[r].get("mtime", 0))
                plan["conflicts"].append({
                    "name": key, "winner": "local" if l_time >= r_time else "cloud",
                    "local_time": l_time, "remote_time": r_time,
                    "device": remote[newest].get("device", "?")})
        return plan if plan["conflicts"] else None

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
            # Il gioco ha già il suo cloud (Steam, Epic, GOG): ResoSync tiene solo le
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
                        self._is_rollback(rel, remote[rel], mtimes):
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
                elif propagate and self._stale_delete([rel], mtimes):
                    # la cancellazione è più vecchia del mio salvataggio: è un manifest vecchio
                    # arrivato in ritardo, non un "cancella" vero. Non cancello, ricarico.
                    self.log(f"[{game.name}] cancellazione di {rel} più vecchia del mio "
                             f"salvataggio: lo tengo e lo ricarico")
                    self._upload(game, rel, False, changes, base, mtimes, remote)
                    stats["up"] += 1
                elif propagate:
                    self._delete_local(game, rel, base)
                    mtimes.pop(rel, None)
                    stats["deleted"] += 1
                else:
                    self._upload(game, rel, False, changes, base, mtimes, remote)
                    stats["up"] += 1
            else:
                stats["conflicts"] += 1
                stale = R is not None and self._is_rollback(rel, remote[rel], mtimes)
                if L is None and stale and propagate:
                    # cancellato qui, e sul cloud c'è solo una versione PIÙ VECCHIA di quella
                    # che avevo (manifest in ritardo): vince la cancellazione
                    self._delete_remote(game, rel, changes, base, remote)
                    mtimes.pop(rel, None)
                    stats["deleted"] += 1
                elif L is None and stale and B and self._restore_known(game, rel, B, base,
                                                                       mtimes, changes, remote):
                    # cancellato qui senza propagare: rimetto la versione più recente che
                    # conoscevo, non quella vecchia che il cloud propone per errore
                    stats["down"] += 1
                elif L is None:
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
        if not L and B and L != R:
            self._group_deleted_here(rels, R, B, propagate, **ctx)
            return
        if L == R or (L != B and R == B):
            # tutto uguale, oppure ho cambiato solo io (il cloud è fermo): file per file va bene
            for r in rels:
                self._sync_one(r, propagate=propagate, **ctx)
            return
        l_time = max((local[r]["mtime"] for r in L), default=0)
        r_time = max((remote[r].get("mtime", 0) for r in R), default=0)
        if L == B:  # è cambiato solo il cloud
            if L and R and self._group_rollback(rels, R, B, remote, mtimes):
                self.log(f"[{game.name}] sul cloud c'è una versione più vecchia del "
                         f"salvataggio {name}: ricarico quella più recente")
                self._push_group(rels, **ctx)
            elif not R and (not propagate or self._stale_delete(rels, mtimes)):
                self._push_group(rels, **ctx)  # sparito dal cloud (o cancellazione vecchia)
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

    def _restore_known(self, game, rel, h, base, mtimes, changes, remote) -> bool:
        """Rimette in locale il contenuto con hash `h` preso dalle versioni sul cloud e lo
        rende di nuovo quello attuale. False se quella versione non c'è più."""
        keys = [k for k in self._list_direct(self._k_blobs(game.id, rel))
                if k.endswith("__" + h[:16])]
        if not keys:
            return False
        entry = {"hash": h, "blob": keys[-1], "mtime": mtimes.get(rel, 0)}
        tmp = self._fetch(game, rel, entry)
        self._commit(game, rel, tmp, entry, base, mtimes)
        self.log(f"[{game.name}] {rel}: rimessa la versione più recente che conoscevo")
        self._upload(game, rel, True, changes, base, mtimes, remote)  # torna quella attuale
        return True

    def _stale_delete(self, rels, mtimes) -> bool:
        """Le cancellazioni nel manifest per questi file sono PIÙ VECCHIE del salvataggio che
        ho (es. un altro PC ha cancellato, io poi ho salvato di nuovo, e il suo manifest è
        arrivato dopo il mio): non valgono."""
        tombs = [self._tomb[r].get("time", 0) for r in rels if r in getattr(self, "_tomb", {})]
        mine = max((mtimes.get(r, 0) for r in rels), default=0)
        return bool(tombs) and max(tombs) < mine - 0.001

    def _group_deleted_here(self, rels, R, B, propagate, game, local, remote, base, mtimes,
                            changes, stats):
        """Lo slot è stato cancellato qui (tutti i suoi file). Sempre tutto insieme."""
        ctx = dict(game=game, local=local, remote=remote, base=base, mtimes=mtimes,
                   changes=changes, stats=stats)
        if R == B:  # il cloud è fermo: è solo la mia cancellazione
            if propagate:
                self._push_group(rels, **ctx)
            else:
                self._apply_group(rels, propagate=False, **ctx)  # lo rimetto, intero
        elif R and not self._group_rollback(rels, R, B, remote, mtimes):
            # sul cloud c'è una versione più nuova di quella che avevo: vince, intera
            stats["conflicts"] += 1
            self._apply_group(rels, propagate=True, **ctx)
        elif propagate:
            self._push_group(rels, **ctx)  # la cancellazione vince su una versione vecchia
        elif not self._restore_known_group(rels, B, **ctx):
            self._apply_group(rels, propagate=False, **ctx)

    def _restore_known_group(self, rels, B, game, local, remote, base, mtimes, changes,
                             stats) -> bool:
        """Rimette INTERA la versione dello slot che conoscevo (quella in `B`), presa dalle
        versioni sul cloud, e la rende di nuovo quella attuale. False se manca qualcosa."""
        fetched = []
        try:
            for r, h in B.items():
                keys = [k for k in self._list_direct(self._k_blobs(game.id, r))
                        if k.endswith("__" + h[:16])]
                if not keys:
                    raise FileNotFoundError(r)
                entry = {"hash": h, "blob": keys[-1], "mtime": mtimes.get(r, 0)}
                fetched.append((r, entry, self._fetch(game, r, entry)))
        except Exception:
            for _, _, tmp in fetched:
                tmp.unlink(missing_ok=True)
            return False
        for r, entry, tmp in fetched:
            self._commit(game, r, tmp, entry, base, mtimes)
            stats["down"] += 1
        for r in rels:
            if r in B:
                self._upload(game, r, r in remote, changes, base, mtimes, remote)
            elif r in remote:
                self._delete_remote(game, r, changes, base, remote)
        self.log(f"[{game.name}] slot {self.group_key(game, rels[0])}: rimessa la versione "
                 f"più recente che conoscevo")
        return True

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
        # Anche la sostituzione è tutto o niente: tengo una copia dei file attuali e, se una
        # si blocca a metà (file aperto dall'antivirus o dal gioco), rimetto com'era prima.
        # Mai il sav.dat nuovo con il metadata vecchio.
        drop = [r for r in rels if r in local and r not in remote]
        saved = {r: (base.get(r), mtimes.get(r)) for r in rels}
        olds, touched, done = {}, [], 0
        try:
            for r in [r for r, _ in fetched] + drop:
                self._check_untouched(game, r)
                dst = Path(game.path, r)
                if dst.exists():
                    olds[r] = dst.with_name(dst.name + ".resosync-old")
                    shutil.copy2(dst, olds[r])
                else:  # file nuovo: se tutto si interrompe va tolto (_recover_interrupted)
                    olds[r] = dst.with_name(dst.name + ".resosync-new")
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    olds[r].touch()
            for r, tmp in fetched:
                touched.append(r)
                self._commit(game, r, tmp, remote[r], base, mtimes)
                stats["down"] += 1
                done += 1
            for r in drop:
                # non fa parte del salvataggio vincente: va nei backup e si toglie
                touched.append(r)
                self._delete_local(game, r, base)
                mtimes.pop(r, None)
        except Exception:
            for _, tmp in fetched:
                tmp.unlink(missing_ok=True)
            for r in reversed(touched):
                dst = Path(game.path, r)
                try:
                    if r in olds and olds[r].name.endswith(".resosync-old"):
                        replace_retry(olds[r], dst)
                        del olds[r]
                    else:
                        dst.unlink(missing_ok=True)
                except OSError as e:  # la copia resta lì, non la cancello
                    self.log(f"[{game.name}] non riesco a rimettere {r}: la copia è in "
                             f"{olds.pop(r, dst)} ({e})")
            for r, (h, t) in saved.items():  # anche la base torna com'era
                for d, v in ((base, h), (mtimes, t)):
                    if v is None:
                        d.pop(r, None)
                    else:
                        d[r] = v
            stats["down"] -= done
            raise
        finally:
            for old in olds.values():
                old.unlink(missing_ok=True)
        for r in rels:
            if r in remote and r not in todo:
                base[r] = remote[r]["hash"]
                mtimes[r] = remote[r].get("mtime", 0)

    def _upload(self, game, rel, overwrite, changes, base, mtimes, remote):
        src = Path(game.path, rel)
        prefix = self._k_blobs(game.id, rel)
        # Fotografia del file: se il gioco lo riscrive mentre carichiamo non mandiamo roba a metà
        with tempfile.TemporaryDirectory() as td:
            snap = Path(td, "snap")
            before = src.stat()
            shutil.copy2(src, snap)
            after, st = src.stat(), snap.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) \
                    or st.st_size != after.st_size:
                # il gioco lo stava scrivendo proprio adesso: la copia può essere a metà
                raise IOError("il gioco sta scrivendo il file, riprovo più tardi")
            h = sha256_file(snap)
            seen = (self._scan or {}).get(rel)
            if seen and seen["hash"] != h:
                # cambiato dopo la scansione: la decisione (carico? conflitto?) era presa su
                # un altro contenuto, si rifà al prossimo giro
                raise IOError("il file è cambiato durante la sincronizzazione, riprovo")
            if rel in remote and not remote[rel].get("blob"):
                self._migrate_legacy(game, rel, remote[rel])
            existing = sorted(self._list_direct(prefix))
            sizes = self.backend.sizes(prefix) if any(
                k.endswith("__" + h[:16]) for k in existing) else {}
            # stesso contenuto già sul cloud (es. ripristino): lo riuso, ma solo se ha la
            # dimensione giusta (un caricamento a metà o una copia rovinata non va riusata)
            same = [k for k in existing if k.endswith("__" + h[:16])
                    and sizes.get(k) in (None, st.st_size,
                                         st.st_size + getattr(self.backend, "overhead", 0))]
            if same:
                blob = same[-1]  # stesso contenuto già sul cloud (es. ripristino)
            else:
                # hash corto nel nome: i percorsi Windows hanno un limite di 260 caratteri
                # (il contenuto viene comunque verificato con l'hash intero al download)
                blob = f"{prefix}{_stamp()}__{self._device_tag()}__{h[:16]}"
                self.backend.upload_file(blob, snap)
                existing.append(blob)
        # da quali versioni discende questa: quella che avevo, quella che sostituisco sul
        # cloud e i loro antenati (vedi _descends)
        prev = remote.get(rel, {})
        anc = []
        for a in [base.get(rel, "")[:16], prev.get("hash", "")[:16], *prev.get("anc", ())]:
            if a and a != h[:16] and a not in anc:
                anc.append(a)
        changes[rel] = {"hash": h, "mtime": st.st_mtime, "size": st.st_size, "blob": blob,
                        "device": self.settings.device_name, "time": time.time(),
                        "anc": anc[:ANCESTRY]}
        self._prune(existing, keep=blob)
        base[rel] = h
        mtimes[rel] = st.st_mtime
        self.log(f"[{game.name}] ↑ {rel}")

    def _fetch(self, game, rel, entry, key=None) -> Path:
        """Scarica in un file temporaneo accanto alla destinazione e verifica l'hash."""
        dst = Path(game.path, rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".resosync-tmp")
        try:
            src = key or (entry or {}).get("blob") or self._k_file(game.id, rel)
            try:
                self.backend.download_file(src, tmp)
            except FileNotFoundError:
                raise IOError("file non ancora arrivato sul cloud, riprovo più tardi")
            if key is None:
                expected = entry["hash"]
            else:  # versione scelta a mano: l'hash (corto) è nel nome, <data>__<pc>__<hash>
                tail = key.rsplit("/", 1)[-1].split("__")[-1]
                expected = tail if re.fullmatch(r"[0-9a-f]{16}", tail) else None
            if expected and not sha256_file(tmp).startswith(expected):
                raise IOError("hash non corrisponde (file sul cloud ancora in caricamento?)")
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        return tmp

    def _check_untouched(self, game, rel):
        """Prima di sostituire o cancellare un file locale: dev'essere ancora quello visto
        dalla scansione. Se il gioco ha salvato nel frattempo, quel salvataggio nuovo non si
        tocca e si decide di nuovo al giro dopo."""
        if self._scan is None:
            return  # ripristino chiesto a mano, non una sync
        p = Path(game.path, rel)
        seen = self._scan.get(rel)
        try:
            st = p.stat()
        except FileNotFoundError:
            return  # non c'è (più): niente da perdere
        if seen is None or self._hash(p, st) != seen["hash"]:
            raise IOError(f"{rel} è appena cambiato in locale, riprovo al prossimo giro")

    def _commit(self, game, rel, tmp, entry, base, mtimes=None, track=True):
        """Sostituisce il file locale con quello scaricato (facendo prima il backup)."""
        dst = Path(game.path, rel)
        try:
            self._check_untouched(game, rel)
            h = sha256_file(tmp)
            if dst.exists():
                self._backup_local(game, rel)
            replace_retry(tmp, dst)
        finally:
            tmp.unlink(missing_ok=True)
            self._hash_cache.pop(str(dst), None)
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
        self._check_untouched(game, rel)
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
        """Tiene il blob attuale + le ultime `max_versions` versioni (e quelle appena arrivate)."""
        old = [b for b in _by_date(set(blobs)) if b != keep]
        for b in old[:-self.settings.max_versions] if self.settings.max_versions > 0 else []:
            if _young(b):
                continue
            try:
                self.backend.delete(b)
            except Exception:
                pass

    def _backup_local(self, game, rel):
        src = Path(game.path, rel)
        folder = self.app_dir / "backups" / game.id / rel
        folder.mkdir(parents=True, exist_ok=True)
        # solo i file: una sottocartella è il backup di un altro file (es. "Saves/x" quando
        # "Saves" era una cartella)
        existing = _by_date(f for f in folder.iterdir() if f.is_file())
        if existing and existing[-1].stat().st_size == src.stat().st_size and \
                sha256_file(existing[-1]) == sha256_file(src):
            return  # identico all'ultimo backup
        dst = folder / _stamp()
        while dst.exists():
            dst = dst.with_name(dst.name + "_")
        shutil.copy2(src, dst)
        old = _by_date(f for f in folder.iterdir() if f.is_file())
        for o in old[:-self.settings.max_versions] if self.settings.max_versions > 0 else []:
            o.unlink()

    def remote_files(self, game) -> list:
        m = self.load_manifest(game.id)
        return sorted(r for r, e in m["files"].items() if not e.get("deleted"))

    def list_versions(self, game, rel) -> list:
        """[(etichetta, origine, chiave/percorso)] dal più recente: versioni cloud + backup locali."""
        out = []
        keys = self._list_direct(self._k_blobs(game.id, rel)) + \
            self._list_direct(self._k_versions(game.id, rel))
        for k in keys:
            stamp, _, device = k.rsplit("/", 1)[-1].partition("__")
            device = device.split("__")[0]
            out.append((stamp, f"cloud ({device})", k))
        local_dir = self.app_dir / "backups" / game.id / rel
        if local_dir.exists():
            for p in local_dir.iterdir():
                if p.is_file():
                    out.append((p.name, "backup locale", str(p)))
        return sorted(out, key=lambda v: stamp_epoch(Path(v[2]).name) or 0, reverse=True)

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
        stamp = Path(ref).name
        folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
        # versioni dei file vicini, caricate entro 10 minuti da questa
        candidates = []
        if origin == "backup locale":
            root = self.app_dir / "backups" / game.id / folder
            for f in (root.iterdir() if root.is_dir() else []):
                if f.is_dir() and f.name != Path(rel).name:
                    for v in f.iterdir():
                        if v.is_file():
                            candidates.append((f.name, "backup locale", str(v), v.name))
        else:
            prefix = f"{game.id}/blobs/{folder}/" if folder else f"{game.id}/blobs/"
            for key in self.backend.list(prefix):
                parts = key[len(prefix):].split("/")
                if len(parts) == 2 and parts[0] != Path(rel).name:
                    candidates.append((parts[0], "cloud", key, parts[1]))

        def distance(c):
            a, b = stamp_epoch(stamp), stamp_epoch(c[3])
            return abs(a - b) if a is not None and b is not None else float("inf")
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
        # Si raggruppa SOLO se la struttura lo dimostra: raggruppare file indipendenti (es.
        # Saves/slot1 e Saves/slot2) farebbe perdere le modifiche fatte in parallelo su due PC.
        root = Path(folder)
        try:
            subdirs = [d for d in root.iterdir() if d.is_dir()]
            files = [f.name for f in root.iterdir() if f.is_file()]
        except OSError:
            return "none"

        def kinds(d):
            try:
                return {f.suffix.lower() or f.name.lower() for f in d.iterdir() if f.is_file()}
            except OSError:
                return set()
        # tante cartelle con dentro lo stesso tipo di file (sav.dat + metadata.json + ...)
        multi = [kinds(d) for d in subdirs if len(kinds(d)) >= 2]
        if len(multi) >= 2 and len(set.intersection(*multi)) >= 2:
            return "folder"
        stems = {}
        for f in files:
            stems.setdefault(f.split(".", 1)[0], set()).add(f)
        if sum(1 for v in stems.values() if len(v) >= 2) >= 2:
            return "name"  # Save1.ess + Save1.skse, Save2.ess + Save2.skse...
        return "none"

    def restore_group(self, game, rel, origin, ref) -> list:
        """Rimette TUTTI i file del salvataggio di `rel` com'erano nel momento della versione
        scelta (es. sav.dat + metadata + screenshot). Ritorna i file ripristinati."""
        key = self.group_key(game, rel)
        stamp = Path(ref).name.split("__")[0]

        secs = stamp_epoch  # i file dello stesso salvataggio: a pochi ms l'uno dall'altro
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
                tmp = dst.with_name(dst.name + ".resosync-tmp")
                try:  # copia accanto e poi sostituisco: mai un salvataggio copiato a metà
                    shutil.copy2(ref, tmp)
                    replace_retry(tmp, dst)
                finally:
                    tmp.unlink(missing_ok=True)
            else:
                self._download(game, rel, None, {}, key=ref)
            # aggiorna la data: è una modifica "nuova" e deve vincere su eventuali conflitti
            os.utime(dst, None)
            self.log(f"[{game.name}] ripristinata versione {Path(ref).name} di {rel}")
