"""Prepare and accept reviewed local material batches (no OCR or medical inference).

prepare REVIEWED_JSON RECEIPT_JSON: documents use date/title/type/subtype/domains/
primary/basis/metadata/body/notes and files [{name,sha256}]. Representations use
material_id/date/file/sha256/metadata/body/checks (name,result,unit,reference,flag).
Optional document registry_extra stores explicit completeness/uncertainty fields.
For separate documents in shared scans, scope_key (lowercase ASCII) and
source_regions [{name,region:top|bottom|whole}] or region:box with normalized
bounds [x0,y0,x1,y1] distinguish non-overlapping regions in the original image.
Shared originals in one batch must use the same archive date and are moved once.
Optional archive_date keeps a shared scan intact while date selects the card folder.
An individual files[].archive_date can place different shared carriers by their
own primary documents without assigning that date to every component.
For unknown event dates, event_date:null requires registry_extra.catalog_date_basis;
date then selects the archive/card catalogue only, never the sampling date.
All medical text must already be reviewed. Dated raw/materials AGENTS must exist.
accept RECEIPT_JSON: recheck cards, inputs and manifest, archive without overwrite,
verify hashes, update manifest and index. A receipt allows recovery after a partial
move; it is temporary and can be deleted after successful acceptance.
"""
import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import materials

ROOT = materials.ROOT
PENDING = materials.PENDING_INTAKE
COMPLETED = 'приём завершён; SHA-256 после переноса совпал, ссылки проверены.'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def serialized(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def atomic_text(path, text):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         dir=path.parent, prefix='.' + path.name + '-', delete=False) as target:
            temporary = Path(target.name)
            target.write(text)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def save(path, value):
    atomic_text(path, serialized(value))


def require(ok, message):
    materials.require(ok, message)


def name_key(value):
    return ' '.join(value.casefold().split())


def field_key(value, field):
    if not isinstance(value, str):
        return value
    value = value.strip()
    if field == 'result' and re.fullmatch(r'[+-]?[0-9]+,[0-9]+', value):
        return value.replace(',', '.')
    return value


def calendar_date(value):
    require(date.fromisoformat(value).isoformat() == value, 'Expected a date in YYYY-MM-DD form')


def text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def finish_card(text, positions):
    for position in reversed(positions):
        require(text[position:position + len(PENDING)] == PENDING, 'Missing generated intake status')
        text = text[:position] + COMPLETED + text[position + len(PENDING):]
    return text


def source(name, expected, event_date):
    if event_date != 'undated':
        calendar_date(event_date)
    require(Path(name).name == name and '/' not in name and '\\' not in name, 'Invalid input filename')
    require(re.fullmatch('[0-9a-f]{64}', expected) is not None, 'Invalid input SHA-256')
    src = materials.local('incoming/' + name)
    dst = materials.local('raw/' + event_date + '/' + name)
    if dst.exists():
        dst = dst.with_name(dst.stem + '__' + expected[:12] + dst.suffix)
    for area in ['raw', 'materials']:
        require(materials.local(area + '/' + event_date + '/AGENTS.md').is_file(), 'Read/create dated AGENTS first')
    require(src.is_file() and materials.digest(src) == expected, 'Input missing or changed: ' + name)
    require(not dst.exists(), 'Archive destination exists: ' + name)
    return dict(source=src.relative_to(ROOT).as_posix(), destination=dst.relative_to(ROOT).as_posix(), sha256=expected)


def check_moves(moves, recover=False):
    destinations = set()
    sources = set()
    hashes = set()
    for move in moves:
        src, dst = materials.local(move['source']), materials.local(move['destination'])
        require(src.parent == materials.local('incoming'), 'Source outside inbox')
        require(dst.parent.parent == materials.local('raw'), 'Destination outside dated archive')
        if dst.parent.name != 'undated':
            calendar_date(dst.parent.name)
        collision_name = src.stem + '__' + move['sha256'][:12] + src.suffix
        require(dst.name in (src.name, collision_name), 'Unexpected archive filename')
        require(dst not in destinations and src not in sources, 'Repeated move')
        require(move['sha256'] not in hashes, 'Same source bytes submitted under different filenames')
        destinations.add(dst)
        sources.add(src)
        hashes.add(move['sha256'])
        if recover and dst.is_file():
            require(materials.digest(dst) == move['sha256'], 'Moved source hash mismatch')
            if src.exists():
                require(materials.digest(src) == move['sha256'], 'Source changed during recovery')
        else:
            require(src.is_file() and materials.digest(src) == move['sha256'], 'Source missing/changed')
            require(not dst.exists(), 'Refusing to overwrite destination')


def prepare(config_path, receipt_path):
    config = read(config_path)
    require(config.get('reviewed') is True, 'Medical content must be reviewed first')
    require(not receipt_path.exists(), 'Receipt already exists')
    require(receipt_path.parent.is_dir(), 'Receipt directory does not exist')
    calendar_date(config['registered_date'])
    manifest_path = materials.local('materials/manifest.json')
    manifest, taxonomy = materials.load()
    before_hash = materials.digest(manifest_path)
    cards, entries, representations, moves = {}, [], [], []
    cards_before, statuses = {}, {}
    ids = {entry['id'] for entry in manifest['materials']}
    shared_sources = {}
    known = {s['sha256'] for e in manifest['materials'] for s in
             (e.get('originals') or [dict(sha256=e['sha256'])]) + e.get('additional_representations', [])}
    for doc in config.get('documents', []):
        if doc['date'] == 'undated':
            require(doc['type'] == 'reference_information' and doc.get('event_date', '') is None,
                    'Undated intake currently supports only explicitly undated reference information')
        else:
            calendar_date(doc['date'])
        event_date = doc.get('event_date', doc['date'])
        if event_date is not None:
            calendar_date(event_date)
            if doc['type'] == 'laboratory_result':
                require(doc['date'] == event_date, 'Laboratory card date must match the known sampling date')
        else:
            require(bool(doc.get('registry_extra', {}).get('catalog_date_basis')), 'Unknown event date requires explicit catalogue basis')
        originals = []
        scope_key = doc.get('scope_key')
        regions = doc.get('source_regions', [])
        if scope_key:
            require(re.fullmatch('[a-z][a-z0-9-]*', scope_key) is not None, 'Invalid document scope key')
            require(len(regions) == len(doc['files']), 'Every scoped source needs a region')
            require({r['name'] for r in regions} == {f['name'] for f in doc['files']}, 'Region/source mismatch')
            for r in regions:
                materials.region_bounds(r)
        else:
            require(not regions, 'Source regions require a scope key')
        for order, item in enumerate(doc['files'], 1):
            require(item['sha256'] not in known, 'Known original requires duplicate/representation handling')
            require(materials.local('materials/' + doc['date'] + '/AGENTS.md').is_file(), 'Read/create card date AGENTS first')
            archive = item.get('archive_date', doc.get('archive_date', doc['date']))
            require(archive != 'undated' or doc['date'] == 'undated', 'Undated carrier requires an undated reference card')
            if doc['type'] == 'laboratory_result' and event_date is not None and not scope_key:
                require(archive == event_date, 'Laboratory archive date must match sampling date for an unshared original')
            move = source(item['name'], item['sha256'], archive)
            region = next((materials.region_bounds(r) for r in regions if r['name'] == item['name']), None)
            previous = shared_sources.get(item['name'])
            if previous:
                require(move == previous['move'], 'Shared scan archive dates or hashes differ')
                require(scope_key and previous['scoped'] and not any(
                        materials.regions_overlap(region, old) for old in previous['regions']),
                        'Shared scan needs distinct explicit document regions')
                previous['regions'].add(region)
            else:
                moves.append(move)
                shared_sources[item['name']] = dict(move=move, scoped=bool(scope_key), regions={region})
            originals.append(dict(order=order, path=move['destination'], sha256=item['sha256'], received_filename=item['name']))
        require(bool(originals), 'Document has no originals')
        sha = originals[0]['sha256'] if len(originals) == 1 else hashlib.sha256(
            b''.join(bytes.fromhex(s['sha256']) for s in originals)).hexdigest()
        ident = 'MAT-' + sha[:12] + ('-' + scope_key if scope_key else '')
        require(ident not in ids, 'Material ID already exists')
        ids.add(ident)
        classification = dict(schema_version=1, material_type=doc['type'], subtype=doc['subtype'],
                              domains=doc['domains'], primary_domain=doc['primary'], basis=doc['basis'],
                              status='reviewed', reviewed_date=config['registered_date'])
        require(doc['type'] in taxonomy['material_types'], 'Unknown material type')
        require(bool(doc['domains']) and set(doc['domains']) <= set(taxonomy['domains']), 'Unknown/empty domains')
        require(doc['primary'] is None or doc['primary'] in doc['domains'], 'Invalid primary domain')
        card_path = 'materials/' + doc['date'] + '/' + ident + '.md'
        require(not materials.local(card_path).exists(), 'Card already exists')
        cards_before[card_path] = None
        entry = dict(id=ident, sha256=sha, card=card_path, title=doc['title'], event_date=event_date,
                     registered_date=config['registered_date'], status='completed', representation_version=2,
                     classification=classification, recognition_method='reviewed_source_transcription')
        if scope_key:
            entry['source_regions'] = [dict(path=original['path'], region=r['region'],
                                           **({'bounds': r['bounds']} if r['region'] == 'box' else {}))
                                       for original in originals
                                       for r in regions if r['name'] == original['received_filename']]
        if doc.get('archive_date'):
            entry['source_archive_date'] = doc['archive_date']
        if len(originals) == 1:
            entry['original'] = originals[0]['path']
        else:
            entry.update(originals=originals, sha256_scope='ordered_original_sha256_bytes',
                         grouping_basis=doc['grouping_basis'])
        if event_date != doc['date']:
            entry['catalog_date'] = None if doc['date'] == 'undated' else doc['date']
            if doc['date'] == 'undated':
                entry['catalog_location'] = 'undated'
        extras = doc.get('registry_extra', {})
        require(not set(extras) & set(entry), 'Extra metadata cannot overwrite identity or classification')
        entry.update(extras)
        text = '# ' + doc['title'] + '\n\n## Кратко и классификация\n\n'
        text += '- Идентификатор: `' + ident + '`; представление v2; регистрация ' + config['registered_date'] + '.\n'
        if doc['date'] == 'undated':
            text += '- Дата составления: **неизвестна**; каталог `undated`, дата медицинского события не назначена.\n'
        else:
            text += ('- Дата события: **' + event_date + '**.\n') if event_date else ('- Дата взятия / события: **неизвестна**. Дата каталога: **' + doc['date'] + '**; основание приведено ниже.\n')
        text += '- Вид: **' + taxonomy['material_types'][doc['type']] + '** (`' + doc['type'] + '`); подвид: `' + doc['subtype'] + '`.\n'
        text += '- Области: ' + ', '.join(taxonomy['domains'][d] for d in doc['domains']) + '.\n'
        text += '- Основная область: ' + (taxonomy['domains'][doc['primary']] if doc['primary'] else 'не выделена — смешанное содержание') + '.\n'
        text += '- Основание: ' + doc['basis'] + '\n- Метки проверены по содержимому; не устанавливают диагноз.\n- Статус: '
        statuses[card_path] = [len(text)]
        text += PENDING + materials.STATUS_MARKER + '\n\n'
        text += doc['metadata'] + '\n\n## Источники\n\n'
        for original in originals:
            link = materials.markdown_link(Path(original['path']).name, '../../' + original['path'])
            text += '- Источник ' + str(original['order']) + ': ' + link + '; SHA-256 `' + original['sha256'] + '`.\n'
        if scope_key:
            text += '\nОбласть этого документа: ' + '; '.join(r['name'] + ' — ' +
                    (('прямоугольник ' + str(r['bounds']) + ' в координатах исходного скана (0–1)')
                     if r['region'] == 'box' else dict(top='верхняя половина', bottom='нижняя половина', whole='весь файл')[r['region']]) for r in regions) + '. Полные сканы сохранены без разделения.\n'
        if len(originals) > 1:
            text += '\n' + doc['grouping_basis'] + ' Сумма комплекта `' + sha + '` — SHA-256 бинарных сумм файлов по порядку.\n'
        text += '\n' + doc['body'] + '\n\n## Проверка и ограничения\n\n' + doc['notes'] + '\n'
        cards[card_path] = text
        entries.append(entry)
    for rep in config.get('representations', []):
        entry = next(e for e in manifest['materials'] if e['id'] == rep['material_id'])
        require(rep['date'] == Path(entry['card']).parent.name, 'Representation date differs from canonical card catalogue')
        require(rep['sha256'] not in known, 'Exact duplicate needs duplicate-receipt handling')
        move = source(rep['file'], rep['sha256'], rep['date'])
        moves.append(move)
        if rep.get('checks'):
            data = read(materials.local(entry['laboratory_data']))
            for check in rep['checks']:
                groups = [g for g in data['analytes'] if name_key(g['name']) == name_key(check['name'])]
                require(len(groups) == 1, 'Unknown or ambiguous canonical analyte: ' + check['name'])
                require(any(all(field_key(v[k], k) == field_key(check[k], k) for k in ('result', 'unit', 'reference', 'flag'))
                            for v in groups[0]['variants']), 'Scan differs from canonical table: ' + check['name'])
        descriptor = dict(path=move['destination'], sha256=rep['sha256'], kind='printed_scan',
                          received_filename=rep['file'],
                          registered_date=config['registered_date'], status='completed',
                          equivalence_basis=rep['equivalence_basis'], row_checks=rep.get('checks', []))
        if rep.get('scope'):
            descriptor['scope'] = rep['scope']
        representations.append(dict(material_id=rep['material_id'], representation=descriptor))
        path = entry['card']
        if path not in cards:
            original = materials.local(path).read_bytes()
            cards[path] = original.decode('utf-8')
            cards_before[path] = hashlib.sha256(original).hexdigest()
            statuses[path] = []
        cards[path] += '\n## Дополнительное представление: ' + rep['file'] + '\n\n'
        cards[path] += materials.markdown_link('Скан', '../../' + move['destination']) + '; SHA-256 `' + rep['sha256'] + '`.\n\n'
        cards[path] += rep['metadata'] + '\n\n' + rep['body'] + '\n\n' + rep['equivalence_basis'] + '\n\nСтатус скана: '
        statuses[path].append(len(cards[path]))
        cards[path] += PENDING + materials.STATUS_MARKER + '\n'
    require(bool(moves), 'Empty batch')
    check_moves(moves)
    for text in cards.values():
        require('temp/' not in text and 'temp\\' not in text, 'Permanent card depends on temp')
    add_prepared_entries(manifest, entries, representations)
    verify_publication(manifest, taxonomy, cards, moves)
    receipt = dict(schema_version=2, manifest_before_sha256=before_hash, entries=entries,
                   representations=representations, moves=moves, cards_before=cards_before,
                   prepared_cards=cards, status_positions=statuses,
                   cards={path: text_hash(text) for path, text in cards.items()})
    require(materials.digest(manifest_path) == before_hash, 'Manifest changed during preparation')
    save(receipt_path, receipt)
    stage_cards(receipt, published=False, write=True)
    print('OK: reviewed batch prepared; hashes, scopes and links checked. Originals remain in incoming.')


def add_prepared_entries(manifest, entries, representations):
    manifest['materials'].extend(entries)
    for update in representations:
        entry = next(e for e in manifest['materials'] if e['id'] == update['material_id'])
        entry.setdefault('additional_representations', []).append(update['representation'])


def staged_sources(moves):
    sources = {}
    for move in moves:
        source = materials.local(move['source'])
        destination = materials.local(move['destination'])
        sources[destination] = source if source.is_file() else destination
    return sources


def verify_publication(manifest, taxonomy, cards, moves):
    staged = {materials.local(path): text for path, text in cards.items()}
    sources = staged_sources(moves)
    materials.verify(manifest, taxonomy, inventory=True, staged_cards=staged, staged_sources=sources)
    index = materials.render_index(manifest, taxonomy)
    materials.check_links(materials.local('materials/INDEX.md'), text=index, staged=staged.keys() | sources.keys())
    return index


def stage_cards(receipt, *, published, write=False):
    require(receipt['cards'].keys() == receipt['cards_before'].keys() ==
            receipt['prepared_cards'].keys() == receipt['status_positions'].keys(), 'Inconsistent receipt card inventory')
    for path, expected in receipt['cards'].items():
        card = materials.local(path)
        actual = materials.digest(card) if card.is_file() else None
        before = receipt['cards_before'][path]
        completed = receipt.get('completed_cards', {}).get(path)
        require(actual == expected or actual == before or (published and completed is not None and completed == actual),
                'Prepared card changed: ' + path)
        text = receipt['prepared_cards'][path]
        require(text_hash(text) == expected, 'Prepared receipt text changed: ' + path)
        if write and actual == before and actual != expected:
            atomic_text(card, text)


def legacy_card(receipt, path, actual, expected, published):
    text = materials.local(path).read_bytes().decode('utf-8')
    if published and actual == receipt.get('completed_cards', {}).get(path):
        return text
    header = any(entry['card'] == path for entry in receipt['entries'])
    material_id = Path(path).stem
    scans = sum(update['material_id'] == material_id for update in receipt['representations'])
    status = PENDING if actual == expected else COMPLETED
    # Legacy generated headers precede Sources; newly appended scan statuses
    # are the last scan statuses, after any earlier accepted representations.
    positions = []
    if header:
        positions += [m.end() - len(status) for m in re.finditer(
            r'^- Статус: ' + re.escape(status) + r'(?=\r?$)', text.split('## Источники', 1)[0], re.M)][:1]
    if scans:
        positions += [m.end() - len(status) for m in re.finditer(
            r'^Статус скана: ' + re.escape(status) + r'(?=\r?$)', text, re.M)][-scans:]
    require(len(positions) == int(header) + scans, 'Missing legacy intake status: ' + path)
    if actual != expected:
        restored = text
        for position in reversed(positions):
            restored = restored[:position] + PENDING + restored[position + len(COMPLETED):]
        # Older Windows preparation used CRLF and publication sometimes used LF.
        require(expected in {text_hash(restored), text_hash(restored.replace('\r\n', '\n').replace('\n', '\r\n'))},
                'Prepared card changed: ' + path)
        return text
    final = finish_card(text, positions)
    if published and text_hash(final.replace('\r\n', '\n')) == receipt['completed_cards'][path]:
        return final.replace('\r\n', '\n')
    return final


def accept(receipt_path):
    receipt = read(receipt_path)
    manifest_path = materials.local('materials/manifest.json')
    current_hash = materials.digest(manifest_path)
    published = current_hash == receipt.get('manifest_after_sha256')
    require(published or current_hash == receipt['manifest_before_sha256'], 'Manifest changed after preparation')
    if receipt['schema_version'] == 2:
        stage_cards(receipt, published=published)
        final_texts = {path: finish_card(text, receipt['status_positions'][path])
                       for path, text in receipt['prepared_cards'].items()}
    else:
        require(receipt['schema_version'] == 1, 'Unknown intake receipt version')
        final_texts = {path: legacy_card(receipt, path, materials.digest(materials.local(path)), expected, published)
                       for path, expected in receipt['cards'].items()}
    check_moves(receipt['moves'], recover=True)
    manifest, taxonomy = materials.load()
    require(materials.digest(manifest_path) == current_hash, 'Manifest changed during acceptance')
    if not published:
        add_prepared_entries(manifest, receipt['entries'], receipt['representations'])
    registered = {entry['id']: entry['card'] for entry in manifest['materials']}
    affected = {entry['card'] for entry in receipt['entries']} | {
        registered[update['material_id']] for update in receipt['representations']}
    require(affected == final_texts.keys(), 'Receipt includes unexpected or missing cards')
    destinations = {source['path'] for entry in receipt['entries'] for source in
                    (entry.get('originals') or [{'path': entry['original']}])}
    destinations.update(update['representation']['path'] for update in receipt['representations'])
    require(destinations == {move['destination'] for move in receipt['moves']}, 'Receipt archive paths differ from sources')
    if published:
        require(receipt['completed_cards'] == {path: text_hash(text) for path, text in final_texts.items()},
                'Receipt does not match the published card checksums')
    index_text = verify_publication(manifest, taxonomy, final_texts, receipt['moves'])
    for move in receipt['moves']:
        src, dst = materials.local(move['source']), materials.local(move['destination'])
        if src.exists():
            if not dst.exists():
                # Exclusive creation refuses overwrites on every supported OS.
                # Leave the source in incoming until the full copy is verified.
                with src.open('rb') as original, dst.open('xb') as archived:
                    for chunk in iter(lambda: original.read(1024 * 1024), b''):
                        archived.write(chunk)
                require(materials.digest(dst) == move['sha256'], 'Archive copy incomplete; source retained')
            require(materials.digest(src) == move['sha256'], 'Source changed before removal')
            require(materials.digest(dst) == move['sha256'], 'Archive changed before source removal')
            src.unlink()
        require(materials.digest(dst) == move['sha256'], 'Archived source hash mismatch')
    if not published:
        receipt['manifest_after_sha256'] = hashlib.sha256(serialized(manifest).encode('utf-8')).hexdigest()
        receipt['completed_cards'] = {path: text_hash(text) for path, text in final_texts.items()}
        save(receipt_path, receipt)
        require(materials.digest(manifest_path) == current_hash, 'Manifest changed during acceptance')
        save(manifest_path, manifest)
    # Only a published registry permits completed labels. Each write is replace-atomic.
    for path, text in final_texts.items():
        card = materials.local(path)
        if not card.is_file() or materials.digest(card) != receipt['completed_cards'][path]:
            atomic_text(card, text)
    index = materials.local('materials/INDEX.md')
    atomic_text(index, index_text)
    materials.verify(manifest, taxonomy, inventory=True)
    materials.check_links(index)
    print('OK: batch archived without overwrites; hashes, cards, manifest and index verified.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare_parser = commands.add_parser('prepare')
    prepare_parser.add_argument('reviewed_json', type=Path)
    prepare_parser.add_argument('receipt_json', type=Path)
    accept_parser = commands.add_parser('accept')
    accept_parser.add_argument('receipt_json', type=Path)
    args = parser.parse_args()
    receipt = materials.local(args.receipt_json)
    if args.command == 'prepare':
        prepare(materials.local(args.reviewed_json), receipt)
    else:
        accept(receipt)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, TypeError, StopIteration) as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        sys.exit(1)
