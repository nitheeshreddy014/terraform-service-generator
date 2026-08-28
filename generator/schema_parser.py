"""
schema_parser.py
----------------
Extracts all resources (and data-sources) belonging to a requested service
from the raw JSON produced by `terraform providers schema -json`.

A "service" is matched by prefix:
    provider  = "aws",   service = "s3"   → matches aws_s3_*, data.aws_s3_*
    provider  = "google", service = "storage" → matches google_storage_*
    provider  = "azurerm", service = "storage" → matches azurerm_storage_*

Returned structure per resource:
{
  "name":        "aws_s3_bucket",
  "kind":        "resource" | "data_source",
  "attributes":  { attr_name: AttrMeta, ... },
  "block_types": { block_name: BlockMeta, ... },
}

AttrMeta = {
  "type":        str,      # HCL2 type expression, e.g. "string", "list(string)"
  "description": str,
  "required":    bool,
  "optional":    bool,
  "computed":    bool,
  "sensitive":   bool,
}

BlockMeta = {
  "nesting_mode": str,          # "list" | "set" | "map" | "single"
  "min_items":    int,
  "max_items":    int | None,
  "attributes":   { ... },      # same AttrMeta shape, recursively
  "block_types":  { ... },      # nested blocks
}
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_service_resources(
    schema: dict[str, Any],
    provider_name: str,
    service_name: str,
) -> list[dict[str, Any]]:
    """
    Return a list of parsed resource dicts for every resource / data-source
    whose name contains ``<provider>_<service>_``.

    ``schema``        – raw dict from terraform providers schema -json
    ``provider_name`` – e.g. "aws"
    ``service_name``  – e.g. "s3"
    """
    provider_schemas = schema.get("provider_schemas", {})
    if not provider_schemas:
        raise ValueError("Schema contains no provider_schemas block.")

    # Find the provider key (e.g. "registry.terraform.io/hashicorp/aws")
    provider_key = _find_provider_key(provider_schemas, provider_name)
    provider_block = provider_schemas[provider_key]

    prefix = f"{provider_name}_{service_name}_"
    # also accept exact match without trailing underscore
    exact  = f"{provider_name}_{service_name}"

    results: list[dict[str, Any]] = []

    # --- managed resources ---
    for rname, rschema in provider_block.get("resource_schemas", {}).items():
        if rname.startswith(prefix) or rname == exact:
            results.append(_parse_resource(rname, "resource", rschema))

    # --- data sources ---
    for rname, rschema in provider_block.get("data_source_schemas", {}).items():
        if rname.startswith(prefix) or rname == exact:
            results.append(_parse_resource(rname, "data_source", rschema))

    return results


def list_available_services(
    schema: dict[str, Any],
    provider_name: str,
) -> list[str]:
    """
    Return a sorted list of unique service names found in the schema.
    Includes both single-word ('storage') and multi-word ('chaos_studio')
    service prefixes so that user input like 'chaos_studio' is always
    recognised and appears in 'Did you mean' suggestions.
    """
    provider_schemas = schema.get("provider_schemas", {})
    provider_key     = _find_provider_key(provider_schemas, provider_name)
    provider_block   = provider_schemas[provider_key]

    services: set[str] = set()
    prefix = f"{provider_name}_"

    all_rnames = list(provider_block.get("resource_schemas", {}).keys()) + \
                 list(provider_block.get("data_source_schemas", {}).keys())

    for rname in all_rnames:
        if not rname.startswith(prefix):
            continue
        remainder = rname[len(prefix):]        # e.g. 'chaos_studio_capability'
        parts     = remainder.split("_")       # ['chaos', 'studio', 'capability']

        # Always add single-word prefix: 'chaos'
        if parts:
            services.add(parts[0])

        # Add two-word prefix when it is genuinely a sub-service:
        # only when at least 3 segments exist (provider_svc1_svc2_resource)
        # so we don't add 'storage_account' when 'storage' is the service.
        # Heuristic: if (svc1_svc2) appears as prefix of >1 resource names,
        # it's a real multi-word service.
        if len(parts) >= 3:
            two_word = f"{parts[0]}_{parts[1]}"
            services.add(two_word)

    return sorted(services)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_provider_key(provider_schemas: dict, provider_name: str) -> str:
    """
    Locate the correct key in provider_schemas for this provider name.
    Keys look like "registry.terraform.io/hashicorp/aws".
    """
    # Exact suffix match first
    for key in provider_schemas:
        if key.endswith(f"/{provider_name}"):
            return key

    # Fallback: substring match
    for key in provider_schemas:
        if provider_name in key:
            return key

    raise ValueError(
        f"Provider '{provider_name}' not found in schema keys: "
        f"{list(provider_schemas.keys())}"
    )


def _parse_resource(
    name: str,
    kind: str,
    rschema: dict[str, Any],
) -> dict[str, Any]:
    block = rschema.get("block", {})
    return {
        "name":        name,
        "kind":        kind,
        "attributes":  _parse_attributes(block.get("attributes", {})),
        "block_types": _parse_block_types(block.get("block_types", {})),
    }


def _parse_attributes(attrs: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for attr_name, meta in attrs.items():
        result[attr_name] = {
            "type":        _type_to_hcl(meta.get("type", "string")),
            "description": meta.get("description", ""),
            "required":    meta.get("required", False),
            "optional":    meta.get("optional", False),
            "computed":    meta.get("computed", False),
            "sensitive":   meta.get("sensitive", False),
        }
    return result


def _parse_block_types(block_types: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for block_name, meta in block_types.items():
        inner = meta.get("block", {})
        result[block_name] = {
            "nesting_mode": meta.get("nesting_mode", "list"),
            "min_items":    meta.get("min_items", 0),
            "max_items":    meta.get("max_items"),   # None = unlimited
            "attributes":   _parse_attributes(inner.get("attributes", {})),
            "block_types":  _parse_block_types(inner.get("block_types", {})),
        }
    return result


def _type_to_hcl(tf_type: Any) -> str:
    """
    Convert the Terraform schema type representation to an HCL2 type string.

    tf_type can be:
      "string"
      "number"
      "bool"
      ["list",  <element_type>]
      ["set",   <element_type>]
      ["map",   <element_type>]
      ["object", {attr: type, ...}]
      ["tuple",  [type, ...]]
    """
    if isinstance(tf_type, str):
        return tf_type  # "string" | "number" | "bool" | "dynamic" | "any"

    if not isinstance(tf_type, list) or len(tf_type) < 2:
        return "any"

    kind     = tf_type[0]
    inner    = tf_type[1]

    if kind in ("list", "set", "map"):
        return f"{kind}({_type_to_hcl(inner)})"

    if kind == "object":
        if isinstance(inner, dict):
            fields = ", ".join(
                f"{k} = {_type_to_hcl(v)}" for k, v in inner.items()
            )
            return f"object({{{fields}}})"
        return "object({})"

    if kind == "tuple":
        if isinstance(inner, list):
            types = ", ".join(_type_to_hcl(t) for t in inner)
            return f"tuple([{types}])"
        return "tuple([])"

    return "any"
