#!/bin/sh
# Render the config from the environment, then hand over to icecast.
#
# Substitution is done by hand rather than with envsubst over the whole file so
# that a stray "$" in the XML can never be eaten, and so a missing password is
# an error instead of an empty string that would leave the server open.
set -eu

: "${ICECAST_SOURCE_PASSWORD:?ICECAST_SOURCE_PASSWORD must be set}"
: "${ICECAST_ADMIN_PASSWORD:?ICECAST_ADMIN_PASSWORD must be set}"
ICECAST_RELAY_PASSWORD="${ICECAST_RELAY_PASSWORD:-$ICECAST_ADMIN_PASSWORD}"
ICECAST_HOSTNAME="${ICECAST_HOSTNAME:-localhost}"
ICECAST_MAX_CLIENTS="${ICECAST_MAX_CLIENTS:-500}"
ICECAST_MAX_SOURCES="${ICECAST_MAX_SOURCES:-50}"

TEMPLATE=/etc/icecast2/icecast.template.xml
CONFIG=/tmp/icecast.xml

sed \
  -e "s|@ICECAST_SOURCE_PASSWORD@|${ICECAST_SOURCE_PASSWORD}|g" \
  -e "s|@ICECAST_RELAY_PASSWORD@|${ICECAST_RELAY_PASSWORD}|g" \
  -e "s|@ICECAST_ADMIN_PASSWORD@|${ICECAST_ADMIN_PASSWORD}|g" \
  -e "s|@ICECAST_HOSTNAME@|${ICECAST_HOSTNAME}|g" \
  -e "s|@ICECAST_MAX_CLIENTS@|${ICECAST_MAX_CLIENTS}|g" \
  -e "s|@ICECAST_MAX_SOURCES@|${ICECAST_MAX_SOURCES}|g" \
  "${TEMPLATE}" > "${CONFIG}"

exec icecast2 -c "${CONFIG}"
