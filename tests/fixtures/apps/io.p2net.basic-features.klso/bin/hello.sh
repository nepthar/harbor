#!/bin/sh
# Trivial demo payload for the basic-features fixture bundle.
while true; do
  date -u >> /myapp/config/hello.log
  sleep 10
done
