"""
generator/schema_store.py
--------------------------
Schema-loading layer for Vercel (pre-generated schema mode).

Public API
----------
  list_providers()                  -> list[dict]
  list_services(provider)           -> list[str]
  get_provider_metadata(provider)   -> dict
  load_service_schema(provider, service) -> tuple[dict, dict]

Design constraints
------------------
* Only the requested service file is decompressed — the entire provider
  schema is never loaded into memory.
* Files are read directly via Python's gzip module; nothing is extracted
  to /tmp.
* Every input is validated and path-traversal is blocked before any file
  I/O is attempted.
* The manifest is loaded once and cached for the lifetime of the process.
"""

from __future__ import annotations

import gzip
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Filesystem layout
# ---------------------------------------------------------------------------
_PKG_ROOT     = Path(__file__).resolve().parent.parent
_SCHEMAS_DIR  = _PKG_ROOT / "generated-schemas"
_MANIFEST_PATH = _SCHEMAS_DIR / "manifest.json"

# ---------------------------------------------------------------------------
# Validation constants
# ---------------------------------------------------------------------------
ALLOWED_PROVIDERS: frozenset[str] = frozenset({"aws", "azurerm", "google"})
_SAFE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")


# ===========================================================================
# Internal helpers
# ===========================================================================

@lru_cache(maxsize=1)
def _load_manifest() -> dict[str, Any]:
    """
    Load and cache manifest.json.  Raises on first failure so the caller
    gets a clear error rather than a generic FileNotFoundError.
    """
    if not _MANIFEST_PATH.exists():
        raise FileNotFoundError(
            f"manifest.json not found at {_MANIFEST_PATH}. "
            "Run scripts/generate_schemas.py to create it."
        )
    try:
        return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"manifest.json is not valid JSON: {exc}"
        ) from exc


def _validate_provider(provider: str) -> str:
    """Normalise and validate a provider name."""
    provider = provider.strip().lower()
    if not provider:
        raise ValueError("Provider name must not be empty.")
    if provider not in ALLOWED_PROVIDERS:
        raise ValueError(
            f"Unsupported provider '{provider}'. "
            f"Allowed providers: {sorted(ALLOWED_PROVIDERS)}"
        )
    return provider


def _validate_service(service: str) -> str:
    """Normalise and validate a service name."""
    service = service.strip().lower()
    if not service:
        raise ValueError("Service name must not be empty.")
    if not _SAFE_NAME_RE.match(service):
        raise ValueError(
            f"Invalid service name '{service}'. "
            "Must start with a letter/digit and contain only [a-z0-9_]."
        )
    # Belt-and-braces: block any path-traversal character
    for dangerous in ("..", "/", "\\", "\x00"):
        if dangerous in service:
            raise ValueError(
                f"Service name '{service}' contains disallowed sequence '{dangerous}'."
            )
    return service


def _resolve_schema_path(raw_path: str) -> Path:
    """
    Convert a manifest file path (may use / or \\ separators) to an
    absolute resolved Path, and verify it stays inside _SCHEMAS_DIR.
    """
    normalised = raw_path.replace("\\", "/")
    resolved   = (_PKG_ROOT / normalised).resolve()
    schemas_root = _SCHEMAS_DIR.resolve()

    try:
        resolved.relative_to(schemas_root)
    except ValueError:
        raise ValueError(
            f"Schema path '{raw_path}' resolves outside generated-schemas/. "
            "Possible path-traversal attempt."
        )
    return resolved


# ===========================================================================
# Public API
# ===========================================================================

def list_providers() -> list[dict[str, Any]]:
    """
    Return a list of provider-metadata dicts sourced from manifest.json.

    Each dict contains:
      name, namespace, version, generated_at, service_count
    """
    manifest = _load_manifest()
    result: list[dict] = []
    for pname, pdata in manifest.get("providers", {}).items():
        result.append(
            {
                "name":          pname,
                "namespace":     pdata.get("namespace", "hashicorp"),
                "description":   _PROVIDER_DESCRIPTIONS.get(pname, ""),
                "version":       pdata.get("version", "unknown"),
                "generated_at":  pdata.get("generated_at", ""),
                "service_count": len(pdata.get("services", {})),
            }
        )
    return result


def list_services(provider: str) -> list[str]:
    """
    Return a sorted list of available service names for *provider*.

    Raises ValueError for unknown providers.
    """
    provider = _validate_provider(provider)
    manifest = _load_manifest()
    pdata    = manifest.get("providers", {}).get(provider)
    if pdata is None:
        raise ValueError(f"Provider '{provider}' not found in manifest.")
    return sorted(pdata.get("services", {}).keys())


def get_provider_metadata(provider: str) -> dict[str, Any]:
    """
    Return version and generation timestamp for *provider*.

      {
        "name":         "aws",
        "namespace":    "hashicorp",
        "type":         "aws",
        "version":      "6.62.0",
        "generated_at": "2025-01-01T00:00:00Z",
      }
    """
    provider = _validate_provider(provider)
    manifest = _load_manifest()
    pdata    = manifest.get("providers", {}).get(provider)
    if pdata is None:
        raise ValueError(f"Provider '{provider}' not found in manifest.")
    return {
        "name":         provider,
        "namespace":    pdata.get("namespace", "hashicorp"),
        "type":         provider,
        "version":      pdata.get("version", "unknown"),
        "generated_at": pdata.get("generated_at", ""),
    }


def load_service_schema(
    provider: str,
    service:  str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Decompress and return the pre-generated schema for *provider* + *service*.

    Returns
    -------
    (schema, provider_meta)

    schema        -- full provider_schemas envelope dict, identical in
                     structure to the output of ``terraform providers schema -json``
    provider_meta -- dict with keys: namespace, type, version, generated_at

    Raises
    ------
    ValueError       -- unsupported provider, unknown service, path traversal
    FileNotFoundError -- schema file absent on disk
    RuntimeError     -- corrupt gzip or invalid JSON
    """
    provider = _validate_provider(provider)
    service  = _validate_service(service)

    manifest = _load_manifest()
    pdata    = manifest.get("providers", {}).get(provider)
    if pdata is None:
        raise ValueError(f"Provider '{provider}' not found in manifest.")

    services = pdata.get("services", {})
    if service not in services:
        available = sorted(services.keys())
        sample    = ", ".join(available[:20])
        suffix    = " ..." if len(available) > 20 else ""
        raise ValueError(
            f"Service '{service}' not found for provider '{provider}'. "
            f"Available ({len(available)}): {sample}{suffix}"
        )

    raw_path = services[service].get("file", "")
    gz_path  = _resolve_schema_path(raw_path)

    if not gz_path.exists():
        raise FileNotFoundError(
            f"Schema file is listed in manifest but missing on disk: {gz_path}. "
            "Re-run scripts/generate_schemas.py to regenerate."
        )

    # Decompress directly — no extraction to /tmp
    try:
        with gzip.open(gz_path, "rb") as fh:
            raw = fh.read()
    except Exception as exc:
        raise RuntimeError(
            f"Failed to decompress '{gz_path.name}': {exc}"
        ) from exc

    if not raw:
        raise RuntimeError(f"Schema file '{gz_path.name}' is empty after decompression.")

    try:
        schema = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"Schema file '{gz_path.name}' contains invalid JSON: {exc}"
        ) from exc

    if not isinstance(schema.get("provider_schemas"), dict):
        raise RuntimeError(
            f"Schema file '{gz_path.name}' is missing the provider_schemas envelope."
        )

    provider_meta: dict[str, Any] = {
        "namespace":    pdata.get("namespace", "hashicorp"),
        "type":         provider,
        "version":      pdata.get("version", "unknown"),
        "generated_at": pdata.get("generated_at", ""),
    }

    return schema, provider_meta


# ---------------------------------------------------------------------------
# Internal lookup table
# ---------------------------------------------------------------------------
_PROVIDER_DESCRIPTIONS: dict[str, str] = {
    "aws":     "Amazon Web Services",
    "azurerm": "Microsoft Azure",
    "google":  "Google Cloud Platform",
}
