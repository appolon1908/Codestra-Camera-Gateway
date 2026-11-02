import json
import tempfile
import unittest
from pathlib import Path
from validate_source_scope import validate


class SourceScopeTests(unittest.TestCase):
    def check(self, files, extra=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            manifest = {'repository': 'appolon1908/example', 'structured_files': {},
                        'required_text': {}, 'baseline_files': []}
            manifest.update(extra or {})
            return validate(root, {p: '100644' for p in files}, manifest)

    def test_application_source_cannot_enter_documentation_lane(self):
        self.assertTrue(self.check({'src/server.py': 'print(1)'}))

    def test_unknown_configuration_cannot_silently_enter_lane(self):
        self.assertTrue(self.check({'deploy/config.yaml': 'enabled: true'}))

    def test_markdown_extension_cannot_hide_executable(self):
        self.assertTrue(self.check({'README.md': '#!/bin/sh\necho unsafe'}))

    def test_broken_internal_link_is_rejected(self):
        self.assertTrue(self.check({'README.md': '[missing](docs/absent.md)'}))

    def test_reference_style_broken_link_is_rejected(self):
        self.assertTrue(self.check({'README.md': '[missing][ref]\n\n[ref]: missing.md'}))

    def test_missing_heading_is_rejected(self):
        self.assertTrue(self.check({'README.md': '[wrong](#missing)\n\n# Existing'}))

    def test_valid_heading_and_reference_links_pass(self):
        self.assertEqual([], self.check({'README.md': '# Hello World\n[ok][ref]\n\n[ref]: #hello-world'}))

    def test_duplicate_json_keys_are_rejected(self):
        self.assertTrue(self.check({'known.json': '{"a":1,"a":2}'}, {'structured_files': {'known.json': {}}}))

    def test_duplicate_yaml_keys_are_rejected(self):
        self.assertTrue(self.check({'known.yaml': 'a: 1\na: 2'}, {'structured_files': {'known.yaml': {}}}))

    def test_required_authority_cannot_disappear(self):
        self.assertTrue(self.check({'README.md': '# Changed'}, {'required_text': {'README.md': ['production effects remain separately gated']}}))

    def test_contract_semantics_cannot_change(self):
        self.assertTrue(self.check({'known.json': '{"enabled": true}'}, {'structured_files': {'known.json': {'enabled': False}}}))

    def test_symlink_or_executable_mode_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'README.md').write_text('# Docs')
            for mode in ('100755', '120000', '160000'):
                self.assertTrue(validate(root, {'README.md': mode}, {'structured_files': {}, 'required_text': {}, 'baseline_files': []}))

    def test_candidate_cannot_remove_trusted_manifest_restrictions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'README.md').write_text('# Docs')
            trusted = {'baseline_files': ['README.md'], 'structured_files': {}, 'authority_hashes': {'README.md': 'fixed'}}
            self.assertTrue(validate(root, {'README.md': '100644'}, {}, trusted_manifest=trusted))

    def test_candidate_cannot_rewrite_contract_expectation_with_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'known.json').write_text('{"enabled": true}')
            trusted = {'baseline_files': ['known.json'], 'structured_files': {'known.json': {'enabled': False}}}
            candidate = {'baseline_files': [], 'structured_files': {'known.json': {'enabled': True}}}
            self.assertTrue(validate(root, {'known.json': '100644'}, candidate, trusted_manifest=trusted))


if __name__ == '__main__':
    unittest.main()
