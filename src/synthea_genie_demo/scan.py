"""Public-output leak scan.

Fails when committed or to-be-published text contains a private insurer brand, a facility
name, an email address, a workspace URL or identifier, a token-like secret, or a patient
identifier. Private brands are matched by SHA-256 of normalized word n-grams so the brand
strings never need to appear in this repository.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .jsonio import dumps_pretty, sha256_text
from .paths import REPO_ROOT

EXCLUDED_DIRS = {
    ".git",
    ".venv",
    ".cache",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
    ".idea",
    ".vscode",
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
FORBIDDEN_BINARY_SUFFIXES = {".zip", ".gz", ".parquet", ".pdf", ".xlsx"}

FACILITY_SUFFIX = (
    r"(?:Hospital|Medical Center|Medical Centre|Health Center|Health Centre|Clinic|"
    r"Infirmary|Urgent Care|Health System|Medical Group|Healthcare Center)"
)
# A path segment that is a real name, not a placeholder such as <you>, {{catalog}} or ...
_REAL_SEGMENT = r"(?![<{.*$])[A-Za-z0-9._@%~-]+"
REGEX_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "facility_name",
        re.compile(rf"\b(?:[A-Z][A-Za-z'&.-]+ ){{1,5}}{FACILITY_SUFFIX}\b"),
    ),
    (
        "facility_name",
        re.compile(
            r"\b(?:[A-Z][A-Z'&.-]+ ){1,5}(?:HOSPITAL|MEDICAL CENTER|CLINIC|HEALTH CENTER|"
            r"URGENT CARE|INFIRMARY)\b"
        ),
    ),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
    (
        "workspace_url",
        re.compile(
            r"https?://(?!docs\.|www\.|community\.|accounts\.|api-docs\.)[A-Za-z0-9-]+"
            r"(?:\.[A-Za-z0-9-]+)*\.(?:cloud\.databricks\.com|azuredatabricks\.net|"
            r"gcp\.databricks\.com|databricksapps\.com|cloud\.databricks\.us)\b"
        ),
    ),
    ("workspace_url", re.compile(r"\badb-\d{6,}\b")),
    ("workspace_url", re.compile(r"\bdbc-[0-9a-f]{8}-[0-9a-f]{4}\b")),
    ("workspace_id", re.compile(r"[?&]o=\d{6,}\b")),
    (
        "workspace_id",
        re.compile(
            r"\"(?:workspace_id|space_id|warehouse_id|org_id|conversation_id|statement_id|"
            r"message_id)\"\s*:\s*\"(?!<|\{\{|REDACTED)[^\"]+\""
        ),
    ),
    ("workspace_id", re.compile(r"(?<![0-9a-f])[0-9a-f]{16}(?![0-9a-f])")),
    ("local_path", re.compile(rf"(?<![A-Za-z0-9_])/(?:Users|home)/{_REAL_SEGMENT}")),
    ("local_path", re.compile(r"\b[A-Za-z]:\\+(?:Users|Documents and Settings)\\")),
    ("local_path", re.compile(rf"\bfile:/+{_REAL_SEGMENT}")),
    ("workspace_path", re.compile(rf"/Workspace/{_REAL_SEGMENT}")),
    ("volume_path", re.compile(rf"(?:\bdbfs:)?/Volumes/{_REAL_SEGMENT}")),
    ("token_like", re.compile(r"\bdapi[0-9a-f]{32}\b")),
    ("token_like", re.compile(r"\bdose[0-9a-f]{32}\b")),
    ("token_like", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("token_like", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("token_like", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("token_like", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("token_like", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("token_like", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("token_like", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*")),
    ("token_like", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    (
        "patient_identifier",
        re.compile(
            r"(?<![0-9a-fA-F])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{12}(?![0-9a-fA-F])"
        ),
    ),
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.detail}"


def normalize_term(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


# A plain decimal number: at most 15 integer digits, so an ID-sized integer never qualifies.
PLAIN_NUMBER_RE = re.compile(r"-?(?:0|[1-9][0-9]{0,14})(?:\.[0-9]{1,20})?")


def _plain_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        value = json.dumps(value)
    return isinstance(value, str) and PLAIN_NUMBER_RE.fullmatch(value) is not None


def _result_table(value: Mapping[str, Any]) -> bool:
    columns, rows = value.get("columns"), value.get("rows")
    return (
        set(value) == {"columns", "rows"}
        and isinstance(columns, list)
        and all(isinstance(c, str) for c in columns)
        and isinstance(rows, list)
        and all(isinstance(r, list) and len(r) == len(columns) for r in rows)
    )


def mask_numeric_result_cells(value: Any) -> Any:
    """A copy with every cell of a numeric result column replaced by 0.

    A result table is an object with exactly ``columns`` (names) and ``rows`` (lists of
    equal length). A column is numeric when it has a value and every non-null cell is a
    plain decimal number. An unrounded rate with 16 fractional digits then does not trip
    the hex rule, while a 16-digit integer, a prefixed string or any text cell is still
    scanned as raw text.
    """
    if isinstance(value, list):
        return [mask_numeric_result_cells(v) for v in value]
    if not isinstance(value, dict):
        return value
    if not _result_table(value):
        return {k: mask_numeric_result_cells(v) for k, v in value.items()}
    rows = [list(r) for r in value["rows"]]
    for index in range(len(value["columns"])):
        cells = [r[index] for r in rows if r[index] is not None]
        if cells and all(map(_plain_number, cells)):
            for row in rows:
                if row[index] is not None:
                    row[index] = 0
    return {"columns": list(value["columns"]), "rows": rows}


def _mask(value: str) -> str:
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:3]}...{value[-2:]} (len {len(value)})"


class Scanner:
    def __init__(
        self,
        forbidden_term_sha256: Iterable[str],
        *,
        max_ngram_words: int = 4,
        exact_literals: Mapping[str, Iterable[str]] | None = None,
    ) -> None:
        self.forbidden = frozenset(forbidden_term_sha256)
        self.max_ngram = max_ngram_words
        self.literals: list[tuple[str, str]] = []
        for rule, values in (exact_literals or {}).items():
            for value in values:
                if value:
                    self.literals.append((rule, value))
                    if "-" in value:
                        self.literals.append((rule, value.replace("-", "")))

    @classmethod
    def from_contract(
        cls,
        contract: Mapping[str, Any],
        exact_literals: Mapping[str, Iterable[str]] | None = None,
    ) -> Scanner:
        rules = contract["public_output_scan"]
        literals = {k: list(v) for k, v in (exact_literals or {}).items()}
        literals.setdefault("repository_path", []).extend([str(REPO_ROOT), REPO_ROOT.as_uri()])
        return cls(
            rules["forbidden_term_sha256"],
            max_ngram_words=int(rules["max_ngram_words"]),
            exact_literals=literals,
        )

    def scan_text(self, text: str, path: str = "<text>") -> list[Finding]:
        findings: list[Finding] = []
        for number, line in enumerate(text.splitlines(), start=1):
            findings.extend(self._scan_line(line, path, number))
        return findings

    def scan_json(self, value: Any, path: str = "<json>") -> list[Finding]:
        """Scan a JSON document that may hold result tables.

        Every pattern runs on the document with numeric result cells masked (see
        ``mask_numeric_result_cells``); exact local values are also checked against the
        unmasked text, so they are found even inside a number.
        """
        findings = self.scan_text(dumps_pretty(mask_numeric_result_cells(value)), path)
        for number, line in enumerate(dumps_pretty(value).splitlines(), start=1):
            findings.extend(self._literal_findings(line, path, number))
        return list(dict.fromkeys(findings))

    def _literal_findings(self, line: str, path: str, number: int) -> list[Finding]:
        return [
            Finding(path, number, rule, "exact local value " + _mask(literal))
            for rule, literal in self.literals
            if literal in line
        ]

    def _scan_line(self, line: str, path: str, number: int) -> list[Finding]:
        findings: list[Finding] = []
        tokens = re.findall(r"[a-z0-9]+", line.casefold())
        for size in range(1, self.max_ngram + 1):
            for start in range(len(tokens) - size + 1):
                gram = " ".join(tokens[start : start + size])
                if sha256_text(gram) in self.forbidden:
                    findings.append(
                        Finding(
                            path, number, "private_payer_brand", f"hashed term match ({size} words)"
                        )
                    )
        for rule, pattern in REGEX_RULES:
            for match in pattern.finditer(line):
                findings.append(Finding(path, number, rule, _mask(match.group(0))))
        findings.extend(self._literal_findings(line, path, number))
        return findings

    def scan_file(self, path: Path, root: Path) -> list[Finding]:
        relative = path.relative_to(root).as_posix()
        suffix = path.suffix.lower()
        if suffix in FORBIDDEN_BINARY_SUFFIXES:
            return [
                Finding(relative, 0, "forbidden_file_type", f"{suffix} files must not be published")
            ]
        if suffix in IMAGE_SUFFIXES:
            return []
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return [Finding(relative, 0, "binary_file", "non-UTF-8 file needs manual review")]
        return self.scan_text(text, relative)

    def scan_tree(self, root: Path) -> list[Finding]:
        findings: list[Finding] = []
        for path in sorted(iter_public_files(root)):
            findings.extend(self.scan_file(path, root))
        return findings


def iter_public_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        parts = path.relative_to(root).parts
        if any(part in EXCLUDED_DIRS or part.endswith(".egg-info") for part in parts[:-1]):
            continue
        if path.name == ".DS_Store":
            continue
        yield path


def image_files(root: Path) -> list[str]:
    """Images cannot be scanned automatically; they need a manual redaction review."""
    return sorted(
        p.relative_to(root).as_posix()
        for p in iter_public_files(root)
        if p.suffix.lower() in IMAGE_SUFFIXES
    )
