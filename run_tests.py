"""
run_tests.py  —  Cross-CSP integration test suite
Tests all 3 major cloud providers with both common and uncommon services,
including alias / free-text inputs like 'AMP (Managed Prometheus)'.
"""
import json
import urllib.parse
import urllib.request
import sys

BASE = "http://localhost:8000"

TESTS = [
    # (provider,  service input,              description)
    # ── AWS ──────────────────────────────────────────────────────────────
    ("aws",      "AMP (Managed Prometheus)",  "AWS AMP alias with parens → prometheus"),
    ("aws",      "mwaa",                      "AWS Managed Apache Airflow"),
    ("aws",      "ivs",                       "AWS Interactive Video Service"),
    ("aws",      "codecatalyst",              "AWS CodeCatalyst"),
    ("aws",      "grafana",                   "AWS Managed Grafana"),
    ("aws",      "apprunner",                 "AWS App Runner"),
    # ── Azure ─────────────────────────────────────────────────────────────
    ("azurerm",  "chaos_studio",              "Azure Chaos Studio (multi-word)"),
    ("azurerm",  "lighthouse",                "Azure Lighthouse"),
    ("azurerm",  "sentinel",                  "Azure Sentinel"),
    ("azurerm",  "pim",                       "Azure PIM"),
    ("azurerm",  "mongo_cluster",             "Azure Cosmos Mongo Cluster"),
    # ── Google ────────────────────────────────────────────────────────────
    ("google",   "BeyondCorp",                "Google BeyondCorp (mixed case)"),
    ("google",   "apigee",                    "Google Apigee"),
    ("google",   "alloydb",                   "Google AlloyDB"),
    ("google",   "looker",                    "Google Looker"),
    ("google",   "dataplex",                  "Google Dataplex"),
]

def test(provider: str, service: str, description: str) -> dict:
    body = urllib.parse.urlencode({"provider": provider, "service": service}).encode()
    req  = urllib.request.Request(
        f"{BASE}/generate",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return {"ok": True, **json.loads(resp.read())}
    except urllib.error.HTTPError as e:
        detail = json.loads(e.read()).get("detail", "unknown error")
        return {"ok": False, "detail": detail}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def main():
    passed = failed = 0
    print(f"\n{'-'*90}")
    print(f"  {'PROVIDER':<12} {'INPUT':<32} {'RESULT'}")
    print(f"{'-'*90}")

    for provider, service, description in TESTS:
        r = test(provider, service, description)
        if r["ok"]:
            passed += 1
            resolved  = r.get("service", "?")
            version   = r.get("version", "?")
            n         = len(r.get("resources", []))
            indicator = "OK" if resolved == service.lower().replace(" ", "_") else f"OK (-> {resolved})"
            print(f"  PASS  {provider:<12} {service:<32} v{version:<14} {n} resources  {indicator}")
        else:
            failed += 1
            detail = r["detail"][:75]
            print(f"  FAIL  {provider:<12} {service:<32} {detail}")

    print(f"{'-'*90}")
    print(f"  Results: {passed} passed  /  {failed} failed  /  {len(TESTS)} total\n")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
