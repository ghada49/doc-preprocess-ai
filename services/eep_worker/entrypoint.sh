#!/bin/sh
# Write Google service account credentials to the path the SDK expects.
# GOOGLE_CREDENTIALS_JSON is injected from AWS Secrets Manager at container start.
if [ -n "$GOOGLE_CREDENTIALS_JSON" ]; then
  mkdir -p /var/secrets/google
  printf '%s' "$GOOGLE_CREDENTIALS_JSON" > /var/secrets/google/key.json
fi
exec "$@"
