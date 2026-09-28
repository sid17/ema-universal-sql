"""Per-tenant secrets — gate tests ``test_secret_indirection`` and ``test_crypto_shred``."""

from dataclasses import dataclass, replace

import pytest
from cryptography.fernet import Fernet, InvalidToken

from src.governance.secrets import SecretsManagerClient
from src.models.errors import ApiError, ErrorCode

ACME_KEY = Fernet.generate_key().decode()
GLOBEX_KEY = Fernet.generate_key().decode()

ACME_TOKEN = "ghp_acme_mock_token"
GLOBEX_TOKEN = "ghp_globex_mock_token"


@dataclass(frozen=True)
class FakeTenant:
    tenant_id: str
    fernet_key: str | None


class FakeStore:
    """Stands in for ControlPlaneRepository's two reads.

    Deliberately *not* a MagicMock: the point of these tests is that the client
    reads the owning tenant out of the secrets row, and a mock would happily
    return a tenant for whatever it was asked, hiding exactly that bug.
    """

    def __init__(self, secrets: dict, tenants: dict) -> None:
        self._secrets = secrets
        self._tenants = tenants
        self.tenant_lookups: list[str] = []

    def get_secret(self, secret_ref: str):
        return self._secrets.get(secret_ref)

    def get_tenant(self, tenant_id: str):
        self.tenant_lookups.append(tenant_id)
        return self._tenants.get(tenant_id)


@pytest.fixture
def store() -> FakeStore:
    return FakeStore(
        secrets={
            "acme/github": {
                "secret_ref": "acme/github",
                "tenant_id": "tenant_acme",
                "ciphertext": SecretsManagerClient.encrypt(ACME_KEY, ACME_TOKEN),
            },
            "globex/github": {
                "secret_ref": "globex/github",
                "tenant_id": "tenant_globex",
                "ciphertext": SecretsManagerClient.encrypt(GLOBEX_KEY, GLOBEX_TOKEN),
            },
        },
        tenants={
            "tenant_acme": FakeTenant("tenant_acme", ACME_KEY),
            "tenant_globex": FakeTenant("tenant_globex", GLOBEX_KEY),
        },
    )


@pytest.fixture
def secrets(store) -> SecretsManagerClient:
    return SecretsManagerClient(store)


# --- GATE: test_secret_indirection -----------------------------------------


def test_secret_indirection(secrets):
    """Each tenant's ref resolves to its own token; no cross-load."""
    assert secrets.resolve("acme/github") == ACME_TOKEN
    assert secrets.resolve("globex/github") == GLOBEX_TOKEN
    assert ACME_TOKEN != GLOBEX_TOKEN


def test_each_tenants_ciphertext_differs_even_for_the_same_plaintext():
    """Two tenants storing the identical credential must not share ciphertext.

    Otherwise a stolen ciphertext would be portable between tenants, and the
    per-tenant key would be decoration.
    """
    same_secret = "identical-token"
    acme = SecretsManagerClient.encrypt(ACME_KEY, same_secret)
    globex = SecretsManagerClient.encrypt(GLOBEX_KEY, same_secret)
    assert acme != globex


def test_one_tenants_key_cannot_decrypt_anothers_ciphertext(store, secrets):
    """The isolation property stated directly, rather than inferred."""
    globex_ciphertext = store.get_secret("globex/github")["ciphertext"]
    with pytest.raises(InvalidToken):
        Fernet(ACME_KEY.encode()).decrypt(globex_ciphertext.encode())


def test_the_owning_tenant_comes_from_the_row_not_the_caller(store, secrets):
    """A confused-deputy check: resolve() takes no tenant argument at all.

    If it did, passing someone else's secret_ref with your own tenant would ask
    this class to break the isolation it exists to provide.
    """
    secrets.resolve("globex/github")
    assert store.tenant_lookups == ["tenant_globex"]


def test_resolve_takes_only_a_secret_ref(secrets):
    with pytest.raises(TypeError):
        secrets.resolve("acme/github", "tenant_globex")


# --- GATE: test_crypto_shred -----------------------------------------------


def test_crypto_shred(store, secrets):
    """Destroying one tenant's key makes its secrets unrecoverable, and only its.

    Backs the HLD §2 claim that offboarding is a key-destroy rather than a row
    scrub: the ciphertext is untouched and still present, but no key exists that
    can read it.
    """
    # Before: both resolve.
    assert secrets.resolve("acme/github") == ACME_TOKEN
    assert secrets.resolve("globex/github") == GLOBEX_TOKEN

    ciphertext_before = store.get_secret("globex/github")["ciphertext"]

    # Destroy globex's key — the offboarding operation, one row write.
    store._tenants["tenant_globex"] = replace(
        store._tenants["tenant_globex"], fernet_key=None
    )

    with pytest.raises(ApiError) as raised:
        secrets.resolve("globex/github")
    assert raised.value.code is ErrorCode.CONNECTOR_AUTH_ERROR

    # The ciphertext is still sitting there, and is now permanently meaningless.
    assert store.get_secret("globex/github")["ciphertext"] == ciphertext_before

    # Every other tenant is untouched — this is not a global outage.
    assert secrets.resolve("acme/github") == ACME_TOKEN


def test_rotating_a_key_without_re_encrypting_shreds_too(store, secrets):
    """A replaced key is as final as a deleted one — the old data stays dark."""
    store._tenants["tenant_globex"] = replace(
        store._tenants["tenant_globex"], fernet_key=Fernet.generate_key().decode()
    )
    with pytest.raises(ApiError) as raised:
        secrets.resolve("globex/github")
    assert raised.value.code is ErrorCode.CONNECTOR_AUTH_ERROR


# --- failure paths ---------------------------------------------------------


def test_unknown_secret_ref_is_an_auth_error(secrets):
    with pytest.raises(ApiError) as raised:
        secrets.resolve("nope/github")
    assert raised.value.code is ErrorCode.CONNECTOR_AUTH_ERROR
    assert raised.value.http == 502


def test_secret_naming_a_missing_tenant_is_an_auth_error(store, secrets):
    store._secrets["orphan/github"] = {
        "secret_ref": "orphan/github",
        "tenant_id": "tenant_vanished",
        "ciphertext": SecretsManagerClient.encrypt(ACME_KEY, "x"),
    }
    with pytest.raises(ApiError) as raised:
        secrets.resolve("orphan/github")
    assert raised.value.code is ErrorCode.CONNECTOR_AUTH_ERROR


def test_every_failure_is_config_not_transient(secrets):
    """CONNECTOR_AUTH_ERROR classifies config_error, so it is NOT degradable.

    A credential problem must fail the query rather than be reported as a
    partial result — it will not fix itself on the next attempt.
    """
    from src.connectors.errors import FailureMode, classify

    assert classify(FailureMode.AUTH).error_code is ErrorCode.CONNECTOR_AUTH_ERROR
    assert classify(FailureMode.AUTH).degradable is False


def test_error_message_never_leaks_key_or_ciphertext(store, secrets):
    """An error rendered to a caller must not carry the material it failed on."""
    store._tenants["tenant_globex"] = replace(
        store._tenants["tenant_globex"], fernet_key=Fernet.generate_key().decode()
    )
    ciphertext = store.get_secret("globex/github")["ciphertext"]
    with pytest.raises(ApiError) as raised:
        secrets.resolve("globex/github")
    message = raised.value.message
    assert ciphertext not in message
    assert GLOBEX_KEY not in message
    assert GLOBEX_TOKEN not in message


def test_encrypt_round_trips(store):
    """The seeder's write path and the request path's read path must agree."""
    client = SecretsManagerClient(store)
    store._secrets["acme/jira"] = {
        "secret_ref": "acme/jira",
        "tenant_id": "tenant_acme",
        "ciphertext": SecretsManagerClient.encrypt(ACME_KEY, "jira-token-123"),
    }
    assert client.resolve("acme/jira") == "jira-token-123"


def test_encrypt_accepts_str_or_bytes_keys():
    """`tenants.fernet_key` is TEXT in Postgres but bytes from Fernet's own API."""
    assert SecretsManagerClient.encrypt(ACME_KEY, "x")
    assert SecretsManagerClient.encrypt(ACME_KEY.encode(), "x")
