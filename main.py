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

# On Vercel the task directory (/var/task) is read-only at runtime;
# only /tmp is writable. Locally the outputs/ sibling folder is used.
import os as _os
OUTPUTS_DIR = (
    Path("/tmp/tfgen-outputs")
    if _os.environ.get("VERCEL") == "1"
    else BASE_DIR / "outputs"
)
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

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
    import re

    # --- Provider normalisation -------------------------------------------
    # Provider names are always single words (aws, azurerm, google).
    # Strip everything that is not a letter or digit.
    provider = re.sub(r'[^a-z0-9]', '', provider.strip().lower())

    # --- Service normalisation --------------------------------------------
    # Users may type product names, aliases, or free text, e.g.:
    #   'AMP (Managed Prometheus)'  ->  'amp_managed_prometheus'
    #   'chaos_studio'              ->  'chaos_studio'
    #   'Bedrock_AgentCore'         ->  'bedrock_agentcore'
    #
    # Pipeline (in order):
    #  1. lowercase
    #  2. replace every non-alphanumeric char (parens, dots, slashes…) with a space
    #  3. collapse runs of spaces / underscores / hyphens into a single '_'
    #  4. strip leading / trailing underscores
    #
    # This gives a clean 'snake_case' base from ANY user input.
    s = service.strip().lower()
    s = re.sub(r'[^a-z0-9\s_\-]', ' ', s)   # '(' ')' '/' '.' etc. → space
    s = re.sub(r'[\s_\-]+', '_', s).strip('_')  # collapse separators

    service_raw      = s                              # e.g. 'amp_managed_prometheus'
    service_stripped = service_raw.replace('_', '')   # e.g. 'ampmanagedprometheus'
    service_segments = [seg for seg in service_raw.split('_') if seg]  # ['amp','managed','prometheus']
    service          = service_raw                    # first attempt uses full cleaned string

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
        import difflib
        available = list_available_services(schema, provider)

        # Fallback 1: try fully-stripped form (e.g. chaosstudio -> still nothing,
        # but Bedrock_AgentCore -> bedrockagentcore -> matches)
        if service_stripped != service:
            try:
                alt = extract_service_resources(schema, provider, service_stripped)
                if alt:
                    service   = service_stripped
                    resources = alt
                    logger.info("Stripped fallback '%s' matched %d resource(s)", service, len(alt))
            except Exception:
                pass

        # Fallback 2: try each clean word segment individually
        # e.g. 'amp_managed_prometheus' -> try 'amp', 'managed', 'prometheus'
        # 'prometheus' matches aws_prometheus_* -> success
        if not resources:
            for seg in service_segments:
                if seg and seg not in (service, service_stripped):
                    try:
                        alt = extract_service_resources(schema, provider, seg)
                        if alt:
                            service   = seg
                            resources = alt
                            logger.info("Segment fallback '%s' matched %d resource(s)", seg, len(alt))
                            break
                    except Exception:
                        pass

        # Fallback 3: also try two-word combos from segments
        # e.g. segments ['managed', 'prometheus'] -> try 'managed_prometheus'
        if not resources and len(service_segments) >= 2:
            for i in range(len(service_segments) - 1):
                combo = f"{service_segments[i]}_{service_segments[i+1]}"
                if combo not in (service, service_stripped):
                    try:
                        alt = extract_service_resources(schema, provider, combo)
                        if alt:
                            service   = combo
                            resources = alt
                            logger.info("Combo fallback '%s' matched %d resource(s)", combo, len(alt))
                            break
                    except Exception:
                        pass

        # Fallback 4: longest available service that stripped input starts with
        # e.g. 'chaosstudio' starts with 'chaos'
        if not resources:
            for svc in sorted(available, key=len, reverse=True):
                svc_stripped = svc.replace('_', '')
                if service_stripped.startswith(svc_stripped) or svc_stripped.startswith(service_stripped):
                    try:
                        alt = extract_service_resources(schema, provider, svc)
                        if alt:
                            service   = svc
                            resources = alt
                            logger.info("Prefix fallback '%s' matched %d resource(s)", svc, len(alt))
                            break
                    except Exception:
                        pass

        # Still nothing -> fuzzy 'Did you mean' error
        if not resources:
            # fuzzy match against all segments plus the full string
            all_attempts = [service_stripped] + service_segments
            candidates  = []
            for attempt in all_attempts:
                candidates += difflib.get_close_matches(attempt, available, n=4, cutoff=0.4)
            prefix_hits = [s for s in available if any(
                s.replace('_','').startswith(seg[:4]) for seg in service_segments
            )][:4]
            suggestions = list(dict.fromkeys(candidates + prefix_hits))[:6]
            if suggestions:
                hint = (
                    f"Service '{service_raw}' not found under provider '{provider}'. "
                    f"Did you mean: {', '.join(suggestions)}?"
                )
            else:
                hint = (
                    f"Service '{service_raw}' not found under provider '{provider}'. "
                    f"Available services: {', '.join(available[:50])}"
                    + (" ..." if len(available) > 50 else "")
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
