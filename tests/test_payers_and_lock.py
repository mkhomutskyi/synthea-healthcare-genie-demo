from __future__ import annotations

import copy
from typing import Any

import pytest
from conftest import FICTIONAL_PRIVATE_NAMES, PUBLIC_NAMES

from synthea_genie_demo.artifact import OFFICIAL_SHA256
from synthea_genie_demo.checks import (
    check_artifact_lock,
    check_schema_contract,
)
from synthea_genie_demo.checks import (
    contract as real_contract,
)
from synthea_genie_demo.checks import (
    lock as real_lock,
)
from synthea_genie_demo.payers import PayerMapping, PayerMappingError


def test_exact_public_mapping(contract: dict[str, Any]) -> None:
    mapping = PayerMapping.from_contract(contract)
    result = mapping.map_names([*PUBLIC_NAMES, *FICTIONAL_PRIVATE_NAMES])
    assert result["Medicare"] == "Medicare"
    assert result["Medicaid"] == "Medicaid"
    assert result["Dual Eligible"] == "Dual Eligible"
    assert result["NO_INSURANCE"] == "No Insurance"
    assert {result[n] for n in FICTIONAL_PRIVATE_NAMES} == {"Private Insurance"}


@pytest.mark.parametrize("name", ["medicare", "Medicare ", "No Insurance", "Unknown Payer"])
def test_inexact_or_unknown_names_fail(contract: dict[str, Any], name: str) -> None:
    mapping = PayerMapping.from_contract(contract)
    with pytest.raises(PayerMappingError) as info:
        mapping.map_names([*PUBLIC_NAMES, *FICTIONAL_PRIVATE_NAMES, name])
    assert "unmapped" in str(info.value)
    assert name not in str(info.value)


def test_missing_expected_names_fail(contract: dict[str, Any]) -> None:
    mapping = PayerMapping.from_contract(contract)
    with pytest.raises(PayerMappingError):
        mapping.map_names([*PUBLIC_NAMES, *FICTIONAL_PRIVATE_NAMES[:-1]])
    with pytest.raises(PayerMappingError):
        mapping.map_names([*PUBLIC_NAMES[1:], *FICTIONAL_PRIVATE_NAMES])


def test_duplicate_names_fail(contract: dict[str, Any]) -> None:
    mapping = PayerMapping.from_contract(contract)
    with pytest.raises(PayerMappingError):
        mapping.map_names([*PUBLIC_NAMES, *FICTIONAL_PRIVATE_NAMES, "Medicare"])


def test_error_messages_never_echo_private_names(contract: dict[str, Any]) -> None:
    mapping = PayerMapping.from_contract(contract)
    secret = "Secret Brand Name For Test"
    with pytest.raises(PayerMappingError) as info:
        mapping.category_for(secret)
    assert secret not in str(info.value)


def test_real_contract_shape() -> None:
    c = real_contract()
    mapping = PayerMapping.from_contract(c)
    assert len(mapping.private_name_sha256) == 6
    assert check_schema_contract(c) == []
    with pytest.raises(PayerMappingError):
        mapping.category_for("Some Private Insurer")


def test_contract_count_mismatch_rejected() -> None:
    c = copy.deepcopy(real_contract())
    c["payer_mapping"]["private_name_count"] = 5
    with pytest.raises(PayerMappingError):
        PayerMapping.from_contract(c)


def test_real_lock_is_consistent() -> None:
    lk = real_lock()
    assert lk["artifact"]["sha256"] == OFFICIAL_SHA256
    assert check_artifact_lock(lk, real_contract()) == []
    assert (
        lk["used_members"]["encounters.csv"]["row_count"]
        == lk["observed"]["encounters"]["row_count"]
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda lk: lk["artifact"].update(sha256="0" * 64),
        lambda lk: lk["artifact"].update(source_url="https://example.invalid/x.zip"),
        lambda lk: lk["observed"]["encounters"]["encounter_class_counts"].update(extra=1),
        lambda lk: lk["observed"]["encounters"]["rows_by_payer_category"].update(Other=1),
        lambda lk: lk["observed"]["payers"].update(private_name_count=5),
        lambda lk: lk["observed"]["encounters"].update(start_date_max="2026-09-01"),
        lambda lk: lk.update(used_members={}),
    ],
)
def test_lock_mutations_fail(mutate: Any) -> None:
    lk = copy.deepcopy(real_lock())
    mutate(lk)
    assert check_artifact_lock(lk, real_contract())


def test_contract_measure_tampering_fails() -> None:
    c = copy.deepcopy(real_contract())
    c["metric_view"]["measures"][5]["expr"] = "AVG(source.payer_coverage)"
    assert check_schema_contract(c)
