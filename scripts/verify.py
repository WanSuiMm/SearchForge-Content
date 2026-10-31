"""Run only the affected fresh-database CPU/loopback checks and save a receipt."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "components/searchforge/src"),
               str(ROOT / "components/content-platform/src")]
TEST_FILES = (
    ("source_projection", "components/content-platform/tests/test_projection.py"),
    ("source_platform", "components/content-platform/tests/test_platform.py"),
    ("source_postgres_shape", "components/content-platform/tests/test_postgres_storage.py"),
    ("source_http", "components/content-platform/tests/test_http_service.py"),
    ("target_projection", "components/searchforge/tests/test_community_projection.py"),
    ("cross_service", "tests/test_integration.py"),
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new receipt path; never overwrite")
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("receipt already exists; select a new output path")
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    groups = []
    for name, relative in TEST_FILES:
        spec = importlib.util.spec_from_file_location("c3check_" + name, ROOT / relative)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        tests = loader.loadTestsFromModule(module)
        groups.append({"name": name, "file": relative, "tests": tests.countTestCases()})
        suite.addTests(tests)
    started = time.monotonic()
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=1).run(suite)
    hashes = {}
    for folder in (ROOT / "src", ROOT / "scripts", ROOT / "contracts",
                   ROOT / "components/searchforge/src/searchforge/community"):
        for path in sorted(folder.rglob("*")):
            if path.is_file() and path.suffix in {".py", ".json"}:
                hashes[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    for _, relative in TEST_FILES:
        hashes[relative] = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
    for relative in ("projection.py", "api/http_service.py", "storage/repositories.py", "storage/migrations.py"):
        path = ROOT / "components/content-platform/src/content_platform" / relative
        hashes[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    receipt = {
        "status": "LOCAL_HTTP_FUNCTIONAL_PASS" if result.wasSuccessful() and not result.skipped else "CHECK_FAILED_OR_SKIPPED",
        "verified_at": datetime.now(timezone.utc).isoformat(), "duration_seconds": round(time.monotonic() - started, 3),
        "tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "skips": len(result.skipped), "groups": groups, "artifact_sha256": hashes,
        "scope": "Synthetic public text, fresh SQLite databases, real loopback HTTP transports, bounded CPU checks",
        "initial_integration_attempt": "A test incorrectly sent a JSON body to the existing empty-body like endpoint; test corrected to the documented API contract before this receipt.",
        "models_called": 0, "training_started": False, "large_evaluation_started": False,
        "historical_databases_opened": False, "postgres_live": False, "kafka_live": False,
        "opensearch_live": False, "search_quality_measured": False, "capacity_measured": False,
        "source_withdrawal_implemented": False, "resume_edits": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({key: receipt[key] for key in ("status", "tests", "failures", "errors", "skips", "duration_seconds")}))
    return 0 if receipt["status"] == "LOCAL_HTTP_FUNCTIONAL_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
