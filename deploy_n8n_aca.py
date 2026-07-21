#!/usr/bin/env python3
import argparse
import json
import os
import secrets
import string
import subprocess
import sys
import time
from typing import List, Optional, Tuple


def run(cmd: List[str], capture: bool = False) -> str:
    print("+", " ".join(cmd))
    if capture:
        result = subprocess.run(cmd, check=True, text=True, stdout=subprocess.PIPE)
        return result.stdout.strip()
    subprocess.run(cmd, check=True)
    return ""




def run_json(cmd: List[str]) -> dict:
    out = run(cmd, capture=True)
    return json.loads(out)


def run_capture(cmd: List[str]) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd))
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def random_suffix(length: int = 6) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))

def az_exists(cmd: List[str]) -> bool:
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except subprocess.CalledProcessError:
        return False


def az_group_exists(name: str) -> bool:
    out = run(["az", "group", "exists", "-n", name], capture=True).strip().lower()
    return out == "true"


def ensure_provider_registered(namespace: str) -> bool:
    state = run(
        ["az", "provider", "show", "-n", namespace, "--query", "registrationState", "-o", "tsv"],
        capture=True,
    ).strip().lower()
    if state == "registered":
        return True
    run(["az", "provider", "register", "-n", namespace])
    for _ in range(60):
        time.sleep(5)
        state = run(
            ["az", "provider", "show", "-n", namespace, "--query", "registrationState", "-o", "tsv"],
            capture=True,
        ).strip().lower()
        if state == "registered":
            return True
    return False


def ensure_acr(acr_name: str, resource_group: str, location: str) -> str:
    acr_final = acr_name
    if not az_exists(["az", "acr", "show", "-n", acr_final, "-g", resource_group]):
        name_ok = run_json(["az", "acr", "check-name", "-n", acr_final])
        if not name_ok.get("nameAvailable", False):
            try:
                existing = run_json([
                    "az", "acr", "list",
                    "-g", resource_group,
                    "--query", f"[?starts_with(name, '{acr_name}')].name",
                ])
                if existing:
                    acr_final = existing[0]
                    print(f"ACR name unavailable. Using existing registry {acr_final}.")
                else:
                    acr_final = f"{acr_final}{random_suffix()}"
                    print(f"ACR name unavailable. Using {acr_final} instead.")
            except Exception:
                acr_final = f"{acr_final}{random_suffix()}"
                print(f"ACR name unavailable. Using {acr_final} instead.")
        run([
            "az", "acr", "create",
            "-n", acr_final,
            "-g", resource_group,
            "-l", location,
            "--sku", "Basic",
        ])
    return acr_final


def acr_credentials(acr_name: str) -> Tuple[str, str, str]:
    login_server = run([
        "az", "acr", "show",
        "-n", acr_name,
        "--query", "loginServer",
        "-o", "tsv",
    ], capture=True)
    creds = None
    try:
        creds = run_json([
            "az", "acr", "credential", "show",
            "-n", acr_name,
        ])
    except subprocess.CalledProcessError:
        run([
            "az", "acr", "update",
            "-n", acr_name,
            "--admin-enabled", "true",
        ])
        creds = run_json([
            "az", "acr", "credential", "show",
            "-n", acr_name,
        ])
    username = creds.get("username")
    password = (creds.get("passwords") or [{}])[0].get("value")
    if not username or not password:
        raise RuntimeError("Unable to read ACR credentials.")
    return login_server, username, password


def build_image(acr_name: str, repo: str, tag: str, dockerfile: str, context: str) -> None:
    run([
        "az", "acr", "build",
        "-r", acr_name,
        "-t", f"{repo}:{tag}",
        "-f", dockerfile,
        context,
    ])


def random_password(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deploy n8n on Azure Container Apps with PostgreSQL Flexible Server and Azure Files.")

    parser.add_argument("--resource-group", default="n8n")
    parser.add_argument("--location", default="eastus")
    parser.add_argument("--env-name", default="n8n", help="Container Apps environment name")
    parser.add_argument("--app-name", default="n8n", help="Container App name")

    parser.add_argument("--domain", default="n8n.vegusa.com", help="Custom domain for n8n")
    parser.add_argument("--domain-type", choices=["apex", "subdomain"], default="subdomain")
    parser.add_argument("--manage-azure-dns", action="store_true",
                        help="Create/ensure DNS records in Azure DNS if not propagated")
    parser.add_argument("--dns-zone", help="Azure DNS zone name (defaults to root domain)")
    parser.add_argument("--dns-resource-group", help="Azure DNS resource group (defaults to --resource-group)")

    parser.add_argument("--db-server-name", default="n8ndb", help="PostgreSQL Flexible Server name")
    parser.add_argument("--db-admin-user", default="n8nadmin")
    parser.add_argument("--db-user-with-server", action="store_true",
                        help="Use the Azure-style user@server format for DB_POSTGRESDB_USER")
    parser.add_argument("--db-admin-password")
    parser.add_argument("--rotate-secrets", action="store_true",
                        help="Rotate DB password/encryption key (updates DB admin password if needed)")
    parser.add_argument("--db-name", default="n8n")
    parser.add_argument("--postgres-location", help="Region for PostgreSQL (defaults to --location)")
    parser.add_argument("--postgres-sku", default="Standard_B1ms")
    parser.add_argument("--postgres-storage", type=int, default=32)
    parser.add_argument("--postgres-backup-retention", type=int, default=7)

    parser.add_argument("--storage-account", default="n8nstorage")
    parser.add_argument("--file-share", default="n8n")

    parser.add_argument("--image", default="docker.n8n.io/n8nio/n8n",
                        help="Full image reference (used when --build-image is not set)")
    parser.add_argument("--build-image", action="store_true",
                        help="Build and push a custom n8n image with curl via ACR")
    parser.add_argument("--acr-name", default="n8nacr", help="ACR name used for build/push")
    parser.add_argument("--image-repo", default="n8n")
    parser.add_argument("--image-tag", default="latest")
    parser.add_argument("--dockerfile", default="Dockerfile.n8n")
    parser.add_argument("--context", default=".")
    parser.add_argument("--registry-server", help="Registry server (if using --image)")
    parser.add_argument("--registry-username", help="Registry username (if using --image)")
    parser.add_argument("--registry-password", help="Registry password (if using --image)")
    parser.add_argument("--subscription", help="Azure subscription ID or name")

    parser.add_argument("--skip-dns-wait", action="store_true")
    parser.add_argument("--disable-settings-perms-check", action="store_true", default=True,
                        help="Set N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS=false (recommended for Azure Files)")

    args = parser.parse_args()

    if args.subscription:
        run(["az", "account", "set", "--subscription", args.subscription])

    run(["az", "account", "show"], capture=False)
    if not ensure_provider_registered("Microsoft.DBforPostgreSQL"):
        print("Provider Microsoft.DBforPostgreSQL is still registering. Re-run the script in a few minutes.")
        return 1

    if not az_group_exists(args.resource_group):
        run(["az", "group", "create", "-n", args.resource_group, "-l", args.location])

    if not az_exists(["az", "containerapp", "env", "show", "-n", args.env_name, "-g", args.resource_group]):
        run([
            "az", "containerapp", "env", "create",
            "-n", args.env_name,
            "-g", args.resource_group,
            "-l", args.location,
        ])

    image_ref = args.image
    registry_server = args.registry_server
    registry_username = args.registry_username
    registry_password = args.registry_password

    if args.build_image:
        acr_name = ensure_acr(args.acr_name, args.resource_group, args.location)
        build_image(acr_name, args.image_repo, args.image_tag, args.dockerfile, args.context)
        login_server, username, password = acr_credentials(acr_name)
        image_ref = f"{login_server}/{args.image_repo}:{args.image_tag}"
        registry_server = login_server
        registry_username = username
        registry_password = password

    db_password = args.db_admin_password


    # Postgres server name must be globally unique. Auto-suffix on create failure.
    db_server_name = args.db_server_name
    db_server_exists = az_exists([
        "az", "postgres", "flexible-server", "show",
        "--name", db_server_name,
        "--resource-group", args.resource_group,
    ])
    if not db_server_exists:
        try:
            existing = run_json([
                "az", "postgres", "flexible-server", "list",
                "--resource-group", args.resource_group,
                "--query", f"[?starts_with(name, '{args.db_server_name}')].name",
            ])
            if existing:
                db_server_name = existing[0]
                db_server_exists = True
        except Exception:
            pass
    if not db_server_exists and not db_password:
        db_password = random_password(32)
    if not db_server_exists:
        location_candidates = []
        if args.postgres_location:
            location_candidates.append(args.postgres_location)
        location_candidates.append(args.location)
        location_candidates.extend(["southcentralus", "centralus", "eastus2", "westus2"])
        seen = set()
        location_candidates = [l for l in location_candidates if not (l in seen or seen.add(l))]

        for attempt in range(6):
            db_location = location_candidates[attempt % len(location_candidates)]
            result = run_capture([
                "az", "postgres", "flexible-server", "create",
                "--resource-group", args.resource_group,
                "--name", db_server_name,
                "--location", db_location,
                "--admin-user", args.db_admin_user,
                "--admin-password", db_password,
                "--tier", "Burstable",
                "--sku-name", args.postgres_sku,
                "--storage-size", str(args.postgres_storage),
                "--backup-retention", str(args.postgres_backup_retention),
                "--high-availability", "Disabled",
                "--public-access", "0.0.0.0",
            ])
            if result.returncode == 0:
                args.postgres_location = db_location
                break

            err = (result.stderr or "") + (result.stdout or "")
            err_lower = err.lower()
            if "location is restricted" in err_lower or "another region" in err_lower:
                print(f"Postgres not available in {db_location}. Trying another region...")
                continue
            if "sku" in err_lower and "not available" in err_lower:
                print(f"Postgres SKU not available in {db_location}. Trying another region...")
                continue
            if "name is already used" in err_lower or "already used" in err_lower:
                db_server_name = f"{args.db_server_name}{random_suffix()}"
                print(f"Postgres server name unavailable. Trying {db_server_name}...")
                continue
            print(err.strip())
            raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
        else:
            raise RuntimeError("Unable to create PostgreSQL server after multiple attempts.")
    else:
        if not db_password:
            # If DB exists and password not provided, we must reuse from app secret or fail later.
            pass

    if args.db_name and args.db_name != "postgres":
        if not az_exists([
            "az", "postgres", "flexible-server", "db", "show",
            "--resource-group", args.resource_group,
            "--server-name", db_server_name,
            "--database-name", args.db_name,
        ]):
            run([
                "az", "postgres", "flexible-server", "db", "create",
                "--resource-group", args.resource_group,
                "--server-name", db_server_name,
                "--database-name", args.db_name,
            ])


    # Storage account name must be globally unique. Auto-suffix only if not already present in RG.
    storage_name = args.storage_account
    file_share = args.file_share
    storage_from_env = False

    try:
        env_storages = run_json([
            "az", "containerapp", "env", "storage", "list",
            "--name", args.env_name,
            "--resource-group", args.resource_group,
        ])
        for item in env_storages:
            if item.get("name") == "n8nfiles":
                azure_file = (item.get("properties") or {}).get("azureFile") or {}
                storage_name = azure_file.get("accountName") or storage_name
                file_share = azure_file.get("shareName") or file_share
                storage_from_env = True
                break
    except Exception:
        pass

    if not storage_from_env:
        if not az_exists([
            "az", "storage", "account", "show",
            "-n", storage_name,
            "-g", args.resource_group,
        ]):
            name_ok = run_json(["az", "storage", "account", "check-name", "-n", storage_name])
            if not name_ok.get("nameAvailable", False):
                storage_name = f"{storage_name}{random_suffix()}"
                print(f"Storage account name unavailable. Using {storage_name} instead.")

            if not az_exists([
                "az", "storage", "account", "show",
                "-n", storage_name,
                "-g", args.resource_group,
            ]):
                run([
                    "az", "storage", "account", "create",
                    "-n", storage_name,
                    "-g", args.resource_group,
                    "-l", args.location,
                    "--sku", "Standard_LRS",
                    "--kind", "StorageV2",
                ])

    storage_key = run([
        "az", "storage", "account", "keys", "list",
        "-g", args.resource_group,
        "-n", storage_name,
        "--query", "[0].value",
        "-o", "tsv",
    ], capture=True)

    if not storage_from_env:
        share_exists = run([
            "az", "storage", "share", "exists",
            "--name", file_share,
            "--account-name", storage_name,
            "--account-key", storage_key,
            "--query", "exists",
            "-o", "tsv",
        ], capture=True).lower() == "true"

        if not share_exists:
            run([
                "az", "storage", "share", "create",
                "--name", file_share,
                "--account-name", storage_name,
                "--account-key", storage_key,
            ])

        run([
            "az", "containerapp", "env", "storage", "set",
            "--name", args.env_name,
            "--resource-group", args.resource_group,
            "--storage-name", "n8nfiles",
            "--azure-file-account-name", storage_name,
            "--azure-file-account-key", storage_key,
            "--azure-file-share-name", file_share,
            "--access-mode", "ReadWrite",
        ])
    else:
        run([
            "az", "containerapp", "env", "storage", "set",
            "--name", args.env_name,
            "--resource-group", args.resource_group,
            "--storage-name", "n8nfiles",
            "--azure-file-account-name", storage_name,
            "--azure-file-account-key", storage_key,
            "--azure-file-share-name", file_share,
            "--access-mode", "ReadWrite",
        ])

    encryption_key = None

    app_exists = az_exists([
        "az", "containerapp", "show",
        "-n", args.app_name,
        "-g", args.resource_group,
    ])
    reuse_secrets = not args.rotate_secrets
    if app_exists and reuse_secrets:
        try:
            secrets_list = run_json([
                "az", "containerapp", "secret", "list",
                "-n", args.app_name,
                "-g", args.resource_group,
            ])
            for s in secrets_list:
                if s.get("name") == "dbpass":
                    db_password = s.get("value")
                if s.get("name") == "encryptionkey":
                    encryption_key = s.get("value")
        except Exception:
            pass

    if not encryption_key:
        # Try to read encryption key from Azure Files config, if present.
        try:
            tmp_cfg = "/tmp/n8n_config"
            dl = run_capture([
                "az", "storage", "file", "download",
                "--account-name", storage_name,
                "--account-key", storage_key,
                "--share-name", args.file_share,
                "--path", "config",
                "--dest", tmp_cfg,
            ])
            if dl.returncode == 0 and os.path.exists(tmp_cfg):
                with open(tmp_cfg, "r") as f:
                    data = json.load(f)
                if isinstance(data, dict) and data.get("encryptionKey"):
                    encryption_key = data["encryptionKey"]
        except Exception:
            pass

    if not db_password:
        if db_server_exists:
            if args.rotate_secrets:
                db_password = random_password(32)
            else:
                print("DB server exists but no DB password available. Provide --db-admin-password or set --rotate-secrets.")
                return 1
        else:
            db_password = random_password(32)
    if not encryption_key:
        encryption_key = secrets.token_hex(32)

    db_user_login = args.db_admin_user
    if args.db_user_with_server and "@" not in db_user_login:
        db_user_login = f"{db_user_login}@{db_server_name}"

    env_vars = [
        f"N8N_HOST={args.domain}",
        "N8N_PROTOCOL=https",
        "N8N_PORT=5678",
        f"WEBHOOK_URL=https://{args.domain}/",
        "N8N_PROXY_HOPS=1",
        "DB_TYPE=postgresdb",
        f"DB_POSTGRESDB_HOST={db_server_name}.postgres.database.azure.com",
        "DB_POSTGRESDB_PORT=5432",
        f"DB_POSTGRESDB_DATABASE={args.db_name}",
        f"DB_POSTGRESDB_USER={db_user_login}",
        "DB_POSTGRESDB_SSL_ENABLED=true",
        "DB_POSTGRESDB_PASSWORD=secretref:dbpass",
        "N8N_ENCRYPTION_KEY=secretref:encryptionkey",
    ]
    if args.disable_settings_perms_check:
        env_vars.append("N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS=false")

    if not app_exists:
        create_cmd = [
            "az", "containerapp", "create",
            "-n", args.app_name,
            "-g", args.resource_group,
            "--environment", args.env_name,
            "--image", image_ref,
            "--ingress", "external",
            "--target-port", "5678",
            "--cpu", args.cpu,
            "--memory", args.memory,
            "--secrets", f"dbpass={db_password}", f"encryptionkey={encryption_key}",
            "--env-vars", *env_vars,
        ]
        if registry_server and registry_username and registry_password:
            create_cmd.extend([
                "--registry-server", registry_server,
                "--registry-username", registry_username,
                "--registry-password", registry_password,
            ])
        run(create_cmd)
    else:
        if args.rotate_secrets:
            # Rotate DB admin password if DB exists.
            if db_server_exists:
                run([
                    "az", "postgres", "flexible-server", "update",
                    "--resource-group", args.resource_group,
                    "--name", db_server_name,
                    "--admin-password", db_password,
                ])
            run([
                "az", "containerapp", "secret", "set",
                "-n", args.app_name,
                "-g", args.resource_group,
                "--secrets", f"dbpass={db_password}", f"encryptionkey={encryption_key}",
            ])
        if registry_server and registry_username and registry_password:
            run([
                "az", "containerapp", "registry", "set",
                "-n", args.app_name,
                "-g", args.resource_group,
                "--server", registry_server,
                "--username", registry_username,
                "--password", registry_password,
            ])
        run([
            "az", "containerapp", "update",
            "-n", args.app_name,
            "-g", args.resource_group,
            "--image", image_ref,
            "--cpu", args.cpu,
            "--memory", args.memory,
            "--set-env-vars", *env_vars,
        ])

    app_spec = run_json([
        "az", "containerapp", "show",
        "-n", args.app_name,
        "-g", args.resource_group,
    ])

    template = app_spec.get("properties", {}).get("template", {})
    changed = False
    volumes = template.get("volumes") or []
    if not any(v.get("name") == "n8nfiles" for v in volumes):
        volumes.append({
            "name": "n8nfiles",
            "storageType": "AzureFile",
            "storageName": "n8nfiles",
        })
        changed = True
    template["volumes"] = volumes

    containers = template.get("containers") or []
    for container in containers:
        mounts = container.get("volumeMounts") or []
        if not any(m.get("volumeName") == "n8nfiles" for m in mounts):
            mounts.append({
                "volumeName": "n8nfiles",
                "mountPath": "/home/node/.n8n",
            })
            changed = True
        container["volumeMounts"] = mounts
    template["containers"] = containers

    if changed:
        app_spec["properties"]["template"] = template

        tmp_path = "/tmp/n8n-containerapp.json"
        with open(tmp_path, "w") as f:
            json.dump(app_spec, f)

        run([
            "az", "containerapp", "update",
            "-n", args.app_name,
            "-g", args.resource_group,
            "--yaml", tmp_path,
        ])

    latest_revision = run([
        "az", "containerapp", "show",
        "-n", args.app_name,
        "-g", args.resource_group,
        "--query", "properties.latestRevisionName",
        "-o", "tsv",
    ], capture=True)

    if latest_revision:
        revisions = run_json([
            "az", "containerapp", "revision", "list",
            "-n", args.app_name,
            "-g", args.resource_group,
            "--query", "[].{name:name,active:properties.active}",
        ])
        for rev in revisions:
            if rev.get("active") and rev.get("name") != latest_revision:
                run([
                    "az", "containerapp", "revision", "deactivate",
                    "-n", args.app_name,
                    "-g", args.resource_group,
                    "--revision", rev["name"],
                ])

    env_static_ip = run([
        "az", "containerapp", "env", "show",
        "-n", args.env_name,
        "-g", args.resource_group,
        "--query", "properties.staticIp",
        "-o", "tsv",
    ], capture=True)

    app_fqdn = run([
        "az", "containerapp", "show",
        "-n", args.app_name,
        "-g", args.resource_group,
        "--query", "properties.configuration.ingress.fqdn",
        "-o", "tsv",
    ], capture=True)

    verification_id = run([
        "az", "containerapp", "show",
        "-n", args.app_name,
        "-g", args.resource_group,
        "--query", "properties.customDomainVerificationId",
        "-o", "tsv",
    ], capture=True)

    print("\nDNS records to add:")
    if args.domain_type == "apex":
        print(f"A    @     {env_static_ip}")
        print(f"TXT  asuid {verification_id}")
    else:
        print(f"CNAME {args.domain} {app_fqdn}")
        print(f"TXT   asuid.{args.domain} {verification_id}")

    def dns_resolves() -> bool:
        if args.domain_type == "apex":
            try:
                out = run_capture(["nslookup", "-type=A", args.domain]).stdout
                txt = run_capture(["nslookup", "-type=TXT", f"asuid.{args.domain}"]).stdout
            except Exception:
                return False
            return env_static_ip in out and verification_id in txt
        try:
            out = run_capture(["nslookup", "-type=CNAME", args.domain]).stdout
            txt = run_capture(["nslookup", "-type=TXT", f"asuid.{args.domain}"]).stdout
        except Exception:
            return False
        return app_fqdn in out and verification_id in txt

    if not dns_resolves() and args.manage_azure_dns:
        dns_zone = args.dns_zone or ".".join(args.domain.split(".")[-2:])
        dns_rg = args.dns_resource_group

        if not dns_rg:
            zones = run_json([
                "az", "network", "dns", "zone", "list",
                "--query", f"[?name=='{dns_zone}']",
            ])
            if len(zones) == 1:
                dns_rg = zones[0]["resourceGroup"]
            elif len(zones) > 1:
                raise RuntimeError(
                    f"Multiple DNS zones named {dns_zone} found. Specify --dns-resource-group."
                )
            else:
                dns_rg = args.resource_group

        if not az_exists(["az", "network", "dns", "zone", "show", "-g", dns_rg, "-n", dns_zone]):
            run(["az", "network", "dns", "zone", "create", "-g", dns_rg, "-n", dns_zone])

        record_name = args.domain.replace(f".{dns_zone}", "")
        if record_name == dns_zone:
            record_name = "@"

        if args.domain_type == "apex":
            run([
                "az", "network", "dns", "record-set", "a", "add-record",
                "-g", dns_rg, "-z", dns_zone, "-n", record_name,
                "-a", env_static_ip,
            ])
            run([
                "az", "network", "dns", "record-set", "txt", "add-record",
                "-g", dns_rg, "-z", dns_zone, "-n", "asuid",
                "-v", verification_id,
            ])
        else:
            run([
                "az", "network", "dns", "record-set", "cname", "set-record",
                "-g", dns_rg, "-z", dns_zone, "-n", record_name,
                "-c", app_fqdn,
            ])
            run([
                "az", "network", "dns", "record-set", "txt", "add-record",
                "-g", dns_rg, "-z", dns_zone, "-n", f"asuid.{record_name}",
                "-v", verification_id,
            ])

    if not args.skip_dns_wait:
        if not dns_resolves():
            print("\nDNS not propagated yet.")
            if args.manage_azure_dns:
                print("Azure DNS records were created/updated. Wait for propagation.")
            else:
                print("Add the DNS records above and re-run with --skip-dns-wait once propagated.")
            return 0
        if not sys.stdin.isatty():
            print("\nNon-interactive session detected. Skipping hostname bind.")
            print("Re-run with --skip-dns-wait after DNS records propagate to bind the domain.")
            return 0
        input("\nPress Enter after DNS records are in place to continue...")

    run([
        "az", "containerapp", "hostname", "add",
        "--hostname", args.domain,
        "-g", args.resource_group,
        "-n", args.app_name,
    ])

    validation_method = "HTTP" if args.domain_type == "apex" else "CNAME"

    run([
        "az", "containerapp", "hostname", "bind",
        "--hostname", args.domain,
        "-g", args.resource_group,
        "-n", args.app_name,
        "--environment", args.env_name,
        "--validation-method", validation_method,
    ])

    print("\nDeployment complete.")
    print(f"n8n URL: https://{args.domain}")
    print(f"DB admin password (save this): {db_password}")
    print(f"n8n encryption key (save this): {encryption_key}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
    parser.add_argument("--cpu", default="2.0")
    parser.add_argument("--memory", default="4.0Gi")
