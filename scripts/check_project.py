"""Read-only project checks; no network, git mutations, or medical validation.

Run from any directory. --empty also checks the initial empty data layers.
The privacy pattern scan is heuristic and cannot establish anonymization.
"""
import argparse
import ast
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LAYERS = ('incoming', 'raw', 'materials', 'records', 'overview', 'outputs',
          'scripts', 'documentation', 'temp', 'examples')
STARTER = {
    'materials/manifest.json', 'materials/classification.json', 'materials/INDEX.md',
    'records/INDEX.md', 'records/DATABASE.md', 'records/medications/CURRENT.md',
}

def documents():
    for path in sorted(ROOT.rglob('*')):
        parts = path.relative_to(ROOT).parts
        if path.is_file() and not any(x in parts for x in ('.git', '.venv', 'venv', '__pycache__', 'temp')):
            yield path

def prose(text):
    return re.sub(r'^(```|~~~).*?^\1[^\n]*$', '', text, flags=re.M | re.S)

def anchors(text):
    result = set(re.findall(r'<a\s+(?:id|name)=["\']([^"\']+)["\']', text))
    seen = {}
    for heading in re.findall(r'^#{1,6}\s+(.+?)\s*#*$', text, re.M):
        slug = re.sub(r'[^\w\s-]', '', heading.lower()).replace(' ', '-')
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        result.add(slug + ('-' + str(count) if count else ''))
    return result

def verify(empty=False):
    errors = []
    for folder in LAYERS:
        if not (ROOT / folder / 'AGENTS.md').is_file():
            errors.append('Missing layer rules: ' + folder)
    required = ['AGENTS.md', 'README.md', '.gitignore', 'requirements.txt',
                'documentation/ARCHITECTURE.md', 'documentation/CURRENT.md',
                'documentation/DECISIONS.md', 'documentation/REQUIREMENTS.md',
                'documentation/PRIVACY.md', 'documentation/CHECKS.md', '.gitattributes']
    for name in required:
        if not (ROOT / name).is_file():
            errors.append('Missing required file: ' + name)
    files = list(documents())
    for path in files:
        name = path.relative_to(ROOT).as_posix()
        if path.suffix not in ('.md', '.py', '.json', '.jsonl', '.txt', '.sql'):
            continue
        try:
            text = path.read_text(encoding='utf-8-sig')
            if path.suffix == '.py':
                ast.parse(text, filename=name)
            if path.suffix == '.json':
                json.loads(text)
            if path.suffix == '.jsonl':
                for line in text.splitlines():
                    if line.strip():
                        json.loads(line)
            if path.suffix == '.md':
                for target in re.findall(r'\]\(([^\s)]+)\)', prose(text)):
                    target = unquote(target.strip('<>'))
                    url = urlsplit(target)
                    if url.scheme or url.netloc:
                        continue
                    linked = (path.parent / url.path).resolve() if url.path else path
                    if not linked.is_relative_to(ROOT) or not linked.exists():
                        errors.append(f'{name}: missing/outside link {target}')
                    elif url.fragment and linked.suffix == '.md':
                        if url.fragment not in anchors(linked.read_text(encoding='utf-8-sig')):
                            errors.append(f'{name}: missing anchor {target}')
            # Flag user-machine paths and common contact/secret formats, not values.
            patterns = (r'(?i)[a-z]:[\\/](?:users|work)[\\/]',
                        r'(?i)/(?:users|home)/[^/\s]+/',
                        r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}',
                        r'\+(?:375|7|380)\s*\(?\d[\d() -]{8,}',
                        r'(?i)(?:sk-[a-z0-9]{20,}|ghp_[a-z0-9]{20,})')
            if any(re.search(pattern, text) for pattern in patterns):
                errors.append(name + ': possible personal path/contact/secret; review required')
        except (ValueError, SyntaxError, OSError) as error:
            errors.append(f'{name}: {error}')
    if empty:
        manifest = json.loads((ROOT / 'materials/manifest.json').read_text(encoding='utf-8'))
        if manifest.get('materials') or manifest.get('duplicate_receipts'):
            errors.append('Expected an empty manifest')
        for path in files:
            rel = path.relative_to(ROOT)
            if rel.parts[0] in ('incoming', 'raw', 'materials', 'records', 'overview', 'outputs'):
                if path.name != 'AGENTS.md' and rel.as_posix() not in STARTER:
                    errors.append('Unexpected private-layer file: ' + rel.as_posix())
        current = (ROOT / 'records/medications/CURRENT.md').read_text(encoding='utf-8')
        if 'Курсы и сообщения ещё не добавлены.' not in current:
            errors.append('Medication CURRENT contains data')
    if errors:
        raise ValueError('\n'.join(errors))
    print('PASS: project structure, local links/anchors, JSON and Python syntax.'
          + (' Empty data layers checked.' if empty else '')
          + ' Heuristic privacy scan passed; this does not prove anonymization.')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--empty', action='store_true')
    try:
        verify(parser.parse_args().empty)
    except (ValueError, OSError) as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        sys.exit(1)
