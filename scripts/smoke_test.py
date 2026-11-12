"""Isolated synthetic intake and provenance checks. No network or source project.

Only a new folder under this project's temp is written. Production medical
files are not copied. Failed test folders are retained for inspection.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

def run(folder, script, *args, ok=True):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
    result = subprocess.run([sys.executable, '-B', str(folder / 'scripts' / script), *map(str, args)],
                            cwd=folder, env=env, capture_output=True, text=True, encoding='utf-8')
    if (result.returncode == 0) != ok:
        raise RuntimeError(f'{script} {args}: {result.stdout}\n{result.stderr}')
    return result.stdout if ok else result.stderr

def expect_error(action):
    try:
        action()
    except (ValueError, AssertionError):
        return
    raise AssertionError('Expected a rejected invalid event')

def check_medications(folder):
    import medications
    course = json.loads((ROOT / 'examples/course.json').read_text(encoding='utf-8'))
    events = [json.loads(x) for x in (ROOT / 'examples/course-events.jsonl').read_text(encoding='utf-8').splitlines()]
    save(folder / 'records/medications/COURSE-demo-only.json', course)
    shutil.copyfile(ROOT / 'examples/course-events.jsonl', folder / course['events_file'])
    run(folder, 'medications.py', 'verify')
    early = medications.state(course, events, '2000-04-04')
    late = medications.state(course, events, '2001-04-04')
    assert early['reported_total_units'] == late['reported_total_units'] == 3
    assert late['status'] == 'active' and late['projection']['confirmed'] is False
    corrected = dict(events[0], id='EVENT-demo-correction', reported_total_units=2, corrects_event_id=events[0]['id'])
    medications.check_events(course, events + [corrected])
    expect_error(lambda: medications.check_events(course, events + [dict(corrected, corrects_event_id=None)]))
    expect_error(lambda: medications.check_events(course, events + events))
    expect_error(lambda: medications.check_events(course, [dict(events[0], effective_date='2000-04-05')]))
    planned = dict(course, initial_reported_status='planned', planned_start_date='2000-04-05')
    plan_event = dict(id='EVENT-demo-plan', course_id=course['id'], type='status_change',
                      effective_date='2000-04-04', recorded_date='2000-04-04', status='planned', source=events[0]['source'])
    for day in ('2000-04-04', '2000-04-05', '2001-04-04'):
        state = medications.state(planned, [plan_event], day)
        assert state['status'] == 'planned' and state['reported_total_units'] is None
    missed = dict(plan_event, id='EVENT-demo-miss', type='missed', scheduled_units=1)
    assert medications.state(course, events + [missed], '2000-04-05')['projection'] is None
    stopped = dict(plan_event, id='EVENT-stop', status='stopped',
                   effective_date='2000-04-10', recorded_date='2000-04-10')
    delayed = dict(plan_event, id='EVENT-delayed-start', status='active',
                   effective_date='2000-04-01', recorded_date='2000-04-11')
    medications.check_events(course, [stopped, delayed])
    assert medications.state(course, [stopped, delayed], '2000-04-11')['status'] == 'stopped'
    assert medications.state(course, [stopped, delayed], '2000-04-09')['status'] == 'unknown'
    # Backdated cumulative observations replay by event date, corrections replace originals.
    recent = dict(events[0], id='EVENT-recent', effective_date='2000-04-06',
                  recorded_date='2000-04-06', last_intake_date='2000-04-06', reported_total_units=5)
    historical = dict(events[0], id='EVENT-history', recorded_date='2000-04-07')
    medications.check_events(course, [recent, historical])
    assert medications.state(course, [recent, historical], '2000-04-07')['reported_total_units'] == 5
    assert medications.state(course, events + [corrected], '2000-04-04')['reported_total_units'] == 2
    later_correction = dict(corrected, effective_date='2000-04-07', recorded_date='2000-04-07')
    medications.check_events(course, [events[0], recent, later_correction])
    assert medications.state(course, [events[0], recent, later_correction], '20000407')['reported_total_units'] == 5
    assert medications.state(course, [events[0], later_correction], '2000-04-06')['reported_total_units'] == 3
    expect_error(lambda: medications.check_events(course, [events[0], recent, later_correction], new_event=True))
    regimen = dict(plan_event, id='EVENT-dose', type='regimen_change', units_per_day=2,
                   effective_date='20000407', recorded_date='20000407')
    baseline = dict(course, current_regimen_start_date='2000-04-01', daily_dose_description='DEMO old dose',
                    regimen=dict(course['regimen'], time_of_day='DEMO old time'))
    view = medications.render([(baseline, events + [regimen])], '20000407')
    assert 'Текущая схема с **2000-04-07**' in view and '2 условная единица в день' in view
    assert 'DEMO old' not in view and 'DEMO old dose' in medications.render([(baseline, events + [regimen])], '2000-04-06')
    aggregate = dict(events[0], id='EVENT-aggregate', effective_date='2000-04-06',
                     recorded_date='2000-04-06', reported_total_units=4, last_intake_date=None,
                     source=dict(kind='user_message', quote='Принял всего 4 единицы.'))
    save(folder / 'temp/aggregate.json', aggregate)
    run(folder, 'medications.py', 'add-event', course['id'], 'temp/aggregate.json')
    run(folder, 'medications.py', 'verify')
    for as_of in ('2000-04-06', '2001-04-04'):
        row = json.loads(run(folder, 'medications.py', 'status', '--as-of', as_of))[0]
        assert row['reported_total_units'] == 4 and row['checkpoint']['last_intake_date'] is None
        assert row['projection'] is None
    run(folder, 'medications.py', 'render', '--as-of', '2000-04-06')
    assert 'Дата последнего приёма неизвестна' in (folder / 'records/medications/CURRENT.md').read_text(encoding='utf-8')
    report_file = folder / 'records/medications/REPORT-demo.json'
    product = course['product']
    report = dict(recorded_date='20000407', product_material_id=product['material_id'],
                  product_card=product['material_card'], source=events[0]['source'])
    save(report_file, report)
    assert len(json.loads(run(folder, 'medications.py', 'status', '--as-of', '2000-04-07'))) == 2
    lab = next(e for e in json.loads((folder / 'materials/manifest.json').read_text(encoding='utf-8'))['materials']
               if e['id'] != product['material_id'])
    save(report_file, dict(report, product_card=lab['card']))
    assert 'ID/card mismatch' in run(folder, 'medications.py', 'verify', ok=False)
    report_file.unlink()


def check_representations(folder):
    import intake
    manifest_path = folder / 'materials/manifest.json'
    manifest = intake.read(manifest_path)
    lab, product = manifest['materials']
    card = folder / lab['card']
    original_card = card.read_bytes()
    card.write_text(original_card.decode('utf-8').replace(lab['original'], product['original']), encoding='utf-8')
    assert 'archive links' in run(folder, 'materials.py', 'verify', ok=False)
    card.write_bytes(original_card)
    lab['id'] = 'MAT-' + '0' * 12
    save(manifest_path, manifest)
    assert 'ID does not match source hash' in run(folder, 'materials.py', 'verify', ok=False)
    lab['id'] = card.stem
    duplicate = folder / lab['original']
    duplicate = duplicate.with_name('demo-repeat.txt')
    shutil.copyfile(folder / lab['original'], duplicate)
    duplicate_rel = duplicate.relative_to(folder).as_posix()
    manifest['duplicate_receipts'].append(dict(canonical_id=lab['id'], sha256=lab['sha256'], archived_copy=duplicate_rel))
    transcript = card.with_name(card.stem + '-scan-v2.md')
    transcript.write_text('Прежнее вымышленное представление.\n', encoding='utf-8')
    card.write_bytes(original_card.replace(b'\n', b'\r\n') +
                     f'\r\n[Повтор](../../{duplicate_rel}). [Расшифровка]({transcript.name}).\r\n'.encode('utf-8'))

    data_path = card.with_name(card.stem + '-laboratory.json')
    row = dict(result='5,4', unit='µg/L', reference='1–6', flag='−')
    analyte = dict(name='DEMO', event_key='demo', variants=[row])
    save(data_path, dict(material_id=lab['id'], analytes=[analyte], occurrences=[],
                         source=dict(path=lab['original'], sha256=lab['sha256'], pdf_pages=[1])))
    lab.update(laboratory_data=data_path.relative_to(folder).as_posix(), laboratory_data_sha256=digest(data_path),
               result_count=1)
    save(manifest_path, manifest)
    submission = folder / 'temp/representations.json'
    scan = folder / 'incoming/DEMO scan (1) #.txt'
    scan.write_text('Synthetic representation', encoding='utf-8')
    literal = 'Проверяемая строка источника: ' + intake.PENDING
    rep = dict(material_id=lab['id'], date='2000-04-02', file=scan.name, sha256=digest(scan),
               metadata='DEMO', body='```text\n' + literal + '\n[текст источника](missing.md)\n```',
               equivalence_basis='Синтетический тест')
    config = dict(reviewed=True, registered_date='2000-04-04', representations=[rep])
    for changed in (dict(row, result='54'), dict(row, unit='g/L'), dict(row, flag='-')):
        rep['checks'] = [dict(changed, name='DEMO')]
        save(submission, config)
        assert 'Scan differs' in run(folder, 'intake.py', 'prepare', submission, 'temp/rep.json', ok=False)
    rep['checks'] = [dict(row, name='DEMO', result='5.4')]
    save(submission, config)
    run(folder, 'intake.py', 'prepare', submission, 'temp/rep.json')
    run(folder, 'intake.py', 'accept', 'temp/rep.json')
    assert literal in card.read_text(encoding='utf-8') and (folder / 'raw/2000-04-02' / scan.name).is_file()
    run(folder, 'materials.py', 'verify')

    # Re-create a v1 interrupted acceptance, including an older completed scan.
    scan = folder / 'incoming/DEMO legacy scan.txt'
    scan.write_text('Second synthetic representation', encoding='utf-8')
    rep.update(file=scan.name, sha256=digest(scan), body='DEMO')
    save(submission, config)
    run(folder, 'intake.py', 'prepare', submission, 'temp/legacy.json')
    receipt_file = folder / 'temp/legacy.json'
    receipt = intake.read(receipt_file)
    text = receipt['prepared_cards'][lab['card']].replace(intake.materials.STATUS_MARKER, '')
    pending = text.replace('\r\n', '\n').replace('\n', '\r\n')
    receipt['cards'] = {lab['card']: intake.text_hash(pending)}
    receipt['schema_version'] = 1
    for field in ('prepared_cards', 'cards_before', 'status_positions'):
        del receipt[field]
    save(receipt_file, receipt)
    prefix, suffix = text.replace('\r\n', '\n').rsplit(intake.PENDING, 1)
    card.write_bytes((prefix + intake.COMPLETED + suffix).encode('utf-8'))
    run(folder, 'intake.py', 'accept', receipt_file)
    run(folder, 'materials.py', 'verify')
    assert literal in card.read_text(encoding='utf-8')

    # An unknown event date still allows representation lookup by catalogue.
    scan = folder / 'incoming/DEMO product scan.txt'
    scan.write_text('Synthetic undated product scan', encoding='utf-8')
    rep.update(material_id=product['id'], file=scan.name, sha256=digest(scan), checks=[])
    save(submission, config)
    run(folder, 'intake.py', 'prepare', submission, 'temp/product-rep.json')
    run(folder, 'intake.py', 'accept', 'temp/product-rep.json')
    run(folder, 'materials.py', 'verify')


def check_empty_layers(folder):
    import check_project
    root = folder / 'temp/empty-check'
    current = 'records/medications/CURRENT.md'
    rules = 'raw/AGENTS.md'
    baseline = {current: 'Курсы и сообщения ещё не добавлены.\n', rules: '# DEMO rules\n'}
    for name, value in baseline.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding='utf-8')
    with patch.object(check_project, 'ROOT', root), patch.object(
            check_project, 'STARTER', {name: hashlib.sha256(value.encode('utf-8')).hexdigest()
                                       for name, value in baseline.items()}):
        assert check_project.empty_layer_errors() == []
        extra = root / 'raw/2000-04-02/AGENTS.md'
        extra.parent.mkdir()
        extra.write_text('Synthetic private data', encoding='utf-8')
        for name in baseline:
            with (root / name).open('a', encoding='utf-8') as target:
                target.write('Synthetic private data\n')
        errors = check_project.empty_layer_errors()
        assert len(errors) == 3 and all(any(name in error for error in errors)
                                       for name in [*baseline, extra.relative_to(root).as_posix()])


def check_shared_original(folder):
    source = folder / 'incoming/shared-demo.txt'
    source.write_text('Два синтетических фрагмента на одном носителе.\n', encoding='utf-8')
    example = json.loads((ROOT / 'examples/reviewed-intake.json').read_text(encoding='utf-8'))
    document = dict(example['documents'][0], files=[dict(name=source.name, sha256=digest(source))],
                    metadata='Вымышленный носитель', body='Синтетическая транскрипция')
    example['documents'] = [dict(document, scope_key=scope, title='DEMO ' + scope,
                                 source_regions=[dict(name=source.name, region=scope)])
                            for scope in ('top', 'bottom')]
    config = folder / 'temp/shared.json'
    receipt = folder / 'temp/shared-receipt.json'
    save(config, example)
    run(folder, 'intake.py', 'prepare', config, receipt)
    assert len(json.loads(receipt.read_text(encoding='utf-8'))['moves']) == 1
    run(folder, 'intake.py', 'accept', receipt)
    run(folder, 'materials.py', 'verify')
    assert not source.exists() and (folder / 'raw/2000-04-02' / source.name).is_file()


def check_formats(folder):
    from zipfile import ZipFile

    import openpyxl
    from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula
    from PIL import Image

    output = folder / 'temp'
    book = openpyxl.Workbook()
    sheet = book.active
    sheet['A1'] = 2
    sheet['B1'] = '=A1+3'
    sheet['C1'] = ArrayFormula('C1:C2', '=ROW(C1:C2)')
    sheet['D1'] = DataTableFormula('D1:D2')
    standard, unusual = output / 'standard.xlsx', output / 'unusual.xlsx'
    book.save(standard)
    book.close()
    with ZipFile(standard) as original, ZipFile(unusual, 'w') as modified:
        for name in original.namelist():
            content = original.read(name)
            if name in ('xl/_rels/workbook.xml.rels', '[Content_Types].xml'):
                content = content.replace(b'worksheets/sheet1.xml', b'worksheets/observations.xml')
            if name == 'xl/worksheets/sheet1.xml':
                name = 'xl/worksheets/observations.xml'
                content = content.replace(b'<f>A1+3</f><v></v>', b'<f>A1+3</f><v>5</v>')
            modified.writestr(name, content)
    before = digest(unusual)
    run(folder, 'xlsx_extract.py', unusual, output)
    sheet = json.loads((output / '01.json').read_text(encoding='utf-8'))
    assert sheet['source_xml'] == 'xl/worksheets/observations.xml' and digest(unusual) == before
    cells = {cell['address']: cell for cell in sheet['cells']}
    assert cells['A1']['raw_xml']['v'] == '2'
    assert cells['B1']['cached_value'] == cells['B1']['derived_value'] == 5
    assert cells['C1']['raw_xml']['formula_attributes'] == {'t': 'array', 'ref': 'C1:C2'}
    assert cells['C1']['value']['text'] == '=ROW(C1:C2)'
    assert cells['D1']['raw_xml']['formula_attributes']['t'] == 'dataTable'
    assert cells['C1']['derivation_status'] == cells['D1']['derivation_status'] == 'unresolved'

    archived = folder / 'raw/2000-04-02'
    source = output / 'demo-orientation.png'
    Image.new('RGBA', (2, 3), (20, 40, 80, 0)).save(source)
    wrong = archived / 'wrong.png'
    Image.new('RGBA', (2, 3), (100, 40, 80, 0)).save(wrong)
    reference = archived / 'right.png'
    options = dict(output_dir='temp', images=[dict(source='temp/' + source.name, source_sha256=digest(source),
                                                   archived_source=reference.relative_to(folder).as_posix(),
                                                   rotation_degrees=90, view_name='demo-rotated.png')])
    config = output / 'orientation.json'
    save(config, options)
    assert 'Archived image source missing' in run(folder, 'image_views.py', 'orient', config, ok=False)
    shutil.copyfile(wrong, reference)
    assert 'Archived image differs' in run(folder, 'image_views.py', 'orient', config, ok=False)
    assert not (output / 'demo-rotated.png').exists() and not (output / 'images.json').exists()
    shutil.copyfile(source, reference)
    run(folder, 'image_views.py', 'orient', config)
    item = json.loads((output / 'images.json').read_text(encoding='utf-8'))['images'][0]
    assert item['source_path'] == reference.relative_to(folder).as_posix() and item['source_sha256'] == digest(reference)
    with Image.open(output / 'demo-rotated.png') as view:
        assert view.size == (3, 2)


def main(formats=False):
    if sys.flags.optimize:
        raise RuntimeError('Do not run checks with -O')
    temp_root = (ROOT / 'temp').resolve()
    assert temp_root.is_relative_to(ROOT) and temp_root.is_dir()
    folder = Path(tempfile.mkdtemp(prefix='smoke-', dir=temp_root)).resolve()
    assert folder.parent == temp_root and folder.name.startswith('smoke-')
    original_manifest = digest(ROOT / 'materials/manifest.json')
    try:
        # Only rules, scripts, empty registry and synthetic examples; never user data.
        for path in ROOT.rglob('AGENTS.md'):
            if 'temp' in path.relative_to(ROOT).parts or any(x in path.relative_to(ROOT).parts for x in ('.git', '.venv', 'venv')):
                continue
            target = folder / path.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        (folder / 'temp').mkdir(exist_ok=True)
        shutil.copyfile(ROOT / 'temp/AGENTS.md', folder / 'temp/AGENTS.md')
        for path in (ROOT / 'scripts').glob('*.py'):
            shutil.copyfile(path, folder / 'scripts' / path.name)
        for path in (ROOT / 'documentation/specs').glob('*.md'):
            shutil.copyfile(path, folder / 'documentation/specs' / path.name)
        for name in ('classification.json', 'INDEX.md'):
            shutil.copyfile(ROOT / 'materials' / name, folder / 'materials' / name)
        save(folder / 'materials/manifest.json', dict(schema_version=1, materials=[], duplicate_receipts=[]))
        for layer in ('raw', 'materials'):
            target = folder / layer / '2000-04-02'
            target.mkdir()
            (target / 'AGENTS.md').write_text('Наследовать ../AGENTS.md. Только вымышленный тест.\n', encoding='utf-8')
        for path in folder.rglob('AGENTS.md'):
            path.read_text(encoding='utf-8')
        for name in ('demo-lab.txt', 'demo-product.txt'):
            shutil.copyfile(ROOT / 'examples' / name, folder / 'incoming' / name)
        config = folder / 'temp/reviewed.json'
        shutil.copyfile(ROOT / 'examples/reviewed-intake.json', config)
        run(folder, 'materials.py', 'index')
        run(folder, 'materials.py', 'verify')
        current = folder / 'records/medications/CURRENT.md'
        assert not current.exists()
        run(folder, 'medications.py', 'render', '--as-of', 'not-a-date', ok=False)
        assert not current.exists()
        run(folder, 'intake.py', 'prepare', config, 'temp/absent/receipt.json', ok=False)
        assert not list((folder / 'materials').rglob('MAT-*.md'))
        bad_config = json.loads(config.read_text(encoding='utf-8'))
        lab = bad_config['documents'][0]
        lab['event_date'] = '2000-04-01'
        save(folder / 'temp/wrong-date.json', bad_config)
        assert 'known sampling date' in run(folder, 'intake.py', 'prepare', 'temp/wrong-date.json', 'temp/date.json', ok=False)
        lab['event_date'] = '20000402'
        save(folder / 'temp/wrong-date.json', bad_config)
        assert 'YYYY-MM-DD' in run(folder, 'intake.py', 'prepare', 'temp/wrong-date.json', 'temp/date.json', ok=False)
        del lab['event_date']
        alias = folder / 'incoming/demo-lab-copy.txt'
        shutil.copyfile(folder / 'incoming/demo-lab.txt', alias)
        left = dict(lab, scope_key='left', source_regions=[dict(name='demo-lab.txt', region='top')])
        right = dict(lab, scope_key='right', files=[dict(lab['files'][0], name=alias.name)],
                     source_regions=[dict(name=alias.name, region='top')])
        save(folder / 'temp/duplicate.json', dict(bad_config, documents=[left, right]))
        assert 'different filenames' in run(folder, 'intake.py', 'prepare', 'temp/duplicate.json', 'temp/duplicate-receipt.json', ok=False)
        alias.unlink()
        bad_relation = {'material_id': 'MAT-unknown'}
        bad_config['documents'][0].setdefault('registry_extra', {})['related_materials'] = [bad_relation]
        save(folder / 'temp/invalid.json', bad_config)
        run(folder, 'intake.py', 'prepare', 'temp/invalid.json', 'temp/invalid-receipt.json', ok=False)
        assert not list((folder / 'materials').rglob('MAT-*.md'))
        taxonomy_rules = folder / 'documentation/specs/material-classification.md'
        rule_content = taxonomy_rules.read_bytes()
        taxonomy_rules.unlink()
        assert 'Missing link in materials/INDEX.md' in run(folder, 'intake.py', 'prepare', config, 'temp/receipt.json', ok=False)
        assert not list((folder / 'materials').rglob('MAT-*.md'))
        taxonomy_rules.write_bytes(rule_content)
        sys.path.insert(0, str(folder / 'scripts'))
        import intake
        real_atomic = intake.atomic_text

        def fail_preparation(path, text):
            if path.parent == folder / 'materials/2000-04-02' and path.name.endswith('c4a4dc23d2c8.md'):
                raise OSError('Synthetic prepared-card write failure')
            real_atomic(path, text)

        with patch.object(intake, 'atomic_text', fail_preparation):
            try:
                intake.prepare(config, folder / 'temp/receipt.json')
                raise AssertionError('Preparation failure was not injected')
            except OSError as error:
                assert 'Synthetic prepared-card' in str(error)
        assert len(list((folder / 'materials').rglob('MAT-*.md'))) == 1
        assert 'Unregistered material card' in run(folder, 'materials.py', 'verify', ok=False)
        assert (folder / 'incoming/demo-lab.txt').is_file()
        receipt = json.loads((folder / 'temp/receipt.json').read_text(encoding='utf-8'))
        source = folder / receipt['moves'][0]['source']
        dest = folder / receipt['moves'][0]['destination']
        original_bytes = source.read_bytes()
        taxonomy_rules.unlink()
        assert 'Missing link in materials/INDEX.md' in run(folder, 'intake.py', 'accept', 'temp/receipt.json', ok=False)
        assert source.is_file() and not dest.exists()
        taxonomy_rules.write_bytes(rule_content)
        receipt['entries'][0]['related_materials'] = [bad_relation]
        save(folder / 'temp/invalid-receipt.json', receipt)
        run(folder, 'intake.py', 'accept', 'temp/invalid-receipt.json', ok=False)
        assert source.read_bytes() == original_bytes and not dest.exists()
        assert all((folder / move['source']).is_file() for move in receipt['moves'])
        source.write_bytes(original_bytes + b'changed')
        run(folder, 'intake.py', 'accept', 'temp/receipt.json', ok=False)
        assert source.is_file() and not dest.exists()
        source.write_bytes(original_bytes)
        dest.write_bytes(b'collision must not be overwritten')
        run(folder, 'intake.py', 'accept', 'temp/receipt.json', ok=False)
        assert dest.read_bytes() == b'collision must not be overwritten' and source.is_file()
        dest.unlink()  # This exact synthetic collision file was created above.
        dest.write_bytes(original_bytes)  # Simulate interrupted copy before source deletion.
        real_save = intake.save
        def fail_registry(path, value):
            if path == folder / 'materials/manifest.json':
                raise OSError('Synthetic registry write failure')
            real_save(path, value)
        intake.save = fail_registry
        try:
            intake.accept(folder / 'temp/receipt.json')
            raise AssertionError('Registry failure was not injected')
        except OSError as error:
            assert 'Synthetic registry' in str(error)
        finally:
            intake.save = real_save
        assert json.loads((folder / 'materials/manifest.json').read_text(encoding='utf-8'))['materials'] == []
        assert all(intake.COMPLETED not in (folder / path).read_text(encoding='utf-8')
                   for path in receipt['cards'] if (folder / path).exists())
        run(folder, 'materials.py', 'verify', ok=False)  # Unregistered raw must be detected.
        # Publish registry, complete one card, then fail the next card write.
        real_atomic = intake.atomic_text
        def fail_card(path, text):
            if path.as_posix().endswith(list(receipt['cards'])[1]):
                raise OSError('Synthetic completed-card write failure')
            real_atomic(path, text)
        intake.atomic_text = fail_card
        try:
            intake.accept(folder / 'temp/receipt.json')
            raise AssertionError('Card failure was not injected')
        except OSError as error:
            assert 'Synthetic completed-card' in str(error)
        finally:
            intake.atomic_text = real_atomic
        assert len(json.loads((folder / 'materials/manifest.json').read_text(encoding='utf-8'))['materials']) == 2
        run(folder, 'materials.py', 'verify', ok=False)  # Published but unfinished cards.
        def fail_index(path, text):
            if path == folder / 'materials/INDEX.md':
                raise OSError('Synthetic index write failure')
            real_atomic(path, text)
        intake.atomic_text = fail_index
        try:
            intake.accept(folder / 'temp/receipt.json')
            raise AssertionError('Index failure was not injected')
        except OSError as error:
            assert 'Synthetic index' in str(error)
        finally:
            intake.atomic_text = real_atomic
        run(folder, 'materials.py', 'verify', ok=False)  # Index still describes the old registry.
        run(folder, 'intake.py', 'accept', 'temp/receipt.json')
        run(folder, 'intake.py', 'accept', 'temp/receipt.json')  # Idempotent retry: no duplicate entries.
        run(folder, 'materials.py', 'verify')
        assert not source.exists() and dest.read_bytes() == original_bytes
        dest.write_bytes(original_bytes + b'tampered')
        run(folder, 'materials.py', 'verify', ok=False)
        dest.write_bytes(original_bytes)
        # A known byte-identical document cannot silently become a new material.
        source.write_bytes(original_bytes)
        run(folder, 'intake.py', 'prepare', config, 'temp/repeat-receipt.json', ok=False)
        run(folder, 'materials.py', 'verify')
        sys.path.insert(0, str(folder / 'scripts'))
        import materials
        assert materials.ROOT == folder
        assert materials.regions_overlap(materials.region_bounds({'region': 'whole'}),
                                        materials.region_bounds({'region': 'top'}))
        assert not materials.regions_overlap(materials.region_bounds({'region': 'top'}),
                                            materials.region_bounds({'region': 'bottom'}))
        expect_error(lambda: materials.local('../outside'))
        check_representations(folder)
        check_shared_original(folder)
        check_medications(folder)
        check_empty_layers(folder)
        if formats:
            check_formats(folder)
        assert digest(ROOT / 'materials/manifest.json') == original_manifest
    except Exception:
        print('FAILED; synthetic test folder retained:', folder, file=sys.stderr)
        raise
    assert folder.resolve().parent == temp_root and folder.name.startswith('smoke-')
    shutil.rmtree(folder)
    print('PASS: isolated intake, recovery, provenance, medication and empty-layer checks.'
          + (' XLSX and image checks passed.' if formats else ''))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--formats', action='store_true', help='Also check XLSX and image tools with optional dependencies')
    main(parser.parse_args().formats)
