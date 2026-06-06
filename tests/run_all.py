"""Run the whole offline test suite in one process — one green/red signal.

The repo deliberately has no pytest dependency (see MEMORY); each test file is import-runnable,
but two used to lack a __main__ block and there was no single entry point. This is it:

    direnv exec . uv run python tests/run_all.py

It points the job/pin store at a temp dir (so tests never touch results/), imports every
tests/test_*.py, runs each module's `test_*` callables, and exits non-zero if any fail.
"""
import importlib
import os
import sys
import tempfile
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))            # repo root (for `import pudding`, engine modules)
sys.path.insert(0, str(_HERE))                   # tests/ (so `import test_jobs` resolves)

# Isolate persistence BEFORE any test imports pudding/store (store reads these at import time).
os.environ.setdefault("PUDDING_JOBS_DIR", tempfile.mkdtemp(prefix="pudding-jobs-"))
os.environ.setdefault("PUDDING_PINS_DIR", tempfile.mkdtemp(prefix="pudding-pins-"))


def main() -> int:
    files = sorted(p.stem for p in _HERE.glob("test_*.py"))
    passed = failed = 0
    failures: list[str] = []
    for mod_name in files:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:                        # noqa: BLE001 — a bad import is a failure, not a crash
            failed += 1
            failures.append(f"{mod_name} (import)")
            print(f"FAIL {mod_name} — import error")
            traceback.print_exc()
            continue
        fns = [v for k, v in sorted(vars(mod).items())
               if k.startswith("test_") and callable(v)]
        ok = 0
        for fn in fns:
            try:
                fn()
                ok += 1
            except Exception:                    # noqa: BLE001
                failed += 1
                failures.append(f"{mod_name}.{fn.__name__}")
                print(f"FAIL {mod_name}.{fn.__name__}")
                traceback.print_exc()
        passed += ok
        print(f"  {mod_name}: {ok}/{len(fns)} passed")
    print(f"\n{'=' * 48}\nTOTAL: {passed} passed, {failed} failed")
    if failures:
        print("failed: " + ", ".join(failures))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
