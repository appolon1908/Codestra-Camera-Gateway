"""Classify exact reviewed historical scanner digest metadata; retain all other findings."""
import argparse, ast, hashlib, json, os, re, subprocess
from pathlib import Path

PATH = '.github/scripts/validate_scanner_upgrade.py'

def git(*args):
    return subprocess.check_output(['git', *args])

def classify(repository, kind, status, findings, registry):
    if type(status) is not int or status not in (0, 1) or kind not in ('history', 'tree'):
        raise ValueError('Scanner error or invalid scope')
    if type(findings) is not list or (status == 0) != (len(findings) == 0):
        raise ValueError('Malformed report/status mismatch')
    receipts = [x for x in registry if x['repository'] == repository]
    if len(receipts) != 1:
        raise ValueError('Unknown or duplicate repository receipt')
    receipt = receipts[0]
    if receipt.get('legacy_metadata_authorized') is False:
        if any(type(item) is not dict for item in findings):
            raise ValueError('Malformed finding')
        return findings, [], status
    filtered, approved = [], []
    for item in findings:
        if type(item) is not dict:
            raise ValueError('Malformed finding')
        commit = receipt['commit'] if kind == 'history' else ''
        fingerprint = (commit + ':' if commit else '') + PATH + ':generic-api-key:6'
        matches = (item.get('File') == PATH and item.get('RuleID') == 'generic-api-key'
                   and type(item.get('StartLine')) is int and item['StartLine'] == 6
                   and type(item.get('EndLine')) is int and item['EndLine'] == 6
                   and item.get('Commit') == commit and item.get('Fingerprint') == fingerprint)
        if not matches:
            filtered.append(item)
            continue
        if approved:
            raise ValueError('Duplicate metadata finding')
        revision = receipt['commit']
        if git('rev-parse', revision + '^').decode().strip() != receipt['parent']:
            raise ValueError('Published receipt parent mismatch')
        if kind == 'tree' and git('rev-parse', 'HEAD').decode().strip() != revision:
            raise ValueError('Tree metadata is not exact historical head')
        entry = git('ls-tree', revision, '--', PATH).decode().strip().split()
        if entry != ['100644', 'blob', receipt['blob'], PATH]:
            raise ValueError('Historical helper Git blob/mode mismatch')
        raw = git('show', revision + ':' + PATH)
        if hashlib.sha256(raw).hexdigest() != receipt['sha256']:
            raise ValueError('Historical helper bytes mismatch')
        if hashlib.sha256(raw.splitlines()[5]).hexdigest() != receipt['line_sha256']:
            raise ValueError('Historical metadata line mismatch')
        if kind == 'tree' and Path(PATH).read_bytes() != raw:
            raise ValueError('Effective tree differs from historical blob')
        tree = ast.parse(raw)
        assignments = {n.targets[0].id: n.value for n in tree.body if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)}
        expected_node = assignments.get('EXPECTED')
        if not isinstance(expected_node, ast.Dict) or expected_node.lineno != 6:
            raise ValueError('Not literal native EXPECTED metadata')
        expected = ast.literal_eval(expected_node)
        if expected != receipt['expected'] or ast.literal_eval(assignments['REPOSITORY']) != repository or ast.literal_eval(assignments['BASE']) != receipt['parent']:
            raise ValueError('Native metadata provenance mismatch')
        for path, digest in expected.items():
            if not re.fullmatch('[a-f0-9]{64}', digest) or hashlib.sha256(git('show', revision + ':' + path)).hexdigest() != digest:
                raise ValueError('Referenced known-file mathematical digest mismatch')
        approved.append({'repository': repository, 'scope': kind, 'commit': revision, 'blob': receipt['blob'], 'fingerprint': fingerprint, 'line_sha256': receipt['line_sha256']})
    return filtered, approved, 1 if filtered else 0

def main():
    p = argparse.ArgumentParser()
    for key in ('repository', 'scope', 'report', 'output', 'registry', 'status-output'):
        p.add_argument('--' + key, required=True)
    p.add_argument('--status', type=int, required=True)
    a = p.parse_args()
    if len({Path(x).resolve() for x in (a.report,a.output,a.status_output)}) != 3:
        raise ValueError('Raw report must remain unchanged')
    findings = json.loads(Path(a.report).read_text())
    encoded = json.loads(Path(a.registry).read_text())
    registry = json.loads(bytes(encoded['encoded_receipts']).decode())
    filtered, approved, status = classify(a.repository, a.scope, a.status, findings, registry)
    Path(a.output).write_text(json.dumps(filtered))
    Path(a.status_output).write_text(str(status) + '\n')
    print(json.dumps({'approved_scanner_metadata': approved, 'remaining_findings': len(filtered), 'raw_report_sha256': hashlib.sha256(Path(a.report).read_bytes()).hexdigest(), 'filtered_status': status}, sort_keys=True))
    # Exit status 0 means classification succeeded, NOT that downstream security passed.
    # Caller must pass the filtered report and derived status to its existing classifier.

if __name__ == '__main__':
    main()
