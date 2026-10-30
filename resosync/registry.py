"""Salvataggi nel registro di Windows (alcuni giochi Unity e molti giochi vecchi salvano lì).

Le chiavi di un gioco vengono esportate in un file JSON dentro la cartella del gioco
(.resosync-registry/registry.json), che poi viaggia come qualsiasi altro salvataggio: versioni,
conflitti, istantanee. Quando quel file cambia (arrivato dall'altro PC, o ripristinato) il suo
contenuto torna nel registro.

Solo HKEY_CURRENT_USER: è lì che salvano i giochi, e si scrive senza permessi di
amministratore. Importando si creano chiavi e si scrivono valori, non si cancella mai niente.
"""
import base64
import hashlib
import json
import os
from pathlib import Path

ROOT = "HKEY_CURRENT_USER"
FOLDER = ".resosync-registry"
FILE = "registry.json"


def supported(key: str) -> bool:
    return key.replace("\\", "/").split("/", 1)[0].upper() == ROOT and "/" in key


class WinRegistry:
    """Il registro vero (solo Windows)."""

    def __init__(self):
        import winreg
        self.w = winreg

    def _split(self, key):
        return key.replace("/", "\\").split("\\", 1)[1]

    def read(self, key):
        """{"values": {nome: [tipo, dato]}, "keys": {sottochiave: ...}} o None se non c'è."""
        try:
            h = self.w.OpenKey(self.w.HKEY_CURRENT_USER, self._split(key))
        except OSError:
            return None
        with h:
            return self._read(h)

    def _read(self, h):
        out = {"values": {}, "keys": {}}
        i = 0
        while True:
            try:
                name, data, kind = self.w.EnumValue(h, i)
            except OSError:
                break
            out["values"][name] = [kind, _enc(data)]
            i += 1
        i = 0
        while True:
            try:
                sub = self.w.EnumKey(h, i)
            except OSError:
                break
            with self.w.OpenKey(h, sub) as sh:
                out["keys"][sub] = self._read(sh)
            i += 1
        return out

    def exists(self, key) -> bool:
        try:
            self.w.CloseKey(self.w.OpenKey(self.w.HKEY_CURRENT_USER, self._split(key)))
            return True
        except OSError:
            return False

    def mtime(self, key) -> float:
        """Ultima modifica della chiave (per ordinare i giochi trovati), in secondi."""
        try:
            with self.w.OpenKey(self.w.HKEY_CURRENT_USER, self._split(key)) as h:
                ns100 = self.w.QueryInfoKey(h)[2]  # centinaia di ns dal 1601
            return ns100 / 1e7 - 11644473600
        except OSError:
            return 0.0

    def write(self, key, tree):
        with self.w.CreateKey(self.w.HKEY_CURRENT_USER, self._split(key)) as h:
            self._write(h, tree)

    def _write(self, h, tree):
        for name, (kind, data) in tree.get("values", {}).items():
            self.w.SetValueEx(h, name, 0, kind, _dec(data))
        for sub, subtree in tree.get("keys", {}).items():
            with self.w.CreateKey(h, sub) as sh:
                self._write(sh, subtree)


def _enc(data):
    if isinstance(data, bytes):
        return {"b64": base64.b64encode(data).decode()}
    return data


def _dec(data):
    if isinstance(data, dict) and "b64" in data:
        return base64.b64decode(data["b64"])
    return data


def default_store():
    return WinRegistry() if os.name == "nt" else None


def dump(store, keys) -> bytes:
    """Le chiavi del gioco come JSON stabile (stesso registro = stessi byte = stesso hash)."""
    data = {k: store.read(k) for k in sorted(keys) if supported(k)}
    return json.dumps(data, sort_keys=True, indent=1, ensure_ascii=False).encode("utf-8")


def load(store, keys, raw: bytes) -> None:
    """Rimette nel registro il contenuto di un file esportato. Solo le chiavi del gioco."""
    data = json.loads(raw.decode("utf-8"))
    allowed = {k.lower() for k in keys if supported(k)}
    for key, tree in data.items():
        if tree is not None and key.lower() in allowed:
            store.write(key, tree)


def _h(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RegistrySync:
    """Tiene allineati registro e file esportato di ogni gioco. Ricorda l'ultimo contenuto
    esportato o importato, così sa chi è cambiato: il registro (il gioco ha salvato) o il file
    (è arrivato dal cloud, o è stato ripristinato)."""

    def __init__(self, app_dir, store=None, log=print):
        self.state_file = Path(app_dir) / "registry-state.json"
        self.store = store if store is not None else default_store()
        self.log = log

    @staticmethod
    def file_for(game) -> Path:
        return Path(game.path, FOLDER, FILE)

    def _state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}

    def _remember(self, gid, h):
        st = self._state()
        st[gid] = h
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(st))

    def _write_file(self, f, data):
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".resosync-tmp")
        tmp.write_bytes(data)
        os.replace(tmp, f)

    def reconcile(self, game) -> str:
        """Prima e dopo ogni sync. Ritorna 'export', 'import' o '' (niente da fare).
        - il registro è cambiato (il gioco ha salvato), o è la prima volta: va nel file
        - è cambiato solo il file (arrivato dal cloud, o ripristinato): va nel registro
        Se cambiano tutti e due vince il registro, che è il salvataggio vivo: la versione
        del file resta tra le versioni."""
        keys = [k for k in getattr(game, "registry", []) if supported(k)]
        if not keys or self.store is None:
            return ""
        f = self.file_for(game)
        last = self._state().get(game.id)
        cur = dump(self.store, keys)
        try:
            on_disk = f.read_bytes()
        except FileNotFoundError:
            on_disk = None
        if all(v is None for v in json.loads(cur).values()):
            # Niente nel registro: gioco mai giocato su questo PC (o chiavi cancellate). Come
            # una cartella vuota: non si esporta MAI sopra un salvataggio vero. Se il file c'è
            # (arrivato dal cloud), il suo contenuto va nel registro.
            if on_disk is None or all(v is None for v in json.loads(on_disk).values()):
                return ""
            return self._import(game, keys, f, on_disk)
        if last is None or _h(cur) != last or on_disk is None:
            if on_disk != cur:
                self._write_file(f, cur)
                # la data del file = quando il gioco ha salvato nel registro, non adesso: un
                # conflitto si decide sul salvataggio vero
                t = max((self.store.mtime(k) for k in keys), default=0)
                if t > 0:
                    os.utime(f, (t, t))
            self._remember(game.id, _h(cur))
            return "export" if on_disk != cur else ""
        if _h(on_disk) == last:
            return ""
        return self._import(game, keys, f, on_disk)

    def _import(self, game, keys, f, on_disk) -> str:
        load(self.store, keys, on_disk)
        after = dump(self.store, keys)
        if after != on_disk:  # nel registro c'era qualcosa in più (non si cancella mai)
            self._write_file(f, after)
        self._remember(game.id, _h(after))
        self.log(f"[{game.name}] registro di Windows aggiornato dal salvataggio")
        return "import"
