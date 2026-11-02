"""Documentation/contract validation only. Application source is denied by default."""
import hashlib
import json
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml
from markdown_it import MarkdownIt

CI_FILES = {
    '.github/workflows/source-scope.yml',
    '.github/scripts/validate_source_scope.py',
    '.github/scripts/test_source_scope.py',
    '.github/source-scope.json',
    '.github/requirements-source-scope.txt',
    'governance/interim-release-policy.json',
}


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate mapping key')
        result[key] = value
    return result


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_yaml(loader, node):
    return unique_pairs((loader.construct_object(key, deep=True),
                         loader.construct_object(value, deep=True))
                        for key, value in node.value)


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_yaml)


def structured(path, text):
    if path.endswith('.json'):
        return json.loads(text, object_pairs_hook=unique_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
    return yaml.load(text, Loader=UniqueLoader)


class HtmlLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.ids = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in ('href', 'src') and value:
                self.links.append(value)
            if key == 'id' or (tag == 'a' and key == 'name'):
                self.ids.append(value)


def markdown_info(text):
    tokens = MarkdownIt('commonmark', {'html': True}).parse(text)
    links, anchors, counts = [], set(), {}
    for index, token in enumerate(tokens):
        if token.type == 'heading_open':
            raw = tokens[index + 1].content
            raw = re.sub(r'<[^>]*>', '', raw).lower()
            slug = re.sub(r'[^\w\- ]', '', raw).replace(' ', '-')
            count = counts.get(slug, 0)
            counts[slug] = count + 1
            anchors.add(slug + (f'-{count}' if count else ''))
        for child in token.children or []:
            if child.type == 'link_open':
                links.append(child.attrGet('href'))
            elif child.type == 'image':
                links.append(child.attrGet('src'))
            elif child.type == 'html_inline':
                parser = HtmlLinks()
                parser.feed(child.content)
                links.extend(parser.links)
                anchors.update(parser.ids)
        if token.type == 'html_block':
            parser = HtmlLinks()
            parser.feed(token.content)
            links.extend(parser.links)
            anchors.update(parser.ids)
    return links, anchors


def validate(root, tracked, manifest):
    errors, texts, markdown = [], {}, {}
    root = root.resolve()
    allowed_structured = manifest.get('structured_files', {})
    for name, mode in tracked.items():
        path = root / name
        if mode != '100644' or path.is_symlink():
            errors.append(f'Forbidden executable/symlink/submodule: {name}')
            continue
        allowed = (name.endswith('.md') or name in CI_FILES or name == '.gitignore'
                   or name in allowed_structured)
        if not allowed:
            errors.append(f'Outside documentation/contract scope; product CI required: {name}')
            continue
        try:
            text = path.read_text(encoding='utf-8')
            if '\0' in text or (text.startswith('#!') and name not in CI_FILES):
                raise ValueError('binary or disguised executable')
            texts[name] = text
            if name.endswith('.md'):
                markdown[name] = markdown_info(text)
            elif name.endswith(('.json', '.yaml', '.yml')):
                structured(name, text)
        except (ValueError, OSError, yaml.YAMLError) as exc:
            errors.append(f'{name}: {exc}')
    for name in manifest.get('baseline_files', []):
        if name not in tracked:
            errors.append(f'Baseline authority/contract removed: {name}')
    for name, phrases in manifest.get('required_text', {}).items():
        for phrase in phrases:
            if phrase not in texts.get(name, ''):
                errors.append(f'Authority requirement missing: {name}')
    for name, expected in allowed_structured.items():
        try:
            if json.dumps(structured(name, texts[name]), sort_keys=True) != json.dumps(expected, sort_keys=True):
                errors.append(f'Contract semantics changed; explicit contract review required: {name}')
        except (KeyError, ValueError, yaml.YAMLError):
            errors.append(f'Contract missing or malformed: {name}')
    for name, digest in manifest.get('authority_hashes', {}).items():
        actual = hashlib.sha256(texts.get(name, '').encode()).hexdigest()
        if actual != digest:
            errors.append(f'Authority changed; explicit authority review required: {name}')
    for name, (links, _) in markdown.items():
        for link in links:
            parsed = urlsplit(link)
            if parsed.scheme or parsed.netloc:
                continue  # Public URLs are not fetched and cannot produce external effects.
            target = root / unquote(parsed.path.lstrip('/')) if parsed.path.startswith('/') else (root / name).parent / unquote(parsed.path)
            if not parsed.path:
                target = root / name
            target = target.resolve()
            if not target.is_relative_to(root):
                errors.append(f'Internal link escapes repository: {name}')
                continue
            relative = target.relative_to(root).as_posix()
            exists = relative in tracked or any(p.startswith(relative.rstrip('/') + '/') for p in tracked)
            if not exists:
                errors.append(f'Broken internal link in {name}: {link}')
            elif parsed.fragment and relative.endswith('.md'):
                if unquote(parsed.fragment) not in markdown.get(relative, ([], set()))[1]:
                    errors.append(f'Broken heading link in {name}: {link}')
    return errors


def main():
    root = Path.cwd()
    raw = subprocess.check_output(['git', 'ls-files', '--stage', '-z']).decode()
    tracked = {}
    for entry in raw.split('\0'):
        if entry:
            metadata, name = entry.split('\t', 1)
            mode, _, stage = metadata.split()
            if stage != '0':
                raise SystemExit('Unmerged index rejected')
            tracked[name] = mode
    manifest = json.loads(Path('.github/source-scope.json').read_text())
    errors = validate(root, tracked, manifest)
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        raise SystemExit(1)
    print('Documentation/contract scope PASS; application and runtime certification NOT granted')


if __name__ == '__main__':
    main()
