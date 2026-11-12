"""Shared material checks and navigation. No OCR or source-file mutations."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
PENDING_INTAKE = 'содержание проверено; перенос ожидается.'
STATUS_MARKER = ' <!-- intake-status -->'
FENCED = re.compile(r'(?ms)^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?^(?: {0,3}\1[`~]*[ \t]*$|\Z)')
INLINE_LINK = re.compile(r'\]\((?:<([^>\n]+)>|((?:[^()\n]|\((?:[^()\n]|\([^()\n]*\))*\))*))\)')


def local(path, parent=ROOT):
    resolved = (parent / path).resolve()
    if not resolved.is_relative_to(ROOT):
        raise ValueError(f'Path outside project: {path}')
    return resolved


def require(condition, message):
    if not condition:
        raise ValueError(message)


def region_bounds(region):
    """Normalized rectangle in the original image, before any viewing rotation."""
    named = {'top': (0, 0, 1, .5), 'bottom': (0, .5, 1, 1), 'whole': (0, 0, 1, 1)}
    if region.get('region') in named:
        require('bounds' not in region, 'Named region cannot override bounds')
        return named[region['region']]
    require(region.get('region') == 'box', 'Invalid source region')
    box = region.get('bounds')
    require(isinstance(box, list) and len(box) == 4 and
            all(type(v) in (int, float) and 0 <= v <= 1 for v in box), 'Invalid region bounds')
    require(box[0] < box[2] and box[1] < box[3], 'Empty/reversed region bounds')
    return tuple(box)


def regions_overlap(a, b):
    return max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def load():
    manifest = json.loads(local('materials/manifest.json').read_text(encoding='utf-8'))
    taxonomy = json.loads(local('materials/classification.json').read_text(encoding='utf-8'))
    return manifest, taxonomy


def markdown_prose(text):
    return FENCED.sub('', text.replace('\r\n', '\n').replace('\r', '\n'))


def markdown_links(text):
    for match in INLINE_LINK.finditer(markdown_prose(text)):
        yield match.group(1) or match.group(2)


def link_target(target):
    url = urlsplit(target)
    if not url.scheme and not url.netloc:
        return unquote(url.path), unquote(url.fragment)
    return None


def markdown_link(label, target):
    label = str(label).replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]')
    return f'[{label}]({quote(target)})'


def check_links(path, *, text=None, staged=()):
    if text is None:
        text = path.read_text(encoding='utf-8')
    links = set()
    for target in markdown_links(text):
        location = link_target(target)
        if not location or not location[0]:
            continue
        target_path = local(location[0], path.parent)
        require(target_path.exists() or target_path in staged,
                f'Missing link in {path.relative_to(ROOT).as_posix()}: {target}')
        links.add(target_path)
    return links


def verify(manifest, taxonomy, *, hashes=True, inventory=False, staged_cards=None, staged_sources=None):
    staged_cards = staged_cards or {}
    staged_sources = staged_sources or {}
    staged_paths = staged_cards.keys() | staged_sources.keys()
    identifiers = set()
    paths = {}
    source_paths_by_hash = {}
    cards = set()
    derived_paths = set()
    regions = {}
    duplicates_by_material = {}
    for receipt in manifest.get('duplicate_receipts', []):
        duplicates_by_material.setdefault(receipt['canonical_id'], set()).add(local(receipt['archived_copy']))
    archive = local('raw')
    materials_dir = local('materials')
    for entry in manifest['materials']:
        ident = entry['id']
        require(ident not in identifiers, f'Duplicate material ID: {ident}')
        identifiers.add(ident)
        require(entry['status'] == 'completed', f'Incomplete intake: {ident}')
        classification = entry['classification']
        require(classification['material_type'] in taxonomy['material_types'], f'Unknown type: {ident}')
        domains = classification['domains']
        require(bool(domains) and set(domains) <= set(taxonomy['domains']), f'Unknown/empty domains: {ident}')
        require(len(domains) == len(set(domains)), f'Repeated domain: {ident}')
        require(classification['primary_domain'] is None or classification['primary_domain'] in domains,
                f'Primary domain absent from domains: {ident}')
        card = local(entry['card'])
        prefix = 'MAT-' + entry['sha256'][:12]
        expected_id = re.escape(prefix) + (r'-[a-z][a-z0-9-]*' if entry.get('source_regions') else '')
        require(re.fullmatch(r'[0-9a-f]{64}', entry['sha256']) and re.fullmatch(expected_id, ident),
                f'ID does not match source hash/scope: {ident}')
        require(card.parent.parent == materials_dir and card.name == ident + '.md',
                f'Card path does not match material ID: {ident}')
        cards.add(card)
        require(card.is_file() or card in staged_cards, f'Missing card: {ident}')
        text = staged_cards[card] if card in staged_cards else card.read_text(encoding='utf-8')
        if inventory and card not in staged_cards:
            prose = markdown_prose(text)
            status = re.escape(PENDING_INTAKE)
            pending = (re.search(r'^(?:- Статус|Статус скана): ' + status + re.escape(STATUS_MARKER) + r'$', prose, re.M) or
                       re.search(r'^- Статус: ' + status + r'$', prose.split('\n## Источники', 1)[0], re.M) or
                       (entry.get('additional_representations') and
                        prose.rstrip().endswith('Статус скана: ' + PENDING_INTAKE)))
            require(not pending, f'Unfinished intake publication: {ident}; resume its receipt')
        require(ident in text, f'Card ID mismatch: {ident}')
        links = check_links(card, text=text, staged=staged_paths)
        # Earlier records may link a retained scan transcription directly.
        scan_transcript = card.with_name(ident + '-scan-v2.md')
        if scan_transcript in links:
            derived_paths.add(scan_transcript)
        for derived in entry.get('derived_files', []):
            derived_path = local(derived['path'])
            require(derived_path.is_relative_to(local('materials')) and derived_path.is_file(),
                    f'Missing/outside derived file: {ident}')
            derived_paths.add(derived_path)
            require(re.fullmatch(r'[0-9a-f]{64}', derived['sha256']) is not None,
                    f'Invalid derived SHA-256: {ident}')
            if hashes:
                require(digest(derived_path) == derived['sha256'], f'Derived file hash mismatch: {derived_path.name}')
            if derived_path.suffix.lower() == '.md':
                check_links(derived_path, staged=staged_paths)
        sources = entry.get('originals') or [{'path': entry['original'], 'sha256': entry['sha256']}]
        source_boxes = {}
        if entry.get('source_regions'):
            source_names = {s['path'] for s in sources}
            require(len(entry['source_regions']) == len(sources), f'Incomplete source regions: {ident}')
            require({r['path'] for r in entry['source_regions']} == source_names, f'Region/source mismatch: {ident}')
            for region in entry['source_regions']:
                box = region_bounds(region)
                source_boxes[local(region['path'])] = box
        scope = entry.get('sha256_scope')
        require(scope in (None, 'ordered_original_sha256_bytes'), f'Unknown hash scope: {ident}')
        if scope == 'ordered_original_sha256_bytes':
            require([s['order'] for s in sources] == list(range(1, len(sources) + 1)), f'Invalid source order: {ident}')
            combined = hashlib.sha256(b''.join(bytes.fromhex(s['sha256']) for s in sources)).hexdigest()
            require(combined == entry['sha256'], f'Bundle hash mismatch: {ident}')
        else:
            require(len(sources) == 1 and sources[0]['sha256'] == entry['sha256'], f'Original hash mismatch: {ident}')
        representations = entry.get('additional_representations', [])
        accepted_paths = {local(source['path']): source['sha256'] for source in sources + representations}
        raw_links = {p for p in links if p.is_relative_to(archive)}
        require(accepted_paths.keys() <= raw_links <= accepted_paths.keys() | duplicates_by_material.get(ident, set()),
                f'Card archive links differ from registered sources: {ident}')
        for source in sources + representations:
            path = local(source['path'])
            require(staged_sources.get(path, path).is_file(), f'Missing source: {source["path"]}')
            require(path.is_relative_to(archive),
                    f'Accepted source outside archive: {source["path"]}')
            require(re.fullmatch(r'[0-9a-f]{64}', source['sha256']) is not None, f'Invalid SHA-256: {path.name}')
            require(path not in paths or paths[path] == source['sha256'], f'Conflicting source hash: {path.name}')
            paths[path] = source['sha256']
            previous_path = source_paths_by_hash.setdefault(source['sha256'], path)
            require(path == previous_path, f'Same source bytes registered at different paths: {ident}')
            box = source_boxes.get(path) if source in sources else None
            previous = regions.setdefault(path, [])
            require(all(box is not None and old is not None and not regions_overlap(box, old) for old in previous),
                    f'Repeated source or overlapping document regions: {ident}')
            previous.append(box)
        if entry.get('laboratory_data'):
            data_path = local(entry['laboratory_data'])
            require(data_path.is_file(), f'Missing laboratory data: {ident}')
            require(digest(data_path) == entry['laboratory_data_sha256'], f'Laboratory data hash mismatch: {ident}')
            data = json.loads(data_path.read_text(encoding='utf-8'))
            require(data['material_id'] == ident, f'Laboratory data ID mismatch: {ident}')
            data_source = local(data['source']['path'])
            require(data_source in accepted_paths, f'Unregistered laboratory source: {ident}')
            require(accepted_paths[data_source] == data['source']['sha256'], f'Laboratory source hash mismatch: {ident}')
            require(len(data['analytes']) == entry['result_count'], f'Laboratory result count mismatch: {ident}')
            keys = [row['event_key'] for row in data['analytes']]
            require(len(keys) == len(set(keys)), f'Repeated analyte group: {ident}')
            page_numbers = data['source']['pdf_pages']
            require(all(isinstance(n, int) and n > 0 for n in page_numbers), f'Invalid PDF page scope: {ident}')
            require(all(row['pdf_page'] in page_numbers for row in data['occurrences']), f'Occurrence outside PDF scope: {ident}')
    for receipt in manifest.get('duplicate_receipts', []):
        require(receipt['canonical_id'] in identifiers, 'Duplicate receipt has no canonical material')
        canonical = next(e for e in manifest['materials'] if e['id'] == receipt['canonical_id'])
        require(receipt['sha256'] == canonical['sha256'], 'Duplicate receipt hash differs from canonical')
        path = local(receipt['archived_copy'])
        require(path.is_file() and path.is_relative_to(local('raw')), 'Missing archived duplicate')
        require(path not in paths, 'Duplicate receipt must refer to a separate archived copy')
        paths[path] = receipt['sha256']
    if hashes:
        for path, expected in paths.items():
            require(digest(staged_sources.get(path, path)) == expected,
                    f'Source hash mismatch: {path.relative_to(ROOT)}')
    if inventory:
        for path in local('raw').rglob('*'):
            if path.is_file() and path.name != 'AGENTS.md':
                require(path.resolve() in paths,
                        f'Unregistered archived file: {path.relative_to(ROOT)}; check unfinished intake receipts')
        for path in materials_dir.glob('*/MAT-*.md'):
            if re.fullmatch(r'MAT-[0-9a-f]{12}(?:-[a-z][a-z0-9-]*)?\.md', path.name):
                resolved = path.resolve()
                require(resolved in cards or resolved in derived_paths,
                        f'Unregistered material card: {path.relative_to(ROOT)}; check unfinished intake receipts')
    for entry in manifest['materials']:
        for related in entry.get('related_materials', []):
            require(isinstance(related, dict) and isinstance(related.get('id'), str),
                    f'Invalid related_materials in {entry["id"]}: expected a material id in field "id"')
            require(related['id'] in identifiers, f'Missing related material: {related["id"]}')


def date(entry):
    return entry.get('event_date') or entry.get('catalog_date') or entry.get('export_date') or 'дата не указана'


def link(entry):
    return markdown_link(entry['title'], Path(entry['card']).relative_to('materials').as_posix())


def render_index(manifest, taxonomy):
    entries = sorted(manifest['materials'], key=lambda e: (date(e), e['title']))
    lines = [
        '# Индекс материалов', '',
        'Навигация по видам, областям и датам. Состояние приёма и ограничения полноты — в карточках; '
        'машинные поля — в [реестре](manifest.json). [Справочник кодов](classification.json) и '
        '[правила классификации](../documentation/specs/material-classification.md).', '',
        'Дата в навигации — дата события, а при её отсутствии — явно обозначенная в карточке дата каталога '
        '(например, выдачи или экспорта); это не подтверждение даты взятия анализа.', '',
    ]
    for heading, labels, field in [('По видам', taxonomy['material_types'], 'material_type'),
                                    ('По областям', taxonomy['domains'], 'domains')]:
        lines += ['## ' + heading, '']
        if field == 'domains':
            lines += ['Одна карточка может входить в несколько областей без копирования. '
                      'Основная и дополнительная области равноправны для поиска; отсутствие метки '
                      'не доказывает отсутствия сведений.', '']
        for code, label in labels.items():
            group = [e for e in entries if code in e['classification'][field]] if field == 'domains' else [
                e for e in entries if code == e['classification'][field]]
            if group:
                lines += ['### ' + label, '']
                lines += [f'- {link(e)} — {date(e)}.' for e in group]
                lines.append('')
    lines += ['## По датам', '', '| Дата | Материал | Вид | Области |', '|---|---|---|---|']
    for entry in entries:
        c = entry['classification']
        lines.append(f'| {date(entry)} | {link(entry)} | {taxonomy["material_types"][c["material_type"]]} | '
                     + ', '.join(taxonomy['domains'][d] for d in c['domains']) + ' |')
    lines += ['', '## Как читать', '',
              'Назначения и заключения автора отделены от выводов проекта. Для графических исследований '
              'текст не заменяет изображения. Неполные документы и частично прочитанная рукопись явно '
              'помечены в карточках. Повторные подачи и дополнительные сканы связаны с исходными '
              'материалами; новое медицинское событие автоматически не создаётся.', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['verify', 'index'])
    args = parser.parse_args()
    manifest, taxonomy = load()
    verify(manifest, taxonomy, hashes=args.command == 'verify', inventory=args.command == 'verify')
    index_path = local('materials/INDEX.md')
    if args.command == 'index':
        text = render_index(manifest, taxonomy)
        check_links(index_path, text=text)
        if index_path.read_text(encoding='utf-8') != text:
            index_path.write_text(text, encoding='utf-8')
        print('OK: index by material type, domain and date updated; sources and registry unchanged.')
    else:
        check_links(index_path)
        require(index_path.read_text(encoding='utf-8') == render_index(manifest, taxonomy), 'Index is stale; run index.')
        print('OK: material structure, classifications, source SHA-256, bundles, links and index verified.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, TypeError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
