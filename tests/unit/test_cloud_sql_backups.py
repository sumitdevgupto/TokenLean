"""The Cloud SQL instance keeps backups and point-in-time recovery unless told not to.

It holds usage_events (the billing source of record), audit_events, tenant configuration,
tenants' provider keys and portal users, and it was created with neither: a bad migration,
a stray DELETE or a lost zone could not be undone.
"""
import re
from pathlib import Path

_INFRA = Path(__file__).resolve().parents[2] / "infra"


def _block(text: str, header: str) -> str:
    """The brace-balanced block that opens at ``header``."""
    assert header in text, f"no {header}"
    start = text.index(header)
    depth = 0
    for j in range(text.index("{", start), len(text)):
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        if depth == 0:
            return text[start:j + 1]
    raise AssertionError(f"unbalanced block at {header}")


def _default(variable: str) -> str:
    block = _block((_INFRA / "variables.tf").read_text(encoding="utf-8"),
                   f'variable "{variable}"')
    match = re.search(r"^\s*default\s*=\s*(.+?)\s*$", block, re.M)
    assert match, f"{variable} has no default"
    return match.group(1)


def _backup_block() -> str:
    instance = _block((_INFRA / "main.tf").read_text(encoding="utf-8"),
                      'resource "google_sql_database_instance" "main"')
    return _block(instance, "backup_configuration")


def test_backups_and_point_in_time_recovery_are_on_by_default():
    backup = _backup_block()
    assert re.search(r"^\s*enabled\s*=\s*var\.db_backups\s*$", backup, re.M)
    assert re.search(r"point_in_time_recovery_enabled\s*=\s*var\.db_backups\s*&&\s*"
                     r"var\.db_point_in_time_recovery\b", backup)
    assert _default("db_backups") == "true"
    assert _default("db_point_in_time_recovery") == "true"


def test_backups_are_kept_for_a_week():
    backup = _backup_block()
    retention = _block(backup, "backup_retention_settings")
    assert re.search(r"retained_backups\s*=\s*var\.db_backup_retained_count\b", retention)
    assert int(_default("db_backup_retained_count")) >= 7
    assert re.search(r"transaction_log_retention_days\s*=\s*var\.db_transaction_log_days\b", backup)
    assert 1 <= int(_default("db_transaction_log_days")) <= 7      # the API's range
