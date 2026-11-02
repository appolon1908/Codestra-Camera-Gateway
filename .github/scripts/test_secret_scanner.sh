#!/usr/bin/env bash
# Trusted embedded defaults; candidate allow mechanisms are never source-scan authority.
set -euo pipefail
unset GITLEAKS_CONFIG GITLEAKS_CONFIG_TOML
work="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/strict-source-secrets.XXXXXX")"
trap 'rm -rf "$work"' EXIT
archive="$work/gitleaks.tar.gz"
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz --output "$archive"
echo "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb  $archive" | sha256sum --check -
tar --no-same-owner -xzf "$archive" -C "$work" gitleaks
printf '[extend]\nuseDefault = true\n' > "$work/trusted-default.toml"
: > "$work/empty.gitleaksignore"
reject_source_ignore() {
    if [ -e "$1/.gitleaksignore" ] || [ -L "$1/.gitleaksignore" ]; then
        echo 'NATIVE_IGNORE_AUTHORITY_REJECTED: source root ignore files and symlinks are forbidden' >&2
        return 1
    fi
}
strict_scan() {
    mode="$1"
    source="$2"
    shift 2
    reject_source_ignore "$source" || return 1
    unset GITLEAKS_CONFIG GITLEAKS_CONFIG_TOML
    "$work/gitleaks" "$mode" "${strict[@]}" "$@" "$source"
}
reject_source_ignore .
strict=(--config "$work/trusted-default.toml" --gitleaks-ignore-path "$work/empty.gitleaksignore" --ignore-gitleaks-allow --no-banner --redact=100 --exit-code 1)
mkdir -p "$work/key" "$work/inline" "$work/overrides"
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$work/key/adjacent.pem" 2>/dev/null
python3 - "$work" <<'PYFIXTURE'
import secrets, string, sys
from pathlib import Path
root = Path(sys.argv[1])
for directory, suffix in (('inline', ' # gitleaks:allow'), ('overrides', '')):
    token = 'ghp_' + ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(36))
    (root / directory / 'adjacent.md').write_text('access_token = "' + token + '"' + suffix + '\n')
PYFIXTURE
expect_status() {
    expected="$1"
    label="$2"
    shift 2
    set +e
    "$@"
    observed=$?
    set -e
    if [ "$observed" -ne "$expected" ]; then
        echo "SECRET_REGRESSION_FAIL=$label: expected $expected, got $observed" >&2
        exit 1
    fi
    echo "SECRET_REGRESSION_PASS=$label"
}
expect_status 1 generated-private-key strict_scan dir "$work/key"
expect_status 0 native-inline-control "$work/gitleaks" dir --config "$work/trusted-default.toml" --gitleaks-ignore-path "$work/empty.gitleaksignore" --no-banner --redact=100 --exit-code 1 "$work/inline"
expect_status 1 inline-comment-cannot-suppress strict_scan dir "$work/inline"
expect_status 1 capture-native-fingerprint strict_scan dir "$work/overrides" --report-format json --report-path "$work/fingerprint.json"
python3 - "$work" <<'PYOVERRIDE'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
findings = json.loads((root / 'fingerprint.json').read_text())
if not findings or any(not item.get('Fingerprint') for item in findings):
    raise SystemExit('Missing exact native fingerprint fixture')
(root / 'candidate-ignore.txt').write_text('\n'.join(item['Fingerprint'] for item in findings) + '\n')
(root / 'overrides/.gitleaks.toml').write_text("[extend]\nuseDefault = true\n[allowlist]\ndescription = 'test-only candidate bypass attempt'\npaths = ['''.*''']\n")
PYOVERRIDE
expect_status 0 native-config-control "$work/gitleaks" dir --config "$work/overrides/.gitleaks.toml" --gitleaks-ignore-path "$work/empty.gitleaksignore" --ignore-gitleaks-allow --no-banner --redact=100 --exit-code 1 "$work/overrides"
expect_status 1 candidate-config-cannot-suppress strict_scan dir "$work/overrides"
expect_status 1 candidate-env-cannot-suppress env GITLEAKS_CONFIG="$work/overrides/.gitleaks.toml" GITLEAKS_CONFIG_TOML='[allowlist]' "$work/gitleaks" dir "${strict[@]}" "$work/overrides"
cp "$work/candidate-ignore.txt" "$work/overrides/.gitleaksignore"
expect_status 0 native-ignore-control "$work/gitleaks" dir --config "$work/trusted-default.toml" --gitleaks-ignore-path "$work/empty.gitleaksignore" --ignore-gitleaks-allow --no-banner --redact=100 --exit-code 1 "$work/overrides"
expect_status 1 candidate-ignore-authority-rejected strict_scan dir "$work/overrides"
mkdir -p "$work/symlink" "$work/dangling"
ln -s "$work/empty.gitleaksignore" "$work/symlink/.gitleaksignore"
ln -s "$work/nonexistent-ignore" "$work/dangling/.gitleaksignore"
expect_status 1 source-ignore-symlink-rejected strict_scan dir "$work/symlink"
expect_status 1 source-ignore-dangling-symlink-rejected strict_scan dir "$work/dangling"
echo 'SCANNER_SUPPRESSION_REGRESSIONS=PASS'
