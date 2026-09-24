"""Offline node index: Manager databases joined with /object_info, searchable without a network.

Plain functions plus the ``@function_tool`` wrappers the agent calls.
No ``server`` / ``nodes`` / ``execution`` / ``folder_paths`` imports at module scope.
"""
import asyncio
import bisect
import configparser
import json
import math
import os
import re
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, NamedTuple, Optional, Set, Tuple

from agents.tool import function_tool

from ..utils.comfy_gateway import ComfyGateway, ComfyUnreachable
from ..utils.logger import log
from ._common import tool_json

DEFAULT_CHANNEL = "https://raw.githubusercontent.com/ltdrdata/ComfyUI-Manager/main"
DB_FILES = ("custom-node-list.json", "extension-node-map.json", "github-stats.json")
REQUIRED_DB_FILES = DB_FILES[:2]       # github-stats.json is optional (stars become None)
CACHE_TTL = 7 * 24 * 3600              # our own fetched copy
OBJECT_INFO_TTL = 120                  # seconds; in-process
FETCH_TIMEOUT = 20
CORE_REPO = "https://github.com/comfyanonymous/ComfyUI"
CORE_TITLE = "ComfyUI core"
COMBO_CAP = 8
MIN_TOKEN = 2

FIELD_WEIGHTS = {"cls": 10.0, "display": 8.0, "pack_title": 4.0, "category": 3.0, "desc": 1.5, "pack_desc": 1.0}
EXACT_BONUS = 100.0
ALL_TOKENS_FACTOR = 1.5
INSTALLED_BONUS = 2.0
DEPRECATED_FACTOR = 0.5


class NodeDatabaseUnavailable(Exception):
    """No Manager files, no usable cache, and the fetch failed."""


class Databases(NamedTuple):
    custom_nodes: List[dict]
    node_map: Dict[str, Tuple[List[str], dict]]
    stats: Dict[str, dict]
    source: str                 # 'manager' | 'cache' | 'fetched' | 'stale'
    loaded_at: float
    paths: List[str]


@dataclass(frozen=True)
class NodePack:
    reference: str
    title: str
    author: str
    description: str
    stars: Optional[int]
    last_update: Optional[str]
    cnr_id: Optional[str]
    install_type: str
    files: Tuple[str, ...]
    pip: Tuple[str, ...]


@dataclass
class NodeRecord:
    class_name: str
    display_name: str
    description: str
    category: str
    installed: bool
    pack: Optional[NodePack]
    python_module: Optional[str]
    inputs: List[str]
    outputs: List[str]
    search_aliases: List[str]
    other_packs: List[str]
    deprecated: bool = False
    # display_name/description/category/inputs/outputs come from /object_info when installed;
    # for uninstalled nodes display_name = class_name and description = "" (pack text is searched separately)
    tokens: Dict[str, FrozenSet[str]] = field(default_factory=dict, repr=False, compare=False)


# -- sources and cache ------------------------------------------------------------


def _folder_paths():
    """The ComfyUI folder_paths module, or None outside the ComfyUI process."""
    try:
        import folder_paths  # only importable inside the ComfyUI process
        return folder_paths
    except Exception:
        return None


def _user_directory() -> Optional[Path]:
    fp = _folder_paths()
    if fp is None:
        return None
    try:
        return Path(fp.get_user_directory())
    except Exception:
        return None


def manager_db_candidates() -> Dict[str, List[Path]]:
    """basename -> existing candidate files. COPILOT_NODE_DB_DIR wins; otherwise every
    <custom_nodes dir>/ComfyUI-Manager/<file> and <user_dir>/__manager/cache/*_<file>."""
    override = os.getenv("COPILOT_NODE_DB_DIR")
    if override:
        base = Path(override)
        return {name: [base / name] for name in DB_FILES if (base / name).is_file()}
    candidates: Dict[str, List[Path]] = {name: [] for name in DB_FILES}
    fp = _folder_paths()
    if fp is None:
        return {name: paths for name, paths in candidates.items() if paths}
    roots: List[Path] = []
    try:
        roots = [Path(p) for p in fp.get_folder_paths("custom_nodes")]
    except Exception:
        pass
    for root in roots:
        for name in DB_FILES:
            path = root / "ComfyUI-Manager" / name
            if path.is_file():
                candidates[name].append(path)
    user_dir = _user_directory()
    if user_dir is not None:
        cache = user_dir / "__manager" / "cache"
        if cache.is_dir():
            for name in DB_FILES:
                candidates[name].extend(p for p in cache.glob(f"*_{name}") if p.is_file())
    return {name: paths for name, paths in candidates.items() if paths}


def _pick_largest(paths: List[Path]) -> Path:
    # The default channel is a superset of the node_db/<channel> subsets that share the basename.
    def key(p: Path):
        st = p.stat()
        return (st.st_size, st.st_mtime)
    return max(paths, key=key)


def chosen_manager_files() -> Dict[str, Path]:
    """basename -> the one Manager file to read (largest, ties by mtime)."""
    return {name: _pick_largest(paths) for name, paths in manager_db_candidates().items()}


def copilot_cache_dir() -> Path:
    override = os.getenv("COPILOT_NODE_INDEX_HOME")
    if override:
        return Path(override)
    user_dir = _user_directory()
    return (user_dir or Path.cwd()) / "copilot-node-index"


def channel_url() -> str:
    """user/__manager/config.ini [default] channel_url when readable, else DEFAULT_CHANNEL."""
    user_dir = _user_directory()
    if user_dir is None:
        return DEFAULT_CHANNEL
    config = user_dir / "__manager" / "config.ini"
    try:
        parser = configparser.ConfigParser()
        parser.read(config, encoding="utf-8")
        url = parser.get("default", "channel_url", fallback="").strip()
    except Exception:
        return DEFAULT_CHANNEL
    return url.rstrip("/") or DEFAULT_CHANNEL


def _default_fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as response:
        return response.read()


def _read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)


def _databases_from(files: Dict[str, Path], source: str, now: float) -> Databases:
    custom = _read_json(files["custom-node-list.json"])
    node_map_raw = _read_json(files["extension-node-map.json"])
    stats = _read_json(files["github-stats.json"]) if "github-stats.json" in files else {}
    custom_nodes = custom.get("custom_nodes", []) if isinstance(custom, dict) else list(custom or [])
    node_map: Dict[str, Tuple[List[str], dict]] = {}
    for url, entry in (node_map_raw or {}).items():
        if isinstance(entry, (list, tuple)) and entry:
            names = [n for n in (entry[0] or []) if isinstance(n, str)]
            meta = entry[1] if len(entry) > 1 and isinstance(entry[1], dict) else {}
            node_map[url] = (names, meta)
    return Databases(custom_nodes, node_map, stats if isinstance(stats, dict) else {}, source, now,
                     [str(files[name]) for name in DB_FILES if name in files])


def _cache_files() -> Dict[str, Path]:
    cache = copilot_cache_dir()
    return {name: cache / name for name in DB_FILES if (cache / name).is_file()}


def load_databases(*, fetch: Optional[Callable[[str], bytes]] = None,
                   now: Callable[[], float] = time.time) -> Databases:
    """Manager files on disk, else a fresh Copilot cache, else fetch (falling back to a stale cache)."""
    moment = now()
    manager = chosen_manager_files()
    if all(name in manager for name in REQUIRED_DB_FILES):
        return _databases_from(manager, "manager", moment)
    cached = _cache_files()
    have_cache = all(name in cached for name in REQUIRED_DB_FILES)
    if have_cache:
        age = moment - min(cached[name].stat().st_mtime for name in REQUIRED_DB_FILES)
        if age < CACHE_TTL:
            return _databases_from(cached, "cache", moment)
    fetch = fetch or _default_fetch
    base = channel_url()
    fetched: Dict[str, Path] = {}
    try:
        for name in DB_FILES:
            try:
                data = fetch(f"{base}/{name}")
            except Exception:
                if name in REQUIRED_DB_FILES:
                    raise
                log.warning(f"node index: {name} not fetched from {base}; stars unavailable")
                continue
            json.loads(data.decode("utf-8"))            # refuse to cache a non-JSON reply
            path = copilot_cache_dir() / name
            _write_atomic(path, data)
            fetched[name] = path
    except Exception as e:
        log.warning(f"node index: fetch from {base} failed: {e!r}")
        if have_cache:
            return _databases_from(cached, "stale", moment)
        raise NodeDatabaseUnavailable(f"no ComfyUI-Manager database on disk and fetch from {base} failed: {e}") from e
    return _databases_from(fetched, "fetched", moment)


# -- text --------------------------------------------------------------------------

_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+\d*|[A-Z]+\d*|\d+")   # trailing digits stay: v2, sd3
_SPLIT = re.compile(r"[^0-9A-Za-z]+")


def tokenize(text: str) -> List[str]:
    """Lower-cased tokens split on non-alphanumerics and CamelCase; tokens shorter than MIN_TOKEN dropped."""
    tokens: List[str] = []
    for chunk in _SPLIT.split(text or ""):
        if not chunk:
            continue
        for part in _CAMEL.findall(chunk):
            if len(part) >= MIN_TOKEN:
                tokens.append(part.lower())
    return tokens


def field_tokens(record: NodeRecord) -> Dict[str, FrozenSet[str]]:
    pack = record.pack
    return {
        "cls": frozenset(tokenize(record.class_name)),
        "display": frozenset(tokenize(record.display_name) + [t for a in record.search_aliases for t in tokenize(a)]),
        "category": frozenset(tokenize(record.category)),
        "desc": frozenset(tokenize(record.description)),
        "pack_title": frozenset(tokenize(pack.title) if pack else ()),
        "pack_desc": frozenset(tokenize(pack.description) if pack else ()),
    }


# -- build -------------------------------------------------------------------------


def _last_segment(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1] if url else ""


def _pack_from_entry(entry: dict, stats: Dict[str, dict]) -> NodePack:
    reference = str(entry.get("reference") or "")
    files = tuple(str(f) for f in entry.get("files") or [])
    stat = stats.get(reference) or (stats.get(files[0]) if files else None) or {}
    stars = stat.get("stars")
    return NodePack(
        reference=reference, title=str(entry.get("title") or _last_segment(reference) or "unknown"),
        author=str(entry.get("author") or ""), description=str(entry.get("description") or ""),
        stars=int(stars) if isinstance(stars, (int, float)) else None, last_update=stat.get("last_update"),
        cnr_id=entry.get("id"), install_type=str(entry.get("install_type") or ""), files=files,
        pip=tuple(str(p) for p in entry.get("pip") or []),
    )


def _pack_from_meta(url: str, meta: dict, stats: Dict[str, dict]) -> NodePack:
    stat = stats.get(url) or {}
    stars = stat.get("stars")
    title = meta.get("title_aux") or meta.get("title") or _last_segment(url) or url
    return NodePack(reference=url, title=str(title), author=str(meta.get("author") or ""),
                    description=str(meta.get("description") or ""),
                    stars=int(stars) if isinstance(stars, (int, float)) else None,
                    last_update=stat.get("last_update"), cnr_id=None, install_type="", files=(url,), pip=())


def _pack_from_folder(folder: str) -> NodePack:
    return NodePack(reference="", title=folder, author="", description="", stars=None, last_update=None,
                    cnr_id=None, install_type="", files=(), pip=())


CORE_PACK = NodePack(reference=CORE_REPO, title=CORE_TITLE, author="comfyanonymous", description="Nodes shipped with ComfyUI.",
                     stars=None, last_update=None, cnr_id=None, install_type="", files=(CORE_REPO,), pip=())


def _is_core_module(module: Optional[str]) -> bool:
    return module == "nodes" or bool(module and module.startswith(("comfy_extras.", "comfy_api_nodes.")))


def _custom_folder(module: Optional[str]) -> Optional[str]:
    if module and module.startswith("custom_nodes."):
        parts = module.split(".")
        return parts[1] if len(parts) > 1 and parts[1] else None
    return None


def _record_from_info(class_name: str, info: dict, existing: Optional[NodeRecord]) -> NodeRecord:
    inputs_spec = info.get("input") or {}
    inputs = [str(k) for section in ("required", "optional") for k in (inputs_spec.get(section) or {})]
    outputs = info.get("output_name") or info.get("output") or []
    record = existing or NodeRecord(class_name, class_name, "", "", False, None, None, [], [], [], [])
    record.installed = True
    record.display_name = str(info.get("display_name") or class_name)
    record.description = str(info.get("description") or "")
    record.category = str(info.get("category") or "")
    record.python_module = info.get("python_module")
    record.inputs = inputs
    record.outputs = [str(o) for o in outputs]
    record.search_aliases = [str(a) for a in info.get("search_aliases") or []]
    record.deprecated = bool(info.get("deprecated"))
    return record


def build_index(db: Databases, object_info: Dict[str, Any]) -> "NodeIndex":
    """Join the Manager databases with the /object_info snapshot (may be empty) into a NodeIndex."""
    packs: Dict[str, NodePack] = {}
    patterns: List[Tuple[re.Pattern, NodePack]] = []
    for entry in db.custom_nodes:
        if not isinstance(entry, dict):
            continue
        pack = _pack_from_entry(entry, db.stats)
        if pack.reference:
            packs.setdefault(pack.reference, pack)
        if pack.files and pack.files[0] != pack.reference:
            packs.setdefault(pack.files[0], pack)
        pattern = entry.get("nodename_pattern")
        if isinstance(pattern, str) and pattern:
            try:
                patterns.append((re.compile(pattern), pack))
            except re.error:
                pass

    # Candidate packs per class name, in node-map order.
    candidates: Dict[str, List[NodePack]] = {}
    for url, (names, meta) in db.node_map.items():
        pack = packs.get(url)
        if pack is None:
            pack = _pack_from_meta(url, meta, db.stats)
            packs[url] = pack
        pattern = meta.get("nodename_pattern")
        if isinstance(pattern, str) and pattern and not any(p is pack for _, p in patterns):
            try:
                patterns.append((re.compile(pattern), pack))
            except re.error:
                pass
        for name in names:
            bucket = candidates.setdefault(name, [])
            if not any(p is pack for p in bucket):
                bucket.append(pack)

    # Which packs are installed: the custom_nodes folder of any installed class matches a reference's last segment.
    by_folder: Dict[str, NodePack] = {}
    for pack in packs.values():
        for url in (pack.reference,) + pack.files:
            segment = _last_segment(url).lower()
            if segment:
                by_folder.setdefault(segment, pack)
                if segment.endswith(".git"):
                    by_folder.setdefault(segment[:-4], pack)
    installed_packs: Set[int] = set()
    for info in object_info.values():
        folder = _custom_folder((info or {}).get("python_module")) if isinstance(info, dict) else None
        if folder and folder.lower() in by_folder:
            installed_packs.add(id(by_folder[folder.lower()]))

    def winner(bucket: List[NodePack]) -> NodePack:
        installed = [p for p in bucket if id(p) in installed_packs]
        if installed:
            return installed[0]
        best = bucket[0]
        for pack in bucket[1:]:
            if (pack.stars or 0) > (best.stars or 0):
                best = pack
        return best

    records: Dict[str, NodeRecord] = {}
    for name, bucket in candidates.items():
        pack = winner(bucket)
        others = [p.reference or p.title for p in bucket if p is not pack]
        records[name] = NodeRecord(name, name, "", "", False, pack, None, [], [], [], others)

    # Installed classes: enrich mapped ones, attribute unmapped ones.
    for class_name, info in object_info.items():
        if not isinstance(info, dict):
            continue
        record = _record_from_info(class_name, info, records.get(class_name))
        records[class_name] = record
        if record.pack is not None:
            continue
        module = record.python_module
        if _is_core_module(module):
            record.pack = CORE_PACK
            continue
        folder = _custom_folder(module)
        if folder and folder.lower() in by_folder:
            record.pack = by_folder[folder.lower()]
            continue
        for pattern, pack in patterns:
            if pattern.search(class_name):
                record.pack = pack
                break
        else:
            record.pack = _pack_from_folder(folder) if folder else None
    packs.setdefault(CORE_REPO, CORE_PACK)
    return NodeIndex(records, packs, object_info, db.source)


# -- search --------------------------------------------------------------------------


def _field_match(tokens: FrozenSet[str], query: str) -> float:
    """1 for an exact token, 0.5 for a prefix match (query of 3+ chars), else 0."""
    if query in tokens:
        return 1.0
    if len(query) >= 3 and any(t.startswith(query) for t in tokens):
        return 0.5
    return 0.0


def score(record: NodeRecord, tokens: List[str], raw_query: str) -> float:
    fields = record.tokens or field_tokens(record)
    total = 0.0
    matched = 0
    for token in tokens:
        best = max((weight * _field_match(fields[name], token) for name, weight in FIELD_WEIGHTS.items()), default=0.0)
        if best > 0:
            matched += 1
            total += best
    if total <= 0:
        return 0.0
    query = raw_query.strip().lower()
    if query and query in (record.class_name.lower(), record.display_name.lower()):
        total += EXACT_BONUS
    if matched == len(tokens):
        total *= ALL_TOKENS_FACTOR
    if record.installed:
        total += INSTALLED_BONUS
    stars = record.pack.stars if record.pack and record.pack.stars else 0
    total += math.log10(stars + 1)
    if record.deprecated:
        total *= DEPRECATED_FACTOR
    return total


class NodeIndex:
    def __init__(self, records: Dict[str, NodeRecord], packs: Dict[str, NodePack],
                 object_info: Optional[Dict[str, Any]] = None, db_source: str = ""):
        self.records = records
        self.packs = packs
        self.object_info = object_info or {}
        self.db_source = db_source
        self.built_at = time.time()
        self._lower = {name.lower(): name for name in records}
        self._inverted: Dict[str, Set[str]] = {}
        for name, record in records.items():
            record.tokens = field_tokens(record)
            for tokens in record.tokens.values():
                for token in tokens:
                    self._inverted.setdefault(token, set()).add(name)
        self._sorted_tokens = sorted(self._inverted)

    @property
    def installed_count(self) -> int:
        return sum(1 for r in self.records.values() if r.installed)

    def _candidates(self, token: str) -> Set[str]:
        found = set(self._inverted.get(token, ()))
        if len(token) >= 3:
            start = bisect.bisect_left(self._sorted_tokens, token)
            for i in range(start, len(self._sorted_tokens)):
                indexed = self._sorted_tokens[i]
                if not indexed.startswith(token):
                    break
                found.update(self._inverted[indexed])
        return found

    def search(self, query: str, limit: int = 10, installed_only: bool = False) -> List[Tuple[NodeRecord, float]]:
        tokens = tokenize(query)
        if not tokens:
            return []
        names: Set[str] = set()
        for token in tokens:
            names.update(self._candidates(token))
        scored = []
        for name in names:
            record = self.records[name]
            if installed_only and not record.installed:
                continue
            value = score(record, tokens, query)
            if value > 0:
                scored.append((record, value))
        scored.sort(key=lambda item: (-item[1], item[0].class_name))
        return scored[:limit]

    def get(self, class_name: str) -> Optional[NodeRecord]:
        record = self.records.get(class_name)
        if record is None and class_name:
            record = self.records.get(self._lower.get(class_name.lower(), ""))
        return record

    def suggest(self, class_name: str, limit: int = 5) -> List[str]:
        needle = (class_name or "").strip().lower()
        if not needle:
            return []
        hits = [(0 if name.lower().startswith(needle) else 1, len(name), name)
                for name in self.records if needle in name.lower()]
        if not hits:
            tokens = tokenize(class_name)
            counts: Dict[str, int] = {}
            for token in tokens:
                for name in self._candidates(token):
                    counts[name] = counts.get(name, 0) + 1
            hits = [(-count, len(name), name) for name, count in counts.items()]
        hits.sort()
        return [name for _, _, name in hits[:limit]]


# -- singleton -----------------------------------------------------------------------

_index: Optional[NodeIndex] = None
_lock: Optional[asyncio.Lock] = None
_snapshot: Optional[Dict[str, Any]] = None
_snapshot_at: float = 0.0
_signature: Optional[Tuple] = None
_first_build_logged: bool = False


def _db_signature() -> Tuple:
    files = chosen_manager_files()
    if not all(name in files for name in REQUIRED_DB_FILES):
        files = _cache_files()
    parts = []
    for name in DB_FILES:
        path = files.get(name)
        if path is not None:
            try:
                st = path.stat()
                parts.append((str(path), st.st_mtime_ns, st.st_size))
            except OSError:
                pass
    return tuple(parts)


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def invalidate_index() -> None:
    global _index, _lock, _snapshot, _snapshot_at, _signature, _first_build_logged
    _index = None
    _lock = None
    _snapshot = None
    _snapshot_at = 0.0
    _signature = None
    _first_build_logged = False


async def get_index(gateway: Optional[ComfyGateway] = None, *, force: bool = False) -> NodeIndex:
    """Cached index; rebuilt when forced, when the DB files change, or when the object_info snapshot expires."""
    global _index, _snapshot, _snapshot_at, _signature, _first_build_logged
    async with _get_lock():
        now = time.time()
        signature = await asyncio.to_thread(_db_signature)
        info_changed = False
        if force or _snapshot is None or now - _snapshot_at > OBJECT_INFO_TTL:
            info: Optional[Dict[str, Any]] = None
            try:
                info = await (gateway or ComfyGateway()).get_object_info()
            except ComfyUnreachable as e:
                log.warning(f"node index: object_info unavailable: {e}")
            if info:
                info_changed = info != _snapshot
                _snapshot = info
            elif _snapshot is None:
                log.warning("node index: no /object_info snapshot; building with installed = none")
                _snapshot = {}
                info_changed = True
            _snapshot_at = now
        if _index is None or force or info_changed or signature != _signature:
            snapshot = _snapshot or {}
            started = time.monotonic()
            _index = await asyncio.to_thread(lambda: build_index(load_databases(), snapshot))
            _signature = signature
            elapsed = time.monotonic() - started
            log.info(f"node index built: {len(_index.records)} records, {_index.installed_count} installed, "
                     f"source={_index.db_source}, {elapsed:.2f}s")
            if not _first_build_logged:
                _first_build_logged = True
                log.info(f"node index: first build, source={_index.db_source}, "
                         f"records={len(_index.records)}, {elapsed:.2f}s")
        return _index


# -- row shapers (the UI contract) ---------------------------------------------------


def _repo_url(pack: Optional[NodePack]) -> str:
    return pack.reference if pack and pack.reference.startswith("http") else ""


def node_row(record: NodeRecord) -> dict:
    """The seven ui/src/types/types.ts Node fields first; the extras are ignored by NodeSearch.tsx."""
    pack = record.pack
    return {
        "name": record.class_name,
        "description": record.description or (pack.description if pack else "") or "",
        "image": "",
        "github_url": _repo_url(pack),
        "github_stars": (pack.stars if pack and pack.stars else 0),
        "from_index": 0,
        "to_index": 0,
        "installed": bool(record.installed),
        "display_name": record.display_name,
        "category": record.category,
        "pack_title": pack.title if pack else "",
    }


def _unknown_row(name: str) -> dict:
    return {"name": name, "description": "", "image": "", "github_url": "", "github_stars": 0,
            "from_index": 0, "to_index": 0, "installed": False, "display_name": name, "category": "", "pack_title": ""}


def install_guide_row(record: Optional[NodeRecord], name: str) -> dict:
    return {"name": record.class_name if record else name, "repository_url": _repo_url(record.pack) if record else ""}


# -- plain functions (route and tools share these) -----------------------------------


def _trim_inputs(info: dict) -> Dict[str, Dict[str, Any]]:
    """{required: {name: spec}, optional: {...}}; combo lists capped at COMBO_CAP + '…(+N)'."""
    trimmed: Dict[str, Dict[str, Any]] = {}
    for section in ("required", "optional"):
        specs = (info.get("input") or {}).get(section) or {}
        if not specs:
            continue
        out: Dict[str, Any] = {}
        for name, spec in specs.items():
            kind = spec[0] if isinstance(spec, (list, tuple)) and spec else spec
            opts = spec[1] if isinstance(spec, (list, tuple)) and len(spec) > 1 and isinstance(spec[1], dict) else {}
            if isinstance(kind, list):
                kind = kind[:COMBO_CAP] + ([f"…(+{len(kind) - COMBO_CAP})"] if len(kind) > COMBO_CAP else [])
            small = {k: opts[k] for k in ("default", "min", "max", "step", "tooltip") if k in opts}
            out[name] = [kind, small] if small else kind
        trimmed[section] = out
    return trimmed


def _pack_dict(pack: Optional[NodePack]) -> Optional[dict]:
    if pack is None:
        return None
    return {"title": pack.title, "author": pack.author, "reference": pack.reference, "stars": pack.stars,
            "cnr_id": pack.cnr_id, "install_type": pack.install_type, "pip": list(pack.pip)}


async def search_nodes(query: str, limit: int = 8) -> dict:
    query = (query or "").strip()
    if not query:
        return {"error": "query is empty"}
    try:
        index = await get_index()
    except NodeDatabaseUnavailable as e:
        return {"error": str(e)}
    hits = index.search(query, limit=max(1, int(limit or 8)))
    rows = [node_row(record) for record, _ in hits]
    installed = sum(1 for record, _ in hits if record.installed)
    return {"answer": f"{len(rows)} nodes matched '{query}' ({installed} installed)",
            "data": rows, "ext": [{"type": "node", "data": rows}]}


async def node_info(node_class: str) -> dict:
    name = (node_class or "").strip()
    if not name:
        return {"error": "node_class is empty"}
    try:
        index = await get_index()
    except NodeDatabaseUnavailable as e:
        return {"error": str(e)}
    record = index.get(name)
    if record is None:
        return {"error": f"node '{name}' not found", "suggestions": index.suggest(name)}
    node: Dict[str, Any] = {"class_name": record.class_name, "display_name": record.display_name,
                            "description": record.description, "category": record.category,
                            "installed": record.installed, "python_module": record.python_module}
    result: Dict[str, Any] = {"node": node, "pack": _pack_dict(record.pack), "other_packs": list(record.other_packs),
                              "ext": [{"type": "node", "data": [node_row(record)]}]}
    if record.installed:
        raw = index.object_info.get(record.class_name) or {}
        node["inputs"] = _trim_inputs(raw)
        node["outputs"] = list(record.outputs)
    else:
        pack = record.pack
        where = f"pack {pack.title} at {pack.reference}" if pack and pack.reference else \
            (f"pack {pack.title}" if pack else "unknown pack")
        result["install_hint"] = f"not installed; {where}"
    return result


async def node_rows_for_types(node_types: List[str]) -> List[dict]:
    """One node_row per requested type, in order; unknown types get an empty row."""
    index = await get_index()
    rows = []
    for node_type in node_types or []:
        record = index.get(str(node_type))
        rows.append(node_row(record) if record else _unknown_row(str(node_type)))
    return rows


# -- agent tools -----------------------------------------------------------------------


@function_tool
async def search_node(query: str, limit: int = 8) -> str:
    """Find ComfyUI nodes by what they do. `query`: 2-4 specific English words (e.g. "tiled vae decode",
    "depth anything preprocessor"); returns installed and installable nodes with repo URL and stars."""
    return tool_json(await search_nodes(query, limit))


@function_tool
async def get_node_info(node_class: str) -> str:
    """Details for one node class: inputs/outputs when installed, otherwise which pack provides it and its repo URL."""
    return tool_json(await node_info(node_class))


@function_tool
async def get_node_info_by_types(node_types: List[str]) -> str:
    """For a list of class names, report which are installed and where the missing ones come from."""
    rows = await node_rows_for_types(node_types)
    missing = [row["name"] for row in rows if not row.get("installed")]
    if rows:
        answer = f"{len(rows) - len(missing)}/{len(rows)} requested node types are installed"
    else:
        answer = "no node types given"
    return tool_json({"answer": answer, "data": rows, "missing": missing,
                      "ext": [{"type": "node", "data": rows}]})
