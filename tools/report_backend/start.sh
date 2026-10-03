#!/bin/sh
set -eu
chown node:node /data
exec su -s /bin/sh node -c 'exec node /service/server.cjs'
