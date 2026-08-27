# ⚡ Terraform Service Generator

> Auto-generates a complete, production-shaped Terraform service folder from
> **any** cloud provider's latest published schema — no AI, no paid APIs,
> 100 % free and open-source.

---

## Table of Contents

1. [Overview](#overview)
2. [How It Works](#how-it-works)
3. [Prerequisites](#prerequisites)
4. [Installation](#installation)
5. [Running the App](#running-the-app)
6. [Using the Frontend](#using-the-frontend)
7. [API Reference](#api-reference)
8. [Generated Folder Structure](#generated-folder-structure)
9. [Example Output](#example-output)
10. [Project Layout](#project-layout)
11. [Configuration & Customisation](#configuration--customisation)
12. [Contributing](#contributing)
13. [License](#license)

---

## Overview

Given a **provider name** (e.g. `aws`) and a **service prefix** (e.g. `s3`),
this tool will:

| Step | What happens |
|------|-------------|
| 1 | Query the Terraform Registry API for the **latest** provider version |
| 2 | Check local schema cache — if hit, skip steps 3 & 4 entirely (instant) |
| 3 | Spin up a throw-away workspace and run `terraform init` |
| 4 | Execute `terraform providers schema -json` to get the full schema + cache it |
| 5 | Filter every `aws_s3_*` resource and data-source from the schema |
| 6 | Generate `variables.tf`, `main.tf`, `outputs.tf`, `terraform.tfvars.example` per resource |
| 7 | Scaffold `profiles/dev.tfvars`, `profiles/prod.tfvars`, `test/main_test.go`, `examples/complete/`, `Makefile`, `dr/` |
| 8 | Write a pinned `versions.tf` and an auto-generated `README.md` |
| 9 | Zip everything up and serve a one-click download link |

Everything is **deterministic** — the same provider + service + version always
produces the same files.

---

## How It Works

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

## Prerequisites

| Requirement | Minimum version | Notes |
|-------------|----------------|-------|
| Python | 3.11+ | 3.12 recommended |
| [Terraform CLI](https://developer.hashicorp.com/terraform/install) | 1.3.0+ | Must be on `PATH` |
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
> providers like `aws`). Subsequent runs for the **same provider + version** are
> **instant** — the schema is cached in `~/.terraform-generator-cache/` and
> Terraform is not invoked again.

---

## Using the Frontend

1. Open **http://localhost:8000**
2. Enter a **Cloud Provider** name:
   - `aws` — Amazon Web Services
   - `azurerm` — Microsoft Azure
   - `google` — Google Cloud Platform
   - `kubernetes`, `vault`, `datadog`, etc.
3. Enter a **Service / Resource Prefix**:
   - `s3`, `ec2`, `lambda`, `rds`, `iam` (AWS)
   - `storage`, `compute`, `container` (Azure / GCP)
4. Click **Generate Terraform Modules**
5. Watch the live progress steps
6. Click **Download ZIP** when ready

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

### `GET /providers`

Returns a curated list of popular providers — useful for frontend dropdowns.

```json
{
  "providers": [
    { "name": "aws",     "namespace": "hashicorp", "description": "Amazon Web Services" },
    { "name": "azurerm", "namespace": "hashicorp", "description": "Microsoft Azure" },
    { "name": "google",  "namespace": "hashicorp", "description": "Google Cloud Platform" }
  ]
}
```

### `GET /services?provider=aws`

Returns all available service prefixes for a given provider.
Uses the cached schema — near-instant on second call.

```json
{
  "provider": "aws",
  "version":  "5.54.1",
  "services": ["acm", "ec2", "iam", "lambda", "rds", "s3", "..." ],
  "count":    142
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
├── generator/
│   ├── __init__.py
│   ├── provider.py          ← Registry API lookup + terraform init/schema
│   ├── schema_parser.py     ← Schema JSON → structured Python dicts
│   ├── file_generator.py    ← Dicts → .tf file content + folder tree
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
| Server host / port | CLI | `uvicorn main:app --port 9000` |
| Output directory | `main.py` | Change `OUTPUTS_DIR` |
| Terraform version floor | `file_generator.py` | Edit `_render_versions_tf()` |
| Add custom README sections | `file_generator.py` | Edit `_render_readme()` |
| Schema cache location | `provider.py` | Change `_CACHE_DIR` (default: `~/.terraform-generator-cache/`) |
| Clear schema cache | shell | `rm -rf ~/.terraform-generator-cache/` |
| Add more popular providers | `main.py` | Edit `_POPULAR_PROVIDERS` list |
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
