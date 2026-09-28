"""Per-tenant credential resolution, and the crypto-shred it makes possible.

Every connector credential is stored as Fernet ciphertext in ``secrets``, and
the key that decrypts it lives on the owning tenant's row (``tenants.fernet_key``).
Nothing in the request path ever holds a plaintext credential for longer than
one ``fetch()``.

**The tenant comes from the stored row, never from the caller.** :meth:`resolve`
takes only a ``secret_ref``; it reads the owning ``tenant_id`` out of the
``secrets`` row and fetches *that* tenant's key. A caller-supplied tenant would
be a confused-deputy seam — pass someone else's ``secret_ref`` with your own
tenant and the class whose entire purpose is isolation would break it.

**Crypto-shred** (HLD §2). Because the key is per-tenant and lives outside the
ciphertext, offboarding is a *key destruction*, not a row scrub: delete one
tenant's ``fernet_key`` and every secret it ever held becomes permanently
unrecoverable, while every other tenant is untouched. That is a one-row write
instead of a cascading delete across every table that ever saw the data.

In production this class is backed by a KMS or Vault; ``Fernet`` + a column
stands in for it (ADR-006). The *indirection* — a reference resolved at fetch
time against per-tenant key material — is the part that stays true.
"""

from typing import Any, Protocol

from cryptography.fernet import Fernet, InvalidToken

from src.models.errors import ApiError, ErrorCode


class SecretStore(Protocol):
    """The two control-plane reads this client needs.

    A narrow protocol rather than the concrete repository, so the resolver can
    be exercised without a database — and so nothing here can reach for an
    unrelated control-plane read by accident.
    """

    def get_secret(self, secret_ref: str) -> dict[str, Any] | None: ...

    def get_tenant(self, tenant_id: str) -> Any: ...


class SecretsManagerClient:
    """Resolves a ``secret_ref`` to a plaintext credential, per tenant."""

    def __init__(self, store: SecretStore) -> None:
        self._store = store

    def resolve(self, secret_ref: str) -> str:
        """Decrypt the secret behind ``secret_ref`` with its own tenant's key.

        Raises ``CONNECTOR_AUTH_ERROR`` (**403**) — classified ``config_error``
        and so **not** degradable — for every failure path. A missing grant, a missing
        key and a destroyed key are all "this connector cannot be called with
        valid credentials", and none of them improve on retry.
        """
        row = self._store.get_secret(secret_ref)
        if row is None:
            raise self._auth_error(f"no secret is registered for ref {secret_ref!r}")

        tenant_id = row["tenant_id"]
        tenant = self._store.get_tenant(tenant_id)
        if tenant is None:
            raise self._auth_error(
                f"secret {secret_ref!r} names tenant {tenant_id!r}, which does not exist"
            )

        key = getattr(tenant, "fernet_key", None)
        if not key:
            # The crypto-shred state: the ciphertext survives, the key does not.
            raise self._auth_error(
                f"tenant {tenant_id!r} has no encryption key; its secrets are "
                f"unrecoverable (offboarded)"
            )

        try:
            return Fernet(self._as_bytes(key)).decrypt(self._as_bytes(row["ciphertext"])).decode()
        except (InvalidToken, ValueError, TypeError) as exc:
            # Never echo the ciphertext or the key into the message.
            raise self._auth_error(
                f"secret {secret_ref!r} could not be decrypted with tenant "
                f"{tenant_id!r}'s key"
            ) from exc

    @staticmethod
    def encrypt(fernet_key: str | bytes, plaintext: str) -> str:
        """Encrypt a credential for storage. Used by the seeder, not the request path."""
        token = Fernet(SecretsManagerClient._as_bytes(fernet_key)).encrypt(plaintext.encode())
        return token.decode()

    @staticmethod
    def _as_bytes(value: str | bytes) -> bytes:
        return value.encode() if isinstance(value, str) else value

    @staticmethod
    def _auth_error(detail: str) -> ApiError:
        return ApiError(
            code=ErrorCode.CONNECTOR_AUTH_ERROR,
            # 403, not 502 (ADR-029). HLD §4, design-doc §8.1 and the phase-2
            # file all say 403, and HLD §9 makes the error vocabulary a
            # provenance rail that must be identical across all three. Phase 1
            # shipped 502; Phase 2 is the first phase to render this code over
            # HTTP, so it is the phase that has to reconcile it.
            #
            # 403 is also the better answer on its merits: a revoked credential
            # is not this server erring, it is this tenant's connector being
            # unusable, and the caller action is administrative ("reconnect
            # GitHub") rather than "retry later" — which is exactly what 403
            # communicates and 502 does not.
            http=403,
            message=f"Connector credentials unavailable: {detail}",
            suggested_action="Reconnect this connector for the tenant (admin).",
        )
