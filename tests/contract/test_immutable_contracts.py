from pathlib import Path

from tools.preflight_audit import validate_jcs_hashes, validate_schemas_and_examples


def test_all_schemas_and_examples() -> None:
    root = Path(__file__).resolve().parents[2]
    result = validate_schemas_and_examples(root)
    assert result["schema_count"] == 47
    assert result["contract_instance_count"] == 39
    assert result["config_instance_count"] == 16
    assert result["failures"] == []


def test_all_jcs_examples() -> None:
    root = Path(__file__).resolve().parents[2]
    result = validate_jcs_hashes(root)
    assert result["check_count"] == 26
    assert result["failures"] == []
