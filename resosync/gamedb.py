"""Database dei percorsi di salvataggio, dal manifest open di Ludusavi
(https://github.com/mtkennerly/ludusavi-manifest, oltre 20.000 giochi).

Il manifest YAML (~17 MB) si scarica una volta a settimana e si converte in un JSON compatto
con solo quello che serve. Poi `detect()` espande i percorsi su questo PC e trova i giochi
che hanno davvero dei salvataggi.
"""
import getpass
import glob
import json
import os
import platform
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import registry

MANIFEST_URL = "https://raw.githubusercontent.com/mtkennerly/ludusavi-manifest/master/data/manifest.yaml"
MAX_AGE = 7 * 24 * 3600
DB_FORMAT = 4  # cambia quando cambia cosa teniamo nella cache (forza un nuovo download)
STORE_NAMES = {"steam": "Steam", "epic": "Epic", "gog": "GOG", "uplay": "Ubisoft",
               "origin": "EA"}
SYSTEM = {"Windows": "windows", "Darwin": "mac"}.get(platform.system(), "linux")


@dataclass
class Detected:
    name: str
    folder: str
    patterns: list
    process: str = ""
    steam_id: int = 0
    mtime: float = 0.0
    files: int = 0
    extra_folders: list = field(default_factory=list)
    install_dir: str = ""
    store_cloud: list = field(default_factory=list)  # es. ["Steam", "GOG"]
    registry: list = field(default_factory=list)  # chiavi del registro di Windows trovate


# ---------- download e cache ----------
def cache_file(app_dir) -> Path:
    return Path(app_dir) / "gamedb.json"


def load(app_dir) -> dict:
    try:
        return json.loads(cache_file(app_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def update(app_dir, force=False, progress=lambda msg: None) -> dict:
    """Scarica e converte il manifest se la cache è vecchia. Ritorna il db."""
    import requests
    import yaml

    db = load(app_dir)
    fresh = db and db.get("v") == DB_FORMAT and time.time() - db.get("fetched", 0) < MAX_AGE
    if fresh and not force:
        return db
    if db.get("v") != DB_FORMAT:
        db = {}  # formato vecchio: niente ETag, va riscaricato e riconvertito
    progress("Scarico il database dei giochi…")
    headers = {"If-None-Match": db["etag"]} if db.get("etag") else {}
    r = requests.get(MANIFEST_URL, headers=headers, timeout=120)
    if r.status_code == 304:
        db["fetched"] = time.time()
        _write(app_dir, db)
        return db
    r.raise_for_status()
    progress("Analizzo il database (qualche secondo)…")
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    raw = yaml.load(r.content, Loader=loader)
    db = {"v": DB_FORMAT, "fetched": time.time(), "etag": r.headers.get("ETag", ""),
          "games": compact(raw)}
    _write(app_dir, db)
    return db


def _write(app_dir, db):
    p = cache_file(app_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, p)


def _oses(when):
    return sorted({w.get("os") for w in (when or []) if isinstance(w, dict) and w.get("os")})


def compact(raw: dict) -> dict:
    """Tiene solo i giochi con file di salvataggio e solo i campi utili."""
    out = {}
    for name, g in raw.items():
        if not isinstance(g, dict) or not (g.get("files") or g.get("registry")):
            continue
        files = []
        for path, info in (g.get("files") or {}).items():
            info = info or {}
            tags = info.get("tags") or []
            if tags and "save" not in tags:
                continue  # solo config, non ci interessa
            files.append([path, _oses(info.get("when"))])
        reg = []
        for key, info in (g.get("registry") or {}).items():
            tags = (info or {}).get("tags") or []
            if (not tags or "save" in tags) and registry.supported(key):
                reg.append(key)
        if not files and not reg:
            continue
        exes = []
        for path, entries in (g.get("launch") or {}).items():
            oses = {o for e in (entries or []) for o in _oses(e.get("when"))}
            exes.append([path.rsplit("/", 1)[-1], sorted(oses)])
        entry = {"f": files}
        if reg:
            entry["r"] = sorted(reg)
        if g.get("installDir"):
            entry["d"] = list(g["installDir"])
        if exes:
            entry["x"] = exes
        if (g.get("steam") or {}).get("id"):
            entry["s"] = g["steam"]["id"]
        extra = (g.get("id") or {}).get("steamExtra") or []
        if extra:
            entry["sx"] = list(extra)
        clouds = sorted(k for k, v in (g.get("cloud") or {}).items() if v is True)
        if clouds:
            entry["c"] = clouds
        out[name] = entry
    return out


# ---------- percorsi di questo PC ----------
def _known_folder(guid: str, fallback: Path) -> Path:
    """Cartelle di Windows (es. Documenti, spesso spostata su OneDrive) via API di sistema."""
    if SYSTEM != "windows":
        return fallback
    try:
        import ctypes
        from ctypes import wintypes
        import uuid

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]
        u = uuid.UUID(guid)
        g = GUID(u.time_low, u.time_mid, u.time_hi_version,
                 (ctypes.c_ubyte * 8).from_buffer_copy(u.bytes[8:]))
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None,
                                                       ctypes.byref(out)) == 0:
            path = Path(out.value)
            ctypes.windll.ole32.CoTaskMemFree(out)
            return path
    except Exception:
        pass
    return fallback


def placeholders() -> dict:
    home = Path.home()
    env = os.environ.get
    p = {"<home>": home, "<osUserName>": getpass.getuser()}
    if SYSTEM == "windows":
        p.update({
            "<winAppData>": Path(env("APPDATA") or home / "AppData" / "Roaming"),
            "<winLocalAppData>": Path(env("LOCALAPPDATA") or home / "AppData" / "Local"),
            "<winLocalAppDataLow>": home / "AppData" / "LocalLow",
            "<winDocuments>": _known_folder("FDD39AD0-238F-46AF-ADB4-6C85480369C7",
                                            home / "Documents"),
            "<winPublic>": Path(env("PUBLIC") or "C:/Users/Public"),
            "<winProgramData>": Path(env("PROGRAMDATA") or "C:/ProgramData"),
            "<winDir>": Path(env("WINDIR") or "C:/Windows"),
        })
    else:
        p.update({
            "<xdgData>": Path(env("XDG_DATA_HOME") or home / ".local" / "share"),
            "<xdgConfig>": Path(env("XDG_CONFIG_HOME") or home / ".config"),
        })
    return p


def proton_placeholders(pfx: Path) -> dict:
    """Segnaposto Windows dentro un prefisso Wine/Proton (il finto C: di Steam Deck/Linux)."""
    users = pfx / "drive_c" / "users"
    names = [d.name for d in _subdirs(users) if d.name.lower() != "public"]
    user = "steamuser" if "steamuser" in names else (names[0] if names else "steamuser")
    home = users / user
    docs = home / "Documents"
    if not docs.exists() and (home / "My Documents").exists():
        docs = home / "My Documents"  # prefissi di Proton vecchi
    return {"<home>": home, "<osUserName>": user,
            "<winAppData>": home / "AppData" / "Roaming",
            "<winLocalAppData>": home / "AppData" / "Local",
            "<winLocalAppDataLow>": home / "AppData" / "LocalLow",
            "<winDocuments>": docs, "<winPublic>": users / "Public",
            "<winProgramData>": pfx / "drive_c" / "ProgramData",
            "<winDir>": pfx / "drive_c" / "windows"}


def steam_app_manifests(lib: Path) -> dict:
    """{appid: cartella d'installazione} dagli appmanifest_<id>.acf di una libreria Steam."""
    out = {}
    for acf in (lib / "steamapps").glob("appmanifest_*.acf"):
        try:
            text = acf.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        appid = re.search(r'"appid"\s+"(\d+)"', text)
        folder = re.search(r'"installdir"\s+"([^"]+)"', text)
        if appid and folder:
            out[int(appid.group(1))] = lib / "steamapps" / "common" / folder.group(1)
    return out


def _norm_title(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def heroic_prefixes() -> dict:
    """{titolo normalizzato: prefisso} per i giochi Epic/GOG installati con Heroic."""
    home = Path.home()
    roots = [home / "Games" / "Heroic" / "Prefixes" / "default",
             home / "Games" / "Heroic" / "Prefixes",
             home / ".var" / "app" / "com.heroicgameslauncher.hgl" / "data" / "heroic" / "Prefixes"]
    out = {}
    for root in roots:
        for d in _subdirs(root):
            if (d / "drive_c").is_dir():
                out.setdefault(_norm_title(d.name), d)
            elif (d / "pfx" / "drive_c").is_dir():
                out.setdefault(_norm_title(d.name), d / "pfx")
    return out


def steam_libraries() -> list:
    """Cartelle delle librerie Steam (per i giochi che salvano nella cartella d'installazione)."""
    roots = []
    if SYSTEM == "windows":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                roots.append(Path(winreg.QueryValueEx(k, "SteamPath")[0]))
        except OSError:
            pass
        roots.append(Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Steam")
    elif SYSTEM == "mac":
        roots.append(Path.home() / "Library" / "Application Support" / "Steam")
    else:
        roots += [Path.home() / ".steam" / "steam", Path.home() / ".local" / "share" / "Steam"]
    libs = []
    for root in roots:
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if vdf.exists():
            text = vdf.read_text(encoding="utf-8", errors="ignore")
            for m in re.findall(r'"path"\s+"([^"]+)"', text):
                libs.append(Path(m.replace("\\\\", "\\")))
        if (root / "steamapps").is_dir():
            libs.append(root)
    seen, out = set(), []
    for lib in libs:
        key = os.path.normcase(str(lib.resolve()) if lib.exists() else str(lib))
        if key not in seen and (lib / "steamapps" / "common").is_dir():
            seen.add(key)
            out.append(lib)
    return out


def installed_games() -> dict:
    """Cartelle d'installazione dei giochi su questo PC: {nome cartella minuscolo: [percorsi]}.
    Fonti: librerie Steam, manifest di Epic, registro e cartella predefinita di GOG."""
    found = {}

    def add(path):
        path = Path(path)
        if path.is_dir():
            found.setdefault(path.name.lower(), []).append(str(path))

    for lib in steam_libraries():
        for d in _subdirs(lib / "steamapps" / "common"):
            add(d)
    if SYSTEM == "windows":
        program_data = Path(os.environ.get("PROGRAMDATA") or "C:/ProgramData")
        for item in (program_data / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests").glob(
                "*.item"):
            try:
                loc = json.loads(item.read_text(encoding="utf-8", errors="ignore")).get(
                    "InstallLocation")
                if loc:
                    add(loc)
            except (OSError, ValueError):
                pass
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\WOW6432Node\GOG.com\Games") as root:
                for i in range(winreg.QueryInfoKey(root)[0]):
                    try:
                        with winreg.OpenKey(root, winreg.EnumKey(root, i)) as k:
                            add(winreg.QueryValueEx(k, "path")[0])
                    except OSError:
                        pass
        except OSError:
            pass
        pf86 = Path(os.environ.get("ProgramFiles(x86)") or "C:/Program Files (x86)")
        pf = Path(os.environ.get("ProgramFiles") or "C:/Program Files")
        for root in (pf86 / "GOG Galaxy" / "Games", pf / "Epic Games", Path("C:/GOG Games")):
            for d in _subdirs(root):
                add(d)
    for k in found:
        found[k] = list(dict.fromkeys(found[k]))
    return found


def install_dir_for(db: dict, name: str, installed: dict = None) -> str:
    """Cartella d'installazione di un gioco del database, se è installato su questo PC."""
    g = db.get("games", {}).get(name)
    if not g:
        return ""
    installed = installed if installed is not None else installed_games()
    for d in g.get("d", []):
        if installed.get(d.lower()):
            return installed[d.lower()][0]
    return ""


def store_cloud(db: dict, name: str) -> list:
    """Store che sincronizzano già i salvataggi di questo gioco, es. ["Steam", "GOG"]."""
    g = db.get("games", {}).get(name) or {}
    return [STORE_NAMES.get(c, c) for c in g.get("c", [])]


def _subdirs(folder):
    try:
        return [d for d in Path(folder).iterdir() if d.is_dir()]
    except OSError:
        return []


_MAGIC = re.compile(r"[*?\[]")


def _ci(text: str) -> str:
    """Pattern glob che ignora maiuscole/minuscole: 'Save' -> '[sS][aA][vV][eE]'.
    Le parti già glob ([...], *, ?) e i segnaposto <...> restano come sono."""
    out, i = [], 0
    while i < len(text):
        c = text[i]
        if c == "[":
            j = text.find("]", i + 2)
            if j != -1:
                out.append(text[i:j + 1])
                i = j + 1
                continue
        if c == "<":
            j = text.find(">", i)
            if j != -1:
                out.append(text[i:j + 1])
                i = j + 1
                continue
        out.append(f"[{c.lower()}{c.upper()}]" if c.isalpha() and c.lower() != c.upper() else c)
        i += 1
    return "".join(out)


def _expand(path: str, ph: dict, installs: list, ci: bool = False) -> list:
    """Percorso del manifest -> pattern glob concreti per questo PC.
    ci=True (giochi Windows dentro un prefisso Proton/Wine): Windows non distingue maiuscole e
    minuscole, quindi il database può dire "eldenring" dove la cartella vera è "EldenRing".
    Solo la parte che viene dal database diventa insensibile, la radice nota resta esatta."""
    if "<storeUserId>" in path:
        path = path.replace("<storeUserId>", "*")
    if "<game>" in path or "<base>" in path or "<root>" in path:
        out = []
        for root, base, game in installs:
            sub = {"<base>": glob.escape(base), "<root>": glob.escape(root),
                   "<game>": glob.escape(game)}
            lead = next((k for k in sub if path.startswith(k)), None)
            rest = path[len(lead):] if lead else path
            for k, v in sub.items():
                rest = rest.replace(k, v)
            expanded = (sub[lead] if lead else "") + (_ci(rest) if ci else rest)
            out += _expand(expanded, ph, [], ci=False)
        return out
    lead = next((k for k in ph if path.startswith(k)), None)
    if ci and lead:
        path = lead + _ci(path[len(lead):])
    for k, v in ph.items():
        if k in path:
            path = path.replace(k, glob.escape(str(v).replace("\\", "/")))
    if "<" in path and ">" in path and re.search(r"<\w+>", path):
        return []  # segnaposto non supportato su questo sistema
    return [path]


def _static_prefix(pattern: str) -> str:
    parts = pattern.split("/")
    keep = []
    for part in parts:
        if _MAGIC.search(part):
            break
        keep.append(part)
    return "/".join(keep)


def _match(g, ph, installs, system, ci=False) -> list:
    """Percorsi di salvataggio di un gioco che esistono davvero: [(cartella, pattern|None, mtime)]"""
    matches = []
    for path, oses in g["f"]:
        if oses and system not in oses:
            continue
        if ("<base>" in path or "<root>" in path or "<game>" in path) and not installs:
            continue
        for pattern in _expand(path, ph, installs, ci=ci):
            prefix = _static_prefix(pattern)
            if prefix != pattern and not os.path.isdir(prefix):
                continue
            for m in (glob.glob(pattern) if prefix != pattern else
                      ([pattern] if os.path.exists(pattern) else [])):
                m = os.path.normpath(m)
                if os.path.isdir(m):
                    matches.append((m, None, _tree_mtime(m)))
                else:
                    # se il manifest dice *.dat teniamo *.dat: così i nuovi slot entrano da soli
                    last = pattern.rsplit("/", 1)[-1]
                    fname = last if _MAGIC.search(last) else os.path.basename(m)
                    matches.append((os.path.dirname(m), fname, _mtime(m)))
    too_broad = {os.path.normcase(os.path.normpath(str(v)))
                 for v in ph.values() if isinstance(v, Path)}
    return [m for m in matches if os.path.normcase(m[0]) not in too_broad]


def detect(db: dict, progress=lambda done, total: None, reg_store=None) -> list:
    """Giochi con salvataggi su questo PC. Su Linux/Steam Deck guarda anche dentro i prefissi
    Proton (steamapps/compatdata/<id Steam>/pfx) e Heroic, usando i percorsi Windows."""
    games = db.get("games", {})
    ph = placeholders()
    libs = steam_libraries()
    installed = {}
    for lib in libs:
        for d in _subdirs(lib / "steamapps" / "common"):
            installed.setdefault(d.name.lower(), []).append(
                (str(lib).replace("\\", "/"), str(d).replace("\\", "/"), d.name))

    # Linux / Steam Deck: prefissi Proton per ID Steam, cartelle d'installazione per ID
    prefixes, app_dirs, heroic = {}, {}, {}
    if SYSTEM != "windows":
        for lib in libs:
            for d in _subdirs(lib / "steamapps" / "compatdata"):
                if d.name.isdigit() and (d / "pfx" / "drive_c").is_dir():
                    prefixes.setdefault(int(d.name), (lib, d / "pfx"))
            for appid, folder in steam_app_manifests(lib).items():
                app_dirs.setdefault(appid, (lib, folder))
        heroic = heroic_prefixes()

    found = []
    if reg_store is None and SYSTEM == "windows":
        reg_store = registry.default_store()
    try:
        all_installs = installed_games()
    except Exception:
        all_installs = {}
    total = len(games)
    for i, (name, g) in enumerate(games.items()):
        if i % 500 == 0:
            progress(i, total)
        installs = [t for d in g.get("d", []) for t in installed.get(d.lower(), [])]
        matches = _match(g, ph, installs, SYSTEM)
        install_dir = install_dir_for(db, name, all_installs)
        proton = False
        ids = [x for x in [g.get("s")] + g.get("sx", []) if x]
        for appid in ids:
            if appid in prefixes:
                lib, pfx = prefixes[appid]
                inst = []
                if appid in app_dirs and app_dirs[appid][1].is_dir():
                    folder = app_dirs[appid][1]
                    inst = [(str(lib), str(folder), folder.name)]
                    install_dir = install_dir or str(folder)
                m = _match(g, proton_placeholders(pfx), inst, "windows", ci=True)
                matches += m
                proton = proton or bool(m)
        if heroic and _norm_title(name) in heroic:
            m = _match(g, proton_placeholders(heroic[_norm_title(name)]), [], "windows",
                       ci=True)
            matches += m
            proton = proton or bool(m)
        reg = [k for k in g.get("r", []) if reg_store is not None and reg_store.exists(k)]
        if not matches and reg:
            # salva solo nel registro di Windows
            found.append(Detected(name=name, folder="", patterns=["*"], steam_id=g.get("s", 0),
                                  mtime=max(reg_store.mtime(k) for k in reg),
                                  install_dir=install_dir, store_cloud=store_cloud(db, name),
                                  registry=reg,
                                  process=", ".join(x for x, oses in g.get("x", []) if
                                                    x.lower().endswith(".exe"))))
            continue
        if not matches:
            continue
        best = max(matches, key=lambda m: m[2])
        folder = best[0]
        same = [m for m in matches if os.path.normcase(m[0]) == os.path.normcase(folder)]
        patterns = ["*"] if any(m[1] is None for m in same) else sorted({m[1] for m in same})
        others = sorted({m[0] for m in matches} - {folder})
        exe_system = "windows" if (SYSTEM == "windows" or proton) else SYSTEM
        exes = [x for x, oses in g.get("x", []) if not oses or exe_system in oses]
        if exe_system == "windows":
            exes = [x for x in exes if x.lower().endswith(".exe")]
        found.append(Detected(name=name, folder=folder, patterns=patterns,
                              process=", ".join(dict.fromkeys(exes)), steam_id=g.get("s", 0),
                              mtime=best[2], files=_count(folder, patterns),
                              extra_folders=others, install_dir=install_dir,
                              store_cloud=store_cloud(db, name), registry=reg))
    progress(total, total)
    found.sort(key=lambda d: d.mtime, reverse=True)
    return found


def _mtime(p) -> float:
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0.0


def _tree_mtime(folder, limit=2000) -> float:
    best = _mtime(folder)
    n = 0
    for dirpath, _, files in os.walk(folder):
        for f in files:
            best = max(best, _mtime(os.path.join(dirpath, f)))
            n += 1
            if n >= limit:
                return best
    return best


def _count(folder, patterns, limit=10000) -> int:
    import fnmatch
    n = 0
    for _, _, files in os.walk(folder):
        n += sum(1 for f in files if patterns == ["*"] or
                 any(fnmatch.fnmatch(f, p) for p in patterns))
        if n >= limit:
            break
    return n
