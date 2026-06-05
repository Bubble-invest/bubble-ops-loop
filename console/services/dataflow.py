"""
dataflow.py — per-dept data-flow, grounded in the dept's real dept.yaml.

For the click-through "data-flow" view on /health: which GitHub repos / vaults /
databases / brokers / queues / external sources each department reads and writes,
broken down BY LAYER (mission input_sources + outputs). Nothing here is assumed —
it's parsed straight from `dept.yaml::layers|recurring_missions|missions`.

Each source is classified into a `kind` so the client can group/colour it:
  repo · vault · wiki · db · broker · queue · llm_ctx · external
"""
from __future__ import annotations

from typing import Any, Dict, List

from console.services import github_reader

# Map a raw source/output token (as written in dept.yaml) to a (kind, label).
# Tokens not matched fall back to "external" with their raw name.
_CLASSIFY = [
    # (predicate on lowercased token, kind, pretty-label-or-None=keep raw)
    (lambda s: s.startswith("queues/") or s.endswith("_queue") or "queue" in s, "queue", None),
    (lambda s: "vault" in s, "vault", None),
    (lambda s: s in ("wiki", "shared_wiki", "shared-wiki"), "wiki", "shared-wiki"),
    (lambda s: "sqlite" in s or s.endswith("_db") or s == "pool_db" or "ledger" in s, "db", None),
    (lambda s: "broker" in s or s in ("alpaca", "saxo", "bourso", "crypto.com", "polymarket"), "broker", None),
    (lambda s: s in ("working_memory", "outputs_today", "mandate", "decision_log",
                     "kpi_snapshots", "layer4_feedback"), "llm_ctx", None),
    (lambda s: "linkedin" in s or "sirene" in s or "data_gouv" in s or "rss" in s
               or "web" in s, "external", None),
]


def _classify(token: str) -> Dict[str, str]:
    t = (token or "").strip()
    low = t.lower()
    for pred, kind, label in _CLASSIFY:
        try:
            if pred(low):
                return {"id": t, "kind": kind, "label": label or t}
        except Exception:
            pass
    return {"id": t, "kind": "external", "label": t}


def _missions(doc: dict) -> List[dict]:
    """All layer/mission entries from a dept.yaml, whatever key they live under."""
    out: List[dict] = []
    for key in ("layers", "recurring_missions", "missions"):
        v = doc.get(key)
        if isinstance(v, list):
            out.extend(m for m in v if isinstance(m, dict))
    return out


def dept_dataflow(slug: str) -> Dict[str, Any]:
    """Per-layer read/write sources for one dept, from its dept.yaml.

    Returns:
      {slug, repos: [...], layers: [{layer, name, reads:[{id,kind,label}],
       writes:[...]}], sources: {id: {kind,label, reads_in:[L], writes_in:[L]}}}
    `repos` lists the actual git remotes the dept owns (own repo + vault when
    present + shared-wiki) for the 'repos they have access to' part.
    """
    doc = github_reader.load_dept_yaml(slug) or {}
    layers: List[Dict[str, Any]] = []
    sources: Dict[str, Dict[str, Any]] = {}

    def _touch(token: str, layer_key: str, direction: str):
        c = _classify(token)
        s = sources.setdefault(c["id"], {"kind": c["kind"], "label": c["label"],
                                         "reads_in": [], "writes_in": []})
        bucket = "reads_in" if direction == "read" else "writes_in"
        if layer_key not in s[bucket]:
            s[bucket].append(layer_key)

    for m in _missions(doc):
        lk = m.get("layer")
        lk = str(lk) if lk is not None else (m.get("id") or "?")
        reads = [t for t in (m.get("input_sources") or []) if isinstance(t, str)]
        writes = []
        if isinstance(m.get("output_queue"), str):
            writes.append(m["output_queue"])
        writes += [t for t in (m.get("creates") or []) if isinstance(t, str)]
        for t in reads:
            _touch(t, lk, "read")
        for t in writes:
            _touch(t, lk, "write")
        layers.append({
            "layer": lk,
            "name": m.get("id") or m.get("description", "")[:48] or f"Layer {lk}",
            "reads": [_classify(t) for t in reads],
            "writes": [_classify(t) for t in writes],
        })

    # actual git repos the dept owns / can access (grounded — these are real
    # remotes; vault only present for some depts; shared-wiki read by all).
    repos = [{"id": f"bubble-ops-{slug}", "kind": "repo",
              "role": "repo du département (R/W : queues, outputs, layers)"}]
    # Vault: only a real GIT vault subrepo counts as a repo node. Some depts
    # (e.g. CGP) reference an *Obsidian* vault in dept.yaml — that's a data
    # source, NOT a GitHub repo, so it must NOT become a vault repo node.
    vault_remote = _vault_git_remote(slug)
    if vault_remote:
        repos.append({"id": vault_remote, "kind": "vault",
                      "role": "vault GitHub de thèses / contenu (R/W)"})
    repos.append({"id": "shared-wiki", "kind": "wiki",
                  "role": "mémoire partagée (lecture)"})

    return {"slug": slug, "repos": repos, "layers": layers, "sources": sources}


def _vault_git_remote(slug: str):
    """Return the vault repo name iff `<dept-repo>/vault` is a real git repo
    with a remote (Ben/Maya have one; CGP's 'vault' is Obsidian, not git)."""
    import subprocess
    from console.services.dept_registry import repo_path
    root = repo_path(slug)
    if root is None:
        return None
    vdir = root / "vault"
    if not (vdir / ".git").exists():
        return None
    try:
        out = subprocess.run(["git", "-C", str(vdir), "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=4)
        url = (out.stdout or "").strip()
        if not url:
            return None
        # bubble-ben-vault.git → bubble-ben-vault
        name = url.rstrip("/").split("/")[-1]
        return name[:-4] if name.endswith(".git") else name
    except Exception:
        return None
