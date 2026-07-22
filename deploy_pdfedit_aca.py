#!/usr/bin/env python3
import argparse
import json
import os
import secrets
import string
import subprocess
import shutil
import shlex
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


def run_mysql_sql(
    resource_group: str,
    server_name: str,
    host: str,
    user: str,
    password: str,
    database: str,
    sql: str,
    location: str,
) -> None:
    result = run_capture([
        "az", "mysql", "flexible-server", "execute",
        "--resource-group", resource_group,
        "--server-name", server_name,
        "--admin-user", user,
        "--admin-password", password,
        "--database-name", database,
        "--sql", sql,
    ])
    if result.returncode == 0:
        return
    err = (result.stderr or "") + (result.stdout or "")
    err_lower = err.lower()
    if "not recognized" in err_lower or "misspelled" in err_lower:
        mysql_bin = shutil.which("mysql")
        if mysql_bin:
            local_result = run_capture([
                mysql_bin,
                "-h", host,
                "-u", user,
                f"-p{password}",
                "--ssl",
                database,
                "-e", sql,
            ])
            if local_result.returncode == 0:
                return
            local_err = (local_result.stderr or "") + (local_result.stdout or "")
            if "can't connect" not in local_err.lower():
                raise subprocess.CalledProcessError(
                    local_result.returncode, local_result.args, local_result.stdout, local_result.stderr
                )
        aci_name = f"mysqlinit-{random_suffix()}"
        sql_escaped = sql.replace("\"", "\\\"")
        install_cmd = (
            "if command -v apt-get >/dev/null 2>&1; then "
            "DEBIAN_FRONTEND=noninteractive apt-get update && apt-get install -y default-mysql-client; "
            "elif command -v apk >/dev/null 2>&1; then "
            "apk add --no-cache mariadb-client; "
            "elif command -v tdnf >/dev/null 2>&1; then "
            "tdnf install -y mariadb; "
            "else echo 'No package manager available' >&2; exit 1; fi"
        )
        mysql_cmd = (
            f"{install_cmd} && "
            "MYSQL_BIN=$(command -v mysql || command -v mariadb) && "
            "if [ -z \"$MYSQL_BIN\" ]; then echo 'MySQL client not found' >&2; exit 1; fi && "
            f"MYSQL_PWD={shlex.quote(password)} $MYSQL_BIN -h {host} -u {user} --ssl "
            f"{database} -e \"{sql_escaped}\""
        )
        run([
            "az", "container", "create",
            "--resource-group", resource_group,
            "--name", aci_name,
            "--image", "mcr.microsoft.com/azure-cli",
            "--location", location,
            "--os-type", "Linux",
            "--cpu", "1",
            "--memory", "1.5",
            "--restart-policy", "Never",
            "--command-line", f"sh -c {shlex.quote(mysql_cmd)}",
        ])
        for _ in range(60):
            state = run([
                "az", "container", "show",
                "--resource-group", resource_group,
                "--name", aci_name,
                "--query", "instanceView.state",
                "-o", "tsv",
            ], capture=True).strip()
            if state in {"Terminated", "Succeeded", "Failed"}:
                break
            time.sleep(5)
        logs = run([
            "az", "container", "logs",
            "--resource-group", resource_group,
            "--name", aci_name,
        ], capture=True)
        run([
            "az", "container", "delete",
            "--resource-group", resource_group,
            "--name", aci_name,
            "--yes",
        ])
        if "ERROR" in logs or "ERROR" in logs.upper():
            raise RuntimeError(f"ACI MySQL init failed. Logs:\\n{logs}")
        return
        raise RuntimeError(
            "Azure CLI does not support 'mysql flexible-server execute' and no mysql client or docker found."
        )
    raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)


def load_env_file(path: str) -> dict:
    data: dict = {}
    if not os.path.exists(path):
        return data
    with open(path, "r") as f:
        for line in f:
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            data[key.strip()] = value.strip()
    return data


def normalize_location_name(location: str) -> str:
    if not location:
        return location
    locs = run_json(["az", "account", "list-locations"])
    for loc in locs:
        if loc.get("name") == location:
            return location
    for loc in locs:
        if loc.get("displayName", "").lower() == location.lower():
            return loc.get("name", location)
    return location


MYSQL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS documentos (
    id_doc INT AUTO_INCREMENT PRIMARY KEY,
    id_archivo_sharepoint VARCHAR(255),
    id_carpeta VARCHAR(255),
    pedido_oc VARCHAR(100),
    nombre_archivo VARCHAR(255),
    metodo_proceso VARCHAR(50),
    message_id VARCHAR(255),
    fecha_proceso DATETIME,
    invoice VARCHAR(100),
    fecha_documento DATE,
    monto_factura DECIMAL(15,2),
    id_proveedor VARCHAR(100)
);

CREATE TABLE IF NOT EXISTS correos (
    id INT AUTO_INCREMENT PRIMARY KEY,
    fecha_recepcion DATETIME,
    remitente VARCHAR(255),
    message_id VARCHAR(255) UNIQUE
);

CREATE TABLE IF NOT EXISTS equipos (
    id INT AUTO_INCREMENT PRIMARY KEY,
    id_file VARCHAR(255),
    serie VARCHAR(100),
    modelo VARCHAR(100)
);

CREATE TABLE IF NOT EXISTS respuestas (
    id INT AUTO_INCREMENT PRIMARY KEY,
    message_id VARCHAR(255),
    id_archivo VARCHAR(255),
    id_carpeta VARCHAR(255),
    destinatarios TEXT,
    fecha_envio DATETIME,
    ref_agente VARCHAR(255),
    remitente VARCHAR(255),
    po_invoice VARCHAR(255),
    proveedor VARCHAR(255),
    fecha_respuesta DATETIME
);

CREATE TABLE IF NOT EXISTS carga_archivo (
    id INT AUTO_INCREMENT PRIMARY KEY,
    usuario VARCHAR(255),
    id_archivo VARCHAR(255),
    id_carpeta_origen VARCHAR(255),
    destinatarios TEXT,
    fecha_carga DATETIME,
    id_carpeta_destino VARCHAR(255)
);
""".strip()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deploy the pdfedit service to Azure Container Apps."
    )
    parser.add_argument("--resource-group", default="n8n")
    parser.add_argument("--location", default="eastus")
    parser.add_argument("--env-name", default="n8n", help="Container Apps environment name")
    parser.add_argument("--app-name", default="pdfedit", help="Container App name")

    parser.add_argument("--image", help="Full image reference (skips ACR build if set)")
    parser.add_argument("--acr-name", default="pdfeditacr", help="ACR name used for build/push")
    parser.add_argument("--image-repo", default="pdfedit")
    parser.add_argument("--image-tag", default="latest")
    parser.add_argument("--dockerfile", default="Dockerfile")
    parser.add_argument("--context", default=".")

    parser.add_argument("--registry-server", help="Registry server (if using --image)")
    parser.add_argument("--registry-username", help="Registry username (if using --image)")
    parser.add_argument("--registry-password", help="Registry password (if using --image)")

    parser.add_argument("--db-server-name", default="pdfeditmysql", help="MySQL Flexible Server name")
    parser.add_argument("--db-admin-user", default="pdfeditadmin")
    parser.add_argument("--db-user-with-server", action="store_true",
                        help="Use the Azure-style user@server format for MYSQL_USER")
    parser.add_argument("--db-admin-password")
    parser.add_argument("--rotate-secrets", action="store_true",
                        help="Rotate DB password (updates DB admin password if needed)")
    parser.add_argument("--db-name", default="automation_db")
    parser.add_argument("--mysql-location", help="Region for MySQL (defaults to --location)")
    parser.add_argument("--mysql-sku", default="Standard_B1ms")
    parser.add_argument("--mysql-storage", type=int, default=20)
    parser.add_argument("--mysql-backup-retention", type=int, default=7)
    parser.add_argument("--skip-db-init", action="store_true",
                        help="Skip DB schema initialization")

    parser.add_argument("--cpu", default="0.5")
    parser.add_argument("--memory", default="1.0Gi")
    parser.add_argument("--subscription", help="Azure subscription ID or name")

    args = parser.parse_args()
    env_values = load_env_file(".env")
    if args.resource_group == "n8n" and env_values.get("RG"):
        args.resource_group = env_values["RG"]
    if args.db_name == "automation_db" and env_values.get("DB_NAME"):
        args.db_name = env_values["DB_NAME"]
    if not args.db_admin_password and env_values.get("DB_PASSWORD"):
        args.db_admin_password = env_values["DB_PASSWORD"]

    if args.subscription:
        run(["az", "account", "set", "--subscription", args.subscription])

    run(["az", "account", "show"], capture=False)
    if not ensure_provider_registered("Microsoft.App"):
        print("Provider Microsoft.App is still registering. Re-run the script in a few minutes.")
        return 1
    if not ensure_provider_registered("Microsoft.OperationalInsights"):
        print("Provider Microsoft.OperationalInsights is still registering. Re-run the script in a few minutes.")
        return 1
    if not ensure_provider_registered("Microsoft.DBforMySQL"):
        print("Provider Microsoft.DBforMySQL is still registering. Re-run the script in a few minutes.")
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

    if not image_ref:
        acr_name = ensure_acr(args.acr_name, args.resource_group, args.location)
        build_image(acr_name, args.image_repo, args.image_tag, args.dockerfile, args.context)
        login_server, username, password = acr_credentials(acr_name)
        image_ref = f"{login_server}/{args.image_repo}:{args.image_tag}"
        registry_server = login_server
        registry_username = username
        registry_password = password

    db_password = args.db_admin_password
    db_location = None

    db_server_name = args.db_server_name
    db_server_exists = az_exists([
        "az", "mysql", "flexible-server", "show",
        "--name", db_server_name,
        "--resource-group", args.resource_group,
    ])
    if not db_server_exists:
        try:
            existing = run_json([
                "az", "mysql", "flexible-server", "list",
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
        if args.mysql_location:
            location_candidates.append(args.mysql_location)
        location_candidates.append(args.location)
        location_candidates.extend(["southcentralus", "centralus", "eastus2", "westus2"])
        seen = set()
        location_candidates = [l for l in location_candidates if not (l in seen or seen.add(l))]

        for attempt in range(6):
            db_location = location_candidates[attempt % len(location_candidates)]
            result = run_capture([
                "az", "mysql", "flexible-server", "create",
                "--resource-group", args.resource_group,
                "--name", db_server_name,
                "--location", db_location,
                "--admin-user", args.db_admin_user,
                "--admin-password", db_password,
                "--tier", "Burstable",
                "--sku-name", args.mysql_sku,
                "--storage-size", str(args.mysql_storage),
                "--backup-retention", str(args.mysql_backup_retention),
                "--high-availability", "Disabled",
                "--public-access", "0.0.0.0",
            ])
            if result.returncode == 0:
                args.mysql_location = db_location
                break

            err = (result.stderr or "") + (result.stdout or "")
            err_lower = err.lower()
            if "location is restricted" in err_lower or "another region" in err_lower:
                print(f"MySQL not available in {db_location}. Trying another region...")
                continue
            if ("sku" in err_lower and "not available" in err_lower) or "no available skus" in err_lower:
                print(f"MySQL SKU not available in {db_location}. Trying another region...")
                continue
            if "name is already used" in err_lower or "already used" in err_lower:
                db_server_name = f"{args.db_server_name}{random_suffix()}"
                print(f"MySQL server name unavailable. Trying {db_server_name}...")
                continue
            print(err.strip())
            raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
        else:
            raise RuntimeError("Unable to create MySQL server after multiple attempts.")
    else:
        if not db_password:
            pass
        db_location = run([
            "az", "mysql", "flexible-server", "show",
            "--resource-group", args.resource_group,
            "--name", db_server_name,
            "--query", "location",
            "-o", "tsv",
        ], capture=True).strip()
        db_location = normalize_location_name(db_location)

    if args.db_name:
        if not az_exists([
            "az", "mysql", "flexible-server", "db", "show",
            "--resource-group", args.resource_group,
            "--server-name", db_server_name,
            "--database-name", args.db_name,
        ]):
            run([
                "az", "mysql", "flexible-server", "db", "create",
                "--resource-group", args.resource_group,
                "--server-name", db_server_name,
                "--database-name", args.db_name,
            ])

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
                "--show-values",
            ])
            for s in secrets_list:
                if s.get("name") == "dbpass" and s.get("value"):
                    db_password = s.get("value")
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

    if args.rotate_secrets and db_server_exists:
        run([
            "az", "mysql", "flexible-server", "update",
            "--resource-group", args.resource_group,
            "--name", db_server_name,
            "--admin-password", db_password,
        ])

    if not args.skip_db_init:
        run_mysql_sql(
            args.resource_group,
            db_server_name,
            f"{db_server_name}.mysql.database.azure.com",
            args.db_admin_user,
            db_password,
            args.db_name,
            MYSQL_SCHEMA_SQL,
            db_location or args.mysql_location or args.location,
        )

    db_user_login = args.db_admin_user
    if args.db_user_with_server and "@" not in db_user_login:
        db_user_login = f"{db_user_login}@{db_server_name}"

    env_vars = [
        "PORT=8000",
        "HOST=0.0.0.0",
        f"MYSQL_HOST={db_server_name}.mysql.database.azure.com",
        "MYSQL_PORT=3306",
        f"MYSQL_DATABASE={args.db_name}",
        f"MYSQL_USER={db_user_login}",
        "MYSQL_PASSWORD=secretref:dbpass",
    ]

    if not app_exists:
        create_cmd = [
            "az", "containerapp", "create",
            "-n", args.app_name,
            "-g", args.resource_group,
            "--environment", args.env_name,
            "--image", image_ref,
            "--ingress", "external",
            "--target-port", "8000",
            "--cpu", args.cpu,
            "--memory", args.memory,
            "--secrets", f"dbpass={db_password}",
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
        run([
            "az", "containerapp", "secret", "set",
            "-n", args.app_name,
            "-g", args.resource_group,
            "--secrets", f"dbpass={db_password}",
        ])
        update_cmd = [
            "az", "containerapp", "update",
            "-n", args.app_name,
            "-g", args.resource_group,
            "--image", image_ref,
            "--cpu", args.cpu,
            "--memory", args.memory,
            "--set-env-vars", *env_vars,
        ]
        run(update_cmd)
        run([
            "az", "containerapp", "ingress", "update",
            "-n", args.app_name,
            "-g", args.resource_group,
            "--type", "external",
            "--target-port", "8000",
            "--transport", "auto",
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

    app_fqdn = run([
        "az", "containerapp", "show",
        "-n", args.app_name,
        "-g", args.resource_group,
        "--query", "properties.configuration.ingress.fqdn",
        "-o", "tsv",
    ], capture=True)

    print("\nDeployment complete.")
    print(f"pdfedit URL: https://{app_fqdn}")
    print(f"MySQL server: {db_server_name}.mysql.database.azure.com")
    print(f"MySQL database: {args.db_name}")
    print(f"MySQL admin user: {db_user_login}")
    print(f"MySQL admin password (save this): {db_password}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
