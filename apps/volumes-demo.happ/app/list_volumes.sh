#!/bin/bash

date

echo "Hello, here are the volumes I was passed"

for arg in "$@"; do
  echo "Contents of volume at $arg"
  ls -al "$arg"
done