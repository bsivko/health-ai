"""Read-only project checks; no network, git mutations, or medical validation.

Run from any directory. --empty also checks the initial empty data layers.
--privacy enables the heuristic publication scan.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import sys
import materials

ROOT = Path(__file__).resolve().parents[1]
LAYERS = ('incoming', 'raw', 'materials', 'records', 'overview', 'outputs',
          'scripts', 'documentation', 'temp', 'examples')
DATA_LAYERS = {'incoming', 'raw', 'materials', 'records', 'overview', 'outputs'}
# LF-normalized baselines of the reviewed, empty working layers.
STARTER = {
    'incoming/AGENTS.md': '3231cfc2752a4d683f40b5a445543a4f6bcfae120db61063039b5749152aae62',
    'raw/AGENTS.md': 'eef18efee71b07b0d90cab3f67db784c9d72852ac557b948e02e226d953d58c7',
    'raw/undated/AGENTS.md': 'ff5e9605b449c7543fb6a4dfabf521aa73ad39e4c29c2698eade67c90d5dd66b',
    'materials/AGENTS.md': '8bb4657d0a99d43a37972469e80fd50e0de98355c13542256f410738434f89b8',
    'materials/classification.json': 'b5d9b6fdf622ff77bec8d14d5878a6592683a4bc4c90448ec9c19d6e9ef8c707',
    'materials/INDEX.md': 'b4bff103e03c0b20fbbafabbb0761e4aeaeb0ccfe7d53b289dbdfc095ec0dadf',
    'materials/manifest.json': '0e95acfa9bd51fe04f4cfab7ebf2fb5454c9b6fe3a720262a3f5a3dd6b890a65',
    'materials/undated/AGENTS.md': '123733051a0fcaec70a75e746f205c75eddb117b8688ba8febac1cc63d7be584',
    'records/AGENTS.md': 'b82966fba28f8e46ef659dc9063dd0422a23cdd831019a585b962040dc1b13a3',
    'records/DATABASE.md': '652af95646bb480442df450513606cd0253caa9b620027c93eebf5d1b0268297',
    'records/INDEX.md': 'fc278750757b056b1f11264b47c1d7a082f5bbc48310bd339a1e988592e0e65d',
    'records/measurements/AGENTS.md': '72c5a46b79c902828e2a0b0fc73c81946e7e0b61cb8b967a12f2e6d3e69783fe',
    'records/medications/AGENTS.md': '3eb5fcc0cec46919d5aa3591fb2404fa5f5db477f89b9b136eca9df87e706f0d',
    'records/medications/CURRENT.md': 'b87f817b1ef90dae276822f0fa203720397c7d25045f9c733c963857d9cedf0a',
    'records/topics/AGENTS.md': 'b94f4886ab793359258b470cd02396570f34f0a1a8a31e1ea85de33204ea37bd',
    'overview/AGENTS.md': '7c8e561576361e6d2044d38d931c20768a1de01e942450f69c15d58335cb0e37',
    'outputs/AGENTS.md': '281a35ee44666ad2bb7746305306a823b627ae411a8a1b718b217e82dec9371f',
}

def documents():
    for path in sorted(ROOT.rglob('*')):
        parts = path.relative_to(ROOT).parts
        if path.is_file() and not any(x in parts for x in ('.git', '.venv', 'venv', '__pycache__', 'temp')):
            yield path

def anchors(text):
    text = materials.markdown_prose(text)
    result = set(re.findall(r'<a\s+(?:id|name)=["\']([^"\']+)["\']', text))
    seen = {}
    for heading in re.findall(r'^#{1,6}\s+(.+?)\s*#*$', text, re.M):
        slug = re.sub(r'[^\w\s-]', '', heading.lower()).replace(' ', '-')
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        result.add(slug + ('-' + str(count) if count else ''))
    return result


def empty_layer_errors():
    errors = []
    # Enumerate the working layers directly, including normally ignored folders.
    for layer in sorted(DATA_LAYERS):
        for path in (ROOT / layer).rglob('*'):
            rel = path.relative_to(ROOT).as_posix()
            if path.is_symlink() or (path.is_file() and rel not in STARTER):
                errors.append('Unexpected private-layer file: ' + rel)
    for name, expected in STARTER.items():
        try:
            text = (ROOT / name).read_text(encoding='utf-8')
            if hashlib.sha256(text.encode('utf-8')).hexdigest() != expected:
                errors.append('Starter content changed: ' + name)
        except (OSError, UnicodeError):
            errors.append('Missing or unreadable starter file: ' + name)
    return errors


def verify(empty=False, *, privacy=False):
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
        if privacy and (path.name == '.env' or path.name.startswith('.env.')):
            errors.append(name + ': review local environment file before publication')
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
                for target in materials.markdown_links(text):
                    location = materials.link_target(target)
                    if location is None:
                        continue
                    destination, fragment = location
                    linked = (path.parent / destination).resolve() if destination else path
                    if not linked.is_relative_to(ROOT) or not linked.exists():
                        errors.append(f'{name}: missing/outside link {target}')
                    elif fragment and linked.suffix == '.md':
                        if fragment not in anchors(linked.read_text(encoding='utf-8-sig')):
                            errors.append(f'{name}: missing anchor {target}')
            # Flag user-machine paths and common contact/secret formats, not values.
            patterns = (r'(?i)[a-z]:[\\/](?:users|work)[\\/]',
                        r'(?i)/(?:users|home)/[^/\s]+/',
                        r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}',
                        r'\+(?:375|7|380)\s*\(?\d[\d() -]{8,}',
                        r'(?i)(?:sk-[a-z0-9]{20,}|ghp_[a-z0-9]{20,})')
            if privacy and any(re.search(pattern, text) for pattern in patterns):
                errors.append(name + ': possible personal path/contact/secret; review required')
        except (ValueError, SyntaxError, OSError) as error:
            errors.append(f'{name}: {error}')
    if empty:
        errors.extend(empty_layer_errors())
    if errors:
        raise ValueError('\n'.join(errors))
    print('PASS: project structure, local links/anchors, JSON and Python syntax.'
          + (' Empty data layers checked.' if empty else '')
          + (' Heuristic privacy scan passed.' if privacy else ''))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--empty', action='store_true')
    parser.add_argument('--privacy', action='store_true', help='Scan for personal paths, contacts and secrets')
    try:
        args = parser.parse_args()
        verify(args.empty, privacy=args.privacy)
    except (ValueError, OSError) as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        sys.exit(1)
