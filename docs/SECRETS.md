# Secrets handling

Layer 1 stores connection credentials through a single interface, `SecretsProvider`
(`backend/src/humisense/secrets/`). No other code path reads or writes a stored
credential, so the operational rules live in one place.

## Providers

| `SECRETS_PROVIDER` | Implementation | Use |
| --- | --- | --- |
| `encrypted_db` | `EncryptedDbStore` | Default. Credentials are sealed with AES-256-GCM and stored in the database. |
| `env` | `EnvSecretsProvider` | Development only. Reads a JSON object from an environment variable. Refused in production unless `SECRETS_ALLOW_ENV_PROVIDER=true` is set explicitly. |

A managed vault is the intended long-term home for credentials. `encrypted_db` is a
documented, bounded substitute for deployments where a vault is genuinely unavailable; it
is not a vault, and it is not a substitute for one.

## Encryption

- Algorithm: AES-256-GCM (AEAD) via the `cryptography` library. No home-made construction
  is used.
- Key: exactly 32 bytes, supplied through `SECRETS_MASTER_KEY` as base64. The key is read
  from configuration at startup and is never written to the database, so a database dump
  alone does not disclose a credential.
- Nonce: 12 random bytes per write. Every write uses a fresh nonce, so two writes of the
  same value produce different ciphertext.
- Associated data: the tenant identifier and the secret name are bound into the AEAD
  associated data. A ciphertext copied into another tenant's row fails to decrypt instead
  of disclosing a credential.
- Key version: stored per row so a future key ring can be introduced without a schema
  change.

The provider raises the same error whether a blob fails because of a wrong key, a wrong
tenant, or tampering, so a decryption failure is not an oracle.

## Exposure rules

- Credentials are write-only through the API. A request may set them; no response model
  contains them.
- The frontend never receives or stores a credential, a token, or a session value.
- `RedactionFilter` scrubs known credential keys and bearer/token patterns from logs.
- Connector errors carry a safe message and a stable code, never the value.

## Key management

- Generate the key with a cryptographically secure source, for example
  `python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"`.
- Store the key in the deployment's secret manager and inject it as `SECRETS_MASTER_KEY`.
  Never commit it, and never place it in the application database.
- Production validation fails startup when `SECRETS_PROVIDER=encrypted_db` and no master
  key is configured.
- Losing the key makes every stored credential unrecoverable; the connections must be
  re-credentialed. Back the key up under the same controls as the database.

## Residual risk

An attacker who holds both a database dump and the master key recovers the credentials.
The key is therefore held outside the database and under separate access control.
