"""No `except Exception:` (or bare `except:`) under src/ swallows an error without a word.

Dozens of handlers were `except Exception: pass` (or `continue`): a failed session revoke, a
tenant config cache that was not refreshed, a login limiter that stopped counting, a metric that
never recorded, all left no trace. Each now logs what was skipped (DEBUG, or WARNING where the
failure leaves something the caller relies on undone). A handler that names a narrower exception
(`except ImportError: pass`) is a deliberate choice and is not counted, as bandit does not count it.
"""
import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"


def _silent_handlers(source: str):
    """Line numbers of the bare / `Exception` handlers whose body is only pass or continue."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ExceptHandler):
            continue
        broad = node.type is None or (isinstance(node.type, ast.Name)
                                      and node.type.id in ("Exception", "BaseException"))
        if broad and all(isinstance(s, (ast.Pass, ast.Continue)) for s in node.body):
            found.append(node.lineno)
    return found


@pytest.mark.parametrize("snippet,silent", [
    ("try:\n    f()\nexcept Exception:\n    pass\n", True),
    ("for x in y:\n    try:\n        f()\n    except Exception:\n        continue\n", True),
    ("try:\n    f()\nexcept:\n    pass\n", True),
    ("try:\n    f()\nexcept Exception as exc:\n    logger.debug('f failed: %r', exc)\n", False),
    ("try:\n    import g\nexcept ImportError:\n    pass\n", False),
], ids=["pass", "continue", "bare", "logged", "narrow"])
def test_the_check_tells_a_silent_handler_from_a_logged_one(snippet, silent):
    assert bool(_silent_handlers(snippet)) is silent


def test_no_handler_under_src_swallows_an_error_silently():
    offenders = [f"{path.relative_to(SRC.parent).as_posix()}:{line}"
                 for path in sorted(SRC.rglob("*.py"))
                 for line in _silent_handlers(path.read_text(encoding="utf-8"))]
    assert not offenders, offenders
