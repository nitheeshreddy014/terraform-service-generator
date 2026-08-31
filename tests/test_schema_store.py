"""
tests/test_schema_store.py
--------------------------
Tests for generator/schema_store.py using the real pre-generated schemas.

All tests are read-only and use the actual generated-schemas/ directory.
"""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import pytest

# Make the repo root importable
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from generator import schema_store


# ===========================================================================
# list_providers
# ===========================================================================

def test_list_providers_returns_list():
    providers = schema_store.list_providers()
    assert isinstance(providers, list)
    assert len(providers) > 0


def test_list_providers_contains_expected():
    names = {p["name"] for p in schema_store.list_providers()}
    assert "aws"     in names
    assert "azurerm" in names
    assert "google"  in names


def test_list_providers_has_required_keys():
    for p in schema_store.list_providers():
        assert "name"          in p
        assert "version"       in p
        assert "generated_at"  in p
        assert "service_count" in p
        assert p["service_count"] > 0


# ===========================================================================
# list_services
# ===========================================================================

@pytest.mark.parametrize("provider,expected_min", [
    ("aws",     100),
    ("azurerm", 50),
    ("google",  50),
])
def test_list_services_count(provider, expected_min):
    services = schema_store.list_services(provider)
    assert isinstance(services, list)
    assert len(services) >= expected_min


def test_list_services_sorted():
    for provider in ("aws", "azurerm", "google"):
        services = schema_store.list_services(provider)
        assert services == sorted(services), f"{provider} services not sorted"


def test_list_services_aws_has_s3():
    assert "s3" in schema_store.list_services("aws")


def test_list_services_azurerm_has_storage():
    assert "storage" in schema_store.list_services("azurerm")


def test_list_services_google_has_compute():
    assert "compute" in schema_store.list_services("google")


def test_list_services_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unsupported provider"):
        schema_store.list_services("invalid_provider_xyz")


def test_list_services_empty_provider_raises():
    with pytest.raises(ValueError):
        schema_store.list_services("")


# ===========================================================================
# get_provider_metadata
# ===========================================================================

@pytest.mark.parametrize("provider", ["aws", "azurerm", "google"])
def test_get_provider_metadata_keys(provider):
    meta = schema_store.get_provider_metadata(provider)
    assert meta["name"]      == provider
    assert meta["type"]      == provider
    assert meta["namespace"] == "hashicorp"
    assert len(meta["version"]) > 0
    assert len(meta["generated_at"]) > 0


def test_get_provider_metadata_unknown_raises():
    with pytest.raises(ValueError, match="Unsupported provider"):
        schema_store.get_provider_metadata("badprovider")


# ===========================================================================
# load_service_schema
# ===========================================================================

@pytest.mark.parametrize("provider,service", [
    ("aws",     "s3"),
    ("azurerm", "storage"),
    ("google",  "compute"),
])
def test_load_service_schema_returns_envelope(provider, service):
    schema, meta = schema_store.load_service_schema(provider, service)
    assert isinstance(schema, dict)
    assert "provider_schemas" in schema
    assert isinstance(schema["provider_schemas"], dict)
    assert len(schema["provider_schemas"]) > 0


@pytest.mark.parametrize("provider,service", [
    ("aws",     "s3"),
    ("azurerm", "storage"),
    ("google",  "compute"),
])
def test_load_service_schema_meta_keys(provider, service):
    _, meta = schema_store.load_service_schema(provider, service)
    assert meta["type"]      == provider
    assert meta["namespace"] == "hashicorp"
    assert len(meta["version"]) > 0
    assert len(meta["generated_at"]) > 0


def test_load_service_schema_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unsupported provider"):
        schema_store.load_service_schema("badprovider", "s3")


def test_load_service_schema_unknown_service_raises():
    with pytest.raises(ValueError, match="not found"):
        schema_store.load_service_schema("aws", "zzz_nonexistent_service_xyz")


def test_load_service_schema_path_traversal_blocked():
    with pytest.raises(ValueError):
        schema_store.load_service_schema("aws", "../../../etc/passwd")


def test_load_service_schema_path_traversal_slash_blocked():
    with pytest.raises(ValueError):
        schema_store.load_service_schema("aws", "s3/../../secret")


# ===========================================================================
# Path safety
# ===========================================================================

def test_validate_provider_rejects_empty():
    with pytest.raises(ValueError):
        schema_store._validate_provider("")


def test_validate_provider_rejects_unknown():
    with pytest.raises(ValueError):
        schema_store._validate_provider("kubernetes")


def test_validate_service_rejects_traversal():
    with pytest.raises(ValueError):
        schema_store._validate_service("../etc/passwd")


def test_validate_service_rejects_slash():
    with pytest.raises(ValueError):
        schema_store._validate_service("s3/subdir")


def test_validate_service_rejects_empty():
    with pytest.raises(ValueError):
        schema_store._validate_service("")


# ===========================================================================
# Round-trip: schema -> extract -> generate
# ===========================================================================

@pytest.mark.parametrize("provider,service", [
    ("aws",     "s3"),
    ("azurerm", "storage"),
    ("google",  "compute"),
])
def test_round_trip_extract_resources(provider, service):
    import tempfile
    from generator.schema_parser import extract_service_resources
    from generator.file_generator import generate_service_folder

    schema, provider_meta = schema_store.load_service_schema(provider, service)
    resources = extract_service_resources(schema, provider, service)
    assert len(resources) > 0, f"No resources for {provider}/{service}"

    r0 = resources[0]
    for key in ("name", "kind", "attributes", "block_types"):
        assert key in r0, f"Resource missing key '{key}'"

    with tempfile.TemporaryDirectory(prefix="tfgen_test_") as tmpdir:
        svc_root = generate_service_folder(
            base_dir=tmpdir,
            provider_name=provider,
            service_name=service,
            resources=resources,
            provider_meta=provider_meta,
        )
        mods_dir = svc_root / "modules"
        assert mods_dir.exists(), "modules/ directory not created"
        mod_dirs = [d for d in mods_dir.iterdir() if d.is_dir()]
        assert len(mod_dirs) > 0, "No module directories generated"
        for fname in ("variables.tf", "main.tf", "outputs.tf"):
            fp = mod_dirs[0] / fname
            assert fp.exists(),            f"{fname} not found in {mod_dirs[0].name}"
            assert fp.stat().st_size > 0,  f"{fname} is empty in {mod_dirs[0].name}"
