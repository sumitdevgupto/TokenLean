"""Every task the proxy starts is held until it finishes.

The event loop keeps only a weak reference to a task. A task that nothing else references can
be garbage-collected before it finishes, dropping the work it was started for (a cache
invalidation, an audit row), and it is cancelled without a trace at shutdown. So a
``create_task`` or ``ensure_future`` result must be kept, in a set or a list, not discarded.
"""
import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"


def _discarded_tasks(source: str):
    """Line numbers of ``create_task(...)`` / ``ensure_future(...)`` calls whose result is
    thrown away, i.e. calls standing alone as a statement."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in ("create_task", "ensure_future"):
                lines.append(node.lineno)
    return lines


def test_no_task_is_started_without_keeping_a_reference():
    scanned, found = 0, []
    for path in sorted(_SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        scanned += 1
        for line in _discarded_tasks(path.read_text(encoding="utf-8", errors="replace")):
            found.append(f"{path.relative_to(_SRC).as_posix()}:{line}")
    assert scanned > 100                              # the scan reached the source tree
    assert not found, (
        f"{found}: these tasks are started and then dropped. Keep each one (add it to a set "
        "and discard it in a done callback) so it cannot be collected before it finishes.")


@pytest.mark.parametrize("statement", [
    "asyncio.create_task(work())",
    "loop.create_task(work())",
    "create_task(work())",
    "asyncio.ensure_future(work())",
])
def test_a_dropped_task_is_reported(statement):
    assert _discarded_tasks(f"async def f():\n    {statement}\n") == [2]


@pytest.mark.parametrize("statement", [
    "task = asyncio.create_task(work())",
    "tasks.add(asyncio.create_task(work()))",
    "await asyncio.create_task(work())",
    "return asyncio.ensure_future(work())",
])
def test_a_kept_or_awaited_task_is_not_reported(statement):
    assert _discarded_tasks(f"async def f():\n    {statement}\n") == []
