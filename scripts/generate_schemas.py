#!/usr/bin/env python3
"""
scripts/generate_schemas.py
---------------------------
Pre-generates per-service schema files for:
    hashicorp/google   (original logic fully preserved)
    hashicorp/aws
    hashicorp/azurerm

Workflow (per provider)
-----------------------
1.  Resolve the latest stable version from the Terraform public registry.
2.  Create a temporary Terraform workspace.
3.  Run `terraform init -backend=false`.
4.  Run `terraform providers schema -json`.
5.  Split the full schema into one .json.gz file per service.
6.  Store files under  generated-schemas/<provider>/
7.  Write a combined generated-schemas/manifest.json covering all providers.
8.  Validate the largest service file for each provider through the full
    extract_service_resources() -> generate_service_folder() round-trip.
9.  Print per-provider measurement reports.
10. Print the combined compressed size and a Vercel-bundling verdict.

Usage
-----
    cd terraform-generator
    python scripts/generate_schemas.py

Restrictions honoured
---------------------
- Does NOT modify main.py, generator/provider.py, vercel.json,
  Dockerfile, .dockerignore, or any application code.
- Does NOT commit or push anything.
- Does NOT implement Vercel schema loading.
- Does NOT create the weekly GitHub Actions workflow.
"""

from __future__ import annotations

import gzip
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Ensure the generator package is importable regardless of CWD
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent   # .../terraform-generator/scripts/
_PKG_ROOT   = _SCRIPT_DIR.parent               # .../terraform-generator/
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

# Import from existing generator code (read-only -- no modifications)
from generator.schema_parser import (
    list_available_services,
    extract_service_resources,
    _find_provider_key,
)
from generator.file_generator import generate_service_folder

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Fallback list if providers.yml is missing
_PROVIDERS_FALLBACK = [
    {"namespace": "hashicorp", "type": "google"},
    {"namespace": "hashicorp", "type": "aws"},
    {"namespace": "hashicorp", "type": "azurerm"},
]


def _load_providers_config(filter_types: set[str] | None = None) -> list[dict]:
    """
    Load providers from providers.yml next to the package root.
    If the file is missing or unreadable, fall back to the hardcoded list.
    Optionally filter by provider type names.
    """
    config_path = _PKG_ROOT / "providers.yml"
    try:
        import yaml
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        providers = config.get("providers", [])
        log.info("Loaded %d provider(s) from %s", len(providers), config_path)
    except Exception as exc:
        log.warning("Could not load providers.yml (%s) — using fallback list.", exc)
        providers = _PROVIDERS_FALLBACK

    if filter_types:
        providers = [p for p in providers if p["type"] in filter_types]
    return providers

OUTPUT_ROOT = _PKG_ROOT / "generated-schemas"   # wiped & recreated at start
MANIFEST    = OUTPUT_ROOT / "manifest.json"

REGISTRY_V2 = "https://registry.terraform.io/v2"
REGISTRY_V1 = "https://registry.terraform.io/v1"

# Vercel serverless-function bundle limit (compressed assets count toward this)
VERCEL_BUNDLE_LIMIT_MB = 250


# ===========================================================================
# Step 1 -- resolve latest stable version  (original logic, unchanged)
# ===========================================================================

def resolve_latest_version(namespace: str, ptype: str) -> str:
    """
    Query the Terraform public registry for the latest stable version.
    Mirrors the async logic in generator/provider.py but runs synchronously.
    Strategy 1: v2 API  ->  Strategy 2: v1 /versions  (semver max).
    """
    import httpx

    log.info("Resolving latest version for %s/%s ...", namespace, ptype)

    # Strategy 1: v2 API -- most reliable, returns latest-version directly
    try:
        url = f"{REGISTRY_V2}/providers/{namespace}/{ptype}"
        with httpx.Client(timeout=30) as client:
            resp = client.get(url)
        if resp.status_code == 200:
            attrs   = resp.json().get("data", {}).get("attributes", {})
            version = attrs.get("latest-version", "")
            if version:
                log.info("  Registry v2 -> %s/%s @ %s", namespace, ptype, version)
                return version
    except Exception as exc:
        log.debug("  v2 API error: %s", exc)

    # Strategy 2: v1 /versions -- pick the true semver max
    try:
        url = f"{REGISTRY_V1}/providers/{namespace}/{ptype}/versions"
        with httpx.Client(timeout=30) as client:
            resp = client.get(url)
        if resp.status_code == 200:
            ver_strings = [
                v["version"]
                for v in resp.json().get("versions", [])
                if isinstance(v, dict) and v.get("version")
            ]
            if ver_strings:
                version = _max_semver(ver_strings)
                log.info("  Registry v1/versions -> %s/%s @ %s", namespace, ptype, version)
                return version
    except Exception as exc:
        log.debug("  v1/versions error: %s", exc)

    raise RuntimeError(
        f"Could not resolve latest version for {namespace}/{ptype} "
        "from the Terraform Registry."
    )


def _max_semver(versions: list[str]) -> str:
    def _key(v: str):
        try:
            return tuple(int(x) for x in v.strip().split("."))
        except (ValueError, AttributeError):
            return (0, 0, 0)
    return max(versions, key=_key)


# ===========================================================================
# Steps 2-4 -- terraform workspace + init + schema  (original logic, unchanged)
# ===========================================================================

def _terraform_exe() -> str:
    for candidate in (_PKG_ROOT / "terraform", _PKG_ROOT / "terraform.exe"):
        if candidate.exists():
            return str(candidate)
    found = shutil.which("terraform")
    if found:
        return found
    raise RuntimeError(
        "terraform binary not found on PATH.\n"
        "Install from: https://developer.hashicorp.com/terraform/install"
    )


def fetch_raw_schema(namespace: str, ptype: str, version: str) -> tuple[dict[str, Any], int]:
    """
    Create a throw-away Terraform workspace, run terraform init + schema,
    and return (schema_dict, raw_json_byte_count).

    Uses file-based stdout capture (identical to generator/provider.py) to
    avoid pipe-buffer issues on large providers such as google or azurerm.
    """
    tf = _terraform_exe()
    log.info("Using terraform binary: %s", tf)

    with tempfile.TemporaryDirectory(prefix="tfgen_schema_") as tmpdir:
        (Path(tmpdir) / "versions.tf").write_text(
            f'terraform {{\n'
            f'  required_version = ">= 1.3.0"\n'
            f'  required_providers {{\n'
            f'    {ptype} = {{\n'
            f'      source  = "{namespace}/{ptype}"\n'
            f'      version = "~> {version}"\n'
            f'    }}\n'
            f'  }}\n'
            f'}}\n',
            encoding="utf-8",
        )
        log.info("Temporary workspace: %s", tmpdir)

        # terraform init
        log.info(
            "Running: terraform init -backend=false  (downloads %s/%s @ %s)",
            namespace, ptype, version,
        )
        r = subprocess.run(
            [tf, "init", "-backend=false", "-no-color", "-input=false"],
            cwd=tmpdir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"terraform init failed (exit {r.returncode}):\n{r.stdout}\n{r.stderr}"
            )
        log.info("terraform init succeeded.")

        # terraform providers schema -json
        log.info("Running: terraform providers schema -json  (may take 30-120 s)")
        schema_file = Path(tmpdir) / "schema.json"
        with open(schema_file, "w", encoding="utf-8", errors="replace") as fh:
            r = subprocess.run(
                [tf, "providers", "schema", "-json"],
                cwd=tmpdir, stdout=fh, stderr=subprocess.PIPE,
                encoding="utf-8", errors="replace",
            )
        if r.returncode != 0:
            raise RuntimeError(
                f"terraform providers schema failed (exit {r.returncode}):\n{r.stderr}"
            )

        raw_bytes = schema_file.read_bytes()
        if not raw_bytes.strip():
            raise RuntimeError("terraform providers schema returned empty output.")

        log.info("Schema fetched. Raw size: %s", _fmt_bytes(len(raw_bytes)))
        return json.loads(raw_bytes.decode("utf-8", errors="replace")), len(raw_bytes)


# ===========================================================================
# Steps 5-6 -- split schema into per-service .json.gz  (original, unchanged)
# ===========================================================================

def split_and_write(
    schema: dict[str, Any],
    provider_name: str,
    namespace: str,
    version: str,
    out_dir: Path,
) -> dict[str, dict]:
    """
    For each service discovered by list_available_services():
      - Carve out resource_schemas and data_source_schemas for that service.
      - Wrap in a valid schema envelope so extract_service_resources works.
      - gzip-compress (level 9) and write to out_dir/<service>.json.gz.

    Returns service_index: { service_name: { file, size_gz, resource_count } }
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    services       = list_available_services(schema, provider_name)
    provider_key   = _find_provider_key(schema["provider_schemas"], provider_name)
    provider_block = schema["provider_schemas"][provider_key]
    provider_conf  = provider_block.get("provider", {})

    log.info(
        "Splitting %d services for %s/%s @ %s ...",
        len(services), namespace, provider_name, version,
    )

    service_index: dict[str, dict] = {}

    for service in services:
        prefix = f"{provider_name}_{service}_"
        exact  = f"{provider_name}_{service}"

        resources_slice: dict[str, Any] = {
            rname: rschema
            for rname, rschema in provider_block.get("resource_schemas", {}).items()
            if rname.startswith(prefix) or rname == exact
        }
        datasources_slice: dict[str, Any] = {
            rname: rschema
            for rname, rschema in provider_block.get("data_source_schemas", {}).items()
            if rname.startswith(prefix) or rname == exact
        }

        if not resources_slice and not datasources_slice:
            log.debug("  Skipping '%s' -- no resources after slice", service)
            continue

        service_schema: dict[str, Any] = {
            "format_version": schema.get("format_version", "1.0"),
            "provider_schemas": {
                provider_key: {
                    "provider":            provider_conf,
                    "resource_schemas":    resources_slice,
                    "data_source_schemas": datasources_slice,
                }
            },
        }

        svc_json_bytes = json.dumps(service_schema, separators=(",", ":")).encode("utf-8")
        out_path       = out_dir / f"{service}.json.gz"
        with gzip.open(out_path, "wb", compresslevel=9) as gf:
            gf.write(svc_json_bytes)

        resource_count = len(resources_slice) + len(datasources_slice)
        rel_path       = str(out_path.relative_to(_PKG_ROOT))

        service_index[service] = {
            "file":           rel_path,
            "size_gz":        out_path.stat().st_size,
            "resource_count": resource_count,
        }

    log.info("Wrote %d service files to %s", len(service_index), out_dir)
    return service_index


# ===========================================================================
# Step 7 -- write combined manifest.json
# ===========================================================================

def write_combined_manifest(manifest_path: Path, provider_results: list[dict]) -> None:
    """
    Write a single manifest.json covering all providers with fields:
    provider name, namespace, version, generated_at, generation_status,
    available_services, service file path, compressed size, resource_count.
    """
    manifest: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "providers":    {},
    }

    for pr in provider_results:
        pname     = pr["provider"]
        svc_index = pr.get("services", {})

        manifest["providers"][pname] = {
            "provider":           pname,
            "namespace":          pr["namespace"],
            "version":            pr.get("version"),
            "generated_at":       pr.get("generated_at"),
            "generation_status":  pr["status"],
            "error":              pr.get("error"),
            "available_services": sorted(svc_index.keys()),
            "services": {
                svc: {
                    "file":           entry["file"],
                    "size_gz":        entry["size_gz"],
                    "resource_count": entry["resource_count"],
                }
                for svc, entry in sorted(svc_index.items())
            },
        }

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log.info("Combined manifest written -> %s", manifest_path)


# ===========================================================================
# Step 8 -- per-provider measurement report
# ===========================================================================

def print_provider_statistics(
    namespace: str,
    ptype: str,
    version: str,
    raw_size: int,
    service_index: dict[str, dict],
) -> None:
    total_gz = sum(e["size_gz"] for e in service_index.values())
    num_svc  = len(service_index)
    avg_gz   = total_gz / num_svc if num_svc else 0
    largest  = max(service_index.items(), key=lambda kv: kv[1]["size_gz"])

    bar = "=" * 66
    print(f"\n{bar}")
    print(f"  MEASUREMENT REPORT  --  {namespace}/{ptype} @ {version}")
    print(bar)
    print(f"  Full original JSON     : {_fmt_bytes(raw_size)}")
    print(f"  Total compressed size  : {_fmt_bytes(total_gz)}   (sum of all .json.gz)")
    if total_gz:
        print(f"  Compression ratio      : {raw_size / total_gz:.1f}x")
    print(f"  Number of service files: {num_svc}")
    print(f"  Largest service file   : {largest[0]}.json.gz  ({_fmt_bytes(largest[1]['size_gz'])})")
    print(f"  Average service size   : {_fmt_bytes(int(avg_gz))}")
    print(bar)


# ===========================================================================
# Step 9 -- validate round-trip  (original logic, unchanged)
# ===========================================================================

def validate_round_trip(
    service_index: dict[str, dict],
    provider_name: str,
    namespace: str,
    version: str,
) -> tuple[str, int, int]:
    """
    Pick the service with the most resources, decompress its .json.gz,
    pass it through extract_service_resources() and generate_service_folder(),
    and confirm valid Terraform files are produced.

    Returns (service_name, resource_count, generated_file_count).
    """
    service_name = max(service_index, key=lambda s: service_index[s]["resource_count"])
    entry        = service_index[service_name]
    gz_path      = (_PKG_ROOT / entry["file"]).resolve()

    log.info("Validating '%s'  (%d resources) ...", service_name, entry["resource_count"])

    with gzip.open(gz_path, "rb") as gf:
        service_schema = json.loads(gf.read().decode("utf-8"))

    resources = extract_service_resources(service_schema, provider_name, service_name)
    if not resources:
        raise ValueError(
            f"extract_service_resources() returned 0 resources for '{service_name}'."
        )
    log.info("  extract_service_resources() -> %d resources  [OK]", len(resources))

    r0 = resources[0]
    for k in ("name", "kind", "attributes", "block_types"):
        if k not in r0:
            raise ValueError(f"Resource dict missing key '{k}'.")
    log.info("  Resource schema shape check (name/kind/attributes/block_types)  [OK]")

    provider_meta = {"namespace": namespace, "type": provider_name, "version": version}
    with tempfile.TemporaryDirectory(prefix="tfgen_validate_") as tmpdir:
        svc_root        = generate_service_folder(
            base_dir=tmpdir,
            provider_name=provider_name,
            service_name=service_name,
            resources=resources,
            provider_meta=provider_meta,
        )
        all_files       = [f for f in svc_root.rglob("*") if f.is_file()]
        generated_count = len(all_files)

        modules_dir = svc_root / "modules"
        mod_dirs    = (
            [d for d in modules_dir.iterdir() if d.is_dir()]
            if modules_dir.exists() else []
        )
        if mod_dirs:
            first_mod = mod_dirs[0]
            for expected in ("variables.tf", "main.tf", "outputs.tf"):
                fp = first_mod / expected
                if not fp.exists():
                    raise ValueError(f"Expected file '{fp.relative_to(tmpdir)}' not generated.")
                if fp.stat().st_size == 0:
                    raise ValueError(f"'{fp.relative_to(tmpdir)}' is empty.")
            log.info("  Module files (variables.tf / main.tf / outputs.tf) non-empty  [OK]")

    log.info("  generate_service_folder() -> %d files  [OK]", generated_count)
    return service_name, len(resources), generated_count


# ===========================================================================
# Per-provider pipeline
# ===========================================================================

def _run_provider(namespace: str, ptype: str) -> dict[str, Any]:
    """
    Full pipeline for one provider. Errors are caught and stored;
    subsequent providers still run.
    """
    result: dict[str, Any] = {
        "provider":     ptype,
        "namespace":    namespace,
        "version":      None,
        "status":       "error",
        "error":        None,
        "raw_size":     None,
        "services":     {},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "validation": {
            "status": "skipped", "service": None,
            "resources": None, "files_generated": None, "error": None,
        },
    }

    bar = "=" * 66
    log.info(bar)
    log.info("Processing provider  --  %s/%s", namespace, ptype)
    log.info(bar)

    try:
        # 1. Resolve version
        version           = resolve_latest_version(namespace, ptype)
        result["version"] = version

        # 2-4. Fetch raw schema (re-use provider.py cache when available)
        schema: dict[str, Any]
        raw_size: int
        try:
            from generator.provider import _cache_key, _load_cached_schema, _save_cached_schema
            cached = _load_cached_schema(namespace, ptype, version)
            if cached:
                log.info("Re-using cached schema for %s/%s @ %s", namespace, ptype, version)
                schema   = cached
                raw_size = _cache_key(namespace, ptype, version).stat().st_size
            else:
                schema, raw_size = fetch_raw_schema(namespace, ptype, version)
                _save_cached_schema(namespace, ptype, version, schema)
        except Exception:
            schema, raw_size = fetch_raw_schema(namespace, ptype, version)

        result["raw_size"] = raw_size

        # 5-6. Split and write per-service .json.gz
        out_dir       = OUTPUT_ROOT / ptype
        service_index = split_and_write(schema, ptype, namespace, version, out_dir)
        if not service_index:
            raise RuntimeError("No service files were written.")

        result["services"] = service_index
        result["status"]   = "ok"

    except Exception as exc:
        result["error"]  = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        result["status"] = "error"
        log.error(
            "Provider %s/%s FAILED: %s  -- continuing with remaining providers.",
            namespace, ptype, exc,
        )
        return result

    # 8. Measurement report
    print_provider_statistics(namespace, ptype, version, raw_size, service_index)

    # 9. Validate round-trip
    log.info("Running validation round-trip for %s/%s ...", namespace, ptype)
    try:
        svc, res_count, file_count = validate_round_trip(
            service_index, ptype, namespace, version,
        )
        result["validation"] = {
            "status": "PASSED", "service": svc,
            "resources": res_count, "files_generated": file_count, "error": None,
        }
    except Exception as exc:
        result["validation"] = {
            "status": "FAILED", "service": None,
            "resources": None, "files_generated": None, "error": str(exc),
        }
        log.error("Validation FAILED for %s/%s: %s", namespace, ptype, exc)

    return result


# ===========================================================================
# Utility
# ===========================================================================

def _fmt_bytes(n: int | float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ===========================================================================
# Combined summary + Vercel verdict
# ===========================================================================

def print_combined_summary(provider_results: list[dict]) -> None:
    bar  = "=" * 66
    bar2 = "-" * 66

    # --- Validation table ---
    print(f"\n{bar}")
    print("  VALIDATION RESULTS  --  ALL PROVIDERS")
    print(bar)
    for pr in provider_results:
        pname = f"{pr['namespace']}/{pr['provider']}"
        ver   = pr.get("version") or "N/A"
        if pr["status"] == "error":
            print(f"\n  Provider : {pname} @ {ver}")
            print( "  Status   : GENERATION FAILED")
            first_line = (pr.get("error") or "unknown").splitlines()[0]
            print(f"  Error    : {first_line}")
        else:
            v = pr["validation"]
            print(f"\n  Provider : {pname} @ {ver}")
            print(f"  Status   : {v['status']}")
            if v["status"] == "PASSED":
                print(f"  Service validated      : {v['service']}")
                print(f"  Resources parsed       : {v['resources']}")
                print(f"  Terraform files gen'd  : {v['files_generated']}")
            else:
                print(f"  Error    : {v.get('error', 'unknown')}")
        print(f"  {bar2}")

    # --- Combined compressed size ---
    total_compressed = sum(
        sum(e["size_gz"] for e in pr.get("services", {}).values())
        for pr in provider_results if pr["status"] == "ok"
    )
    total_mb = total_compressed / (1024 * 1024)

    print(f"\n{bar}")
    print("  COMBINED COMPRESSED SIZE SUMMARY")
    print(bar)
    for pr in provider_results:
        pname   = f"{pr['namespace']}/{pr['provider']}"
        svc_tot = sum(e["size_gz"] for e in pr.get("services", {}).values())
        num_svc = len(pr.get("services", {}))
        if pr["status"] == "ok":
            print(f"  {pname:<30} {_fmt_bytes(svc_tot):>10}   ({num_svc} service files)")
        else:
            print(f"  {pname:<30} {'FAILED':>10}")
    print(f"  {bar2}")
    print(f"  {'COMBINED TOTAL':<30} {_fmt_bytes(total_compressed):>10}")
    print(bar)

    # --- Vercel bundling verdict ---
    print(f"\n{bar}")
    print("  VERCEL BUNDLING VERDICT")
    print(bar)
    print(f"  Threshold           : {VERCEL_BUNDLE_LIMIT_MB} MB  "
          "(Vercel serverless function limit)")
    print(f"  Combined compressed : {_fmt_bytes(total_compressed)}  ({total_mb:.1f} MB)")

    failed = [pr["provider"] for pr in provider_results if pr["status"] == "error"]
    if failed:
        print(f"\n  WARNING: {len(failed)} provider(s) failed: {', '.join(failed)}")
        print( "  Cannot make a complete bundling assessment.")
        print( "\n  VERDICT: INCOMPLETE -- resolve provider errors first.")
    elif total_mb <= VERCEL_BUNDLE_LIMIT_MB:
        print(f"\n  VERDICT: SUITABLE FOR VERCEL BUNDLING")
        print(f"  Combined compressed size ({total_mb:.1f} MB) is within the")
        print(f"  {VERCEL_BUNDLE_LIMIT_MB} MB Vercel serverless function limit.")
        print( "  Schemas are gzip-compressed; Vercel counts them at compressed size,")
        print( "  which comfortably fits within the 250 MB uncompressed bundle limit.")
    else:
        print(f"\n  VERDICT: NOT SUITABLE AS-IS FOR VERCEL BUNDLING")
        print(f"  Combined compressed size ({total_mb:.1f} MB) exceeds {VERCEL_BUNDLE_LIMIT_MB} MB.")
        print( "  Recommendation: host schemas in external object storage (S3/GCS/Azure Blob)")
        print( "  and load them on-demand at runtime instead of bundling.")
    print(bar)

    # --- Files created ---
    print(f"\n{bar}")
    print("  FILES CREATED / MODIFIED")
    print(bar)
    print(f"  {OUTPUT_ROOT}/")
    for pr in provider_results:
        if pr["status"] == "ok":
            num = len(pr.get("services", {}))
            print(f"    +-- {pr['provider']}/   ({num} .json.gz service files)")
    print( "    +-- manifest.json   (combined, covers all providers)")
    print( "")
    print( "  scripts/generate_schemas.py   (extended for Google + AWS + AzureRM)")
    print(bar)


# ===========================================================================
# Main
# ===========================================================================

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Pre-generate per-service Terraform schema files."
    )
    parser.add_argument(
        "--providers",
        nargs="+",
        choices=["aws", "azurerm", "google"],
        default=None,
        help="Which providers to generate (default: all three).",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Root output directory (default: generated-schemas/ next to this package).",
    )
    args = parser.parse_args()

    # Allow caller to override the output root (used by CI for staging)
    global OUTPUT_ROOT, MANIFEST
    if args.output_dir:
        OUTPUT_ROOT = Path(args.output_dir).resolve()
        MANIFEST    = OUTPUT_ROOT / "manifest.json"

    # Load providers from providers.yml (filtered by --providers flag if given)
    filter_types = set(args.providers) if args.providers else None
    selected = _load_providers_config(filter_types)

    bar = "=" * 66
    log.info(bar)
    log.info("Pre-generating schemas  --  %s", " | ".join(p['type'] for p in selected))
    log.info(bar)

    # Only wipe the provider subdirectories we are regenerating, not the whole root
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    for p in selected:
        pdir = OUTPUT_ROOT / p["type"]
        if pdir.exists():
            log.info("Removing existing %s ...", pdir)
            shutil.rmtree(pdir)
    log.info("Output directory: %s", OUTPUT_ROOT)

    # Run pipeline for each selected provider
    provider_results: list[dict] = []
    for p in selected:
        pr = _run_provider(p["namespace"], p["type"])
        provider_results.append(pr)

    # Merge with any existing manifest entries for providers we did NOT regenerate
    existing_manifest: dict = {}
    if MANIFEST.exists():
        try:
            existing_manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        except Exception:
            pass

    merged_results = list(provider_results)
    regenerated    = {pr["provider"] for pr in provider_results}
    for pname, pdata in existing_manifest.get("providers", {}).items():
        if pname not in regenerated:
            merged_results.append(
                {
                    "provider":     pname,
                    "namespace":    pdata.get("namespace", "hashicorp"),
                    "version":      pdata.get("version"),
                    "generated_at": pdata.get("generated_at"),
                    "status":       pdata.get("generation_status", "ok"),
                    "error":        pdata.get("error"),
                    "raw_size":     None,
                    "services":     {
                        svc: {
                            "file":           e["file"],
                            "size_gz":        e.get("size_gz", 0),
                            "resource_count": e["resource_count"],
                        }
                        for svc, e in pdata.get("services", {}).items()
                    },
                    "validation":   {"status": "preserved"},
                }
            )

    # Write combined manifest (covers all providers)
    write_combined_manifest(MANIFEST, merged_results)

    # Print combined summary, validation table & Vercel verdict
    print_combined_summary(provider_results)

    print(f"\n  Output directory : {OUTPUT_ROOT}")
    print(f"  Manifest         : {MANIFEST}\n")

    # Exit non-zero only if every provider we were asked to generate failed
    if all(pr["status"] == "error" for pr in provider_results):
        sys.exit(1)


if __name__ == "__main__":
    main()
