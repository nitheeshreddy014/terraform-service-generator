# ⚡ Terraform Service Generator

> Auto-generates a complete, production-shaped Terraform service folder from
> **any** cloud provider's latest published schema — no AI, no paid APIs,
> 100 % free and open-source.

---

## Table of Contents

1. [Overview](#overview)
2. [How It Works](#how-it-works)
3. [Automated Schema Updates](#automated-schema-updates)
4. [Prerequisites](#prerequisites)
5. [Installation](#installation)
6. [Running the App](#running-the-app)
7. [Using the Frontend](#using-the-frontend)
8. [API Reference](#api-reference)
9. [Generated Folder Structure](#generated-folder-structure)
10. [Example Output](#example-output)
11. [Project Layout](#project-layout)
12. [Configuration & Customisation](#configuration--customisation)
13. [Contributing](#contributing)
14. [License](#license)

---

## Overview

Given a **provider name** (e.g. `aws`) and a **service prefix** (e.g. `s3`),
this tool will:

| Step | What happens |
|------|-------------|
| 1 | Query the Terraform Registry API for the **latest** provider version (3-strategy semver resolution) |
| 2 | Spin up a throw-away workspace and run `terraform init` |
| 3 | Execute `terraform providers schema -json` — written to file (no pipe-buffer limit) |
| 4 | Normalise service input — strip special chars, try exact / segment / prefix / fuzzy fallbacks |
| 5 | Filter every `aws_s3_*` resource and data-source from the schema |
| 6 | Generate `variables.tf`, `main.tf`, `outputs.tf`, `terraform.tfvars.example` per resource |
| 7 | Scaffold `profiles/dev.tfvars`, `profiles/prod.tfvars`, `test/main_test.go`, `examples/complete/`, `Makefile`, `dr/` |
| 8 | Write a pinned `versions.tf` and an auto-generated `README.md` |
| 9 | Zip everything up and serve a one-click download link |

Everything is **deterministic** — the same provider + service + version always
produces the same files.

---

## How It Works

The app runs in **two modes** depending on the environment:

### Mode 1 — Vercel / Production (pre-generated schemas)

On Vercel the filesystem is read-only, so Terraform cannot run live.
Instead, the app reads **pre-baked, compressed schemas** committed into the
repository under `generated-schemas/`. These are refreshed automatically
every week by a GitHub Actions workflow (see [Automated Schema Updates](#automated-schema-updates)).

```
Browser  ──POST /generate──►  FastAPI
                                │
                          load schema from
                          generated-schemas/<provider>/<service>.json.gz
                                │
                          extract_service_resources()
                          (schema_parser)
                                │
                          generate_service_folder()
                          (file_generator)
                                │
                          zip_service_folder()
                          (zipper)
                                │
         ◄── JSON { download_url } ──
                                │
Browser  ──GET /download/x.zip──► FileResponse
```

### Mode 2 — Local / Docker (live Terraform)

When running locally or in Docker, the app fetches schemas on-demand
via a real `terraform init` + `terraform providers schema -json` call.

```
Browser  ──POST /generate──►  FastAPI
                                │
                          resolve_provider()
                          (Terraform Registry API)
                                │
                          fetch_schema()
                          terraform init + providers schema -json
                                │
                          extract_service_resources()
                          (schema_parser)
                                │
                          generate_service_folder()
                          (file_generator)
                                │
                          zip_service_folder()
                          (zipper)
                                │
         ◄── JSON { download_url } ──
                                │
Browser  ──GET /download/x.zip──► FileResponse
```

---

## Automated Schema Updates

Provider schemas are kept up-to-date automatically — no manual intervention needed.

### How it works

| Trigger | When |
|---|---|
| Scheduled | Every **Tuesday at 03:00 UTC** |
| Push | Every push to **any branch** |
| Manual | Via **GitHub Actions → Run workflow** |

On each run the workflow:
1. Reads all providers from **`providers.yml`**
2. Downloads the **latest stable version** of each from the Terraform Registry
3. Splits the schema into per-service `.json.gz` files into a **staging directory**
4. **Validates** every file before touching the live folder
5. **Atomically replaces** `generated-schemas/<provider>/` with the validated staging data
6. **Commits and pushes** the updated schemas back (`[skip ci]` prevents an infinite loop)
7. **Restores the backup** automatically if anything fails

### Adding a new CSP provider

Edit **`providers.yml`** — that's it. No changes to any script or workflow file needed.

```yaml
# providers.yml
providers:
  - namespace: hashicorp
    type: aws
  - namespace: hashicorp
    type: azurerm
  - namespace: hashicorp
    type: google
  - namespace: hashicorp   # ← just add this
    type: kubernetes        # ← commit & push — workflow handles the rest
```

The namespace and type must match the [Terraform Registry](https://registry.terraform.io/browse/providers).

---

## Prerequisites

| Requirement | Minimum version | Notes |
|-------------|----------------|-------|
| Python | 3.11+ | 3.12 recommended |
| [Terraform CLI](https://developer.hashicorp.com/terraform/install) | 1.3.0+ | Local/Docker mode only — **not needed on Vercel** |
| pip | latest | `python -m pip install --upgrade pip` |
| Internet access | — | Registry API + provider download |

Verify Terraform is installed:

```bash
terraform version
```

---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/your-org/terraform-generator.git
cd terraform-generator

# 2. Create and activate a virtual environment (recommended)
python -m venv .venv

# Linux / macOS
source .venv/bin/activate

# Windows PowerShell
.\.venv\Scripts\Activate.ps1

# 3. Install Python dependencies
pip install -r requirements.txt
```

---

## Running the App

```bash
# Development (auto-reload on file changes)
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Production
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2
```

Then open **http://localhost:8000** in your browser.

> **Note:** The first generation for a new provider can take **30–90 seconds**
> because Terraform must download the provider binary (~100–400 MB for large
> providers like `aws`). Terraform caches provider binaries in
> `~/.terraform.d/plugin-cache` automatically, so subsequent runs for the
> **same provider** are significantly faster.

---

## Using the Frontend

1. Open **http://localhost:8000**
2. Enter a **Cloud Provider** name:
   - `aws` — Amazon Web Services
   - `azurerm` — Microsoft Azure
   - `google` — Google Cloud Platform
   - `kubernetes`, `vault`, `datadog`, etc.
3. Enter a **Service / Resource Prefix** — the tool auto-normalises your input:
   - Special characters `()[]./` are stripped automatically
   - Spaces and hyphens are converted to underscores
   - Multi-word aliases resolve via segment fallback (e.g. `AMP (Managed Prometheus)` → `prometheus`)
   - Examples: `s3`, `ec2`, `lambda`, `chaos_studio`, `bedrockagent`, `prometheus`
   - If unsure, submit anything — the error message lists every valid service
4. Click **Generate Terraform Modules**
5. Watch the live progress steps
6. Click **Download ZIP** when ready

### Service naming quick reference

| What you type | Resolves to | Why |
|---|---|---|
| `s3` | `s3` | exact match |
| `chaos_studio` | `chaos_studio` | multi-word with underscore |
| `Bedrock_AgentCore` | `bedrockagentcore` | stripped + concatenated |
| `AMP (Managed Prometheus)` | `prometheus` | parens stripped, segment `prometheus` matched |
| `BeyondCorp` | `beyondcorp` | lowercased |

---

## API Reference

Interactive Swagger UI is available at **http://localhost:8000/docs**

### `POST /generate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `provider` | `string` (form) | ✅ | Provider name, e.g. `aws` |
| `service`  | `string` (form) | ✅ | Service prefix, e.g. `s3` |

**Success response `200`:**

```json
{
  "status":       "ok",
  "provider":     "aws",
  "service":      "s3",
  "version":      "5.54.1",
  "resources":    ["aws_s3_bucket", "aws_s3_bucket_acl", "..."],
  "download_url": "/download/s3.zip",
  "filename":     "s3.zip"
}
```

**Error response `404` — with smart suggestions:**

If the service name is close to a valid one, the API returns a `Did you mean?` hint:

```json
{
  "detail": "Service 's33' not found under provider 'aws'. Did you mean: s3, s3files?"
}
```

If nothing close is found, it falls back to listing available services:

```json
{
  "detail": "Service 'xyz' not found under provider 'aws'. Available services: acm, acmpca, ..."
}
```

### `GET /download/{filename}`

Streams the generated `.zip` file. Safe against path-traversal attacks.

### `GET /health`

```json
{ "status": "ok" }
```

---

## Generated Folder Structure

```
<service>/
├── modules/
│   ├── <provider>_<service>_<resource_a>/
│   │   ├── variables.tf              ← one variable per schema attribute + block_type
│   │   ├── main.tf                   ← resource/data block wired to var.*
│   │   ├── outputs.tf                ← all computed attributes exposed as outputs
│   │   └── terraform.tfvars.example  ← required + optional vars with placeholders
│   ├── <provider>_<service>_<resource_b>/
│   │   └── ...
│   └── ...
├── examples/
│   └── complete/
│       ├── main.tf                   ← wires every module together as a working reference
│       ├── variables.tf              ← inputs for the complete example
│       ├── outputs.tf                ← exposes key IDs from all modules
│       └── terraform.tfvars.example  ← copy → terraform.tfvars and fill in values
├── profiles/
│   ├── dev.tfvars                    ← dev environment variable overrides
│   └── prod.tfvars                   ← prod environment variable overrides
├── test/
│   └── main_test.go                  ← Terratest boilerplate (init+plan + per-module validate)
├── dr/
│   └── .gitkeep                      ← scaffold: disaster-recovery workspace
├── Makefile                          ← init / validate / fmt / plan / apply / destroy / test
├── versions.tf                       ← pinned provider source + version constraint
└── README.md                         ← auto-generated docs for this service
```

### What each generated file contains

#### `variables.tf`
- One `variable` block per schema **attribute** (required attrs have no default)
- One `variable` block per schema **block_type** (typed as `list(object(...))`,
  `map(...)`, or the single-object equivalent)
- Sensitive attributes get `sensitive = true`

#### `main.tf`
- A single `resource` or `data` block referencing every `var.<attr>`
- Nested blocks rendered as `dynamic` blocks iterating over the corresponding
  list/map variable

#### `outputs.tf`
- Every **computed** attribute exposed as an `output`
- Sensitive computed attrs get `sensitive = true`
- Always includes `<resource>_id` if the resource has an `id` attribute

#### `versions.tf` (top-level)
- Pins the exact provider source (`registry.terraform.io/<namespace>/<type>`)
- Uses a pessimistic version constraint (`~> X.Y`)
- Requires Terraform `>= 1.3.0`

---

## Example Output

Running with `provider=aws`, `service=s3` against provider `5.x` produces:

```
s3/
├── modules/
│   ├── aws_s3_bucket/
│   │   ├── variables.tf              (bucket, force_destroy, object_lock_enabled, tags, …)
│   │   ├── main.tf
│   │   ├── outputs.tf                (id, arn, bucket_domain_name, …)
│   │   └── terraform.tfvars.example
│   ├── aws_s3_bucket_acl/
│   │   └── ...
│   ├── aws_s3_bucket_cors_configuration/
│   │   └── ...
│   └── ... (30+ resources)
├── examples/complete/
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   └── terraform.tfvars.example
├── profiles/
│   ├── dev.tfvars
│   └── prod.tfvars
├── test/
│   └── main_test.go
├── dr/        .gitkeep
├── Makefile
├── versions.tf
└── README.md
```

---

## Project Layout

```
terraform-generator/
├── main.py                  ← FastAPI app + all route handlers
├── requirements.txt         ← Python dependencies
├── providers.yml            ← CSP provider list — add new providers here
├── run_tests.py             ← Cross-CSP integration test suite (16 tests)
├── Dockerfile               ← Multi-stage build with pinned Terraform binary
├── docker-compose.yml       ← One-command start with provider cache volume
├── .github/
│   └── workflows/
│       └── update-provider-schemas.yml  ← Weekly auto-update workflow
├── scripts/
│   ├── generate_schemas.py  ← Downloads providers + splits into .json.gz files
│   └── validate_schemas.py  ← Validates every schema file before going live
├── generated-schemas/       ← Pre-baked schemas used by Vercel (auto-updated)
│   ├── aws/                 ← *.json.gz — one file per AWS service
│   ├── azurerm/             ← *.json.gz — one file per Azure service
│   ├── google/              ← *.json.gz — one file per GCP service
│   └── manifest.json        ← Index of all providers, versions and services
├── generator/
│   ├── __init__.py
│   ├── provider.py          ← Registry API lookup + terraform init/schema (file-based)
│   ├── schema_parser.py     ← Schema JSON → structured Python dicts
│   ├── file_generator.py    ← Dicts → .tf file content + full folder tree
│   └── zipper.py            ← Folder → .zip archive
├── static/
│   └── index.html           ← Single-page frontend (no framework)
├── outputs/                 ← Generated zips are stored here (git-ignored)
└── README.md                ← This file
```

---

## Configuration & Customisation

| What | Where | How |
|------|-------|-----|
| Add / remove a CSP provider | `providers.yml` | Add an entry, push — workflow auto-downloads on next run |
| Schema update schedule | `.github/workflows/update-provider-schemas.yml` | Change the `cron` expression |
| Server host / port | CLI | `uvicorn main:app --port 9000` |
| Output directory | `main.py` | Change `OUTPUTS_DIR` |
| Terraform version floor | `file_generator.py` | Edit `_render_versions_tf()` |
| Add custom README sections | `file_generator.py` | Edit `_render_readme()` |
| Provider binary cache | Terraform | Stored in `~/.terraform.d/plugin-cache` automatically |
| Cache old zips | `main.py` `/download` route | Already persisted in `outputs/` |

### Cleaning up old zips

```bash
# Delete zips older than 1 day
find outputs/ -name "*.zip" -mtime +1 -delete
```

### Clearing the schema cache

```bash
# Force re-download of provider schema on next generation
rm -rf ~/.terraform-generator-cache/
```

---

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feat/my-feature`)
3. Make your changes and add tests if applicable
4. Open a Pull Request

Bug reports and feature requests welcome via GitHub Issues.

---

## License

MIT © your-org

```
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software...
```

Full license text: [LICENSE](LICENSE)
