#!/bin/sh
# Container entrypoint (Demo Slice 2). A Fly volume mounts root-owned, so the only root step is
# handing /data to the unprivileged user; the server itself never runs as root.
set -eu
mkdir -p /data
chown 10001:10001 /data
chmod 0700 /data
exec setpriv --reuid=10001 --regid=10001 --clear-groups --inh-caps=-all "$@"
