#!/usr/bin/env bash
# Run the functional suite on the realm, where the s3 data plane is.
#
# Usage: run-functional-in-realm.sh
#
# The suite talks to the s3 instance on its own address, which only the
# realm's network reaches -- not the runner.  So wait for the metapaas control
# plane, then copy this checkout to the realm's core VM over ssh and run tox
# there, against the core and the control plane on that VM.
#
# REALM_CORE_URL, SSH_KEY, SSH_HOST, SSH_PORT and ADMIN_PASSWORD come from the
# environment, as exordos_tests' element_realm_test workflow leaves them with
# `ssh: true`.
set -euo pipefail

: "${REALM_CORE_URL:?REALM_CORE_URL is not set}"
: "${SSH_KEY:?SSH_KEY is not set}"
: "${SSH_HOST:?SSH_HOST is not set}"
: "${SSH_PORT:?SSH_PORT is not set}"
: "${ADMIN_PASSWORD:?ADMIN_PASSWORD is not set}"

# An ACTIVE s3aas element only means the core finished reconciling it: the
# metapaas control plane it registers the s3 type in is a VM that may still be
# booting, and the ingress answers 5xx until it is up.  Any answer the CP
# produces itself -- 401 included, the call is unauthenticated -- means it is.
cp_url="${REALM_CORE_URL%/core}/metapaas"
deadline=$((SECONDS + 780))
while :; do
    code="$(curl -s -m 10 -o /dev/null -w '%{http_code}' \
        "$cp_url/v1/types/s3/versions/")" || code=000
    if [ "$code" != "000" ] && [ "$code" -lt 500 ]; then
        echo "The metapaas control plane answers ($code)"
        break
    fi
    if [ "$SECONDS" -ge "$deadline" ]; then
        echo "The metapaas control plane never answered on $cp_url (last: $code)" >&2
        exit 1
    fi
    echo "Waiting for the metapaas control plane... ($code)"
    sleep 5
done

realm=(ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
       -o BatchMode=yes -o ServerAliveInterval=30 -i "$SSH_KEY"
       -p "$SSH_PORT" "ubuntu@$SSH_HOST")

# The whole checkout, .git included: the package's version comes from git.
echo "Copying the checkout to the realm"
tar -C . -czf - . | "${realm[@]}" \
    'rm -rf ~/s3 && mkdir ~/s3 && tar -C ~/s3 -xzf -'

# Through stdin, not the command line, so it shows in no process list.
printf '%s' "$ADMIN_PASSWORD" | "${realm[@]}" \
    'umask 077 && cat > ~/.s3_admin_password'

"${realm[@]}" bash -s <<'REMOTE'
set -euo pipefail
cd ~/s3

if ! command -v uv > /dev/null && [ ! -x ~/.local/bin/uv ]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
uv tool install tox --with tox-uv

# The core and the control plane are served on this VM, through its ingress,
# which resolves the "default" IAM client the suite logs in with.
export EXORDOS_ENDPOINT=http://localhost/api/core
export EXORDOS_S3_CP_URL=http://localhost/api/metapaas
export EXORDOS_USERNAME=admin
EXORDOS_PASSWORD="$(cat ~/.s3_admin_password)"
export EXORDOS_PASSWORD
export EXORDOS_POLL_TIMEOUT=1800

tox -e py312-functional
REMOTE
