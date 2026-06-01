"""qmd — QMD-backed semantic memory provider for Hermes.

Recalls facts from a local QMD-indexed markdown corpus using on-device
embeddings (hybrid BM25 + vector + optional rerank). Plugs into the Hermes
MemoryProvider ABC, so it activates via `memory.provider: qmd` in config.yaml —
no core edits.

Two recall paths:
  1. prefetch() — runs every turn with the user's message and injects the top
     matching facts as ephemeral pre-turn context. The agent never has to decide
     to look, so there is no "empty fact_store" dead-end (important with models
     that fire tools weakly, e.g. MiniMax).
  2. recall_memory tool — explicit, higher-quality (reranked) lookup the agent
     can call when it wants to dig.

This provider is READ/RECALL ONLY. Memory FORMATION (writing facts) is handled
by scripts/memory_formation.py + the QMD Reindex cron. No on_memory_write
mirror — we keep exactly one source of truth (the markdown notes corpus).

Config in $HERMES_HOME/config.yaml:
  plugins:
    qmd:
      collection: cockpit            # QMD collection to search
      qmd_bin: qmd                   # qmd executable (resolved on PATH)
      node_bin_path: /opt/homebrew/opt/node@22/bin  # node@22 (keg-only) for qmd
      prefetch_enabled: true
      prefetch_limit: 3              # max facts injected per turn
      prefetch_min_score: 0.5        # drop weak matches (0..1)
      prefetch_timeout: 8            # seconds; recall must never stall a turn
      prefetch_max_chars: 700        # hard cap on injected block size
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)


RECALL_TOOL_SCHEMA = {
    "name": "recall_memory",
    "description": (
        "Search long-term semantic memory for facts about the user, their family, "
        "work, preferences, decisions, and the systems being built. Use this when "
        "you need to remember something not already in front of you. Matches by "
        "MEANING, not just keywords — paraphrases work."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What you want to remember, as a natural-language question or phrase.",
            },
            "limit": {
                "type": "integer",
                "description": "Max facts to return (default 5).",
            },
        },
        "required": ["query"],
    },
}


class QmdMemoryProvider(MemoryProvider):
    """Semantic recall over a QMD-indexed markdown corpus."""

    # Common install locations, in case a (daemonized) gateway has a thin PATH.
    _FALLBACK_BIN_PATHS = (
        "/opt/homebrew/bin/qmd",
        "/usr/local/bin/qmd",
    )

    def __init__(self, config: Dict[str, Any] | None = None):
        self._config = config or {}
        self._collection = self._config.get("collection", "cockpit")
        self._qmd_bin = self._config.get("qmd_bin", "qmd")
        self._node_bin = self._config.get("node_bin_path", "/opt/homebrew/opt/node@22/bin")
        self._prefetch_enabled = bool(self._config.get("prefetch_enabled", True))
        self._prefetch_limit = int(self._config.get("prefetch_limit", 3))
        # vsearch returns absolute cosine similarity. Measured on real facts:
        # on-topic queries score ~0.54-0.67 (first-person phrasings sit at the
        # low end, e.g. "what is my last day at work" → 0.541), off-topic
        # queries ~0.46 (tokyo weather, kubernetes). 0.50 separates them.
        self._prefetch_min_score = float(self._config.get("prefetch_min_score", 0.50))
        self._prefetch_timeout = int(self._config.get("prefetch_timeout", 8))
        self._prefetch_max_chars = int(self._config.get("prefetch_max_chars", 700))
        self._resolved_bin: Optional[str] = None
        # qmd resolves its index by walking UP from cwd to find a `.qmd` dir.
        # We host the index at $HERMES_HOME/.qmd, so every qmd call must run with
        # cwd pinned to HERMES_HOME — otherwise (e.g. from the gateway's cwd) the
        # collection isn't found and recall silently returns nothing. Captured in
        # initialize() from the hermes_home kwarg; config can override.
        self._index_cwd: Optional[str] = self._config.get("index_cwd") or None

    # -- MemoryProvider interface -------------------------------------------

    @property
    def name(self) -> str:
        return "qmd"

    def _env(self) -> dict:
        env = dict(os.environ)
        # qmd lives under node@22 (keg-only); make sure node is on PATH.
        if self._node_bin and os.path.isdir(self._node_bin):
            env["PATH"] = self._node_bin + ":" + env.get("PATH", "")
        return env

    def _find_bin(self) -> Optional[str]:
        if self._resolved_bin:
            return self._resolved_bin
        if os.path.isabs(self._qmd_bin) and os.path.exists(self._qmd_bin):
            self._resolved_bin = self._qmd_bin
            return self._resolved_bin
        found = shutil.which(self._qmd_bin, path=self._env().get("PATH"))
        if not found:
            for cand in self._FALLBACK_BIN_PATHS:
                if os.path.exists(cand):
                    found = cand
                    break
        self._resolved_bin = found
        return found

    def is_available(self) -> bool:
        return self._find_bin() is not None

    def initialize(self, session_id: str, **kwargs) -> None:
        # Nothing to open — qmd is invoked per-query as a subprocess.
        # Resolve the binary once so is_available()/prefetch() stay cheap.
        self._find_bin()
        # Pin cwd to HERMES_HOME so qmd finds $HERMES_HOME/.qmd regardless of
        # where the gateway process was launched from. Config index_cwd wins.
        if not self._index_cwd:
            hh = kwargs.get("hermes_home") or os.environ.get("HERMES_HOME")
            if hh and os.path.isdir(hh):
                self._index_cwd = hh
        logger.debug(
            "qmd memory provider initialized (collection=%s, bin=%s, cwd=%s)",
            self._collection, self._resolved_bin, self._index_cwd,
        )

    # -- Query helper --------------------------------------------------------

    def _run(self, subcommand: str, query: str, limit: int, timeout: int,
             extra: Optional[List[str]] = None) -> List[dict]:
        """Run a qmd subcommand (vsearch|query|search) → parsed result list.

        Note on scoring: `vsearch` returns absolute cosine similarity (a fixed
        threshold is meaningful), while `query` returns query-relative RRF
        fusion scores (there is always a ~0.5 top hit, even for irrelevant
        queries — do NOT threshold on those). So prefetch uses vsearch; the
        explicit recall tool uses the higher-quality reranked query path.
        """
        binp = self._find_bin()
        if not binp or not query.strip():
            return []
        cmd = [
            binp, subcommand, query,
            "-c", self._collection,
            "-n", str(limit),
            "--format", "json",
        ]
        if extra:
            cmd.extend(extra)
        try:
            proc = subprocess.run(
                cmd, env=self._env(), capture_output=True, text=True,
                timeout=timeout, check=False,
                cwd=self._index_cwd if self._index_cwd else None,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                return []
            data = json.loads(proc.stdout)
            # `qmd query --format json` returns a top-level LIST of result
            # objects. Be tolerant of a {"results": [...]} wrapper too.
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get("results", []) or []
            return []
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
            logger.debug("qmd query failed (%s): %s", type(e).__name__, e)
            return []

    @staticmethod
    def _text_of(result: dict) -> str:
        """Extract clean fact text from a qmd result object.

        qmd snippets are framed with a unified-diff hunk header like
        `@@ -1,3 @@ (0 before, 17 after)` and may carry `§` section separators.
        Strip that framing so injected memory reads as plain facts.
        """
        raw = (
            result.get("content")
            or result.get("snippet")
            or result.get("excerpt")
            or ""
        )
        lines = []
        for ln in raw.splitlines():
            s = ln.strip()
            if not s or s == "§":
                continue
            if s.startswith("@@"):  # diff hunk header
                continue
            # strip leading markdown bullet so we control formatting
            if s.startswith("- "):
                s = s[2:].strip()
            lines.append(s)
        return " ".join(lines).strip()

    @staticmethod
    def _source_of(result: dict) -> str:
        return result.get("file") or result.get("path") or result.get("source") or ""

    # -- Recall: pre-turn injection -----------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._prefetch_enabled or not query:
            return ""
        # vsearch → absolute cosine similarity, so prefetch_min_score is a
        # meaningful relevance gate (real queries ~0.6+, off-topic ~0.46).
        results = self._run(
            "vsearch", query, self._prefetch_limit, self._prefetch_timeout,
        )
        facts = []
        for r in results:
            try:
                if float(r.get("score", 0)) < self._prefetch_min_score:
                    continue
            except (TypeError, ValueError):
                continue
            text = self._text_of(r)
            if text:
                facts.append(text)
        if not facts:
            return ""

        body = "\n".join(f"- {f}" for f in facts)
        block = (
            "## Recalled memory (semantic)\n"
            "Relevant facts from long-term memory for this message. "
            "Treat as background knowledge; verify before acting on it.\n"
            f"{body}"
        )
        if len(block) > self._prefetch_max_chars:
            block = block[: self._prefetch_max_chars].rstrip() + " …"
        return block

    # -- Recall: explicit tool ----------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [RECALL_TOOL_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name != "recall_memory":
            raise NotImplementedError(f"Provider {self.name} does not handle tool {tool_name}")
        args = args or {}
        query = args.get("query", "")
        try:
            limit = int(args.get("limit", 5))
        except (TypeError, ValueError):
            limit = 5
        # Explicit lookups get the higher-quality hybrid + reranked path.
        results = self._run("query", query, limit, timeout=20)
        out = []
        for r in results:
            text = self._text_of(r)
            if not text:
                continue
            try:
                score = round(float(r.get("score", 0)), 3)
            except (TypeError, ValueError):
                score = 0.0
            out.append({"fact": text, "score": score, "source": self._source_of(r)})
        return json.dumps({"results": out, "count": len(out)})

    # -- Write path: intentionally inert ------------------------------------
    # Formation is handled by the memory-formation cron writing markdown that
    # QMD indexes. We do NOT mirror built-in memory writes here (single source
    # of truth, no drift). sync_turn / on_session_end / on_memory_write stay
    # no-ops (inherited from the ABC).

    def shutdown(self) -> None:
        self._resolved_bin = None

    # -- Config UI -----------------------------------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "collection", "description": "QMD collection to search", "default": "cockpit"},
            {"key": "prefetch_enabled", "description": "Inject memory pre-turn", "default": "true", "choices": ["true", "false"]},
            {"key": "prefetch_limit", "description": "Max facts injected per turn", "default": "3"},
            {"key": "prefetch_min_score", "description": "Min match score 0..1", "default": "0.5"},
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """Write non-secret config to config.yaml under plugins.qmd."""
        from pathlib import Path
        config_path = Path(hermes_home) / "config.yaml"
        try:
            import yaml
            existing = {}
            if config_path.exists():
                with open(config_path, encoding="utf-8-sig") as f:
                    existing = yaml.safe_load(f) or {}
            existing.setdefault("plugins", {})
            existing["plugins"]["qmd"] = values
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(existing, f, default_flow_style=False)
        except Exception:
            pass


def _load_config_for_register() -> dict:
    """Load plugin config from config.yaml for the register() entry point."""
    try:
        from hermes_cli.config import load_config, cfg_get
        all_config = load_config()
        return cfg_get(all_config, "plugins", "qmd", default={}) or {}
    except Exception:
        return {}


def register(ctx) -> None:
    """Register the QMD memory provider with the plugin system."""
    config = _load_config_for_register()
    provider = QmdMemoryProvider(config=config)
    ctx.register_memory_provider(provider)
