"""Cifratura lato client: il provider cloud vede solo dati illeggibili.

AES-256-GCM, chiave derivata dalla passphrase con scrypt. Il "sale" sta sul cloud in
crypto.json, così ogni PC con la stessa passphrase ricava la stessa chiave.
I nomi dei file restano in chiaro, il contenuto (compresi manifest ed elenco giochi) no.
"""
import base64
import json
import os

from .backends import Backend

MAGIC = b"SSE1"
META_KEY = "crypto.json"
CHECK = b"resosync-ok"


class CryptoError(Exception):
    pass


def _derive(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(passphrase.encode("utf-8"))


class EncryptedBackend(Backend):
    def __init__(self, inner: Backend, passphrase: str):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        if not passphrase:
            raise CryptoError("Cifratura attiva ma passphrase vuota")
        self.inner = inner
        meta = inner.read_bytes(META_KEY)
        if meta:
            meta = json.loads(meta)
            salt = base64.b64decode(meta["salt"])
            self._aes = AESGCM(_derive(passphrase, salt))
            try:
                ok = self._dec(base64.b64decode(meta["check"])) == CHECK
            except Exception:
                ok = False
            if not ok:
                raise CryptoError("Passphrase sbagliata: non coincide con quella usata sul cloud")
        else:
            if _has_encrypted_data(inner):
                # Dati cifrati ma crypto.json non c'è: quasi sempre è solo in ritardo (PC
                # nuovo, client cloud che sta ancora scaricando). Crearne uno con un sale nuovo
                # lo sovrascriverebbe e renderebbe illeggibile tutto il cloud.
                raise CryptoError("Sul cloud ci sono salvataggi cifrati ma manca ancora "
                                  "crypto.json (le chiavi della cifratura): se il programma "
                                  "del cloud sta ancora scaricando, aspetta che finisca e "
                                  "riprova.")
            # cloud vuoto, o in chiaro (si sta attivando la cifratura): chiavi nuove
            salt = os.urandom(16)
            self._aes = AESGCM(_derive(passphrase, salt))
            inner.write_bytes(META_KEY, json.dumps({
                "v": 1, "kdf": "scrypt-n15-r8-p1", "cipher": "aes-256-gcm",
                "salt": base64.b64encode(salt).decode(),
                "check": base64.b64encode(self._enc(CHECK)).decode(),
            }).encode())

    def _enc(self, data: bytes) -> bytes:
        nonce = os.urandom(12)
        return MAGIC + nonce + self._aes.encrypt(nonce, data, None)

    def _dec(self, data: bytes) -> bytes:
        if not data.startswith(MAGIC):
            return data  # file caricato prima di attivare la cifratura
        from cryptography.exceptions import InvalidTag
        try:
            return self._aes.decrypt(data[4:16], data[16:], None)
        except InvalidTag:
            raise CryptoError("File cifrato con un'altra chiave o danneggiato")

    def read_bytes(self, key):
        data = self.inner.read_bytes(key)
        return None if data is None else self._dec(data)

    def write_bytes(self, key, data):
        self.inner.write_bytes(key, self._enc(data))

    def upload_file(self, key, path):
        with open(path, "rb") as f:
            self.inner.write_bytes(key, self._enc(f.read()))

    def download_file(self, key, path):
        data = self.inner.read_bytes(key)
        if data is None:
            raise FileNotFoundError(key)
        with open(path, "wb") as f:
            f.write(self._dec(data))

    def copy(self, src, dst):
        self.inner.copy(src, dst)

    def delete(self, key):
        self.inner.delete(key)

    def list(self, prefix):
        return self.inner.list(prefix)

    def sizes(self, prefix):
        return self.inner.sizes(prefix)


def _has_encrypted_data(inner: Backend) -> bool:
    """C'è già qualcosa di cifrato sul cloud? Guardo l'elenco dei giochi o un manifest."""
    sample = inner.read_bytes("games.json")
    if sample is None:
        key = next((k for k in inner.list("") if k.endswith("/manifest.json")), None)
        sample = inner.read_bytes(key) if key else None
    return bool(sample) and sample.startswith(MAGIC)


def cloud_is_encrypted(backend: Backend) -> bool:
    return backend.read_bytes(META_KEY) is not None
