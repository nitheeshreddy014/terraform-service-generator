"""
main.py
-------
FastAPI application entry point.

Endpoints
---------
GET  /                  → serves the HTML frontend (static/index.html)
POST /generate          → runs the full pipeline and returns a download URL
GET  /download/{fname}  → streams the generated .zip file
GET  /health            → simple liveness check
"""

from __future__ import annotations

import logging
import tempfile
import traceback
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from generator.file_generator import generate_service_folder
from generator.provider import fetch_schema
from generator.schema_parser import extract_service_resources, list_available_services
from generator.zipper import zip_service_folder

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Terraform Service Generator",
    description=(
        "Auto-generates a Terraform service folder from any cloud provider's "
        "latest provider schema. 100 % free, open-source, no AI or paid APIs."
    ),
    version="1.0.0",
)

BASE_DIR    = Path(__file__).parent
OUTPUTS_DIR = BASE_DIR / "outputs"
OUTPUTS_DIR.mkdir(exist_ok=True)

# Serve static files (index.html, any CSS/JS you add later)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index() -> HTMLResponse:
    """Serve the frontend."""
    html = (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html)


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.post("/generate")
async def generate(
    provider: str = Form(..., description="Cloud provider name, e.g. aws, azurerm, google"),
    service:  str = Form(..., description="Service / resource prefix, e.g. s3, ec2, storage"),
) -> JSONResponse:
    """
    Full generation pipeline:
      1. Resolve provider → fetch latest version from Terraform Registry.
      2. Run terraform init + terraform providers schema -json.
      3. Parse schema for the requested service.
      4. Write the folder structure to disk.
      5. Zip it up.
      6. Return a JSON body with a download URL.
    """
    # Normalise both inputs: lowercase, strip whitespace, remove separators.
    # This means 'Bedrock_AgentCore', 'bedrock-agentcore', 'Bedrock AgentCore'
    # all resolve correctly to 'bedrockagentcore'.
    import re
    provider = re.sub(r'[\s_\-]+', '', provider.strip().lower())
    service  = re.sub(r'[\s_\-]+', '', service.strip().lower())

    if not provider:
        raise HTTPException(status_code=422, detail="'provider' must not be empty.")
    if not service:
        raise HTTPException(status_code=422, detail="'service' must not be empty.")

    logger.info("▶  generate  provider=%s  service=%s", provider, service)

    # ------------------------------------------------------------------
    # Step 1 + 2 — fetch schema (runs terraform under the hood)
    # ------------------------------------------------------------------
    try:
        schema, provider_meta = await fetch_schema(provider)
    except RuntimeError as exc:
        logger.error("Schema fetch failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    except ValueError as exc:
        logger.warning("Provider resolution failed: %s", exc)
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error("Unexpected error during schema fetch:\n%s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Unexpected error: {exc}")

    # ------------------------------------------------------------------
    # Step 3 — parse schema for requested service
    # ------------------------------------------------------------------
    try:
        resources = extract_service_resources(schema, provider, service)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    if not resources:
        available = list_available_services(schema, provider)
        hint = (
            f"No resources found for service '{service}' under provider '{provider}'. "
            f"Available services: {', '.join(available[:40])}"
            + (" …" if len(available) > 40 else "")
        )
        raise HTTPException(status_code=404, detail=hint)

    logger.info("Found %d resource(s) for %s_%s_*", len(resources), provider, service)

    # ------------------------------------------------------------------
    # Step 4 — generate folder tree in a temp directory
    # ------------------------------------------------------------------
    try:
        with tempfile.TemporaryDirectory(prefix="tfgen_out_") as tmpdir:
            service_root = generate_service_folder(
                base_dir=tmpdir,
                provider_name=provider,
                service_name=service,
                resources=resources,
                provider_meta=provider_meta,
            )

            # ------------------------------------------------------------------
            # Step 5 — zip
            # ------------------------------------------------------------------
            zip_path = zip_service_folder(service_root, OUTPUTS_DIR)

    except Exception as exc:
        logger.error("Generation failed:\n%s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Generation error: {exc}")

    logger.info("✔  Zip ready: %s", zip_path.name)

    # ------------------------------------------------------------------
    # Step 6 — return download URL
    # ------------------------------------------------------------------
    return JSONResponse({
        "status":        "ok",
        "provider":      provider,
        "service":       service,
        "version":       provider_meta["version"],
        "resources":     [r["name"] for r in resources],
        "download_url":  f"/download/{zip_path.name}",
        "filename":      zip_path.name,
    })


@app.get("/download/{filename}")
async def download(filename: str) -> FileResponse:
    """Stream a previously generated zip file to the browser."""
    # Sanitise: no path traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")

    zip_path = OUTPUTS_DIR / filename
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="File not found. Please regenerate.")

    return FileResponse(
        path=zip_path,
        media_type="application/zip",
        filename=filename,
    )



# ── Popular providers (frontend autocomplete) ────────────────────────────────
_POPULAR_PROVIDERS = [
    {"name": "aws",        "namespace": "hashicorp",    "description": "Amazon Web Services"},
    {"name": "azurerm",    "namespace": "hashicorp",    "description": "Microsoft Azure"},
    {"name": "google",     "namespace": "hashicorp",    "description": "Google Cloud Platform"},
    {"name": "kubernetes", "namespace": "hashicorp",    "description": "Kubernetes"},
    {"name": "helm",       "namespace": "hashicorp",    "description": "Helm charts"},
    {"name": "vault",      "namespace": "hashicorp",    "description": "HashiCorp Vault"},
    {"name": "datadog",    "namespace": "DataDog",      "description": "Datadog monitoring"},
    {"name": "github",     "namespace": "integrations", "description": "GitHub"},
    {"name": "cloudflare", "namespace": "cloudflare",   "description": "Cloudflare"},
]


@app.get("/providers")
async def list_providers() -> JSONResponse:
    """Return a curated list of popular Terraform providers for frontend autocomplete."""
    return JSONResponse({"providers": _POPULAR_PROVIDERS})


@app.get("/services")
async def list_services(provider: str) -> JSONResponse:
    """
    Return every available service prefix for a given provider.
    Uses the cached schema on repeated calls — near-instant second call.
    """
    import re as _re
    provider = _re.sub(r"[\s_\-]+", "", provider.strip().lower())
    if not provider:
        raise HTTPException(status_code=422, detail="'provider' must not be empty.")
    logger.info("list_services provider=%s", provider)
    try:
        schema, provider_meta = await fetch_schema(provider)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    services = list_available_services(schema, provider)
    return JSONResponse({
        "provider": provider,
        "version":  provider_meta["version"],
        "services": services,
        "count":    len(services),
    })

# ---------------------------------------------------------------------------
# Dev runner
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
