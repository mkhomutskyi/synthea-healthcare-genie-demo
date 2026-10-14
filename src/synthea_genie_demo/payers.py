"""Exact, fail-closed mapping from raw payer names to display-safe payer categories."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .jsonio import sha256_text


class PayerMappingError(ValueError):
    """Raised when source payer names do not match the frozen contract exactly."""


def _describe(name: str) -> str:
    # Never echo a raw payer name; it may be a private brand.
    return f"sha256={sha256_text(name)[:16]}... (length {len(name)})"


@dataclass(frozen=True)
class PayerMapping:
    public_program_labels: Mapping[str, str]
    private_label: str
    private_name_sha256: frozenset[str]
    categories: tuple[str, ...]

    @classmethod
    def from_contract(cls, contract: Mapping[str, Any]) -> PayerMapping:
        section = contract["payer_mapping"]
        hashes = frozenset(section["private_name_sha256"])
        if len(hashes) != section["private_name_count"]:
            raise PayerMappingError("Contract private_name_count does not match the digest list.")
        mapping = cls(
            public_program_labels=dict(section["public_program_labels"]),
            private_label=section["private_label"],
            private_name_sha256=hashes,
            categories=tuple(contract["payer_categories"]),
        )
        expected = sorted({*mapping.public_program_labels.values(), mapping.private_label})
        if expected != sorted(mapping.categories):
            raise PayerMappingError("Contract payer_categories do not match the mapping labels.")
        return mapping

    def category_for(self, name: str) -> str:
        if name in self.public_program_labels:
            return self.public_program_labels[name]
        if sha256_text(name) in self.private_name_sha256:
            return self.private_label
        raise PayerMappingError(f"Unmapped source payer name: {_describe(name)}")

    def map_names(self, names: Iterable[str]) -> dict[str, str]:
        """Map every observed name, requiring the observed set to equal the contract set."""
        observed = list(names)
        if len(set(observed)) != len(observed):
            raise PayerMappingError("Duplicate payer names in source; mapping must be one-to-one.")
        unknown = [n for n in observed if self._is_unknown(n)]
        if unknown:
            details = ", ".join(_describe(n) for n in unknown)
            raise PayerMappingError(f"{len(unknown)} unmapped source payer name(s): {details}")
        missing_public = sorted(set(self.public_program_labels) - set(observed))
        observed_private = {sha256_text(n) for n in observed} & self.private_name_sha256
        missing_private = len(self.private_name_sha256 - observed_private)
        if missing_public or missing_private:
            raise PayerMappingError(
                "Source payers drifted from the contract: "
                f"missing public program names {missing_public}, "
                f"missing private names {missing_private}."
            )
        return {n: self.category_for(n) for n in observed}

    def _is_unknown(self, name: str) -> bool:
        return (
            name not in self.public_program_labels
            and sha256_text(name) not in self.private_name_sha256
        )
