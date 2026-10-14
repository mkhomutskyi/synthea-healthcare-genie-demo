"""The payload-held-out suite in evaluation/payload-holdout.json.

"Payload-held-out" means exactly this: the suite's questions, boundary paraphrases and
canonical SQL are not among ``render.TEMPLATE_SOURCES``, so none of them is in the
serialized space sent to Genie. The file is public in this repository. It is not secret,
and it is not blind to anyone who reads it, including whoever writes the instructions.

The suite has its own version and a content hash over everything except its version
history, so a formal evaluation and its capture can prove which exact suite was asked.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .artifact import ArtifactError, verify_staged_files
from .jsonio import load_json, sha256_text
from .paths import PAYLOAD_HOLDOUT_PATH
from .reference import (
    HOLDOUT_FREEZE_COMMAND,
    HOLDOUT_SPECS,
    check_reference_against_digest,
    compute_all,
    load_staged_rows,
)

HOLDOUT_SUITE = "synthea-healthcare-ops-payload-holdout"
HOLDOUT_EXPORT_FORMAT = "synthea-genie-payload-holdout-export"
HOLDOUT_CANONICAL_RESULTS_FILE = "payload-holdout-canonical-results.json"
HOLDOUT_RUNS = 3
HOLDOUT_QUESTIONS = 4
HOLDOUT_PROBES_PER_CLASS = 2
HOLDOUT_PROBE_CLASSES = {
    "diagnosis_inference": "refuse",
    "facility_or_provider_ranking": "refuse",
    "payer_recommendation": "safe_redirect",
}
HOLDOUT_TOP_KEYS = frozenset(
    {
        "suite",
        "suite_version",
        "frozen",
        "frozen_on",
        "mode",
        "scope",
        "notes",
        "metric_view_placeholder",
        "years",
        "analytical_runs",
        "intents",
        "questions",
        "boundary_probes",
        "version_history",
    }
)
HOLDOUT_SCOPE = (
    "payload-held-out: never rendered into the Agent configuration; public in this "
    "repository, not secret and not blind"
)


def load_holdout(path: Path = PAYLOAD_HOLDOUT_PATH) -> dict[str, Any]:
    data: dict[str, Any] = load_json(path)
    return data


def content_sha256(suite: Mapping[str, Any]) -> str:
    """SHA-256 of the suite's canonical JSON without its version history."""
    body = {k: v for k, v in suite.items() if k != "version_history"}
    return sha256_text(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def holdout_meta(suite: Mapping[str, Any]) -> dict[str, str]:
    """What a plan, checkpoint and capture record to name the exact suite asked."""
    return {
        "suite": suite["suite"],
        "suite_version": suite["suite_version"],
        "content_sha256": content_sha256(suite),
    }


def export_suite(suite: Mapping[str, Any]) -> dict[str, Any]:
    """The analytical half, in the shape the export validation and scoring read."""
    return {
        "suite": suite["suite"],
        "suite_version": suite["suite_version"],
        "intents": suite["intents"],
        "questions": suite["questions"],
    }


def probe_suite(suite: Mapping[str, Any]) -> dict[str, Any]:
    """The boundary half, in the shape the transcript and public responses read."""
    return {
        "suite": suite["suite"],
        "suite_version": suite["suite_version"],
        "probes": suite["boundary_probes"],
    }


def holdout_prompt_count(suite: Mapping[str, Any]) -> int:
    return HOLDOUT_RUNS * len(suite["questions"]) + len(suite["boundary_probes"])


def holdout_reference(
    staging_dir: Path,
    lock: Mapping[str, Any],
    contract: Mapping[str, Any],
    suite: Mapping[str, Any],
    digest_path: Path,
) -> tuple[dict[str, dict[str, Any]] | None, list[str]]:
    """The four holdout answers computed in memory from the verified staged CSV files.

    Nothing is written. The result is returned only if it equals the committed digest,
    which must belong to this archive and to these intent semantics.
    """
    try:
        verify_staged_files(staging_dir, lock, contract)
    except ArtifactError as exc:
        return None, [f"the staged files cannot back the payload-holdout reference: {exc}"]
    results = compute_all(load_staged_rows(staging_dir), HOLDOUT_SPECS)
    try:
        frozen = load_json(digest_path)
    except (OSError, ValueError) as exc:
        return None, [f"the payload-holdout reference digest is unreadable: {exc}"]
    problems = check_reference_against_digest(
        results,
        frozen,
        artifact_sha256=lock["artifact"]["sha256"],
        intents=suite["intents"],
        generated_by=HOLDOUT_FREEZE_COMMAND,
    )
    return (None, problems) if problems else (results, [])
