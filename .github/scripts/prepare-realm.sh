#!/usr/bin/env bash
# Get a realm ready for s3aas: run with the CLI pointed at the realm, before
# it is installed.
#
# Usage: prepare-realm.sh
#
# - Overbook the realm's hypervisor.  A warm realm is sized for the pool's
#   defaults, and the metapaas CP plus the s3 data plane do not both fit on it
#   one to one -- the runner test overbooked the same way.
# - Install metapaas, which s3aas plugs into, unless the realm already has it,
#   and wait until it is ACTIVE.
set -euo pipefail

here="$(dirname "$0")"

uuid="$(exordos c h l -o json | jq -r '.[0].uuid // ""')"
if [ -z "$uuid" ]; then
    echo "The realm reports no hypervisor to overbook" >&2
    exit 1
fi
exordos compute hypervisors update "$uuid" --cores-ratio 10.0 --ram-ratio 10.0

if [ -z "$(exordos ee l -o json -f name=metapaas | jq -r '.[0].status // ""')" ]; then
    exordos e e install metapaas
else
    echo "The realm already has metapaas"
fi
"$here/wait-for-element.sh" metapaas 1800
exordos e e show metapaas
