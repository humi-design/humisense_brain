# Image for the reference deployment's TLS init container.
#
# It only needs openssl to generate the database CA and server certificate. The generator
# script is mounted read-only at run time (see ops/compose/docker-compose.yml), so the
# image holds no secret and can be rebuilt freely.
#
# A real deployment does not use this image: it mounts certificates issued by its own CA.

FROM alpine:3.20

RUN apk add --no-cache openssl

# The script is mounted at /generate-tls.sh; this is the default so the image is usable
# on its own for inspection.
CMD ["/bin/sh", "-c", "echo 'mount ops/postgres/generate-tls.sh at /generate-tls.sh'"]
