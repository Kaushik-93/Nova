"""Who can read what. Every case here is one where getting it wrong would show someone another
tenant's data, so the failures are checked as carefully as the successes."""

from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from pydantic import ValidationError

from agent.api.auth import ALGORITHM, LoginThrottle, MAX_FAILURES, check_secret, issue_token, sso_refusal
from agent.config import Settings
from agent.harness.access import Grant, UserAccess, build_policy, make_skills_view
from agent.harness.errors import PermissionFailure
from agent.tools import connectors
from agent.tools.connectors import (Connection, create_catalog_sql, decrypt_credentials, encrypt_credentials,
                                    write_credentials)
from agent.tools.profiler import ColumnProfile, TableProfile, suggest_tenancy
from agent.tools.sources import Source

SECRET = "x" * 40
PASSWORD = "s3cret=with:odd#chars!\nnext=line"


def rental(**changes) -> Source:
    fields = dict(id="r", slug="rental", display_name="Rental", trino_catalog="rental_db", dialect="postgres",
                  tenant_column="tenant_id", tenant_table="public.tenants", tenant_label_column="name")
    return Source(**(fields | changes))


def shop() -> Source:
    return Source(id="s", slug="shop", display_name="Shop", trino_catalog="shop_db", dialect="postgres")


def table(name: str, *columns: tuple[str, str]) -> TableProfile:
    return TableProfile(schema_name="public", name=name,
                        columns=[ColumnProfile(name=n, type=t, nullable=False) for n, t in columns])


class Store:
    """Only the two calls access control makes."""

    def __init__(self, *tables: TableProfile) -> None:
        self.tables = tables

    def exists(self, slug: str) -> bool:
        return True

    def read_profile(self, slug: str) -> SimpleNamespace:
        return SimpleNamespace(tables=self.tables)


STORE = Store(table("rentals", ("tenant_id", "integer"), ("amount", "double")), table("fx_rates", ("rate", "double")))


def user(**grants) -> UserAccess:
    return UserAccess(email="a@b.c", role="analyst", grants={key: Grant(source_id=key, tenant_ids=value) for key, value in grants.items()})


ADMIN = UserAccess(email="root@b.c", role="admin")


# --- Reading a source -------------------------------------------------------------------------

def test_no_grant_means_no_access():
    assert not user().can_read(rental())


def test_a_limited_grant_stops_working_if_tenancy_is_removed():
    """It must not widen: this person was limited to one tenant, and would now read them all."""
    limited = user(r=["1"])
    source = rental(tenant_column=None)
    assert not limited.can_read(source)
    assert build_policy([source], limited, STORE).allowed_catalogs == frozenset()


def test_a_whole_source_grant_survives_tenancy_being_removed():
    assert user(r=None).can_read(rental(tenant_column=None))


def test_policy_only_lists_granted_catalogs():
    policy = build_policy([rental(), shop()], user(s=None), STORE)
    assert policy.allowed_catalogs == {"shop_db"}
    assert policy.row_scopes == {}


def test_policy_limits_a_tenant_user_and_marks_tables_with_the_column():
    policy = build_policy([rental()], user(r=["1", "2"]), STORE)
    scope = policy.row_scopes["rental_db"]
    assert (scope.column, scope.values, scope.numeric) == ("tenant_id", ("1", "2"), True)
    assert scope.scoped_tables == {"rental_db.public.rentals"}


def test_an_empty_tenant_list_is_a_limit_to_nothing_and_not_no_limit():
    scope = build_policy([rental()], user(r=[]), STORE).row_scopes["rental_db"]
    assert scope.values == ()


def test_admin_and_the_command_line_are_not_limited():
    for access in (ADMIN, None):
        policy = build_policy([rental()], access, STORE)
        assert policy.allowed_catalogs == {"rental_db"} and policy.row_scopes == {}


# --- The skills a user's agent can read -------------------------------------------------------

@pytest.fixture
def skills_dir(tmp_path: Path) -> str:
    for slug in ("rental", "shop"):
        folder = tmp_path / "skills" / slug
        (folder / "model").mkdir(parents=True)
        for name in ("SKILL.md", f"model/{slug}.yml", "model/access.json", "examples.json", "schema.json"):
            (folder / name).write_text(f"{slug} {name}")
    return str(tmp_path / "skills")


def files_in(view: str) -> set[str]:
    return {str(path.relative_to(view)) for path in Path(view).rglob("*") if path.is_file()}


def test_skills_view_leaves_out_ungranted_sources(skills_dir):
    view = make_skills_view([rental(), shop()], user(s=None), skills_dir)
    assert {name.split("/")[0] for name in files_in(view)} == {"shop"}


def test_skills_view_hides_measured_values_from_a_tenant_user(skills_dir):
    """schema.json holds values measured across every tenant."""
    view = make_skills_view([rental()], user(r=["1"]), skills_dir)
    assert files_in(view) == {"rental/SKILL.md", "rental/model/rental.yml", "rental/model/access.json",
                              "rental/examples.json"}


def test_skills_view_keeps_measured_values_for_a_whole_source_grant(skills_dir):
    assert "rental/schema.json" in files_in(make_skills_view([rental()], user(r=None), skills_dir))


def test_someone_with_no_grants_gets_an_empty_view(skills_dir):
    assert files_in(make_skills_view([rental(), shop()], user(), skills_dir)) == set()


def test_admin_and_the_command_line_use_the_real_folder(skills_dir):
    assert make_skills_view([rental()], ADMIN, skills_dir) is None
    assert make_skills_view([rental()], None, skills_dir) is None


# --- Sessions ---------------------------------------------------------------------------------

@pytest.mark.parametrize("secret", ["", "short"])
def test_a_weak_signing_secret_stops_startup(secret):
    with pytest.raises(RuntimeError):
        check_secret(secret)


def decode(token: str, secret: str = SECRET) -> dict:
    return jwt.decode(token, secret, algorithms=[ALGORITHM], options={"require": ["exp", "sub"]})


def test_a_session_token_round_trips():
    assert decode(issue_token("user-1", SECRET))["sub"] == "user-1"


def test_a_session_signed_with_another_key_is_refused():
    with pytest.raises(jwt.InvalidTokenError):
        decode(issue_token("user-1", "y" * 40))


def test_an_expired_session_is_refused():
    with pytest.raises(jwt.InvalidTokenError):
        decode(issue_token("user-1", SECRET, lifetime=-10))


def test_an_unsigned_session_is_refused():
    with pytest.raises(jwt.InvalidTokenError):
        decode(jwt.encode({"sub": "user-1", "exp": 9999999999}, key="", algorithm="none"))


def test_login_throttle_blocks_then_clears():
    throttle = LoginThrottle()
    assert not throttle.blocked("a@b.c")
    for _ in range(MAX_FAILURES):
        throttle.failed("a@b.c")
    assert throttle.blocked("a@b.c")
    assert not throttle.blocked("other@b.c")
    throttle.cleared("a@b.c")
    assert not throttle.blocked("a@b.c")


def test_login_throttle_doesnt_remember_made_up_emails():
    throttle = LoginThrottle()
    for number in range(100):
        throttle.blocked(f"{number}@b.c")
    assert not throttle._failures


# --- SSO --------------------------------------------------------------------------------------

def settings(**changes) -> Settings:
    return Settings(**changes)


def test_sso_needs_an_email_and_a_subject():
    assert sso_refusal({"sub": "1"}, settings()) == "missing_email"
    assert sso_refusal({"email": "a@b.c"}, settings()) == "missing_email"


def test_sso_domain_allow_list():
    allowed = settings(oidc_allowed_domains="Example.com, other.org")
    assert sso_refusal({"sub": "1", "email": "a@example.com"}, allowed) is None
    assert sso_refusal({"sub": "1", "email": "a@evil.com"}, allowed) == "domain_not_allowed"
    assert sso_refusal({"sub": "1", "email": "a@example.com.evil.com"}, allowed) == "domain_not_allowed"


# --- Connecting a source ----------------------------------------------------------------------

def connection(**changes) -> Connection:
    fields = dict(name="Prod", kind="postgres", host="db.internal", port=5432, database="app", username="reader", password=PASSWORD)
    return Connection(**(fields | changes))


@pytest.mark.parametrize("field, value", [
    ("host", "db.internal/app?sslfactory=evil"), ("host", "db.internal:5432"), ("host", "bad host"), ("host", ""),
    ("database", "app?socketFactory=evil"), ("database", "app&x=1"), ("database", "a/b"),
    ("port", 0), ("port", 70000), ("kind", "oracle"),
])
def test_values_that_could_alter_a_connection_url_are_refused(field, value):
    with pytest.raises(ValidationError):
        connection(**{field: value})


def test_the_catalog_statement_holds_no_login():
    """Trino logs and displays this statement."""
    sql = create_catalog_sql(connection(), catalog="src_prod")
    assert "reader" not in sql and "s3cret" not in sql
    assert "'/data/trino/secrets/src_prod.properties'" in sql
    assert "jdbc:postgresql://db.internal:5432/app" in sql


def test_a_password_cant_add_settings_to_the_credentials_file(tmp_path):
    write_credentials(connection(), catalog="src_prod", directory=str(tmp_path))
    lines = (tmp_path / "src_prod.properties").read_text().splitlines()
    assert len(lines) == 2  # the newline in the password stayed inside its value
    assert lines[0] == "connection-user=reader"
    assert lines[1].startswith("connection-password=s3cret\\=with\\:odd\\#chars\\!\\nnext\\=line")


def test_the_credentials_file_is_private(tmp_path):
    write_credentials(connection(), catalog="src_prod", directory=str(tmp_path))
    assert stat.S_IMODE((tmp_path / "src_prod.properties").stat().st_mode) == 0o600


def test_credentials_are_encrypted_and_come_back_intact():
    key = "kP8sV2fYQw7mX1cZr3tBn6uLe9aJd4hGoT5yIpMqS0w="
    token = encrypt_credentials(connection(), key)
    assert "s3cret" not in token and "reader" not in token
    assert decrypt_credentials(token, key) == {"username": "reader", "password": PASSWORD}


def test_a_connection_never_shows_its_password():
    assert "s3cret" not in repr(connection()) and "s3cret" not in str(connection().model_dump())


def test_every_kind_of_source_has_a_connector():
    from typing import get_args
    assert set(get_args(Connection.model_fields["kind"].annotation)) == set(connectors.CONNECTORS)


# --- Suggesting tenancy -----------------------------------------------------------------------

def profile(*tables: TableProfile) -> SimpleNamespace:
    return SimpleNamespace(tables=list(tables))


def test_tenancy_is_suggested_from_a_shared_column_and_its_owning_table():
    tenants = table("tenants", ("tenant_id", "integer"), ("tenant_name", "varchar"))
    tenants.primary_key = ["tenant_id"]
    found = suggest_tenancy(profile(tenants, table("rentals", ("tenant_id", "integer")), table("fx", ("rate", "double"))))
    assert found == {"column": "tenant_id", "table": "public.tenants", "label_column": "tenant_name",
                     "tables_without_column": ["public.fx"]}


def test_a_column_in_one_table_isnt_tenancy():
    assert suggest_tenancy(profile(table("rentals", ("tenant_id", "integer")), table("fx", ("rate", "double")))) is None
