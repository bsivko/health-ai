"""Isolated synthetic intake and provenance checks. No network or source project.

Only a new folder under this project's temp is written. Production medical
files are not copied. Failed test folders are retained for inspection.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

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
    return result.stdout

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
    run(folder, 'medications.py', 'render', '--as-of', '2000-04-04')

def main():
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
        run(folder, 'intake.py', 'prepare', config, 'temp/receipt.json')
        assert (folder / 'incoming/demo-lab.txt').is_file()
        receipt = json.loads((folder / 'temp/receipt.json').read_text(encoding='utf-8'))
        source = folder / receipt['moves'][0]['source']
        dest = folder / receipt['moves'][0]['destination']
        original_bytes = source.read_bytes()
        source.write_bytes(original_bytes + b'changed')
        run(folder, 'intake.py', 'accept', 'temp/receipt.json', ok=False)
        assert source.is_file() and not dest.exists()
        source.write_bytes(original_bytes)
        dest.write_bytes(b'collision must not be overwritten')
        run(folder, 'intake.py', 'accept', 'temp/receipt.json', ok=False)
        assert dest.read_bytes() == b'collision must not be overwritten' and source.is_file()
        dest.unlink()  # This exact synthetic collision file was created above.
        dest.write_bytes(original_bytes)  # Simulate interrupted copy before source deletion.
        sys.path.insert(0, str(folder / 'scripts'))
        import intake
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
                   for path in receipt['cards'])
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
        check_medications(folder)
        assert digest(ROOT / 'materials/manifest.json') == original_manifest
    except Exception:
        print('FAILED; synthetic test folder retained:', folder, file=sys.stderr)
        raise
    assert folder.resolve().parent == temp_root and folder.name.startswith('smoke-')
    shutil.rmtree(folder)
    print('PASS: isolated intake, collision/recovery, tamper detection, medication invariants. Working data untouched.')

if __name__ == '__main__':
    main()
