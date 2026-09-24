# Design: Phase 2 — offline node search, node info, install guide

Produced 2026-09-24 by the planning agent, reviewed by the orchestrator.
Conventions follow `phase-0-1-debugger.md`: plain functions plus thin
`@function_tool` wrappers in `backend/tools/`, no `server`/`nodes`/`execution`/
`folder_paths` imports at module scope, unittest tests with `FakeComfy`, PRs do
not touch `CHANGELOG.md` or the version.

## Findings that shape the design

1. **Manager data on this machine** (`ComfyUI/custom_nodes/ComfyUI-Manager/` and
   `ComfyUI/user/__manager/cache/`): 5,942 packs, 5,638 node-map entries, 41,296
   distinct class names. `extension-node-map` keys match `custom-node-list.reference`
   for 5,589 and `files[0]` for 5,627 (join on both); `github-stats` keys match
   `reference` for 5,887. **1,818 class names appear in more than one pack**, so a
   dedupe rule is needed. Meta in the node map is usually only `title_aux`; the
   Manager DB has no per-node description or category, so search text for
   uninstalled nodes is class-name tokens plus pack title/description.
   `config.ini` here: `security_level = weak`, `allow_git_url_install = False`.
2. **UI shapes** (exact fields):
   - `Node` (`ui/src/types/types.ts:37-45`): `{name, description, image, github_url, github_stars, from_index, to_index}`.
     `NodeSearch.tsx` reads `ext[type='node'].data[]`, uses `node.name` for
     `installedNodes.some(n => n === name)` (so `name` must be the `/object_info`
     class key) and for `addNodeOnGraph(node.name)`, `github_stars` (number),
     `github_url` (Download link; absent -> Google search link), `description`
     (hover). `from_index`/`to_index` are junk (self-connect); set both to 0.
   - `NodeInstallGuide.tsx` reads `ext[type='node_install_guide'].data[] -> {name, repository_url}`.
     The Accept flows (`WorkflowOption.tsx:102-112`, `DownstreamSubgraphs.tsx:172-183`)
     build those rows from `batchGetNodeInfo()`, which must return
     `{success: true, data: Node[]}` and read `info.name`, `info.github_url`.
   - `batchGetNodeInfo` calls `${BASE_URL}/api/chat/get_node_info_by_types`;
     `BASE_URL` is the dead upstream host in production builds. A UI change is
     required.
3. **How `mcp_client.py` turns tool output into `ext`** (lines 405-455): it
   captures top-level `ext` only for `workflow_update`/`param_update`; everything
   else is read from `tool_output_data["text"]` parsed as JSON `{answer, data, ext}`
   (the upstream MCP envelope). Final `ext` = first truthy `tool_results[*]["ext"]`
   (lines 654-658). Local tools need a small parser change rather than
   double-encoding JSON into `"text"`.
4. **Name clash**: `workflow_rewrite_tools.get_node_info` (raw object_info) is used
   by the debug/rewrite agents; the main agent's prompt references
   `search_node`/`get_node_info`. The main agent does not hold the old one, so the
   new tools keep the upstream names; import with an alias in `mcp_client.py`.
   Do not add the index tools to the rewrite agent in this phase.
5. `/object_info` is expensive on ComfyUI (calls every `INPUT_TYPES`). Cache it
   in-process with a short TTL.

## 1. `backend/tools/node_index.py` (new)

### Sources and cache

```python
DEFAULT_CHANNEL = "https://raw.githubusercontent.com/ltdrdata/ComfyUI-Manager/main"
DB_FILES = ("custom-node-list.json", "extension-node-map.json", "github-stats.json")
CACHE_TTL = 7 * 24 * 3600          # our own fetched copy
OBJECT_INFO_TTL = 120              # seconds; in-process
CORE_REPO = "https://github.com/comfyanonymous/ComfyUI"

def manager_db_candidates() -> Dict[str, List[Path]]
    # basename -> candidate files. COPILOT_NODE_DB_DIR env (tests/standalone) wins; otherwise, importing
    # folder_paths inside the function: every <custom_nodes dir>/ComfyUI-Manager/<file> and
    # <user_dir>/__manager/cache/*_<file>. Per basename pick the largest file (the default channel is a
    # superset of the node_db/<channel> subsets that share the basename in the cache), ties by mtime.
def copilot_cache_dir() -> Path
    # COPILOT_NODE_INDEX_HOME env, else Path(folder_paths.get_user_directory()) / 'copilot-node-index'
def channel_url() -> str
    # user/__manager/config.ini [default] channel_url when readable, else DEFAULT_CHANNEL
def load_databases(*, fetch: Optional[Callable[[str], bytes]] = None, now: Callable[[], float] = time.time) -> Databases
    # 1. Manager files on disk (candidates above)            -> source 'manager'
    # 2. else Copilot cache younger than CACHE_TTL            -> source 'cache'
    # 3. else fetch(channel_url()/<file>) for the 3 files, write atomically (tmp + os.replace) -> 'fetched'
    # 4. fetch failed: stale Copilot cache if any             -> 'stale'; else raise NodeDatabaseUnavailable
    # Default fetch = urllib.request GET with a 20 s timeout, run via asyncio.to_thread by the async caller.
```

`Databases = NamedTuple(custom_nodes: List[dict], node_map: Dict[str, Tuple[List[str], dict]], stats: Dict[str, dict], source: str, loaded_at: float, paths: List[str])`.
Missing `github-stats.json` is tolerated (stars become `None`).

### Data model

```python
@dataclass(frozen=True)
class NodePack:
    reference: str; title: str; author: str; description: str
    stars: Optional[int]; last_update: Optional[str]; cnr_id: Optional[str]
    install_type: str; files: Tuple[str, ...]; pip: Tuple[str, ...]

@dataclass
class NodeRecord:
    class_name: str; display_name: str; description: str; category: str
    installed: bool; pack: Optional[NodePack]; python_module: Optional[str]
    inputs: List[str]; outputs: List[str]; search_aliases: List[str]
    other_packs: List[str]
    # display_name/description/category/inputs/outputs come from /object_info when installed;
    # for uninstalled nodes display_name = class_name and description = "" (pack text is searched separately)
```

### Build

```python
def build_index(db: Databases, object_info: Dict[str, Any]) -> "NodeIndex"
```
- Packs: one `NodePack` per `custom_nodes` entry keyed by `reference`, also
  registered under `files[0]` when it differs; stars from `stats[reference]`.
  Node-map entries whose key matches neither get a pack synthesized from
  `title_aux`/`title` in the meta (title falls back to the last URL segment).
- Records: for every `(pack_url, [class names])` in the node map, one record per
  class name. Duplicate class names: keep the pack that is installed, else the
  one with the most stars, else first seen; losers go in `record.other_packs`.
- Installed: for each key in `object_info`, mark/create a record with
  `installed=True`, `display_name`, `description`, `category`, `search_aliases`,
  input names (`input.required` + `input.optional` keys), `output_name`
  (fallback `output`), `python_module`. If the class was not in the node map:
  `python_module == "nodes"` or `comfy_extras.*` -> the core pack (`CORE_REPO`,
  title "ComfyUI core", stars None); `custom_nodes.<folder>` -> match `<folder>`
  case-insensitively against the last path segment of pack references, else
  synthesize a pack titled `<folder>` with no URL. `nodename_pattern` regexes
  are applied to unmapped installed classes as a last step, like Manager's
  `getmappings`.
- Text index: `tokenize()` lower-cases, splits on non-alphanumerics and
  CamelCase boundaries (`VAEDecodeTiled` -> `vae decode tiled`), drops tokens
  shorter than 2 chars. Per record, token sets per field: `cls`, `display`
  (plus aliases), `category`, `desc`, `pack_title`, `pack_desc`. An inverted
  index `token -> set(record ids)` plus a sorted token list for prefix lookup
  (bisect) keeps search well under 100 ms on 41k records; build runs in
  `asyncio.to_thread`.

### Search and scoring

```python
class NodeIndex:
    records: Dict[str, NodeRecord]; packs: Dict[str, NodePack]; built_at: float; db_source: str
    def search(self, query: str, limit: int = 10, installed_only: bool = False) -> List[Tuple[NodeRecord, float]]
    def get(self, class_name: str) -> Optional[NodeRecord]          # exact, then case-insensitive
    def suggest(self, class_name: str, limit: int = 5) -> List[str] # substring/prefix candidates for "not found"
def score(record: NodeRecord, tokens: List[str], raw_query: str) -> float
```
Per query token, the best match across fields counts (exact token, or prefix
match at half weight for tokens of 3+ chars): `cls` 10, `display` 8,
`pack_title` 4, `category` 3, `desc` 1.5, `pack_desc` 1. Then: whole-query
equals class/display name (case-insensitive) +100; every token matched
somewhere x1.5; `installed` +2; `+log10(stars+1)` as a stable tie-break;
`deprecated` records x0.5. Results with score 0 are dropped; ties broken by
class name. No dependencies, no embeddings.

### Singleton

```python
async def get_index(gateway: Optional[ComfyGateway] = None, *, force: bool = False) -> NodeIndex
    # module-level cache guarded by an asyncio.Lock; rebuilds when force, when the chosen DB file set/mtimes
    # changed, or when the object_info snapshot is older than OBJECT_INFO_TTL (object_info via
    # gateway.get_object_info(); on ComfyUnreachable reuse the last snapshot or build with installed = none).
def invalidate_index() -> None
```

### Row shapers (the UI contract)

```python
def node_row(record: NodeRecord) -> dict
    # {"name": class_name, "description": record.description or pack.description or "", "image": "",
    #  "github_url": pack.reference if it starts with http else "", "github_stars": pack.stars or 0,
    #  "from_index": 0, "to_index": 0, "installed": bool, "display_name", "category", "pack_title"}
    # The first seven keys are exactly ui/src/types/types.ts Node; the extras are ignored by NodeSearch.tsx.
def install_guide_row(record: Optional[NodeRecord], name: str) -> dict   # {"name", "repository_url"}
```

### Plain functions (route and tools share these)

```python
async def search_nodes(query: str, limit: int = 8) -> dict
    # {"answer": "<n> nodes matched '<query>' (<k> installed)", "data": [node_row...],
    #  "ext": [{"type": "node", "data": [node_row...]}]}    ; {"error": ...} on empty query / DB unavailable
async def node_info(node_class: str) -> dict
    # installed: {"node": {class_name, display_name, description, category, installed, python_module,
    #             "inputs": trimmed object_info input dict (combo lists capped at 8 + "…(+N)"), "outputs": [...]},
    #             "pack": {title, author, reference, stars, cnr_id, install_type, pip}, "other_packs": [...],
    #             "ext": [{"type": "node", "data": [node_row]}]}
    # not installed: same without inputs/outputs plus "install_hint": "not installed; pack <title> at <url>"
    # unknown: {"error": "...not found", "suggestions": index.suggest(name)}
async def node_rows_for_types(node_types: List[str]) -> List[dict]
    # one node_row per requested type, in order; unknown types -> {"name": t, "github_url": "", "github_stars": 0, ...}
```

### Tools (PR H)

```python
@function_tool
async def search_node(query: str, limit: int = 8) -> str
    """Find ComfyUI nodes by what they do. `query`: 2-4 specific English words (e.g. "tiled vae decode",
    "depth anything preprocessor"); returns installed and installable nodes with repo URL and stars."""
@function_tool
async def get_node_info(node_class: str) -> str
    """Details for one node class: inputs/outputs when installed, otherwise which pack provides it and its repo URL."""
@function_tool
async def get_node_info_by_types(node_types: List[str]) -> str
    """For a list of class names, report which are installed and where the missing ones come from."""
    # returns {"answer", "data": rows, "missing": [...], "ext": [{"type": "node", "data": rows}]}
```
`get_node_info_by_types` for the agent returns `ext` type `node`, not
`node_install_guide`, because `NodeInstallGuide.tsx` renders a "Loading graph
directly" button that only works with `metadata.pendingSubgraph`/`pendingWorkflow`
set by the UI's own Accept flow. The route below serves the Accept flow.

## 2. `backend/controller/node_api.py` (new) and `__init__.py` (PR H)

```python
@server.PromptServer.instance.routes.post("/api/copilot/node_info_by_types")
async def node_info_by_types(request):
    # body {"node_types": [...]} -> {"success": True, "data": node_rows_for_types(types)}; 400 on bad body;
    # {"success": False, "message": ...} on NodeDatabaseUnavailable
@server.PromptServer.instance.routes.post("/api/copilot/node_index/refresh")
async def refresh_index(request):   # get_index(force=True); returns {"success", "source", "records", "installed"}
```
Register in the root `__init__.py` next to `validate_api`. New Copilot path
rather than reusing `/api/chat/get_node_info_by_types`, because the UI must
change anyway and `/api/copilot/*` is the fork's namespace.

## 3. `backend/service/mcp_client.py` (PR H)

- Import: `from ..tools.node_index import search_node, get_node_info as node_info_tool, get_node_info_by_types`;
  `tools=[get_current_workflow, search_node, node_info_tool, get_node_info_by_types]` (line 304).
- Parser (lines 405-416): after the `workflow_update`/`param_update` check, add
  ```python
  elif "ext" in tool_output_data and tool_output_data["ext"]:   # local tools: top-level envelope
      tool_results[tool_name] = {"answer": tool_output_data.get("answer"), "data": tool_output_data.get("data"),
                                 "ext": tool_output_data["ext"], "content_dict": tool_output_data}
  ```
  and keep the `"text"` branch for remote MCP tools. When several tools produced
  `type: node` ext, merge their rows (dedupe by `name`) instead of taking the first only.
- Prompt: add **CASE 4: NODE SEARCH / NODE INFO** ("which node does X", "is there
  a node for", "what does node X do", "how do I install X", "find nodes for ...")
  -> `search_node(query)`; a specific class name -> `get_node_info`; a list of
  classes / "what am I missing" -> `get_node_info_by_types`. Present results
  briefly; the card shows the details. Replace lines 287-288 with a string built
  at runtime: when `server_list` is non-empty keep the `bing_search` fallback
  sentences; otherwise "If `search_node` finds nothing, say so and suggest 2-3
  alternative queries; do not invent node names." Keep CASE 1's "DO NOT call
  search_node" line.
- Rewrite agent: unchanged in this phase.

## 4. UI: `ui/src/apis/workflowChatApi.ts` (PR H)

`batchGetNodeInfo`: URL -> relative `/api/copilot/node_info_by_types` (same
pattern as `chatUrl = '/api/chat/invoke'` at line 256). Rebuild
`dist/copilot_web`. Optional: drop the self-`connect` call in `NodeSearch.tsx:81`.

## 5. Install action (design only, follow-up PR I)

```python
# backend/utils/comfy_gateway.py (later)
async def manager_queue_install(self, item: dict) -> Tuple[int, Any]   # POST /api/manager/queue/install
async def manager_queue_start(self) -> int                             # POST /api/manager/queue/start
async def manager_queue_status(self) -> dict                           # GET  /api/manager/queue/status
async def manager_version(self) -> Optional[str]                       # GET  /api/manager/version; None -> no Manager
# backend/tools/node_index.py (later)
async def install_pack(reference_or_class: str, *, gateway=None, wait: float = 300) -> dict
@function_tool async def install_node_pack(node_class: str) -> str    # only registered when COPILOT_ALLOW_NODE_INSTALL=1
```
Why follow-up: 403 on `security_level = strong`, git-URL installs hit the risk
check and may require `allow_git_url_install`, installs need a ComfyUI restart,
and the CNR `version` semantics were not verified.

## 6. Tests

Fixtures `tests/fixtures/node_db/`: `custom-node-list.json` (4 packs: a
KJNodes-like pack with `id`, an Impact-like pack with `preemptions`, a low-star
pack that re-declares one KJNodes class name, and one malformed entry whose
`reference` differs from `files[0]`), `extension-node-map.json` (~8 class names
incl. the duplicate and a `nodename_pattern` pack), `github-stats.json` (stars
for 3 of 4). Extend `tests/fake_comfy.py` `OBJECT_INFO` entries with
`python_module` (`nodes` for the four core nodes) and add two custom entries:
one class from the fixture map (`python_module: custom_nodes.comfyui-kjnodes`,
with `description`, `search_aliases`) and one installed class absent from the
map under a folder that matches a fixture reference.

`tests/test_node_index.py` (`unittest`, `IsolatedAsyncioTestCase` where the fake is needed):
- `test_load_databases_prefers_manager_files_then_cache_then_fetch`
- `test_build_index_joins` (installed flag; join via `reference` and `files[0]`; duplicate class resolved with `other_packs`; unmapped installed class -> pack by folder name; core node -> `CORE_REPO`; `nodename_pattern` assignment)
- `test_search_ranking` (`"VAEDecodeTiled"` exact first; `"tiled vae decode"` ranks `VAEDecodeTiled` above `VAEDecode`; pack-title query finds uninstalled classes; installed wins ties; `limit`; empty query -> error; `installed_only`)
- `test_node_row_shape` (exactly the seven `Node` fields with correct types; `github_url` empty for synthesized packs; `from_index == to_index == 0`)
- `test_node_info` (installed -> trimmed inputs/outputs; uninstalled -> pack + `install_hint`; unknown -> `suggestions`)
- `test_node_rows_for_types_order_and_unknowns`
- `test_get_index_uses_fake_object_info_and_caches` (second call within `OBJECT_INFO_TTL` does not hit `/api/object_info`; `invalidate_index()` forces rebuild; fake stopped -> index still builds with installed = none)

## 7. PR split

| PR | Scope | Files | Depends on |
|---|---|---|---|
| G | Index: databases, cache, build, search, row shapers, plain functions; fixtures; tests. No agent wiring, nothing user-visible. | `backend/tools/node_index.py`, `tests/fixtures/node_db/*`, `tests/fake_comfy.py` (OBJECT_INFO additions), `tests/test_node_index.py` | — |
| H | Wiring: tool wrappers on the main agent, prompt CASE 4 and conditional web fallback, parser change, route + `__init__.py`, `batchGetNodeInfo` URL, `dist/` rebuild | `backend/tools/node_index.py` (tool wrappers), `backend/service/mcp_client.py`, `backend/controller/node_api.py`, `__init__.py`, `ui/src/apis/workflowChatApi.ts`, `dist/copilot_web/*` | G |
| I (follow-up) | `install_node_pack` behind `COPILOT_ALLOW_NODE_INSTALL`, gateway Manager methods, FakeComfy `/api/manager/*` routes | `backend/utils/comfy_gateway.py`, `backend/tools/node_index.py`, `tests/fake_comfy.py` | H |

If the dist rebuild needs a different worker, split H into H1 (backend) and H2 (UI + dist).

## Flagged uncertainties

- Manager location variants: a pip-installed `comfyui_manager` package ships the same files elsewhere and is not probed; `COPILOT_NODE_DB_DIR` covers it manually; fetch covers it automatically.
- "Largest file per basename" heuristic for picking the default-channel copy in the cache.
- Duplicate class names: the installed/most-stars rule can attribute an uninstalled class to the wrong pack; `other_packs` is exposed.
- Search quality for uninstalled nodes rests on class-name tokens and pack text only.
- Build time for ~41k records not measured; expected 1-2 s once per process.
- Parser change ordering in `mcp_client.py`: the `elif` must not swallow a `workflow_update` ext.
- Live `/object_info` `python_module` values for packs whose folder name differs from the repo name were not sampled.
