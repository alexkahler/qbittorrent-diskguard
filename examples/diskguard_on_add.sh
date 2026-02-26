#!/bin/sh
# Usage: diskguard_on_add.sh "<hash>"
# Edit the URL in the -X POST to point at diskguard's on-add endpoint. 
# This script is called by qBittorrent when a torrent is added, and the torrent's hash is passed as an argument. 
# The script makes a POST request to diskguard's on-add endpoint with the torrent hash.

HASH="$1"
DISKGUARD_URL=diskguard # Change this to match the service name, or localhost if using Gluetun
DISKGUARD_SERVER_PORT=7070 # Remember to update this if you have changed the default DiskGuard server port in the environment settings.
DISKGUARD_ON_ADD_AUTH_TOKEN=your-secret-token # Set this to the same value as [server].on_add_auth_token in DiskGuard config.

curl -fsS -m 2 \
  -X POST "http://${DISKGUARD_URL}:${DISKGUARD_SERVER_PORT}/on-add" \
  -H "X-DiskGuard-Token: ${DISKGUARD_ON_ADD_AUTH_TOKEN}" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "hash=${HASH}" \
  >/dev/null 2>&1 &

exit 0
