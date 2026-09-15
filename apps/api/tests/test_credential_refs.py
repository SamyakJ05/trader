"""Credential refs name environment variables, so who may attach one matters.

A ref is interpolated into os.environ lookups and hands the account's adapter
whatever API key and secret sit behind it. Without an ownership check one
tenant can name another's ref and -- because the broker read paths are not
behind the live gate -- read that tenant's real profile, funds and holdings.
"""

import pytest
from fastapi import HTTPException

from app.api.routes import brokers as broker_routes
from app.core.config import CREDENTIAL_REF_PATTERN, BrokerEnvCredentials, credential_ref_owners


class _User:
    def __init__(self, email):
        self.email = email


@pytest.fixture
def owners(monkeypatch):
    monkeypatch.setenv(
        "BROKER_CREDENTIAL_OWNERS",
        "ICICI_MAIN:owner@example.com,ZERODHA_MAIN:owner@example.com",
    )


def test_pattern_accepts_real_refs():
    for ref in ("ICICI_MAIN", "ZERODHA_MAIN", "BREEZE2", "A"):
        assert CREDENTIAL_REF_PATTERN.match(ref)


def test_pattern_rejects_anything_that_could_reshape_a_lookup():
    for ref in ("icici_main", "ICICI.MAIN", "ICICI-MAIN", "ICICI MAIN", "_LEADING", "", "A" * 65):
        assert not CREDENTIAL_REF_PATTERN.match(ref)


def test_owner_may_attach_their_own_ref(owners):
    broker_routes._check_credential_ref("ICICI_MAIN", _User("owner@example.com"))


def test_owner_check_is_case_insensitive(owners):
    broker_routes._check_credential_ref("icici_main", _User("OWNER@example.com"))


def test_another_user_may_not_attach_it(owners):
    """The whole finding: user B naming user A's ref gets A's credentials."""
    with pytest.raises(HTTPException) as exc:
        broker_routes._check_credential_ref("ICICI_MAIN", _User("intruder@example.com"))
    assert exc.value.status_code == 403


def test_undeclared_ref_is_refused_for_everyone(owners):
    with pytest.raises(HTTPException) as exc:
        broker_routes._check_credential_ref("SOME_OTHER_SECRET", _User("owner@example.com"))
    assert exc.value.status_code == 403


def test_refusals_do_not_distinguish_unknown_from_unowned(owners):
    """Differing messages would tell a user which refs the instance holds."""
    with pytest.raises(HTTPException) as unowned:
        broker_routes._check_credential_ref("ICICI_MAIN", _User("intruder@example.com"))
    with pytest.raises(HTTPException) as unknown:
        broker_routes._check_credential_ref("NOT_PROVISIONED", _User("intruder@example.com"))
    assert unowned.value.detail == unknown.value.detail
    assert unowned.value.status_code == unknown.value.status_code


def test_malformed_ref_is_rejected_before_any_lookup(owners):
    with pytest.raises(HTTPException) as exc:
        broker_routes._check_credential_ref("ICICI.MAIN", _User("owner@example.com"))
    assert exc.value.status_code == 422


def test_no_ref_is_allowed(owners):
    """Paper accounts carry none."""
    broker_routes._check_credential_ref(None, _User("owner@example.com"))


def test_credentials_refuse_a_malformed_ref(monkeypatch):
    """Defence in depth: a row predating validation must not become a lookup."""
    monkeypatch.setenv("EVIL_API_KEY", "should-not-be-read")
    creds = BrokerEnvCredentials("EVIL-")
    assert creds.api_key is None
    assert creds.api_secret is None
    assert creds.access_token is None


def test_credentials_still_resolve_a_valid_ref(monkeypatch):
    monkeypatch.setenv("ICICI_MAIN_API_KEY", "k")
    monkeypatch.setenv("ICICI_MAIN_API_SECRET", "s")
    creds = BrokerEnvCredentials("ICICI_MAIN")
    assert creds.has_api_keys


def test_owners_map_ignores_malformed_entries(monkeypatch):
    monkeypatch.setenv(
        "BROKER_CREDENTIAL_OWNERS",
        "GOOD_REF:a@example.com,,noseparator,bad-ref:b@example.com,  SPACED :c@example.com",
    )
    owners = credential_ref_owners()
    # Surrounding whitespace is stripped, so " SPACED " is a valid declaration.
    # An entry with no separator, and one whose ref could not be an env-var
    # prefix, are both dropped.
    assert owners == {"GOOD_REF": "a@example.com", "SPACED": "c@example.com"}


def test_no_owners_declared_means_nothing_is_attachable(monkeypatch):
    monkeypatch.delenv("BROKER_CREDENTIAL_OWNERS", raising=False)
    with pytest.raises(HTTPException):
        broker_routes._check_credential_ref("ICICI_MAIN", _User("owner@example.com"))
