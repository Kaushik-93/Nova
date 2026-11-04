#!/bin/sh
# /etc/trino/catalog is a named volume, so catalogs added with CREATE CATALOG
# survive restarts. On each start, drop the image's sample catalogs (`memory` is
# writable, and none of them are real sources) and install the demo ones.
set -eu
rm -f /etc/trino/catalog/jmx.properties /etc/trino/catalog/memory.properties \
      /etc/trino/catalog/tpcds.properties /etc/trino/catalog/tpch.properties
cp /nova/catalog/*.properties /etc/trino/catalog/
mkdir -p /data/trino/secrets
chmod 700 /data/trino/secrets
exec /usr/lib/trino/bin/launcher run --etc-dir /etc/trino
