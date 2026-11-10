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


def normalize(value):
    return re.sub(r'[^а-яa-z0-9<>.]', '', value.lower().replace('t3', 'т3'))


def source(name, expected, event_date):
    if event_date != 'undated':
        date.fromisoformat(event_date)
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
    for move in moves:
        src, dst = materials.local(move['source']), materials.local(move['destination'])
        require(src.parent == materials.local('incoming'), 'Source outside inbox')
        require(dst.parent.parent == materials.local('raw'), 'Destination outside dated archive')
        if dst.parent.name != 'undated':
            date.fromisoformat(dst.parent.name)
        collision_name = src.stem + '__' + move['sha256'][:12] + src.suffix
        require(dst.name in (src.name, collision_name), 'Unexpected archive filename')
        require(dst not in destinations and src not in sources, 'Repeated move')
        destinations.add(dst)
        sources.add(src)
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
    manifest_path = materials.local('materials/manifest.json')
    manifest, taxonomy = materials.load()
    before_hash = materials.digest(manifest_path)
    cards, entries, representations, moves = {}, [], [], []
    ids = {entry['id'] for entry in manifest['materials']}
    shared_sources = {}
    known = {s['sha256'] for e in manifest['materials'] for s in
             (e.get('originals') or [dict(sha256=e['sha256'])]) + e.get('additional_representations', [])}
    for doc in config.get('documents', []):
        if doc['date'] == 'undated':
            require(doc['type'] == 'reference_information' and doc.get('event_date', '') is None,
                    'Undated intake currently supports only explicitly undated reference information')
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
        event_date = doc.get('event_date', doc['date'])
        if event_date is not None:
            date.fromisoformat(event_date)
        else:
            require(bool(doc.get('registry_extra', {}).get('catalog_date_basis')), 'Unknown event date requires explicit catalogue basis')
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
        if event_date is None:
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
        text += '- Основание: ' + doc['basis'] + '\n- Метки проверены по содержимому; не устанавливают диагноз.\n- Статус: ' + PENDING + '\n\n'
        text += doc['metadata'] + '\n\n## Источники\n\n'
        for original in originals:
            text += '- Источник ' + str(original['order']) + ': [' + Path(original['path']).name + '](../../' + original['path'] + '); SHA-256 `' + original['sha256'] + '`.\n'
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
        require(rep['date'] == entry['event_date'], 'Representation date differs from canonical event')
        require(rep['sha256'] not in known, 'Exact duplicate needs duplicate-receipt handling')
        move = source(rep['file'], rep['sha256'], rep['date'])
        moves.append(move)
        if rep.get('checks'):
            data = read(materials.local(entry['laboratory_data']))
            for check in rep['checks']:
                group = next(g for g in data['analytes'] if normalize(g['name']) == normalize(check['name']))
                require(any(all(normalize(v[k]) == normalize(check[k]) for k in ['result', 'unit', 'reference', 'flag'])
                            for v in group['variants']), 'Scan differs from canonical table: ' + check['name'])
        descriptor = dict(path=move['destination'], sha256=rep['sha256'], kind='printed_scan',
                          received_filename=rep['file'],
                          registered_date=config['registered_date'], status='completed',
                          equivalence_basis=rep['equivalence_basis'], row_checks=rep.get('checks', []))
        if rep.get('scope'):
            descriptor['scope'] = rep['scope']
        representations.append(dict(material_id=rep['material_id'], representation=descriptor))
        path = entry['card']
        if path not in cards:
            cards[path] = materials.local(path).read_text(encoding='utf-8')
        cards[path] += '\n## Дополнительное представление: ' + rep['file'] + '\n\n'
        cards[path] += '[Скан](../../' + move['destination'] + '); SHA-256 `' + rep['sha256'] + '`.\n\n'
        cards[path] += rep['metadata'] + '\n\n' + rep['body'] + '\n\n' + rep['equivalence_basis'] + '\n\nСтатус скана: ' + PENDING + '\n'
    require(bool(moves), 'Empty batch')
    check_moves(moves)
    future = {materials.local(move['destination']) for move in moves}
    future_cards = {materials.local(path) for path in cards}
    for path, text in cards.items():
        require('temp/' not in text and 'temp\\' not in text, 'Permanent card depends on temp')
        for target in re.findall(r'\]\(([^)]+)\)', text):
            if '://' in target or target.startswith('#'):
                continue
            resolved = materials.local(target.split('#')[0], materials.local(path).parent)
            require(resolved.exists() or resolved in future or resolved in future_cards, 'Missing staged link: ' + target)
    for path, text in cards.items():
        materials.local(path).write_text(text, encoding='utf-8')
    save(receipt_path, dict(schema_version=1, manifest_before_sha256=before_hash, entries=entries,
                            representations=representations, moves=moves,
                            cards={path: materials.digest(materials.local(path)) for path in cards}))
    print('OK: reviewed batch prepared; hashes, scopes and links checked. Originals remain in incoming.')


def accept(receipt_path):
    receipt = read(receipt_path)
    manifest_path = materials.local('materials/manifest.json')
    current_hash = materials.digest(manifest_path)
    published = current_hash == receipt.get('manifest_after_sha256')
    require(published or current_hash == receipt['manifest_before_sha256'], 'Manifest changed after preparation')
    completed_cards = receipt.get('completed_cards', {})
    final_texts = {}
    for path, expected in receipt['cards'].items():
        card = materials.local(path)
        actual = materials.digest(card)
        # Recover old receipts whose card status was changed before registry publication.
        legacy_final = (actual != expected and COMPLETED.encode() in card.read_bytes() and
                        hashlib.sha256(card.read_bytes().replace(COMPLETED.encode(), PENDING.encode())).hexdigest() == expected)
        require(actual == expected or (published and actual == completed_cards.get(path)) or legacy_final,
                'Prepared card changed: ' + path)
        text = card.read_text(encoding='utf-8')
        if legacy_final and not published:
            text = text.replace(COMPLETED, PENDING)
        final_texts[path] = text.replace(PENDING, COMPLETED)
    check_moves(receipt['moves'], recover=True)
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
    manifest, taxonomy = materials.load()
    if not published:
        manifest['materials'].extend(receipt['entries'])
        for update in receipt['representations']:
            entry = next(e for e in manifest['materials'] if e['id'] == update['material_id'])
            entry.setdefault('additional_representations', []).append(update['representation'])
    materials.verify(manifest, taxonomy)
    if not published:
        receipt['manifest_after_sha256'] = hashlib.sha256(serialized(manifest).encode('utf-8')).hexdigest()
        receipt['completed_cards'] = {path: hashlib.sha256(text.encode('utf-8')).hexdigest()
                                      for path, text in final_texts.items()}
        save(receipt_path, receipt)
        require(materials.digest(manifest_path) == current_hash, 'Manifest changed during acceptance')
        save(manifest_path, manifest)
    # Only a published registry permits completed labels. Each write is replace-atomic.
    for path, text in final_texts.items():
        if materials.digest(materials.local(path)) != receipt['completed_cards'][path]:
            atomic_text(materials.local(path), text)
    index = materials.local('materials/INDEX.md')
    atomic_text(index, materials.render_index(manifest, taxonomy))
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
