#!/bin/sh
# volumes are mounted via HAPP_VOLUMES ("name:/guest/path,name:/guest/path").

date

echo "Hello, here are the volumes I was passed (from \$HAPP_VOLUMES)"

echo "$HAPP_VOLUMES" | tr ',' '\n' | while IFS=':' read -r name path; do
  echo
  echo "Volume '$name' mounted at $path:"
  ls -al "$path"
done
