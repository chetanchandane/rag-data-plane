"""The stand-in token issuer's keys and tokens (DECISIONS 2026-11-21, "Step 11").

    export PYTHONPATH=services
    uv run python scripts/issuer.py create-key
    uv run python scripts/issuer.py activate-key <kid>
    uv run python scripts/issuer.py retire-key <kid>
    uv run python scripts/issuer.py mint --tenant <uuid> --role-level 2 [--scope s] [--check]

Private keys are SSM SecureStrings at /ragdp/issuer/signing_keys/<kid>, and the String
/ragdp/issuer/active_kid names the one that signs. They are outside /ragdp/prod/, so no
workload role's permissions boundary reaches them, and the deploy role has no SSM access:
only a human sign-in can mint. A private key goes from memory straight to SSM; it is never
written to disk or printed.

Public keys go in infra/envs/prod/jwks.json, which is committed and published by Terraform.
Rotation and the order of these commands: docs/runbooks/issuer-keys.md.

`mint` prints only the token, so `TOKEN=$(... mint ...)` works. The issuer URL comes from
--issuer or JWT_ISSUER (.env); it is `terraform output -raw issuer_url` in infra/envs/prod.
"""

import argparse
import json
import os
import sys
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import boto3
import jwt
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from dotenv import load_dotenv

from shared import tokens

ROOT = Path(__file__).resolve().parent.parent
JWKS_FILE = ROOT / "infra" / "envs" / "prod" / "jwks.json"
ACCOUNT_ID = "905418224080"  # every subcommand refuses any other account
KEY_PARAM_PREFIX = "/ragdp/issuer/signing_keys/"
ACTIVE_KID_PARAM = "/ragdp/issuer/active_kid"
KEY_SIZE = 2048
FETCH_TIMEOUT_SECONDS = 10


class IssuerError(Exception):
    """A refused operation, with a message for the person running the script."""


# --- JWKS file -----------------------------------------------------------------------------


def load_jwks(path: Path) -> dict[str, list[dict[str, str]]]:
    if not path.exists():
        return {"keys": []}
    return json.loads(path.read_text())


def save_jwks(path: Path, jwks: dict[str, list[dict[str, str]]]) -> None:
    path.write_text(json.dumps(jwks, indent=2) + "\n")


def kids(jwks: dict[str, list[dict[str, str]]]) -> list[str]:
    return [key["kid"] for key in jwks["keys"]]


# --- SSM -----------------------------------------------------------------------------------


def _get(ssm: Any, name: str, decrypt: bool = False) -> str | None:
    try:
        return ssm.get_parameter(Name=name, WithDecryption=decrypt)["Parameter"]["Value"]
    except ClientError as error:
        if error.response["Error"]["Code"] == "ParameterNotFound":
            return None
        raise


def active_kid(ssm: Any) -> str | None:
    return _get(ssm, ACTIVE_KID_PARAM)


def load_signing_key(ssm: Any) -> rsa.RSAPrivateKey:
    kid = active_kid(ssm)
    if kid is None:
        raise IssuerError("no active signing key: run create-key first")
    pem = _get(ssm, KEY_PARAM_PREFIX + kid, decrypt=True)
    if pem is None:
        raise IssuerError(f"the active key {kid} has no private key in SSM")
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey) or tokens.key_id(key.public_key()) != kid:
        raise IssuerError(f"the private key stored for {kid} is not that key")
    return key


# --- Subcommands ---------------------------------------------------------------------------


def create_key(ssm: Any, jwks_path: Path) -> str:
    """A new key: private half to SSM, public half to the JWKS file. The first key is also
    activated; later ones wait until the JWKS with them is deployed (see the runbook)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)
    jwk = tokens.public_jwk(key.public_key())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        # SSM encrypts the SecureString with KMS; a passphrase would only have to live
        # somewhere else.
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    ssm.put_parameter(
        Name=KEY_PARAM_PREFIX + jwk["kid"],
        Description="Token issuer private signing key (scripts/issuer.py)",
        Value=pem,
        Type="SecureString",
        Overwrite=False,  # a kid is a key's thumbprint; an existing one is never replaced
        Tags=[{"Key": "Project", "Value": "ragdp"}, {"Key": "ManagedBy", "Value": "issuer.py"}],
    )
    jwks = load_jwks(jwks_path)
    jwks["keys"].append(jwk)
    save_jwks(jwks_path, jwks)
    if active_kid(ssm) is None:
        _set_active(ssm, jwk["kid"])
    return jwk["kid"]


def _set_active(ssm: Any, kid: str) -> None:
    ssm.put_parameter(
        Name=ACTIVE_KID_PARAM,
        Description="kid of the token issuer's signing key (scripts/issuer.py)",
        Value=kid,
        Type="String",
        Overwrite=True,
    )


def activate_key(ssm: Any, jwks_path: Path, kid: str) -> None:
    if kid not in kids(load_jwks(jwks_path)):
        raise IssuerError(f"{kid} is not in {jwks_path.name}: verifiers couldn't check its tokens")
    if _get(ssm, KEY_PARAM_PREFIX + kid, decrypt=False) is None:
        raise IssuerError(f"{kid} has no private key in SSM")
    _set_active(ssm, kid)


def retire_key(ssm: Any, jwks_path: Path, kid: str) -> None:
    if kid == active_kid(ssm):
        raise IssuerError(f"{kid} is the active signing key: activate another one first")
    jwks = load_jwks(jwks_path)
    if kid not in kids(jwks):
        raise IssuerError(f"{kid} is not in {jwks_path.name}")
    jwks["keys"] = [key for key in jwks["keys"] if key["kid"] != kid]
    save_jwks(jwks_path, jwks)
    try:
        ssm.delete_parameter(Name=KEY_PARAM_PREFIX + kid)
    except ClientError as error:
        if error.response["Error"]["Code"] != "ParameterNotFound":
            raise


# --- Checking a token against what's published ---------------------------------------------


def fetch_json(url: str) -> Any:
    if not url.startswith("https://"):
        raise IssuerError(f"refusing to fetch keys over plain HTTP: {url}")
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:
        return json.load(response)


def check_against_issuer(
    token: str, issuer: str, audience: str = tokens.AUDIENCE, fetch=fetch_json
) -> dict[str, Any]:
    """Verify `token` the way a relying party would: discovery document -> jwks_uri -> the
    key named by the token's kid -> signature, iss, aud and exp. Returns the claims."""
    config = fetch(f"{issuer}/.well-known/openid-configuration")
    if config.get("issuer") != issuer:
        raise IssuerError(f"the discovery document names issuer {config.get('issuer')!r}")
    jwks = jwt.PyJWKSet.from_dict(fetch(config["jwks_uri"]))
    kid = jwt.get_unverified_header(token).get("kid")
    matching = [key for key in jwks.keys if key.key_id == kid]
    if not matching:
        raise IssuerError(f"the published JWKS has no key {kid}: deployed yet, or still cached?")
    return jwt.decode(
        token,
        matching[0].key,
        algorithms=[tokens.ALGORITHM],
        audience=audience,
        issuer=issuer,
        options={"require": ["exp", "iat", "iss", "aud", "sub"]},
    )


# --- Command line --------------------------------------------------------------------------


def _ssm() -> Any:
    session = boto3.Session()
    account = session.client("sts").get_caller_identity()["Account"]
    if account != ACCOUNT_ID:
        raise IssuerError(f"signed in to account {account}, not {ACCOUNT_ID}")
    return session.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-2"))


def main(argv: list[str] | None = None) -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("create-key", help="new key pair; the first one is also activated")
    for name, text in [("activate-key", "sign with this key"), ("retire-key", "remove a key")]:
        commands.add_parser(name, help=text).add_argument("kid")
    mint = commands.add_parser("mint", help="print a signed access token")
    mint.add_argument("--tenant", required=True, type=uuid.UUID)
    mint.add_argument("--role-level", required=True, type=int, choices=tokens.ROLE_LEVELS)
    mint.add_argument("--scope", action="append", default=[], help="repeatable")
    mint.add_argument("--subject", default="local-dev")
    mint.add_argument("--issuer", default=os.environ.get("JWT_ISSUER"))
    mint.add_argument("--audience", default=tokens.AUDIENCE)
    mint.add_argument("--check", action="store_true", help="verify it against the JWKS online")
    args = parser.parse_args(argv)

    try:
        ssm = _ssm()
        if args.command == "create-key":
            kid = create_key(ssm, JWKS_FILE)
            print(f"Created key {kid}; its public key is in {JWKS_FILE.relative_to(ROOT)}.")
            print(f"Active signing key: {active_kid(ssm)}")
        elif args.command == "activate-key":
            activate_key(ssm, JWKS_FILE, args.kid)
            print(f"Active signing key: {args.kid}")
        elif args.command == "retire-key":
            retire_key(ssm, JWKS_FILE, args.kid)
            print(f"Retired {args.kid}: removed from {JWKS_FILE.name} and SSM. Deploy the JWKS.")
        else:
            if not args.issuer:
                raise IssuerError("no issuer: pass --issuer or set JWT_ISSUER")
            token = tokens.mint(
                load_signing_key(ssm),
                issuer=args.issuer,
                tenant_id=args.tenant,
                role_level=args.role_level,
                subject=args.subject,
                scopes=args.scope,
                audience=args.audience,
            )
            if args.check:
                claims = check_against_issuer(token, args.issuer, args.audience)
                print(
                    f"verified against {args.issuer}: kid ok, expires {claims['exp']}",
                    file=sys.stderr,
                )
            print(token)
    except IssuerError as error:
        sys.exit(f"issuer.py: {error}")


if __name__ == "__main__":
    main()
