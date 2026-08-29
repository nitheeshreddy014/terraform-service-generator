"""
provider.py
-----------
Handles:
  1. Resolving the correct registry namespace for any provider name.
  2. Fetching the latest published version from the Terraform Registry.
  3. Writing a temporary Terraform workspace, running `terraform init`
     and `terraform providers schema -json`, then returning the raw schema dict.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _max_semver(versions: list[str]) -> str:
    """
    Return the highest semantic version from a list of version strings.
    Compares numerically per segment so '5.1.0' > '3.69.0' correctly.
    """
    def _key(v: str):
        try:
            return tuple(int(x) for x in v.strip().split("."))
        except (ValueError, AttributeError):
            return (0, 0, 0)
    return max(versions, key=_key)


REGISTRY_BASE  = "https://registry.terraform.io/v1"
REGISTRY_BASE2 = "https://registry.terraform.io/v2"
SEARCH_URL     = f"{REGISTRY_BASE}/providers"


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


# ── Schema caching (speeds up repeated generation for same provider+version) ──
import json as _json_cache, pathlib as _pl

# On Vercel the home directory (/root) is read-only; only /tmp is writable.
_CACHE_DIR = (
    _pl.Path("/tmp/.terraform-generator-cache")
    if os.environ.get("VERCEL") == "1"
    else _pl.Path.home() / ".terraform-generator-cache"
)

def _cache_key(ns, pt, ver):
    return _CACHE_DIR / f"{ns}__{pt}__{ver}.json"

def _load_cached_schema(ns, pt, ver):
    p = _cache_key(ns, pt, ver)
    if p.exists():
        logger.info("Cache HIT: %s", p.name)
        return _json_cache.loads(p.read_text(encoding="utf-8"))
    logger.info("Cache MISS: will run terraform init+schema")
    return None

def _save_cached_schema(ns, pt, ver, schema):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_key(ns, pt, ver).write_text(_json_cache.dumps(schema), encoding="utf-8")
    logger.info("Schema cached: %s", _cache_key(ns, pt, ver).name)

async def resolve_provider(provider_name: str) -> dict[str, str]:
    """
    Returns {'namespace': '...', 'type': '...', 'version': '...'} for the
    given provider name by querying the Terraform public registry.

    Tries 'hashicorp/<name>' first (covers 95 % of cases), then falls back
    to the registry search endpoint.
    """
    name = provider_name.strip().lower()

    # --- Try canonical hashicorp namespace first ---
    candidate = await _fetch_provider_meta("hashicorp", name)
    if candidate:
        return candidate

    # --- Registry search fallback ---
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(SEARCH_URL, params={"q": name, "limit": 5})
        resp.raise_for_status()
        data = resp.json()

    providers = data.get("providers", [])
    if not providers:
        raise ValueError(
            f"Provider '{name}' not found in the Terraform Registry. "
            "Check the spelling and try again."
        )

    # Prefer exact type match
    match = next((p for p in providers if p["attributes"]["full-name"].split("/")[1] == name), providers[0])
    attrs      = match["attributes"]
    namespace  = attrs["namespace"]
    ptype      = attrs["full-name"].split("/")[1]

    meta = await _fetch_provider_meta(namespace, ptype)
    if not meta:
        raise ValueError(f"Could not retrieve metadata for {namespace}/{ptype}.")
    return meta


async def fetch_schema(provider_name: str) -> tuple[dict[str, Any], dict[str, str]]:
    """
    Full pipeline:
      1. Resolve provider → namespace / type / latest version.
      2. Create a throw-away Terraform workspace.
      3. `terraform init -backend=false -no-color`
      4. `terraform providers schema -json`
      5. Return (schema_dict, provider_meta).

    Raises RuntimeError if terraform is not on PATH or any step fails.
    """
    _require_terraform()

    provider_meta = await resolve_provider(provider_name)
    namespace     = provider_meta["namespace"]
    ptype         = provider_meta["type"]
    version       = provider_meta["version"]

    logger.info("Using provider %s/%s version %s", namespace, ptype, version)

    cached = _load_cached_schema(namespace, ptype, version)
    if cached:
        return cached, provider_meta

    with tempfile.TemporaryDirectory(prefix="tfgen_") as tmpdir:
        _write_versions_tf(tmpdir, namespace, ptype, version)
        _terraform_init(tmpdir)
        schema = _terraform_schema(tmpdir)

    _save_cached_schema(namespace, ptype, version, schema)
    return schema, provider_meta


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _fetch_provider_meta(namespace: str, ptype: str) -> dict[str, str] | None:
    """
    Fetch the latest version for namespace/ptype using three strategies:
      1. Terraform Registry v2 API  → data.attributes.latest-version  (most reliable)
      2. v1 /versions endpoint       → last entry in the versions list
      3. v1 flat provider endpoint   → 'version' or 'tag' field
    Returns None if the provider is not found (404).
    """
    async with httpx.AsyncClient(timeout=30) as client:

        # ── Strategy 1: v2 API ────────────────────────────────────────────
        try:
            url2  = f"{REGISTRY_BASE2}/providers/{namespace}/{ptype}"
            resp2 = await client.get(url2)
            if resp2.status_code == 200:
                attrs   = resp2.json().get("data", {}).get("attributes", {})
                version = attrs.get("latest-version", "")
                if version:
                    logger.debug("v2 API resolved %s/%s → %s", namespace, ptype, version)
                    return {"namespace": namespace, "type": ptype, "version": version}
        except Exception:
            pass

        # Strategy 2: v1 /versions endpoint — pick TRUE semver max
        try:
            url_ver  = f"{REGISTRY_BASE}/providers/{namespace}/{ptype}/versions"
            resp_ver = await client.get(url_ver)
            if resp_ver.status_code == 200:
                versions_data = resp_ver.json().get("versions", [])
                if versions_data:
                    ver_strings = [
                        v["version"]
                        for v in versions_data
                        if isinstance(v, dict) and v.get("version")
                    ]
                    if ver_strings:
                        version = _max_semver(ver_strings)
                        logger.info("v1/versions resolved %s/%s -> %s", namespace, ptype, version)
                        return {"namespace": namespace, "type": ptype, "version": version}
        except Exception as exc:
            logger.debug("v1/versions failed: %s", exc)

        # Strategy 3: v1 flat endpoint
        try:
            url_flat  = f"{REGISTRY_BASE}/providers/{namespace}/{ptype}"
            resp_flat = await client.get(url_flat)
            if resp_flat.status_code == 404:
                return None
            if resp_flat.status_code == 200:
                data     = resp_flat.json()
                explicit = (
                    data.get("version")
                    or data.get("tag")
                    or data.get("attributes", {}).get("latest-version", "")
                )
                if explicit:
                    version = str(explicit).strip()
                else:
                    raw = data.get("versions", [])
                    if raw:
                        if isinstance(raw[0], dict):
                            ver_strings = [v.get("version", "") for v in raw if v.get("version")]
                        else:
                            ver_strings = [str(v) for v in raw if v]
                        version = _max_semver(ver_strings) if ver_strings else ""
                    else:
                        version = ""
                if version:
                    logger.info("v1 flat resolved %s/%s -> %s", namespace, ptype, version)
                    return {"namespace": namespace, "type": ptype, "version": version}
        except Exception as exc:
            logger.debug("v1 flat failed: %s", exc)

    return None


def _write_versions_tf(directory: str, namespace: str, ptype: str, version: str) -> None:
    """Write the minimal versions.tf needed to initialise the provider."""
    content = f"""\
terraform {{
  required_version = ">= 1.3.0"
  required_providers {{
    {ptype} = {{
      source  = "{namespace}/{ptype}"
      version = "~> {version}"
    }}
  }}
}}
"""
    Path(directory, "versions.tf").write_text(content)


def _terraform_init(directory: str) -> None:
    """Run terraform init inside *directory*, raising on failure."""
    cmd = [_terraform_exe(), "init", "-backend=false", "-no-color", "-input=false"]
    result = subprocess.run(
        cmd,
        cwd=directory,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"terraform init failed:\n{result.stdout}\n{result.stderr}"
        )
    logger.info("terraform init succeeded.")


def _terraform_schema(directory: str) -> dict[str, Any]:
    """
    Run `terraform providers schema -json` and write output directly to a
    file instead of capturing via a pipe.

    WHY FILE-BASED:
    Large providers (google, azurerm) produce 50-200 MB of JSON.
    On Windows, subprocess pipe buffers overflow for large outputs,
    causing result.stdout to be None or truncated, which produces:
      'JSON object must be str, bytes or bytearray, not NoneType'
    Writing stdout directly to a file bypasses all pipe-buffer limits
    and works identically for every CSP / provider size.
    """
    schema_file = Path(directory) / "schema.json"
    cmd          = [_terraform_exe(), "providers", "schema", "-json"]

    with open(schema_file, "w", encoding="utf-8", errors="replace") as out_fh:
        result = subprocess.run(
            cmd,
            cwd=directory,
            stdout=out_fh,          # stream directly to disk — no pipe limit
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )

    if result.returncode != 0:
        raise RuntimeError(
            f"terraform providers schema failed (exit {result.returncode}):\n"
            f"{result.stderr}"
        )

    raw = schema_file.read_text(encoding="utf-8", errors="replace").strip()
    if not raw:
        raise RuntimeError(
            "terraform providers schema returned empty output. "
            "Ensure terraform init completed successfully."
        )

    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Could not parse schema JSON: {exc}") from exc


def _require_terraform() -> None:
    """Raise RuntimeError if the `terraform` binary is not found."""
    if _terraform_exe() is None:
        raise RuntimeError(
            "The `terraform` CLI was not found on PATH or next to main.py. "
            "Please install Terraform (https://developer.hashicorp.com/terraform/install) "
            "or place terraform.exe in the terraform-generator/ directory."
        )


def _terraform_exe() -> str | None:
    """
    Resolve the terraform binary.
    Priority:
      1. terraform.exe / terraform sitting next to main.py  (bundled)
      2. Anywhere on PATH
    """
    # Directory that contains main.py (project root)
    project_root = Path(__file__).parent.parent

    for candidate in ("terraform.exe", "terraform"):
        local = project_root / candidate
        if local.exists():
            return str(local)

    return shutil.which("terraform")
