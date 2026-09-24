#!/usr/bin/env bash
# Creates local-only development credentials under dev/secrets (git-ignored).
set -euo pipefail
cd "$(dirname "$0")"
umask 077
mkdir -p secrets/openbao/kv/cameras/mock
[[ -f secrets/api-token ]] || python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > secrets/api-token
# Matches the mock-rtsp read credentials in docker-compose.dev.yml.
printf 'dev-camera-pass\n' > secrets/openbao/kv/cameras/mock/password
chmod -R a+rX secrets  # container runs as uid 10001
echo "API token: $(cat secrets/api-token)"
