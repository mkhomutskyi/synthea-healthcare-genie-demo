"""Deployment and cleanup plans built on the dry-run-first CLI runner.

Genie Agent create and update use ``databricks api`` against the documented REST paths
(``POST /api/2.0/genie/spaces`` and ``PATCH /api/2.0/genie/spaces/{space_id}``) with a
request body validated here. ``databricks genie create-space`` in CLI 1.20.0 takes
WAREHOUSE_ID and SERIALIZED_SPACE as positional arguments instead.

Runtime validation required: response shapes (genie spaces, schemas get, warehouses get)
are assumed from public documentation and CLI help, not observed live.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import PROJECT_SLUG
from .artifact import STAGED_FILES, ArtifactError, verify_staged_files
from .databricks_cli import CliRunner, execute_statement
from .jsonio import dumps_pretty, sha256_bytes, sha256_file
from .render import (
    RenderError,
    agent_title,
    build_create_request,
    validate_identifier,
    validate_run_label,
    validate_schema_name,
    validate_serialized_space,
    validate_warehouse_id,
)
from .safeio import (
    FileIdentity,
    file_identity,
    read_regular,
    write_private_atomic,
)
from .sqltools import (
    DEFAULT_VOLUME,
    render_sql,
    split_statements,
    sql_parameters,
    strip_comments,
)

STEPS = ("preflight", "schema", "upload", "load", "metric_view", "agent")
SQL_BY_STEP = {
    "schema": ("00_create_schema.sql",),
    "load": ("10_bronze.sql", "20_clean.sql"),
    "metric_view": ("30_metric_view.sql",),
}
GENIE_SPACES_PATH = "/api/2.0/genie/spaces"
CREATE_REQUIRED = ("warehouse_id", "serialized_space")
CREATE_FIELDS = (*CREATE_REQUIRED, "title", "description", "parent_path")
UPDATE_FIELDS = ("warehouse_id", "serialized_space", "title", "description")
SPACE_ID_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z_-]{0,127}")
LEGACY_STATE_VERSION = 1
STATE_VERSION = 2
LEGACY_STATE_KEYS = (
    "project",
    "state_version",
    "catalog",
    "schema",
    "volume",
    "warehouse_id",
    "run_label",
    "title",
    "space_id",
)
# Version 2 adds the local create-space request receipt. Version 1 files stay readable.
STATE_KEYS = (*LEGACY_STATE_KEYS, "parent_path", "agent_request_sha256")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
MAX_PARENT_PATH_CHARS = 1024
MAX_STATE_BYTES = 64 * 1024
MAX_REQUEST_BYTES = 4 * 1024 * 1024
STATE_FILE_MODE = 0o600
REQUEST_FILE_MODE = 0o400
RECEIPT_SCOPE = (
    "Local receipt: the SHA-256 of the exact create-space request file that "
    "deploy_agent.py validated and sent, recorded in the operator's deploy state. It is "
    "not a remote cryptographic attestation of the Agent's configuration."
)
IDENTITY_KEYS = ("catalog", "schema", "volume", "run_label", "title")


class DeployError(RuntimeError):
    pass


@dataclass(frozen=True)
class Target:
    catalog: str
    schema: str
    warehouse_id: str
    run_label: str
    volume: str = DEFAULT_VOLUME

    def __post_init__(self) -> None:
        validate_identifier(self.catalog, "catalog")
        validate_schema_name(self.schema)
        validate_identifier(self.volume, "volume")
        validate_warehouse_id(self.warehouse_id)
        validate_run_label(self.run_label)

    @property
    def full_schema(self) -> str:
        return f"{self.catalog}.{self.schema}"

    def volume_uri(self, name: str) -> str:
        return f"dbfs:/Volumes/{self.catalog}/{self.schema}/{self.volume}/{name}"


def validate_parent_path(value: Any) -> str | None:
    """The single check for an Agent's workspace parent folder; ``None`` means none given.

    Run before the create request is built and before any deploy state is written, and
    again when a state or request is read. A parent path must be an absolute workspace
    folder path of at most MAX_PARENT_PATH_CHARS characters, with no control or format
    characters, backslash, empty, ``.`` or ``..`` segment, padded segment or trailing slash.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise DeployError("parent path must be a string")
    if len(value) > MAX_PARENT_PATH_CHARS:
        raise DeployError(f"parent path must be at most {MAX_PARENT_PATH_CHARS} characters")
    if any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in value):
        raise DeployError("parent path must not contain control or format characters")
    if "\\" in value:
        raise DeployError("parent path must use '/' separators, not backslashes")
    if not value.startswith("/") or value == "/" or value.endswith("/"):
        raise DeployError(
            "parent path must be an absolute workspace folder path that starts with '/', "
            "without a trailing slash"
        )
    segments = value.split("/")[1:]
    if any(s in ("", ".", "..") or s != s.strip() for s in segments):
        raise DeployError("parent path must not contain empty, '.', '..' or padded segments")
    return value


def validate_space_id(value: Any) -> str:
    if not isinstance(value, str) or not SPACE_ID_RE.fullmatch(value):
        raise DeployError("space ID must be 1-128 letters, digits, '_' or '-'")
    return value


def render_sql_files(
    target: Target,
    lock: Mapping[str, Any],
    contract: Mapping[str, Any],
    sql_dir: Path,
    out_dir: Path,
    names: tuple[str, ...],
) -> dict[str, str]:
    params = sql_parameters(
        lock,
        contract,
        catalog=target.catalog,
        schema=target.schema,
        run_label=target.run_label,
        volume=target.volume,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    rendered: dict[str, str] = {}
    for name in names:
        text = render_sql((sql_dir / name).read_text(encoding="utf-8"), params)
        (out_dir / name).write_text(text, encoding="utf-8")
        rendered[name] = text
    return rendered


def check_staging(
    staging_dir: Path, lock: Mapping[str, Any], contract: Mapping[str, Any]
) -> list[str]:
    """Both staged files must exist and match the manifest digests, contract and lock."""
    try:
        verify_staged_files(staging_dir, lock, contract)
    except ArtifactError as exc:
        return [str(exc)]
    return []


def validate_create_request(body: Mapping[str, Any], *, warehouse_id: str, title: str) -> list[str]:
    """Check the documented POST /api/2.0/genie/spaces body before anything is sent."""
    problems: list[str] = []
    extra = sorted(set(body) - set(CREATE_FIELDS))
    if extra:
        problems.append(f"create request has undocumented fields {extra}")
    for key in CREATE_REQUIRED:
        if key not in body:
            problems.append(f"create request is missing required {key}")
    for key, value in body.items():
        if not isinstance(value, str) or not value:
            problems.append(f"create request field {key} must be a non-empty string")
    if problems:
        return problems
    if "parent_path" in body:
        try:
            validate_parent_path(body["parent_path"])
        except DeployError as exc:
            problems.append(f"create request {exc}")
    if body["warehouse_id"] != warehouse_id:
        problems.append("create request warehouse_id differs from the target")
    if body.get("title") != title:
        problems.append("create request title differs from the unique run title")
    try:
        space = json.loads(body["serialized_space"])
    except json.JSONDecodeError:
        return [*problems, "serialized_space is not a JSON string"]
    if not isinstance(space, dict):
        return [*problems, "serialized_space must encode a JSON object"]
    problems.extend(validate_serialized_space(space, rendered=True))
    return problems


def update_request(create_request: Mapping[str, Any]) -> dict[str, Any]:
    """PATCH body: same content, without parent_path (on update it would move the space)."""
    return {k: create_request[k] for k in UPDATE_FIELDS if k in create_request}


def new_state(
    target: Target,
    title: str,
    space_id: str | None,
    *,
    parent_path: str | None = None,
    agent_request_sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "project": PROJECT_SLUG,
        "state_version": STATE_VERSION,
        "catalog": target.catalog,
        "schema": target.schema,
        "volume": target.volume,
        "warehouse_id": target.warehouse_id,
        "run_label": target.run_label,
        "title": title,
        "space_id": space_id,
        "parent_path": parent_path,
        "agent_request_sha256": agent_request_sha256,
    }


def request_bytes(request: Mapping[str, Any]) -> bytes:
    """The exact bytes create_agent writes to the request file the CLI reads."""
    return dumps_pretty(dict(request)).encode("utf-8")


def request_sha256(request: Mapping[str, Any]) -> str:
    """SHA-256 of the request file exactly as create_agent writes and sends it."""
    return sha256_bytes(request_bytes(request))


def _state_keys(state: Mapping[str, Any]) -> tuple[str, ...]:
    version = state.get("state_version")
    legacy = type(version) is int and version == LEGACY_STATE_VERSION
    return LEGACY_STATE_KEYS if legacy else STATE_KEYS


def validate_state(state: Any, *, title_prefix: str) -> list[str]:
    """A state file is trusted only if every field has this project's exact shape.

    Version 1 files (no request receipt) written before version 2 stay valid.
    """
    if not isinstance(state, dict):
        return ["deploy state must be a JSON object"]
    keys = _state_keys(state)
    if sorted(state) != sorted(keys):
        return [f"deploy state must have exactly the keys {list(keys)}"]
    problems: list[str] = []
    if state["project"] != PROJECT_SLUG:
        problems.append(f"deploy state is not from {PROJECT_SLUG}")
    version = state["state_version"]
    if type(version) is not int or version not in (LEGACY_STATE_VERSION, STATE_VERSION):
        problems.append(f"deploy state version must be {LEGACY_STATE_VERSION} or {STATE_VERSION}")
    if keys == STATE_KEYS:
        parent, receipt = state["parent_path"], state["agent_request_sha256"]
        try:
            validate_parent_path(parent)
        except DeployError as exc:
            problems.append(f"deploy state parent_path must be null or valid: {exc}")
        if receipt is not None and (
            not isinstance(receipt, str) or not SHA256_RE.fullmatch(receipt)
        ):
            problems.append("deploy state agent_request_sha256 must be null or a SHA-256 digest")
    text_keys = ("catalog", "schema", "volume", "warehouse_id", "run_label", "title")
    if not all(isinstance(state[k], str) for k in text_keys):
        return [*problems, "deploy state identifiers and title must be strings"]
    try:
        target = Target(
            state["catalog"],
            state["schema"],
            state["warehouse_id"],
            state["run_label"],
            state["volume"],
        )
        if state["title"] != agent_title(title_prefix, target.run_label):
            problems.append("deploy state title is not this project's title for its run label")
    except RenderError as exc:
        problems.append(f"deploy state is invalid: {exc}")
    if state["space_id"] is not None:
        try:
            validate_space_id(state["space_id"])
        except DeployError as exc:
            problems.append(f"deploy state is invalid: {exc}")
    return problems


def load_state(path: Path) -> Any:
    """The parsed state; ``None`` if absent; ``"unreadable"`` (never trusted) otherwise.

    The file is read once through a no-follow handle: a symlink, a non-regular file, an
    oversized or unparsable file is ``"unreadable"``, which every caller refuses.
    """
    if not os.path.lexists(path):
        return None
    try:
        return json.loads(read_regular(path, MAX_STATE_BYTES).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "unreadable"


MAX_STATE_SCAN_BYTES = 256 * 1024


def _state_like_name(name: str) -> bool:
    return name.endswith(".json") or "state" in name.lower()


def _plausible_state(value: Any) -> bool:
    return isinstance(value, dict) and (
        value.get("project") == PROJECT_SLUG
        or "space_id" in value
        or len(set(value) & set(STATE_KEYS)) >= 3
    )


def _read_no_follow(path: str, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        return handle.read(limit + 1)


def cache_purge_blockers(cache_dir: Path, *, title_prefix: str) -> list[str]:
    """Reasons not to delete ``cache_dir``: any deployment state, or anything that might be one.

    Walks the cache without following symlinks. Blocks on: a validated deploy state; a
    JSON object that looks like a state (project slug, a space_id, or 3+ state keys);
    an unreadable or unparsable file whose name ends in .json or contains "state"; a
    symlink with such a name; an unreadable directory; and a symlinked or non-directory
    cache root. Unknown is treated as "may be a state".
    """
    if not os.path.lexists(cache_dir):
        return []
    root_info = os.lstat(cache_dir)
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        return [f"{cache_dir} is a symlink or not a directory; not purging it"]
    blockers: list[str] = []

    def unreadable_dir(exc: OSError) -> None:
        blockers.append(f"cannot read {exc.filename}; it may hold a deployment state")

    for dirpath, dirnames, filenames in os.walk(cache_dir, onerror=unreadable_dir):
        for name in [*dirnames, *filenames]:
            path = os.path.join(dirpath, name)
            shown = os.path.relpath(path, cache_dir.parent)
            try:
                info = os.lstat(path)
            except OSError:
                blockers.append(f"cannot inspect {shown}; it may be a deployment state")
                continue
            if stat.S_ISLNK(info.st_mode):
                if _state_like_name(name):
                    blockers.append(f"{shown} is a symlink that may point to a deployment state")
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if info.st_size > MAX_STATE_SCAN_BYTES:
                if "state" in name.lower():
                    blockers.append(f"{shown} is too large to inspect and may be a state")
                continue
            try:
                value = json.loads(_read_no_follow(path, MAX_STATE_SCAN_BYTES).decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                if _state_like_name(name):
                    blockers.append(f"{shown} is unreadable JSON and may be a deployment state")
                continue
            if _plausible_state(value):
                kind = (
                    "still records a deployment (valid deploy state)"
                    if not validate_state(value, title_prefix=title_prefix)
                    else "looks like a deployment state (not valid, kept to be safe)"
                )
                blockers.append(f"{shown} {kind}")
    return blockers


def same_identity(state: Mapping[str, Any], target: Target, title: str) -> bool:
    current = new_state(target, title, None)
    return all(state[k] == current[k] for k in IDENTITY_KEYS)


def list_spaces(runner: CliRunner) -> list[dict[str, Any]] | None:
    spaces: list[dict[str, Any]] = []
    token: str | None = None
    for _ in range(100):
        args = ["genie", "list-spaces"]
        if token:
            args += ["--page-token", token]
        page = runner.call("list Genie Agents (title uniqueness)", *args, mutating=False)
        if page is None:
            return None
        spaces.extend(page.get("spaces", []))
        token = page.get("next_page_token")
        if not token:
            return spaces
    raise DeployError("list-spaces pagination did not finish")


def schema_marker(run_label: str) -> str:
    return f"{PROJECT_SLUG} run-label={validate_run_label(run_label)}:"


def schema_marker_ok(schema_info: Any, run_label: str) -> bool:
    """The schema comment must start with this project's marker for this exact run label."""
    if not isinstance(schema_info, Mapping):
        return False
    return str(schema_info.get("comment") or "").startswith(schema_marker(run_label))


def preflight(runner: CliRunner, target: Target, title: str) -> list[str]:
    """Read-only checks. In dry-run mode the commands are printed and nothing is checked."""
    problems: list[str] = []
    ok, _ = runner.try_call("current user (auth check)", "current-user", "me")
    if runner.execute and not ok:
        return [
            "Databricks CLI is not authenticated for this profile; authenticate yourself and rerun"
        ]
    ok, warehouse = runner.try_call(
        "SQL warehouse (never started or resized here)", "warehouses", "get", target.warehouse_id
    )
    if runner.execute:
        if not ok:
            problems.append("warehouse not found or no CAN USE permission")
        elif not (
            warehouse.get("warehouse_type") == "PRO" or warehouse.get("enable_serverless_compute")
        ):
            problems.append("Genie requires a pro or serverless SQL warehouse")
    ok, _ = runner.try_call(
        "catalog exists (never created here)", "catalogs", "get", target.catalog
    )
    if runner.execute and not ok:
        problems.append(
            f"catalog {target.catalog} not found or not visible; it is never created here"
        )
    ok, schema = runner.try_call("schema state", "schemas", "get", target.full_schema)
    if runner.execute and ok and not schema_marker_ok(schema, target.run_label):
        problems.append(
            f"schema {target.full_schema} exists without the {PROJECT_SLUG} marker for run "
            f"label {target.run_label}; use a dedicated new schema"
        )
    spaces = list_spaces(runner)
    if spaces is not None:
        clashes = [s for s in spaces if s.get("title") == title]
        if clashes:
            problems.append(f"an Agent titled {title!r} already exists; pick a new --run-label")
    return problems


def run_sql(runner: CliRunner, target: Target, name: str, text: str, payload_dir: Path) -> None:
    run_statements(runner, target, name, split_statements(text), payload_dir)


def run_statements(
    runner: CliRunner, target: Target, name: str, statements: list[str], payload_dir: Path
) -> None:
    for index, statement in enumerate(statements, start=1):
        execute_statement(
            runner,
            warehouse_id=target.warehouse_id,
            statement=statement,
            payload_path=payload_dir / f"{name}.{index:02d}.json",
            description=f"{name} statement {index}",
        )


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


def real_dir_under(root: Path, target: Path) -> Path:
    """Create or confirm ``target`` as a real directory chain inside ``root``.

    ``root`` must be an absolute path with no symlink anywhere in it. Every component
    from ``root`` down to ``target`` must be a real directory (missing ones are created
    with mode 0700, one at a time); a symlink, a non-directory, or a path that leaves
    ``root`` raises DeployError.
    """
    root_str = os.path.abspath(root)
    if os.path.realpath(root_str) != root_str:
        raise DeployError(f"{root} passes through a symlink; use a real local directory")
    relative = os.path.relpath(os.path.abspath(target), root_str)
    if relative == os.pardir or relative.startswith(os.pardir + os.sep) or os.path.isabs(relative):
        raise DeployError(f"{target} is outside the approved directory {root}")
    os.makedirs(root_str, mode=0o700, exist_ok=True)
    current = root_str
    for part in ("", *Path(relative).parts):
        if part and part != os.curdir:
            current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            with contextlib.suppress(FileExistsError):
                os.mkdir(current, 0o700)
            info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise DeployError(f"{current} is a symlink or not a directory; refusing to write")
    return Path(current)


def _hash_fd(fd: int) -> str:
    digest = hashlib.sha256()
    with os.fdopen(fd, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_verified_snapshot(source: Path, directory: Path, name: str, expected: str) -> Path:
    """Copy ``source`` to ``directory/name`` exclusively, without following symlinks.

    The destination is created with O_CREAT | O_EXCL | O_NOFOLLOW relative to a
    no-follow handle on ``directory``. An existing regular file from an earlier run is
    replaced; a symlink or any other file type is refused. The copied bytes, and then
    the file re-read through a no-follow handle, must both hash to ``expected``.
    """
    if os.path.basename(name) != name or name in ("", os.curdir, os.pardir):
        raise DeployError(f"invalid snapshot file name {name!r}")
    try:
        return _write_snapshot(source, directory, name, expected)
    except OSError as exc:
        raise DeployError(f"cannot write snapshot {directory / name} safely: {exc}") from exc


def _write_snapshot(source: Path, directory: Path, name: str, expected: str) -> Path:
    dir_fd = os.open(directory, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
    try:
        if not stat.S_ISDIR(os.fstat(dir_fd).st_mode):
            raise DeployError(f"{directory} is not a directory")
        try:
            existing = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISREG(existing.st_mode):
                raise DeployError(
                    f"snapshot destination {directory / name} is a symlink or not a regular "
                    "file; refusing to write"
                )
            os.unlink(name, dir_fd=dir_fd)
        digest = hashlib.sha256()
        src_fd = os.open(source, os.O_RDONLY | _NOFOLLOW)
        with os.fdopen(src_fd, "rb") as src:
            out_fd = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=dir_fd
            )
            with os.fdopen(out_fd, "wb") as out:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
        reread = _hash_fd(os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=dir_fd))
        if digest.hexdigest() != expected or reread != expected:
            os.unlink(name, dir_fd=dir_fd)
            raise DeployError(f"staged {name} changed while preparing the upload")
    finally:
        os.close(dir_fd)
    return directory / name


def upload_staging(
    runner: CliRunner,
    target: Target,
    staging_dir: Path,
    lock: Mapping[str, Any],
    contract: Mapping[str, Any],
    snapshot_dir: Path,
    *,
    root: Path,
) -> None:
    """Upload a verified snapshot of each staged file.

    With --execute the staged files are re-verified (presence, manifest SHA-256, header,
    row count) immediately before upload, copied into ``snapshot_dir`` (a real directory
    chain inside ``root``, never through a symlink) with exclusive no-follow writes, and
    the copy is hashed again; only that verified copy is uploaded.
    """
    digests = verify_staged_files(staging_dir, lock, contract) if runner.execute else {}
    snapshot = real_dir_under(root, snapshot_dir) if runner.execute else snapshot_dir
    for name in STAGED_FILES:
        source = staging_dir / name
        if runner.execute:
            source = write_verified_snapshot(staging_dir / name, snapshot, name, digests[name])
            real_dir_under(root, snapshot_dir)
            info = os.lstat(source)
            if not stat.S_ISREG(info.st_mode) or sha256_file(source) != digests[name]:
                raise DeployError(f"snapshot of {name} changed before upload")
        runner.call(
            f"upload verified staged {name} to the project volume",
            "fs",
            "cp",
            str(source),
            target.volume_uri(name),
            "--overwrite",
            mutating=True,
        )


@dataclass(frozen=True)
class CreateResult:
    """What a create or update call produced.

    ``agent_request_sha256`` is set only when the request file the CLI read was, before
    and after the call, the same regular file with the validated bytes. ``drift`` lists
    why it is not; the Agent may still exist and must then be recorded for cleanup.
    """

    space_id: str | None
    agent_request_sha256: str | None = None
    drift: tuple[str, ...] = ()


def write_request_file(path: Path, data: bytes) -> FileIdentity:
    """Write the exact request bytes to a private, read-only regular file and identify it.

    The file is replaced atomically, never through a symlink, and re-read through a
    no-follow handle; its content must be exactly ``data``.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_private_atomic(path, data, mode=REQUEST_FILE_MODE)
        written = file_identity(path, MAX_REQUEST_BYTES)
    except OSError as exc:
        raise DeployError(f"cannot write {path.name} safely: {exc}") from exc
    if written.sha256 != sha256_bytes(data) or written.size != len(data):
        raise DeployError(f"the written {path.name} differs from the validated request")
    return written


def request_drift(path: Path, written: FileIdentity) -> list[str]:
    """Reasons the request file is no longer the file and bytes that were written."""
    try:
        current = file_identity(path, MAX_REQUEST_BYTES)
    except OSError as exc:
        return [f"{path.name} can no longer be read as the written regular file: {exc}"]
    if (current.device, current.inode) != (written.device, written.inode):
        return [f"{path.name} was replaced by another file"]
    if current != written:
        return [f"{path.name} content changed"]
    return []


def create_agent(
    runner: CliRunner,
    request: Mapping[str, Any],
    work_dir: Path,
    state: Mapping[str, Any] | None,
    *,
    update_existing: bool,
) -> CreateResult:
    """Create (or, with ``update_existing``, update) the Agent from a verified request file.

    The CLI reads the request from a private file whose exact bytes are hashed. With
    --execute the file must be unchanged (same file, same bytes) immediately before the
    call, or nothing is sent; if it changed during the call, the new space is still
    returned for cleanup but without a request receipt.
    """
    title = request["title"]
    if update_existing:
        if not state or state.get("title") != title or not state.get("space_id"):
            raise DeployError("--update-existing needs a recorded space for this exact title")
        space_id = validate_space_id(state["space_id"])
        path = work_dir / "update-space.request.json"
        written = write_request_file(path, request_bytes(update_request(request)))
        if runner.execute and (drift := request_drift(path, written)):
            raise DeployError(f"nothing was sent: {'; '.join(drift)}")
        runner.call(
            "update the recorded Genie Agent",
            "api",
            "patch",
            f"{GENIE_SPACES_PATH}/{space_id}",
            "--json",
            f"@{path}",
            mutating=True,
        )
        return CreateResult(space_id)
    path = work_dir / "create-space.request.json"
    data = request_bytes(request)
    written = write_request_file(path, data)
    if written.sha256 != request_sha256(request):
        raise DeployError("the written create-space request differs from the validated one")
    if runner.execute and (drift := request_drift(path, written)):
        raise DeployError(f"nothing was sent: {'; '.join(drift)}")
    response = runner.call(
        "create the Genie Agent",
        "api",
        "post",
        GENIE_SPACES_PATH,
        "--json",
        f"@{path}",
        mutating=True,
    )
    if response is None:
        return CreateResult(None)
    after = request_drift(path, written)
    try:
        space_id = validate_space_id(response.get("space_id"))
    except DeployError as exc:
        raise DeployError(
            f"create returned no valid space_id; check the Agent titled {title!r} manually"
        ) from exc
    if after:
        return CreateResult(space_id, None, tuple(after))
    return CreateResult(space_id, written.sha256)


def write_state(path: Path, state: Mapping[str, Any]) -> None:
    """Atomically replace the deploy state with a private regular file (never a symlink)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_private_atomic(path, dumps_pretty(dict(state)).encode("utf-8"), mode=STATE_FILE_MODE)
    except OSError as exc:
        raise DeployError(f"cannot write {path.name} safely: {exc}") from exc


def title_for(template: Mapping[str, Any], run_label: str) -> str:
    return agent_title(str(template["title_prefix"]), run_label)


def verify_space_for_cleanup(runner: CliRunner, space_id: str, title: str) -> list[str]:
    space_id = validate_space_id(space_id)
    space = runner.call(
        "read the recorded Agent before trashing", "genie", "get-space", space_id, mutating=False
    )
    if space is None:
        return []
    if space.get("title") != title:
        return [f"space {space_id} title does not match the recorded unique title; not trashing"]
    if space.get("space_id") not in (None, space_id):
        return [f"get-space returned a different space than {space_id}; not trashing"]
    return []


CLEANUP_STATEMENTS = (
    "DROP VIEW IF EXISTS {s}.encounter_operations_metrics",
    "DROP TABLE IF EXISTS {s}.clean_encounters",
    "DROP TABLE IF EXISTS {s}.payer_categories",
    "DROP TABLE IF EXISTS {s}.bronze_encounters",
    "DROP TABLE IF EXISTS {s}.bronze_payer_categories",
    "DROP VOLUME IF EXISTS {s}.{v}",
    "DROP SCHEMA IF EXISTS {s}",
)


def validate_cleanup_statements(text: str, target: Target) -> list[str]:
    """Rendered cleanup SQL must be exactly the allowlisted drops for the validated target.

    Returns the comment-free statements, which are the only ones cleanup runs.
    """
    expected = [s.format(s=target.full_schema, v=target.volume) for s in CLEANUP_STATEMENTS]
    statements = [" ".join(strip_comments(s).split()) for s in split_statements(text)]
    if statements != expected:
        raise DeployError(
            "rendered cleanup SQL differs from the allowlisted project drops; not running it"
        )
    return statements


def request_receipt(
    state: Mapping[str, Any], template: Mapping[str, Any], template_sha256: str
) -> tuple[dict[str, str] | None, list[str]]:
    """Bind an evaluation to the create-space request recorded at deployment.

    The current template must still render, for the recorded target, a request whose
    SHA-256 equals the one deploy_agent.py recorded before sending it. Version 1 states
    and Agents changed with --update-existing carry no receipt and cannot be bound.
    """
    recorded = state.get("agent_request_sha256")
    if state.get("state_version") != STATE_VERSION or not isinstance(recorded, str):
        return None, [
            "the deploy state records no create-space request receipt (deployed before "
            "receipts existed, or changed with --update-existing); deploy a new run label"
        ]
    try:
        request = build_create_request(
            template,
            catalog=state["catalog"],
            schema=state["schema"],
            warehouse_id=state["warehouse_id"],
            run_label=state["run_label"],
            parent_path=state["parent_path"],
        )
    except (KeyError, RenderError, ValueError) as exc:
        return None, [f"the current Agent template cannot render the recorded request: {exc}"]
    if request_sha256(request) != recorded:
        return None, [
            "the current Agent template does not render the create-space request recorded "
            "at deployment; the deployed Agent may not hold the current configuration"
        ]
    return {"agent_request_sha256": recorded, "agent_template_sha256": template_sha256}, []
