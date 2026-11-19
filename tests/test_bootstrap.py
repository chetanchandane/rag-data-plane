"""db/bootstrap.py's pure parts: endpoint guards and URL building. No network."""

import importlib.util
from pathlib import Path
from urllib.parse import urlsplit

import pytest

_PATH = Path(__file__).resolve().parent.parent / "db" / "bootstrap.py"
_spec = importlib.util.spec_from_file_location("bootstrap", _PATH)
bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bootstrap)

DEV = "ep-dev-endpoint-111"
HOST = "ep-prod-endpoint-222.c-2.us-east-2.aws.neon.tech"


@pytest.mark.parametrize("host", [f"{DEV}.c-2.us-east-2.aws.neon.tech", f"{DEV}-pooler.c-2.x"])
def test_production_refuses_the_dev_endpoint_direct_or_pooled(host):
    with pytest.raises(SystemExit):
        bootstrap.check_production_endpoint(host, DEV)


def test_production_refuses_when_dev_is_unknown():
    with pytest.raises(SystemExit):
        bootstrap.check_production_endpoint(HOST, "")


def test_production_accepts_another_endpoint():
    assert bootstrap.check_production_endpoint(HOST, DEV) == "ep-prod-endpoint-222"


def test_role_urls_put_each_role_on_its_endpoint_with_its_drivers_parameters():
    passwords = {role: f"pw-{role}" for role in bootstrap.ROLES}
    urls = bootstrap.role_urls(passwords, HOST, "neondb")

    owner = urlsplit(urls["app_owner"])
    assert (owner.username, owner.password) == ("app_owner", "pw-app_owner")
    assert owner.hostname == HOST  # direct
    assert owner.query == bootstrap.LIBPQ_PARAMS  # psycopg: with channel_binding
    for role in ("app_api", "app_ingest"):
        url = urlsplit(urls[role])
        assert url.hostname == "ep-prod-endpoint-222-pooler.c-2.us-east-2.aws.neon.tech"
        assert url.path == "/neondb"
        assert url.query == bootstrap.ASYNCPG_PARAMS  # asyncpg: no channel_binding
        assert "verify-full" in url.query


def test_as_libpq_adds_channel_binding_exactly_once():
    urls = bootstrap.role_urls({role: "pw" for role in bootstrap.ROLES}, HOST, "neondb")
    for url in urls.values():
        assert urlsplit(bootstrap.as_libpq(url)).query == bootstrap.LIBPQ_PARAMS


def test_production_parameters_match_the_terraform_names():
    tf = (_PATH.parent.parent / "infra/modules/secrets/main.tf").read_text()
    for name in bootstrap.PRODUCTION_PARAMETERS.values():
        assert f'"${{local.prefix}}/{name.rsplit("/", 1)[1]}"' in tf
        assert name.startswith("/ragdp/prod/")
