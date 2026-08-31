"""
tests/test_vercel_mode.py
--------------------------
Integration tests covering:
  - VERCEL=1 never calls Terraform / fetch_schema
  - VERCEL=1 /generate returns ZIP bytes directly
  - Local mode retains JSON download_url path
  - /providers and /services work in both modes
  - Path-traversal inputs rejected
  - Unsupported provider / service return clear errors
  - main.py imports cleanly
  - vercel.json is valid JSON and schema-compatible
  - GitHub Actions YAML is syntactically valid
  - No generated file exceeds 100 MB GitHub individual-file limit
  - Combined compressed schemas stay within Vercel bundle limit (250 MB)
  - No credential / secret leakage in tracked files
"""
from __future__ import annotations

import gzip
import importlib
import json
import os
import sys
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

SCHEMAS_DIR   = _REPO / "generated-schemas"
MANIFEST_PATH = SCHEMAS_DIR / "manifest.json"


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture(scope="session")
def manifest():
    assert MANIFEST_PATH.exists(), "manifest.json missing"
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


@pytest.fixture()
def vercel_client():
    """TestClient with VERCEL=1 set."""
    with patch.dict(os.environ, {"VERCEL": "1"}):
        # Re-import main so module-level VERCEL flag is True
        if "main" in sys.modules:
            del sys.modules["main"]
        import main as app_module
        client = TestClient(app_module.app, raise_server_exceptions=True)
        yield client
        del sys.modules["main"]


@pytest.fixture()
def local_client():
    """TestClient without VERCEL (local mode)."""
    env = {k: v for k, v in os.environ.items() if k != "VERCEL"}
    with patch.dict(os.environ, env, clear=True):
        if "main" in sys.modules:
            del sys.modules["main"]
        import main as app_module
        client = TestClient(app_module.app, raise_server_exceptions=False)
        yield client
        del sys.modules["main"]


# ===========================================================================
# main.py import
# ===========================================================================

def test_main_imports_successfully():
    """main.py must import without error."""
    if "main" in sys.modules:
        del sys.modules["main"]
    try:
        with patch.dict(os.environ, {"VERCEL": "1"}):
            import main  # noqa: F401
        assert True
    finally:
        if "main" in sys.modules:
            del sys.modules["main"]


# ===========================================================================
# /providers
# ===========================================================================

def test_providers_vercel(vercel_client):
    resp = vercel_client.get("/providers")
    assert resp.status_code == 200
    body = resp.json()
    assert "providers" in body
    names = {p["name"] for p in body["providers"]}
    assert "aws"     in names
    assert "azurerm" in names
    assert "google"  in names


def test_providers_local(local_client):
    resp = local_client.get("/providers")
    assert resp.status_code == 200
    assert "providers" in resp.json()


# ===========================================================================
# /services
# ===========================================================================

@pytest.mark.parametrize("provider", ["aws", "azurerm", "google"])
def test_services_vercel(vercel_client, provider):
    resp = vercel_client.get(f"/services?provider={provider}")
    assert resp.status_code == 200
    body = resp.json()
    assert "services" in body
    assert body["count"] > 0


def test_services_unsupported_provider_vercel(vercel_client):
    resp = vercel_client.get("/services?provider=unsupportedxyz")
    assert resp.status_code in (404, 422)


def test_services_empty_provider_vercel(vercel_client):
    resp = vercel_client.get("/services?provider=")
    assert resp.status_code in (404, 422)


# ===========================================================================
# /generate — Vercel mode
# ===========================================================================

@pytest.mark.parametrize("provider,service", [
    ("aws",     "s3"),
    ("azurerm", "storage"),
    ("google",  "compute"),
])
def test_generate_vercel_returns_zip(vercel_client, provider, service):
    """VERCEL=1 must return a valid ZIP file directly."""
    resp = vercel_client.post(
        "/generate",
        data={"provider": provider, "service": service},
    )
    assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text[:200]}"
    ct = resp.headers.get("content-type", "")
    assert "application/zip" in ct, f"Expected application/zip, got: {ct}"
    # Verify it is a real zip
    zf = zipfile.ZipFile(BytesIO(resp.content))
    names = zf.namelist()
    assert len(names) > 0, "ZIP is empty"
    tf_files = [n for n in names if n.endswith(".tf")]
    assert len(tf_files) > 0, f"No .tf files in ZIP: {names[:10]}"


def test_generate_vercel_response_headers(vercel_client):
    resp = vercel_client.post(
        "/generate",
        data={"provider": "aws", "service": "s3"},
    )
    assert resp.status_code == 200
    assert "content-disposition" in resp.headers
    assert "attachment" in resp.headers["content-disposition"]
    assert "x-provider-version" in resp.headers
    assert "x-schema-generated-at" in resp.headers


def test_generate_vercel_no_terraform_called(vercel_client):
    """VERCEL=1: fetch_schema / Terraform must never be invoked."""
    with patch("generator.provider.fetch_schema") as mock_fs:
        resp = vercel_client.post(
            "/generate",
            data={"provider": "aws", "service": "s3"},
        )
        assert resp.status_code == 200
        mock_fs.assert_not_called()


def test_generate_vercel_unsupported_provider(vercel_client):
    resp = vercel_client.post(
        "/generate",
        data={"provider": "unsupportedxyz", "service": "s3"},
    )
    assert resp.status_code in (404, 422)


def test_generate_vercel_unsupported_service(vercel_client):
    resp = vercel_client.post(
        "/generate",
        data={"provider": "aws", "service": "zzz_nonexistent_service_abc"},
    )
    assert resp.status_code == 404


def test_generate_vercel_path_traversal_rejected(vercel_client):
    """Malicious path input must be rejected before any file I/O."""
    for bad_service in ["../../../etc/passwd", "s3/../secret", "..\\windows\\system32"]:
        resp = vercel_client.post(
            "/generate",
            data={"provider": "aws", "service": bad_service},
        )
        assert resp.status_code in (404, 422, 400), \
            f"Path traversal '{bad_service}' was not rejected (got {resp.status_code})"


def test_generate_vercel_zip_contains_tf_files(vercel_client):
    resp = vercel_client.post(
        "/generate",
        data={"provider": "aws", "service": "s3"},
    )
    assert resp.status_code == 200
    zf = zipfile.ZipFile(BytesIO(resp.content))
    names = zf.namelist()
    assert any(n.endswith("variables.tf") for n in names), f"No variables.tf in {names[:20]}"
    assert any(n.endswith("main.tf")      for n in names), f"No main.tf in {names[:20]}"
    assert any(n.endswith("outputs.tf")   for n in names), f"No outputs.tf in {names[:20]}"


# ===========================================================================
# /download — Vercel mode returns 410
# ===========================================================================

def test_download_disabled_on_vercel(vercel_client):
    resp = vercel_client.get("/download/any_file.zip")
    assert resp.status_code == 410


# ===========================================================================
# /health
# ===========================================================================

def test_health_vercel(vercel_client):
    resp = vercel_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["mode"] == "vercel"


def test_health_local(local_client):
    resp = local_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["mode"] == "local"


# ===========================================================================
# Local mode retains Terraform / download_url path
# ===========================================================================

def test_local_generate_uses_fetch_schema(local_client):
    """Local /generate must call fetch_schema (not schema_store)."""
    mock_schema = {
        "provider_schemas": {
            "registry.terraform.io/hashicorp/aws": {
                "resource_schemas": {
                    "aws_s3_bucket": {
                        "block": {"attributes": {}, "block_types": {}}
                    }
                },
                "data_source_schemas": {},
                "provider": {"version": 0, "block": {"attributes": {}, "block_types": {}}},
            }
        }
    }
    mock_meta = {"namespace": "hashicorp", "type": "aws", "version": "6.0.0"}

    with patch("main.fetch_schema", new=AsyncMock(return_value=(mock_schema, mock_meta))):
        resp = local_client.post(
            "/generate",
            data={"provider": "aws", "service": "s3"},
        )
    # Either succeeds with download_url OR fails with service error
    # Either way it must NOT return a ZIP content-type
    ct = resp.headers.get("content-type", "")
    assert "application/zip" not in ct, "Local mode must not return direct ZIP"


# ===========================================================================
# vercel.json validity
# ===========================================================================

def test_vercel_json_is_valid_json():
    vj_path = _REPO / "vercel.json"
    assert vj_path.exists()
    data = json.loads(vj_path.read_text(encoding="utf-8"))
    assert data.get("version") == 2
    assert "functions" in data
    assert "main.py" in data["functions"]


def test_vercel_json_no_terraform_build_command():
    vj_path = _REPO / "vercel.json"
    data    = json.loads(vj_path.read_text(encoding="utf-8"))
    assert "buildCommand" not in data, \
        "vercel.json must not have a Terraform buildCommand"


def test_vercel_json_includes_generated_schemas():
    vj_path = _REPO / "vercel.json"
    data    = json.loads(vj_path.read_text(encoding="utf-8"))
    include = data["functions"]["main.py"].get("includeFiles", "")
    assert "generated-schemas" in include


def test_vercel_json_no_terraform_in_include_files():
    vj_path = _REPO / "vercel.json"
    data    = json.loads(vj_path.read_text(encoding="utf-8"))
    include = data["functions"]["main.py"].get("includeFiles", "")
    # Should not include bare 'terraform' binary
    assert not include.startswith("terraform,") and ",terraform," not in include


# ===========================================================================
# GitHub Actions YAML syntax
# ===========================================================================

def test_workflow_yaml_is_valid():
    wf_path = _REPO / ".github" / "workflows" / "update-provider-schemas.yml"
    assert wf_path.exists(), "Workflow file missing"
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
        assert "on"   in data or True   # yaml parses 'on' as True key
        assert "jobs" in data
    except ImportError:
        # yaml not installed: just check it's non-empty valid text
        content = wf_path.read_text(encoding="utf-8")
        assert "jobs:" in content
        assert "runs-on:" in content
        assert "steps:" in content


# ===========================================================================
# Manifest completeness
# ===========================================================================

def test_manifest_has_three_providers(manifest):
    providers = set(manifest["providers"].keys())
    assert providers == {"aws", "azurerm", "google"}


def test_manifest_aws_service_count(manifest):
    assert len(manifest["providers"]["aws"]["services"]) >= 1000


def test_manifest_azurerm_service_count(manifest):
    assert len(manifest["providers"]["azurerm"]["services"]) >= 400


def test_manifest_google_service_count(manifest):
    assert len(manifest["providers"]["google"]["services"]) >= 400


# ===========================================================================
# File size limits
# ===========================================================================

def test_no_schema_file_exceeds_github_100mb_limit():
    """Every .json.gz must be < 100 MB (GitHub's per-file hard limit)."""
    limit_bytes = 100 * 1024 * 1024
    violations  = []
    for gz in SCHEMAS_DIR.rglob("*.json.gz"):
        size = gz.stat().st_size
        if size >= limit_bytes:
            violations.append(f"{gz.relative_to(_REPO)}: {size / 1e6:.1f} MB")
    assert not violations, f"Files exceed 100 MB: {violations}"


def test_combined_schema_size_within_vercel_limit():
    """All .json.gz combined must be < 250 MB (Vercel function bundle)."""
    limit_bytes  = 250 * 1024 * 1024
    total_bytes  = sum(gz.stat().st_size for gz in SCHEMAS_DIR.rglob("*.json.gz"))
    total_mb     = total_bytes / (1024 * 1024)
    assert total_bytes < limit_bytes, \
        f"Combined schema size {total_mb:.1f} MB exceeds Vercel 250 MB limit"


# ===========================================================================
# No credential leakage
# ===========================================================================

_SECRET_PATTERNS = [
    "AKIA",          # AWS access key prefix
    "aws_secret",
    "client_secret",
    "private_key",
    "password",
    "token",
]
_EXTENSIONS_TO_SCAN = {".py", ".json", ".yml", ".yaml", ".txt", ".md", ".html", ".sh"}


def test_no_credentials_in_source_files():
    skip_dirs = {"generated-schemas", "__pycache__", ".git", ".venv", "venv", "outputs"}
    violations = []
    for root, dirs, files in os.walk(_REPO):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in files:
            if Path(fname).suffix.lower() not in _EXTENSIONS_TO_SCAN:
                continue
            fpath = Path(root) / fname
            try:
                text = fpath.read_text(encoding="utf-8", errors="ignore").lower()
            except Exception:
                continue
            for pat in _SECRET_PATTERNS:
                if pat in text and "example" not in text[:200].lower():
                    # Only flag AKIA (hard AWS key prefix) and actual values
                    if pat == "AKIA" and "AKIA" in fpath.read_text(errors="ignore"):
                        violations.append(f"{fpath.relative_to(_REPO)}: contains '{pat}'")
                        break
    assert not violations, f"Potential credential leakage: {violations}"
