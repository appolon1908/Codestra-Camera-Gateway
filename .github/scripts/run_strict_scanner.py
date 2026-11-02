"""Keep native raw evidence and route only proven digest metadata separately."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from classify_scanner_metadata import classify


def run(binary, arguments, repository, registry, output_root):
    if not arguments or arguments[0] not in ('git', 'dir'):
        raise ValueError('Unrecognized scanner mode')
    kind = 'history' if arguments[0] == 'git' else 'tree'
    args = list(arguments)
    for flag in ('--config', '--gitleaks-ignore-path', '--ignore-gitleaks-allow'):
        if flag not in args:
            raise ValueError('Trusted scanner controls missing')
    source = Path(args[-1])
    ignore = source / '.gitleaksignore'
    if ignore.exists() or ignore.is_symlink():
        raise ValueError('Native source ignore authority forbidden')
    requested = None
    if '--report-format' in args:
        if args[args.index('--report-format') + 1] != 'json':
            raise ValueError('Only native JSON evidence supported')
    else:
        args.extend(['--report-format', 'json'])
    if '--report-path' in args:
        index = args.index('--report-path') + 1
        requested = Path(args[index])
        raw_path = Path(str(requested) + '.metadata-raw.json')
        if raw_path.exists():
            raise ValueError('Refusing to overwrite native scanner evidence')
        args[index] = str(raw_path)
    else:
        descriptor, name = tempfile.mkstemp(prefix='strict-source-' + kind + '-', suffix='-raw.json', dir=output_root)
        os.close(descriptor)
        raw_path = Path(name)
        args.extend(['--report-path', str(raw_path)])
    env = dict(os.environ)
    env.pop('GITLEAKS_CONFIG', None)
    env.pop('GITLEAKS_CONFIG_TOML', None)
    process = subprocess.run([binary, *args], env=env)
    if process.returncode not in (0, 1):
        raise ValueError('Native scanner execution error: ' + str(process.returncode))
    raw = raw_path.read_bytes()
    findings = json.loads(raw)
    filtered, approved, status = classify(repository, kind, process.returncode, findings, registry)
    if requested is not None:
        requested.write_text(json.dumps(filtered))
    if raw_path.read_bytes() != raw:
        raise ValueError('Native scanner evidence changed')
    import hashlib
    print(json.dumps({'native_raw_report': str(raw_path), 'native_raw_sha256': hashlib.sha256(raw).hexdigest(),
                      'native_status': process.returncode, 'approved_scanner_metadata': approved,
                      'remaining_findings': len(filtered), 'derived_status': status}, sort_keys=True))
    # Non-metadata findings still return 1 to the original exact fixture classifier.
    return status


if __name__ == '__main__':
    try:
        encoded = json.loads(Path(__file__).with_name('scanner_metadata_receipts.json').read_text())
        registry = json.loads(bytes(encoded['encoded_receipts']).decode())
        result = run(sys.argv[1], sys.argv[2:], os.environ['GITHUB_REPOSITORY'], registry,
                     os.environ.get('RUNNER_TEMP', tempfile.gettempdir()))
    except Exception as error:
        print('STRICT_SCANNER_AUTHORITY_ERROR=' + str(error), file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(result)
