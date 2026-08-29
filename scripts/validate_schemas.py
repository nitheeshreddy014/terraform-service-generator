#!/usr/bin/env python3
"""
scripts/validate_schemas.py
---------------------------
Automated validation of every file under generated-schemas/.

Checks
------
 1. Every .json.gz can be decompressed.
 2. Every decompressed file contains valid JSON.
 3. Every file has the expected Terraform provider_schemas envelope.
 4. Every service listed in manifest.json has a corresponding .json.gz file.
 5. Every .json.gz file has a corresponding manifest entry.
 6. No duplicate provider/service mapping.
 7. No empty schema (0 resources + 0 data sources).
 8. Resource counts in manifest.json match actual resource counts.
 9. Paths in manifest.json stay inside generated-schemas/.
10. Provider names are limited to: aws, azurerm, google.
11. Service names/paths contain no path traversal sequences.
12. At least one rich service per provider passes the full round-trip:
    gzip → JSON → extract_service_resources() → generate_service_folder()
    → variables.tf / main.tf / outputs.tf non-empty.

Exit codes
----------
  0  – all checks PASSED
  1  – one or more checks FAILED
"""

from __future__ import annotations

import gzip
import json
import sys
import tempfile
import traceback
from pathlib import Path

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Bootstrap so generator package is importable regardless of CWD
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_PKG_ROOT   = _SCRIPT_DIR.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from generator.schema_parser import extract_service_resources
from generator.file_generator import generate_service_folder

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCHEMAS_DIR      = _PKG_ROOT / "generated-schemas"
MANIFEST_PATH    = SCHEMAS_DIR / "manifest.json"
ALLOWED_PROVIDERS = {"aws", "azurerm", "google"}

# Rich services to use for the round-trip validation (one per provider)
ROUND_TRIP_SERVICES = {
    "aws":     "s3",
    "azurerm": "storage",
    "google":  "compute",
}


# ===========================================================================
# Helpers
# ===========================================================================

def _resolve_file_path(raw_path: str) -> Path:
    """
    Convert a manifest file path (may use either / or \\ as separator)
    to an absolute Path relative to _PKG_ROOT.
    """
    normalised = raw_path.replace("\\", "/")
    return (_PKG_ROOT / normalised).resolve()


def _path_is_inside(p: Path, root: Path) -> bool:
    """Return True iff *p* is inside *root* (no traversal)."""
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def _load_gz_json(gz_path: Path) -> tuple[dict | None, str | None]:
    """
    Decompress and parse a .json.gz file.
    Returns (parsed_dict, None) on success or (None, error_message) on failure.
    """
    try:
        with gzip.open(gz_path, "rb") as gf:
            raw = gf.read()
    except Exception as exc:
        return None, f"gzip error: {exc}"

    if not raw:
        return None, "gzip file is empty after decompression"

    try:
        return json.loads(raw.decode("utf-8")), None
    except Exception as exc:
        return None, f"JSON parse error: {exc}"


def _count_resources(schema: dict) -> int:
    """Count total resources + data-sources across all provider blocks."""
    total = 0
    for pb in schema.get("provider_schemas", {}).values():
        total += len(pb.get("resource_schemas", {}))
        total += len(pb.get("data_source_schemas", {}))
    return total


def _has_schema_envelope(schema: dict) -> bool:
    """Return True iff the parsed dict has a valid provider_schemas envelope."""
    return (
        isinstance(schema, dict)
        and "provider_schemas" in schema
        and isinstance(schema["provider_schemas"], dict)
        and len(schema["provider_schemas"]) > 0
    )


# ===========================================================================
# Main validation
# ===========================================================================

def validate_all() -> int:
    """
    Run all validation checks.
    Returns 0 if PASS, 1 if FAIL.
    Prints a structured report to stdout.
    """
    bar  = "=" * 72
    bar2 = "-" * 72

    print(f"\n{bar}")
    print("  SCHEMA VALIDATION  —  generated-schemas/")
    print(bar)

    # ── Load manifest ────────────────────────────────────────────────────────
    if not MANIFEST_PATH.exists():
        print(f"\n  FATAL: manifest.json not found at {MANIFEST_PATH}")
        print(f"\n  RESULT: FAIL\n{bar}")
        return 1

    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"\n  FATAL: manifest.json is not valid JSON: {exc}")
        print(f"\n  RESULT: FAIL\n{bar}")
        return 1

    providers_in_manifest: dict[str, dict] = manifest.get("providers", {})

    # ── Collect every .json.gz on disk ───────────────────────────────────────
    all_gz_on_disk: set[Path] = set(SCHEMAS_DIR.rglob("*.json.gz"))

    # ── Counters ─────────────────────────────────────────────────────────────
    total_checked         = 0
    passed                = 0
    failed                = 0
    per_provider: dict[str, int] = {}

    invalid_gzip          : list[str] = []
    invalid_json          : list[str] = []
    empty_schemas         : list[str] = []
    bad_envelope          : list[str] = []
    count_mismatches      : list[str] = []
    path_traversal_issues : list[str] = []
    missing_files         : list[str] = []
    missing_manifest_entries: list[str] = []
    unknown_providers     : list[str] = []
    duplicate_keys        : list[str] = []

    # Track every (provider, service) pair seen in manifest
    manifest_pairs: set[tuple[str, str]] = set()
    # Track every resolved path referenced by manifest
    manifest_paths: set[Path] = set()

    # ── Check 10: provider names ─────────────────────────────────────────────
    for pname in providers_in_manifest:
        if pname not in ALLOWED_PROVIDERS:
            unknown_providers.append(pname)

    # ── Per-provider, per-service checks ─────────────────────────────────────
    for pname, pdata in providers_in_manifest.items():
        services: dict[str, dict] = pdata.get("services", {})
        per_provider[pname] = 0

        for svc_name, svc_entry in services.items():
            total_checked += 1
            per_provider[pname] += 1
            key = (pname, svc_name)

            # Check 6: duplicates
            if key in manifest_pairs:
                duplicate_keys.append(f"{pname}/{svc_name}")
            manifest_pairs.add(key)

            # Check 11: path traversal in service name
            if ".." in svc_name or "/" in svc_name or "\\" in svc_name:
                path_traversal_issues.append(f"{pname}/{svc_name} (service name)")

            raw_file = svc_entry.get("file", "")
            # Check 11: path traversal in file path
            if ".." in raw_file:
                path_traversal_issues.append(f"{pname}/{svc_name} path '{raw_file}'")

            resolved = _resolve_file_path(raw_file)
            manifest_paths.add(resolved)

            # Check 9: path stays inside generated-schemas/
            if not _path_is_inside(resolved, SCHEMAS_DIR.resolve()):
                path_traversal_issues.append(
                    f"{pname}/{svc_name} escapes generated-schemas/: {raw_file}"
                )

            # Check 4: file exists
            if not resolved.exists():
                missing_files.append(f"{pname}/{svc_name} → {raw_file}")
                failed += 1
                continue

            # Checks 1 & 2: gzip + JSON
            schema, err = _load_gz_json(resolved)
            if err:
                if "gzip" in err:
                    invalid_gzip.append(f"{pname}/{svc_name}: {err}")
                else:
                    invalid_json.append(f"{pname}/{svc_name}: {err}")
                failed += 1
                continue

            # Check 3: envelope
            if not _has_schema_envelope(schema):
                bad_envelope.append(f"{pname}/{svc_name}")
                failed += 1
                continue

            # Check 7: not empty
            actual_count = _count_resources(schema)
            if actual_count == 0:
                empty_schemas.append(f"{pname}/{svc_name}")
                failed += 1
                continue

            # Check 8: resource count matches manifest
            manifest_count = svc_entry.get("resource_count", -1)
            if manifest_count != actual_count:
                count_mismatches.append(
                    f"{pname}/{svc_name}: manifest={manifest_count}, actual={actual_count}"
                )
                failed += 1
                continue

            passed += 1

    # ── Check 5: every .json.gz on disk has a manifest entry ─────────────────
    for gz_path in sorted(all_gz_on_disk):
        if gz_path not in manifest_paths:
            missing_manifest_entries.append(str(gz_path.relative_to(_PKG_ROOT)))

    # ── Check 12: round-trip validation for one service per provider ──────────
    round_trip_results: dict[str, dict] = {}
    for pname, svc_name in ROUND_TRIP_SERVICES.items():
        if pname not in providers_in_manifest:
            round_trip_results[pname] = {"status": "SKIP", "reason": "provider not in manifest"}
            continue

        services = providers_in_manifest[pname].get("services", {})

        # Fall back to largest service if preferred service not present
        if svc_name not in services:
            if not services:
                round_trip_results[pname] = {"status": "SKIP", "reason": "no services"}
                continue
            svc_name = max(services, key=lambda s: services[s].get("resource_count", 0))

        entry    = services[svc_name]
        gz_path  = _resolve_file_path(entry["file"])

        try:
            schema, err = _load_gz_json(gz_path)
            if err:
                raise ValueError(f"Load error: {err}")

            resources = extract_service_resources(schema, pname, svc_name)
            if not resources:
                raise ValueError("extract_service_resources() returned 0 resources")

            r0 = resources[0]
            for k in ("name", "kind", "attributes", "block_types"):
                if k not in r0:
                    raise ValueError(f"Resource dict missing key '{k}'")

            pdata_m   = providers_in_manifest[pname]
            namespace = pdata_m.get("namespace", "hashicorp")
            version   = pdata_m.get("version", "0.0.0")
            pmeta     = {"namespace": namespace, "type": pname, "version": version}

            with tempfile.TemporaryDirectory(prefix="tfgen_rtval_") as tmpdir:
                svc_root = generate_service_folder(
                    base_dir=tmpdir,
                    provider_name=pname,
                    service_name=svc_name,
                    resources=resources,
                    provider_meta=pmeta,
                )
                modules_dir = svc_root / "modules"
                mod_dirs    = [d for d in modules_dir.iterdir() if d.is_dir()] if modules_dir.exists() else []
                if not mod_dirs:
                    raise ValueError("generate_service_folder() produced no module directories")

                for expected_file in ("variables.tf", "main.tf", "outputs.tf"):
                    fp = mod_dirs[0] / expected_file
                    if not fp.exists():
                        raise ValueError(f"Missing generated file: {expected_file}")
                    if fp.stat().st_size == 0:
                        raise ValueError(f"Empty generated file: {expected_file}")

            round_trip_results[pname] = {
                "status":    "PASS",
                "service":   svc_name,
                "resources": len(resources),
                "modules":   len(mod_dirs),
            }
        except Exception as exc:
            round_trip_results[pname] = {
                "status": "FAIL",
                "service": svc_name,
                "error":  str(exc),
            }
            failed += 1

    # ════════════════════════════════════════════════════════════════════════
    # Report
    # ════════════════════════════════════════════════════════════════════════
    print(f"\n  Manifest path : {MANIFEST_PATH}")
    print(f"  Total .json.gz on disk : {len(all_gz_on_disk)}")
    print(f"  Total checked (manifest entries) : {total_checked}")

    print(f"\n  Files checked per provider:")
    for pname, cnt in sorted(per_provider.items()):
        print(f"    {pname:<10} : {cnt}")

    print(f"\n  Passed  : {passed}")
    print(f"  Failed  : {failed}")

    def _print_issues(label: str, items: list[str]) -> None:
        if items:
            print(f"\n  {label} ({len(items)}):")
            for it in items[:20]:
                print(f"    • {it}")
            if len(items) > 20:
                print(f"    … and {len(items) - 20} more")

    _print_issues("Missing files (in manifest but not on disk)", missing_files)
    _print_issues("Missing manifest entries (on disk but not in manifest)", missing_manifest_entries)
    _print_issues("Invalid gzip files", invalid_gzip)
    _print_issues("Invalid JSON files", invalid_json)
    _print_issues("Bad schema envelope (no provider_schemas)", bad_envelope)
    _print_issues("Empty service schemas (0 resources)", empty_schemas)
    _print_issues("Resource count mismatches", count_mismatches)
    _print_issues("Path traversal issues", path_traversal_issues)
    _print_issues("Unknown providers (not aws/azurerm/google)", unknown_providers)
    _print_issues("Duplicate provider/service keys", duplicate_keys)

    # Round-trip results
    print(f"\n{bar2}")
    print("  Round-trip validation (gzip -> → JSON → extract → generate):")
    rt_failed = False
    for pname, res in sorted(round_trip_results.items()):
        status = res["status"]
        if status == "PASS":
            print(
                f"    {pname:<10} PASS  service={res['service']}"
                f"  resources={res['resources']}  modules={res['modules']}"
            )
        elif status == "FAIL":
            print(f"    {pname:<10} FAIL  service={res.get('service','?')}  error={res.get('error','?')}")
            rt_failed = True
        else:
            print(f"    {pname:<10} SKIP  {res.get('reason','')}")

    # Overall result
    all_issues = (
        missing_files + invalid_gzip + invalid_json + bad_envelope
        + empty_schemas + count_mismatches + path_traversal_issues
        + unknown_providers + duplicate_keys + missing_manifest_entries
    )
    overall_fail = bool(all_issues) or rt_failed or (failed > 0)

    print(f"\n{bar}")
    if overall_fail:
        print("  RESULT: FAIL")
    else:
        print("  RESULT: PASS — all schema files validated successfully")
    print(f"{bar}\n")

    return 1 if overall_fail else 0


# ===========================================================================
# Entry point
# ===========================================================================
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Validate generated-schemas/ directory.")
    ap.add_argument(
        "--schemas-dir",
        default=None,
        help="Override path to schemas root (default: <repo>/generated-schemas/).",
    )
    _args = ap.parse_args()
    if _args.schemas_dir:
        SCHEMAS_DIR   = Path(_args.schemas_dir).resolve()
        MANIFEST_PATH = SCHEMAS_DIR / "manifest.json"
    sys.exit(validate_all())
