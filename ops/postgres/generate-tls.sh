#!/bin/sh
#
# Generate the database TLS material for the reference deployment.
#
# Runs once, in a short-lived init container, before PostgreSQL starts. It writes a
# self-signed CA and a server certificate into a shared volume that both PostgreSQL and
# the backend read. A real deployment replaces this with certificates from its own CA;
# the point here is that the reference stack exercises the same verified-TLS path that
# production uses, instead of disabling the requirement to make the demo start.
#
# The server certificate carries the service name `db` as a subject alternative name, so
# the backend's `verify-full` hostname check succeeds.
#
# Idempotent: if a usable CA already exists in the volume, nothing is regenerated, so a
# restart does not invalidate certificates a running backend already trusts.

set -eu

CERT_DIR="${CERT_DIR:-/certs}"
CA_KEY="$CERT_DIR/ca.key"
CA_CRT="$CERT_DIR/ca.crt"
SERVER_KEY="$CERT_DIR/server.key"
SERVER_CRT="$CERT_DIR/server.crt"
# PostgreSQL runs as uid 70 in the official image; the key must be owned by it and not
# world-readable.
PG_UID="${PG_UID:-70}"

if [ -s "$CA_CRT" ] && [ -s "$SERVER_CRT" ] && [ -s "$SERVER_KEY" ]; then
    echo "generate-tls: existing certificates found in $CERT_DIR, leaving them in place"
    exit 0
fi

mkdir -p "$CERT_DIR"

# 1. A self-signed certificate authority.
openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes \
    -keyout "$CA_KEY" -out "$CA_CRT" \
    -subj "/CN=humisense-reference-ca" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" >/dev/null 2>&1

# 2. A server key and a certificate signed by that authority, valid for the compose
#    service name and for loopback.
openssl req -newkey rsa:2048 -sha256 -nodes \
    -keyout "$SERVER_KEY" -out "$CERT_DIR/server.csr" \
    -subj "/CN=db" >/dev/null 2>&1

cat >"$CERT_DIR/server.ext" <<'EOF'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:db,DNS:localhost,IP:127.0.0.1
EOF

openssl x509 -req -in "$CERT_DIR/server.csr" -sha256 -days 3650 \
    -CA "$CA_CRT" -CAkey "$CA_KEY" -CAcreateserial \
    -extfile "$CERT_DIR/server.ext" -out "$SERVER_CRT" >/dev/null 2>&1

rm -f "$CERT_DIR/server.csr" "$CERT_DIR/server.ext" "$CERT_DIR/ca.srl"

# The server key is readable only by PostgreSQL. The CA certificate is world-readable so
# the backend can read it as its non-root user.
chown "$PG_UID:$PG_UID" "$SERVER_KEY"
chmod 0600 "$SERVER_KEY"
chmod 0644 "$CA_CRT" "$SERVER_CRT"
chmod 0600 "$CA_KEY"

echo "generate-tls: wrote CA and server certificate to $CERT_DIR"
