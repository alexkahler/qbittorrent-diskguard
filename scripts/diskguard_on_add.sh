#!/bin/sh
# Usage: diskguard_on_add.sh "<hash>"
# Edit the URL in the -X POST to point at diskguard's on-add endpoint. 
# This script is called by qBittorrent when a torrent is added, and the torrent's hash is passed as an argument. 
# The script makes a POST request to diskguard's on-add endpoint with the torrent hash.

HASH="$1"

curl -fsS -m 2 \
  -X POST "http://diskguard:7070/on-add" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "hash=$HASH" \
  >/dev/null 2>&1 &

exit 0
