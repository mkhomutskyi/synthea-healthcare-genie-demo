"""Repository locations and the rule that local data stays in ignored paths."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = REPO_ROOT / "data"
SQL_DIR = REPO_ROOT / "sql"
GENIE_DIR = REPO_ROOT / "genie"
EVALUATION_DIR = REPO_ROOT / "evaluation"
DOCS_DIR = REPO_ROOT / "docs"

ARTIFACT_LOCK_PATH = DATA_DIR / "artifact.lock.json"
SCHEMA_CONTRACT_PATH = DATA_DIR / "schema-contract.json"
BENCHMARKS_PATH = EVALUATION_DIR / "benchmarks.json"
BOUNDARY_PROBES_PATH = EVALUATION_DIR / "boundary-probes.json"
REFERENCE_DIGEST_PATH = EVALUATION_DIR / "expected-results" / "reference-digest.json"
PAYLOAD_HOLDOUT_PATH = EVALUATION_DIR / "payload-holdout.json"
PAYLOAD_HOLDOUT_DIGEST_PATH = (
    EVALUATION_DIR / "expected-results" / "payload-holdout-reference-digest.json"
)
AGENT_SETTINGS_PATH = GENIE_DIR / "agent_settings.json"
INSTRUCTIONS_PATH = GENIE_DIR / "instructions.md"
EXAMPLE_QUERIES_PATH = GENIE_DIR / "example_queries.sql"
AGENT_TEMPLATE_PATH = GENIE_DIR / "healthcare_ops.template.json"
CANONICAL_SQL_PATH = SQL_DIR / "40_canonical_answers.sql"

CACHE_DIR = REPO_ROOT / ".cache"
DEFAULT_ARCHIVE_DIR = CACHE_DIR / "synthea"
DEFAULT_STAGING_DIR = CACHE_DIR / "staging"
RENDERED_DIR = CACHE_DIR / "rendered"
RESULTS_DIR = CACHE_DIR / "results"
REFERENCE_DIR = CACHE_DIR / "reference"
REBASELINE_DIR = CACHE_DIR / "rebaseline"
DEPLOY_STATE_PATH = CACHE_DIR / "deploy-state.json"

ARCHIVE_FILENAME = "synthea_sample_data_csv_latest.zip"


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def symlinked_component(path: Path, base: Path) -> Path | None:
    """First existing symlink from ``base`` down to ``path`` (lexically), or None.

    ``path`` must be lexically inside ``base``; ``base`` itself is checked too.
    """
    absolute = Path(os.path.abspath(path.expanduser()))
    relative = absolute.relative_to(base)
    current = base
    for part in ("", *relative.parts):
        current = current / part if part else current
        if current.is_symlink():
            return current
        if not current.exists():
            return None
    return None


def ensure_local_output_dir(path: Path) -> Path:
    """Reject output locations inside the repository unless they are under .cache/.

    Raw or staged Synthea data must never land in a tracked path. A path given under
    .cache/ must not pass through a symlink (including .cache itself), so it cannot be
    redirected outside the cache.
    """
    absolute = Path(os.path.abspath(path.expanduser()))
    if absolute == CACHE_DIR or CACHE_DIR in absolute.parents:
        link = symlinked_component(absolute, CACHE_DIR)
        if link is not None:
            raise ValueError(f"Refusing to use {absolute}: {link} is a symlink")
    resolved = path.expanduser().resolve()
    if is_within(resolved, REPO_ROOT) and not is_within(resolved, CACHE_DIR):
        raise ValueError(
            f"Refusing to write local data to {resolved}: inside the repository, only "
            f"{CACHE_DIR} (git-ignored) may hold downloaded or staged data."
        )
    return resolved


def repo_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)
