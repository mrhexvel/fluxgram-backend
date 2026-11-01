import argparse
import hashlib
import ipaddress
import json
import os
import secrets
import ssl
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

VAULT_ADDRESS = "https://vault:8200"
TLS_DIRECTORY = Path("/vault/tls")
ADMIN_DIRECTORY = Path("/vault/admin")
BOOTSTRAP_DIRECTORY = Path("/vault/bootstrap")
SECRETS_DIRECTORY = Path("/vault/secrets")
CONFIG_DIRECTORY = Path("/vault/config-output")
POLICY_FILE = Path("/deployment/publisher-policy.hcl")
ROLE_FILE = Path("/deployment/publisher-role.json")


def write_file(path: Path, data: bytes, mode: int, uid: int, gid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = f".{path.name}."
    for candidate in path.parent.glob(f"{prefix}*.tmp"):
        if candidate.is_file() or candidate.is_symlink():
            candidate.unlink()

    temporary = path.with_name(f"{prefix}{secrets.token_hex(8)}.tmp")

    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(descriptor)

    os.chmod(temporary, mode)
    os.chown(temporary, uid, gid)
    os.replace(temporary, path)

    directory_descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def prepare_tls() -> None:
    TLS_DIRECTORY.mkdir(parents=True, exist_ok=True)
    Path("/vault/data").mkdir(parents=True, exist_ok=True)
    os.chmod("/vault/data", 0o700)
    os.chown("/vault/data", 100, 1000)

    ca_path = TLS_DIRECTORY / "vault-ca.pem"
    certificate_path = TLS_DIRECTORY / "vault-cert.pem"
    key_path = TLS_DIRECTORY / "vault-key.pem"
    existing = [path.exists() for path in (ca_path, certificate_path, key_path)]

    if any(existing) and not all(existing):
        raise RuntimeError("local Vault TLS volume is incomplete; reset the local deployment")

    if all(existing):
        certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        certificate_public_key = certificate.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        private_public_key = key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        if certificate_public_key != private_public_key:
            raise RuntimeError("local Vault certificate and key do not match")
        if certificate.not_valid_after_utc <= datetime.now(UTC):
            raise RuntimeError("local Vault certificate expired; reset the local deployment")
        return

    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "FluxGram Local Vault CA")])
    ca_certificate = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    server_key = ec.generate_private_key(ec.SECP256R1())
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "vault")])
    server_certificate = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("vault"),
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    write_file(
        ca_path,
        ca_certificate.public_bytes(serialization.Encoding.PEM),
        0o644,
        100,
        1000,
    )
    write_file(
        certificate_path,
        server_certificate.public_bytes(serialization.Encoding.PEM),
        0o644,
        100,
        1000,
    )
    write_file(
        key_path,
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        0o600,
        100,
        1000,
    )


class VaultClient:
    def __init__(self, token: str | None = None) -> None:
        context = ssl.create_default_context(cafile=str(TLS_DIRECTORY / "vault-ca.pem"))
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context)
        )
        self._token = token

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        wrap_ttl: str | None = None,
    ) -> dict[str, Any]:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self._token is not None:
            headers["X-Vault-Token"] = self._token
        if wrap_ttl is not None:
            headers["X-Vault-Wrap-TTL"] = wrap_ttl

        request = urllib.request.Request(
            f"{VAULT_ADDRESS}/v1/{path}",
            data=body,
            headers=headers,
            method=method,
        )
        with self._opener.open(request, timeout=10) as response:
            data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise RuntimeError("Vault response exceeds local bootstrap limit")
            return json.loads(data) if data else {}


def wait_for_vault(client: VaultClient) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            client.request("GET", "sys/health")
            return
        except urllib.error.HTTPError as error:
            if error.code in (429, 472, 473, 501, 503):
                return
        except (OSError, TimeoutError):
            pass
        time.sleep(0.5)

    raise RuntimeError("local Vault did not become reachable")


def initialize_and_unseal() -> str:
    client = VaultClient()
    wait_for_vault(client)
    initialized = client.request("GET", "sys/init").get("initialized")
    state_path = ADMIN_DIRECTORY / "state.json"

    if initialized:
        if not state_path.exists():
            raise RuntimeError("local Vault is initialized but its admin state is missing")
        state = json.loads(state_path.read_text(encoding="utf-8"))
    else:
        state = client.request(
            "POST",
            "sys/init",
            {"secret_shares": 1, "secret_threshold": 1},
        )
        persisted = {
            "root_token": state["root_token"],
            "unseal_key": state["keys_base64"][0],
        }
        write_file(
            state_path,
            json.dumps(persisted, separators=(",", ":")).encode("utf-8"),
            0o600,
            0,
            0,
        )
        state = persisted

    seal_status = client.request("GET", "sys/seal-status")
    if seal_status.get("sealed"):
        client.request("POST", "sys/unseal", {"key": state["unseal_key"]})

    return str(state["root_token"])


def issue_wrapped_secret(client: VaultClient) -> None:
    role_id = client.request(
        "GET", "auth/approle/role/plugin-reputation-publisher/role-id"
    )["data"]["role_id"]
    wrapped = client.request(
        "POST",
        "auth/approle/role/plugin-reputation-publisher/secret-id",
        wrap_ttl="5m",
    )["wrap_info"]["token"]

    write_file(
        BOOTSTRAP_DIRECTORY / "role-id",
        role_id.encode("ascii"),
        0o400,
        65534,
        65534,
    )
    write_file(
        BOOTSTRAP_DIRECTORY / "wrapped-secret-id",
        wrapped.encode("ascii"),
        0o600,
        65534,
        65534,
    )


def configure_vault(client: VaultClient) -> None:
    mounts = client.request("GET", "sys/mounts")
    if "transit/" not in mounts:
        client.request("POST", "sys/mounts/transit", {"type": "transit"})

    auth_methods = client.request("GET", "sys/auth")
    if "approle/" not in auth_methods:
        client.request("POST", "sys/auth/approle", {"type": "approle"})

    client.request(
        "POST",
        "transit/keys/plugin-reputation",
        {
            "type": "ecdsa-p256",
            "exportable": False,
            "allow_plaintext_backup": False,
        },
    )
    client.request(
        "PUT",
        "sys/policies/acl/plugin-reputation-publisher",
        {"policy": POLICY_FILE.read_text(encoding="utf-8")},
    )
    client.request(
        "POST",
        "auth/approle/role/plugin-reputation-publisher",
        json.loads(ROLE_FILE.read_text(encoding="utf-8")),
    )

    key_data = client.request("GET", "transit/keys/plugin-reputation")["data"]
    public_pem = key_data["keys"]["1"]["public_key"].encode("ascii")
    public_key = serialization.load_pem_public_key(public_pem)
    public_der = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    authority_key_id = f"p256:{hashlib.sha256(public_der).hexdigest()}"

    for directory, uid, gid in (
        (BOOTSTRAP_DIRECTORY, 65534, 65534),
        (SECRETS_DIRECTORY, 65534, 65534),
        (CONFIG_DIRECTORY, 65534, 65534),
    ):
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        os.chown(directory, uid, gid)

    write_file(
        CONFIG_DIRECTORY / "authority-key-id",
        authority_key_id.encode("ascii"),
        0o400,
        65534,
        65534,
    )
    write_file(
        CONFIG_DIRECTORY / "vault-ca.pem",
        (TLS_DIRECTORY / "vault-ca.pem").read_bytes(),
        0o400,
        65534,
        65534,
    )
    issue_wrapped_secret(client)


def supervise_vault(root_token: str) -> None:
    client = VaultClient(root_token)
    state = json.loads((ADMIN_DIRECTORY / "state.json").read_text(encoding="utf-8"))
    wrapped_path = BOOTSTRAP_DIRECTORY / "wrapped-secret-id"
    refresh_at = time.monotonic() + 240

    while True:
        try:
            seal_status = client.request("GET", "sys/seal-status")
            if seal_status.get("sealed"):
                client.request("POST", "sys/unseal", {"key": state["unseal_key"]})

            now = time.monotonic()
            if not wrapped_path.exists() or now >= refresh_at:
                issue_wrapped_secret(client)
                refresh_at = now + 240
        except urllib.error.HTTPError as error:
            if error.code not in (429, 472, 473, 501, 503):
                raise
        except (OSError, TimeoutError, urllib.error.URLError):
            pass

        time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare-tls", "supervise"))
    arguments = parser.parse_args()

    if arguments.action == "prepare-tls":
        prepare_tls()
        return

    root_token = initialize_and_unseal()
    configure_vault(VaultClient(root_token))
    supervise_vault(root_token)


if __name__ == "__main__":
    main()
