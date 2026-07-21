#!/usr/bin/env python3
import argparse
import json
import subprocess
import sys
import re
from typing import Dict, List, Optional, Tuple


def run(cmd: List[str], capture: bool = False) -> str:
    print("+", " ".join(cmd))
    if capture:
        result = subprocess.run(cmd, check=True, text=True, stdout=subprocess.PIPE)
        return result.stdout.strip()
    subprocess.run(cmd, check=True)
    return ""


def run_json(cmd: List[str]) -> Dict:
    out = run(cmd, capture=True)
    return json.loads(out)


def run_capture(cmd: List[str]) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd))
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def env_name_from_id(env_id: Optional[str]) -> Optional[str]:
    if not env_id:
        return None
    parts = env_id.strip("/").split("/")
    return parts[-1] if parts else None


def ingress_details(app: Dict) -> Tuple[bool, Optional[bool], Optional[str], Optional[int]]:
    ingress = app.get("properties", {}).get("configuration", {}).get("ingress")
    if not ingress:
        return False, None, None, None
    internal = ingress.get("internal")
    fqdn = ingress.get("fqdn")
    target_port = ingress.get("targetPort")
    return True, internal, fqdn, target_port


def format_bool(value: Optional[bool]) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate connectivity between n8n and pdfedit Azure Container Apps."
    )
    parser.add_argument("--resource-group", default="n8n")
    parser.add_argument("--env-name", default="n8n")
    parser.add_argument("--n8n-app", default="n8n")
    parser.add_argument("--pdfedit-app", default="pdfedit")
    parser.add_argument("--subscription", help="Azure subscription ID or name")
    parser.add_argument("--path", default="/docs", help="Path to test on pdfedit")
    parser.add_argument("--exec-test", action="store_true",
                        help="Run a curl test from the n8n container to pdfedit")
    args = parser.parse_args()

    if args.subscription:
        run(["az", "account", "set", "--subscription", args.subscription])

    run(["az", "account", "show"], capture=False)

    n8n = run_json([
        "az", "containerapp", "show",
        "-n", args.n8n_app,
        "-g", args.resource_group,
    ])
    pdfedit = run_json([
        "az", "containerapp", "show",
        "-n", args.pdfedit_app,
        "-g", args.resource_group,
    ])

    n8n_env = env_name_from_id(n8n.get("properties", {}).get("environmentId"))
    pdfedit_env = env_name_from_id(pdfedit.get("properties", {}).get("environmentId"))

    n8n_ingress, n8n_internal, n8n_fqdn, n8n_port = ingress_details(n8n)
    pdf_ingress, pdf_internal, pdf_fqdn, pdf_port = ingress_details(pdfedit)

    print("\nApp summary")
    print(f"n8n app: {args.n8n_app}")
    print(f"- env: {n8n_env or 'unknown'}")
    print(f"- ingress enabled: {format_bool(n8n_ingress)}")
    print(f"- ingress internal: {format_bool(n8n_internal)}")
    print(f"- fqdn: {n8n_fqdn or 'unknown'}")
    print(f"- target port: {n8n_port if n8n_port is not None else 'unknown'}")

    print(f"\npdfedit app: {args.pdfedit_app}")
    print(f"- env: {pdfedit_env or 'unknown'}")
    print(f"- ingress enabled: {format_bool(pdf_ingress)}")
    print(f"- ingress internal: {format_bool(pdf_internal)}")
    print(f"- fqdn: {pdf_fqdn or 'unknown'}")
    print(f"- target port: {pdf_port if pdf_port is not None else 'unknown'}")

    same_env = n8n_env and pdfedit_env and n8n_env == pdfedit_env
    if not pdf_ingress:
        print("\nConnectivity: FAIL")
        print("pdfedit has no ingress enabled. Enable ingress to allow access.")
        return 2

    if pdf_internal:
        if not same_env:
            print("\nConnectivity: FAIL")
            print("pdfedit uses internal ingress but is not in the same ACA environment as n8n.")
            print("Move apps to the same ACA environment or switch pdfedit to external ingress.")
            return 2
        print("\nConnectivity: OK (internal ingress)")
    else:
        print("\nConnectivity: OK (external ingress)")

    if pdf_fqdn:
        print(f"Target URL: https://{pdf_fqdn}{args.path}")
    else:
        print("Target URL: unknown (missing ingress fqdn)")

    if args.exec_test:
        if not sys.stdin.isatty():
            print("\nExec test skipped: non-interactive session.")
            print("Run the script locally in a real terminal to exec into the container.")
            return 0
        if not pdf_fqdn:
            print("\nExec test skipped: missing pdfedit fqdn.")
            return 2

        containers = (n8n.get("properties", {})
                        .get("template", {})
                        .get("containers") or [])
        container_name = containers[0].get("name") if containers else None
        if not container_name:
            print("\nExec test skipped: unable to resolve n8n container name.")
            return 2

        url = f"https://{pdf_fqdn}{args.path}"
        curl_cmd = (
            "if command -v curl >/dev/null 2>&1; then "
            f"curl -k -sS -o /dev/null -w 'HTTPSTATUS:%{{http_code}}' {url}; "
            "else echo 'curl not found'; exit 127; fi"
        )

        exec_cmd = f"sh -c \"{curl_cmd}\""
        result = run_capture([
            "az", "containerapp", "exec",
            "-n", args.n8n_app,
            "-g", args.resource_group,
            "--container", container_name,
            "--command", exec_cmd,
        ])

        print("\nExec test result")
        if result.returncode != 0:
            print(result.stderr.strip() or result.stdout.strip())
            print("Exec test failed. Verify curl exists in the container and network policies allow egress.")
            return 3

        raw_out = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        match = re.findall(r"HTTPSTATUS:(\d{3})", raw_out)
        code = match[-1] if match else ""
        print(f"HTTP status from pdfedit: {code or 'unknown'}")
        if code.startswith("2"):
            print("Connectivity test: PASS")
        else:
            print("Connectivity test: FAIL (non-2xx response)")
            return 4

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
