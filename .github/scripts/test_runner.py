import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_classify_scanner_metadata import Classification
import run_strict_scanner as runner


class RunnerRouting(unittest.TestCase):
    setUp = Classification.setUp
    g = Classification.g

    def execute(self, findings, status=1, requested=True):
        with tempfile.TemporaryDirectory() as external:
            external = Path(external)
            binary = external / 'scanner'
            binary.write_text('#!/usr/bin/env python3\nimport os,sys,pathlib\np=pathlib.Path(sys.argv[sys.argv.index("--report-path")+1]);p.write_text(os.environ["TEST_NATIVE_REPORT"]);raise SystemExit(int(os.environ["TEST_NATIVE_STATUS"]))\n')
            binary.chmod(0o755)
            args = ['git', '--config', str(external/'trusted.toml'), '--gitleaks-ignore-path', str(external/'empty.ignore'), '--ignore-gitleaks-allow', '--redact=100']
            destination = external / 'existing-classifier.json'
            if requested:
                args += ['--report-format', 'json', '--report-path', str(destination)]
            args += ['.']
            raw = json.dumps(findings)
            with patch.dict(os.environ, TEST_NATIVE_REPORT=raw, TEST_NATIVE_STATUS=str(status)):
                observed = runner.run(str(binary), args, 'owner/test', [self.receipt], str(external))
            reports = list(external.glob('*raw.json'))
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0].read_text(), raw)
            filtered = json.loads(destination.read_text()) if requested else None
            return observed, filtered

    def test_only_proven_metadata_derives_success(self):
        status, rows = self.execute([self.item])
        self.assertEqual((status, rows), (0, []))

    def test_other_fixture_rows_reach_existing_classifier_unchanged(self):
        other = dict(self.item, File='existing-public-fixture.pem', Fingerprint='unchanged-existing-fixture')
        status, rows = self.execute([self.item, other])
        self.assertEqual(status, 1)
        self.assertEqual(rows, [other])

    def test_adjacent_credential_stays_failure(self):
        other = dict(self.item, File='adjacent-new-key.pem', RuleID='private-key', Fingerprint='unknown-real-key')
        self.assertEqual(self.execute([self.item, other])[0], 1)

    def test_original_no_report_invocation_still_retains_native_raw(self):
        self.assertEqual(self.execute([self.item], requested=False)[0], 0)

    def test_zero_scan_is_not_faked(self):
        self.assertEqual(self.execute([], status=0), (0, []))

    def test_scanner_execution_error_is_fatal(self):
        with self.assertRaises(ValueError):
            self.execute([], status=2)

    def test_native_root_ignore_is_fatal(self):
        Path('.gitleaksignore').write_text('candidate suppression attempt\n')
        with self.assertRaises(ValueError):
            self.execute([])


if __name__ == '__main__':
    unittest.main()
