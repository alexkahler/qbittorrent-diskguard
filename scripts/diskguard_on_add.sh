#!/bin/sh
# Usage: diskguard_on_add.sh "<hash>"

HASH="$1"

curl -fsS -m 2 \
  -X POST "http://diskguard:7070/on-add" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "hash=$HASH" \
  >/dev/null 2>&1 &

exit 0
