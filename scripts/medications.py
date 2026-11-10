"""Local ongoing medication/supplement records. No automatic intake confirmation.

verify; status --as-of YYYY-MM-DD; render --as-of YYYY-MM-DD;
add-event COURSE_ID EVENT_JSON (reviewed user report, append after validation).
Course definitions and immutable event history live in records/medications.
"""
import argparse
from datetime import date, timedelta
import json
from pathlib import Path
import sys

import materials

FOLDER = materials.local('records/medications')
KINDS = {'checkpoint', 'status_change', 'regimen_change', 'missed'}
STATES = {'planned', 'active', 'paused', 'stopped', 'completed'}


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def timeline(events):
    """Replay effective dates; a visible correction replaces its checkpoint."""
    superseded = {e['corrects_event_id'] for e in events
                  if e['type'] == 'checkpoint' and e.get('corrects_event_id')}
    return [event for _, event in sorted(
        ((i, e) for i, e in enumerate(events) if e['id'] not in superseded),
        key=lambda item: (item[1]['effective_date'], item[1]['recorded_date'], item[0]))]


def check_events(course, events):
    ids = {}
    for event in events:
        ident = event['id']
        materials.require(ident not in ids, 'Duplicate event ID')
        materials.require(event['course_id'] == course['id'], 'Wrong course ID')
        materials.require(event['type'] in KINDS, 'Unsupported event type')
        when, recorded = date.fromisoformat(event['effective_date']), date.fromisoformat(event['recorded_date'])
        materials.require(when <= recorded, 'Future expectation cannot be a confirmed event')
        materials.require(event['source']['kind'] == 'user_message' and bool(event['source']['quote'].strip()),
                          'Event requires a quoted user report')
        if event['type'] == 'checkpoint':
            value = event['reported_total_units']
            materials.require(isinstance(value, int) and not isinstance(value, bool) and value >= 0,
                              'Invalid cumulative unit count')
            materials.require(event['last_intake_date'] <= event['effective_date'], 'Future intake date')
            date.fromisoformat(event['last_intake_date'])
            correction = event.get('corrects_event_id')
            if correction:
                materials.require(correction in ids and ids[correction]['type'] == 'checkpoint',
                                  'Correction must reference an earlier checkpoint')
            if event.get('expected_next'):
                expected = event['expected_next']
                materials.require(date.fromisoformat(expected['date']) > when and expected['ordinal'] == value + 1,
                                  'Invalid next expected dose')
        elif event['type'] == 'status_change':
            materials.require(event['status'] in STATES, 'Invalid course status')
        elif event['type'] == 'missed':
            materials.require(event['scheduled_units'] > 0, 'Invalid missed amount')
        else:
            materials.require(event['units_per_day'] > 0, 'Invalid reported regimen')
        ids[ident] = event
    total = None
    for event in timeline(events):
        if event['type'] == 'checkpoint':
            value = event['reported_total_units']
            materials.require(total is None or value >= total or event.get('corrects_event_id'),
                              'Count decrease needs explicit correction')
            total = value


def load():
    manifest, _ = materials.load()
    known = {e['id']: e for e in manifest['materials']}
    courses = []
    for path in sorted(FOLDER.glob('COURSE-*.json')):
        course = read(path)
        materials.require(path.stem == course['id'], 'Course ID/path mismatch')
        materials.require(course['initial_reported_status'] in STATES, 'Invalid initial status')
        materials.require(course['regimen']['origin'] == 'user_report', 'Unsupported regimen origin')
        materials.require(course['regimen']['units_per_day'] > 0, 'Invalid course dose')
        for field in ('planned_duration_days', 'planned_total_units'):
            value = course[field]
            materials.require(value is None or (type(value) is int and value > 0), 'Invalid course target')
        source = course['product']['material_id']
        materials.require(source in known and known[source]['card'] == course['product']['material_card'],
                          'Product material reference mismatch')
        if course.get('history_file'):
            history_path = materials.local(course['history_file'])
            materials.require(history_path.parent == FOLDER and history_path.is_file(), 'Missing history file')
            history = read(history_path)
            for item in history['sources']:
                materials.require(item['material_id'] in known, 'Unknown history material')
                extracted = read(materials.local(item['extracted_cells']))
                cells = {c['address']: c for c in extracted['cells']}
                materials.require(materials.local(item['readable_sheet']).is_file(), 'Missing readable history source')
                for observation in history['observations']:
                    materials.require(cells[observation['source_cell']]['value'] == observation['value'],
                                      'Historical cell value changed')
                    address = observation['date_cell']
                    materials.require((cells[address]['value'] if address else None) == observation['recorded_row_date'],
                                      'Historical row date changed')
        events_path = materials.local(course['events_file'])
        materials.require(events_path.parent == FOLDER, 'Event history outside medication records')
        events = [json.loads(line) for line in events_path.read_text(encoding='utf-8').splitlines() if line.strip()]
        check_events(course, events)
        courses.append((course, events))
    return courses


def state(course, events, as_of):
    date.fromisoformat(as_of)
    visible = [e for e in events if e['effective_date'] <= as_of and e['recorded_date'] <= as_of]
    status, dose, checkpoint = 'unknown', course['regimen']['units_per_day'], None
    changed = False
    for event in timeline(visible):
        if status == 'unknown':
            status = course['initial_reported_status']
        if event['type'] == 'checkpoint':
            checkpoint = event
        elif event['type'] == 'status_change':
            status = event['status']
            changed = True
        else:
            if event['type'] == 'regimen_change':
                dose = event['units_per_day']
            changed = True
    out = dict(course_id=course['id'], as_of=as_of, status=status, units_per_day=dose,
               checkpoint=checkpoint, reported_total_units=None, remaining_units_at_checkpoint=None,
               projection=None)
    if checkpoint:
        count = checkpoint['reported_total_units']
        target = course['planned_total_units']
        out.update(reported_total_units=count,
                   remaining_units_at_checkpoint=max(0, target - count) if target is not None else None)
        # A calendar projection is never an intake event or a completion status.
        if status == 'active' and not changed and dose == 1 and target is not None and count > 0 and count <= target:
            last = date.fromisoformat(checkpoint['last_intake_date'])
            out['projection'] = dict(start_date=(last - timedelta(days=count - 1)).isoformat(),
                                     last_day=(last + timedelta(days=target - count)).isoformat(),
                                     basis='conditional_daily_one_unit_without_missed_days',
                                     confirmed=False)
    return out


def user_reports():
    manifest, _ = materials.load()
    known = {e['id'] for e in manifest['materials']}
    reports = []
    for path in sorted(FOLDER.glob('REPORT-*.json')):
        report = read(path)
        date.fromisoformat(report['recorded_date'])
        materials.require(report['source']['kind'] == 'user_message' and bool(report['source']['quote']),
                          'Report needs a quoted user message')
        key = 'prescription' if report.get('prescription_material_id') else 'product'
        materials.require(report[key + '_material_id'] in known, 'Unknown report source')
        materials.require(materials.local(report[key + '_card']).is_file(), 'Missing report source card')
        reports.append((path, report))
    return reports


def render(courses, as_of):
    lines = ['# Текущий учёт приёма', '', 'Снимок на **' + as_of + '**. Подтверждения и расчётные даты разделены. '
             'Количество принято́го меняется только по сообщению пользователя, не с ходом времени.', '']
    for course, events in courses:
        current = state(course, events, as_of)
        lines += ['## ' + course['product']['name'], '',
                  '- Курс: [' + course['id'] + '](' + course['id'] + '.json); '
                  '[история сообщений](' + Path(course['events_file']).name + ').',
                  '- [Упаковка и состав](../../' + course['product']['material_card'] + ').',
                  '- Статус по сообщениям: **' + current['status'] + '**; схема пользователя: **' +
                  str(current['units_per_day']) + ' ' + course['regimen']['unit'] + ' в день**.']
        if course['planned_duration_days'] is not None or course['planned_total_units'] is not None:
            lines += ['- Длительность: **' + str(course['planned_duration_days'] or 'не задана') +
                      ' дней**; цель: **' + str(course['planned_total_units'] or 'не задана') +
                      ' ед.**. Это пользовательский учёт, не назначение врача.']
        else:
            lines += ['- Приём без сообщённой даты окончания; срок и итоговое количество не заданы.']
        starts = [event for event in events if event['type'] == 'status_change'
                  and event['status'] == 'active' and event['effective_date'] <= as_of
                  and event['recorded_date'] <= as_of]
        starts.sort(key=lambda event: (event['effective_date'], event['recorded_date']))
        if course.get('planned_start_date'):
            if starts:
                lines += ['- Начало приёма подтверждено сообщением пользователя: **' +
                          starts[0]['effective_date'] + '**.']
            else:
                lines += ['- Запланированное начало: **' + course['planned_start_date'] + '**; фактическое начало требует отдельного подтверждения.']
        if course.get('planned_end_date'):
            lines += ['- Условное окончание при старте по плану и без перерывов: **' + course['planned_end_date'] + '**; календарь не подтверждает приём и завершение.']
        if course.get('prescription_card'):
            lines += ['- [Назначение врача](../../' + course['prescription_card'] + '); схема упаковки и схема приёма сопоставлены отдельно.']
        if course.get('current_regimen_start_date'):
            lines += ['- Текущая схема с **' + course['current_regimen_start_date'] +
                      '** по подтверждению пользователя; ежедневные приёмы за этот период не перечислялись.']
        for prior in course.get('reported_prior_regimens', []):
            lines += ['- Предшествующая схема: ' + str(prior['units_per_day']) + ' ' +
                      course['regimen']['unit'] + ' в день; ' + prior['date_description'] + '.']
        if course.get('no_prior_use_reported'):
            lines += ['- По сообщению пользователя до указанной даты приёма не было.']
        if course.get('reported_start_period'):
            lines += ['- Начало по сообщению пользователя: **' + course['reported_start_period'] + '**; точная дата неизвестна.']
        if course.get('daily_dose_description'):
            lines += ['- Доза по текущей схеме: ' + course['daily_dose_description'] + '.']
        timing = [course['regimen'].get('time_of_day'), course['regimen'].get('relation_to_meals')]
        if any(timing):
            lines += ['- Время по сообщению пользователя: ' + '; '.join(v for v in timing if v) + '.']
        if course.get('reported_history'):
            lines += ['- История со слов пользователя: ' + course['reported_history'] + '.']
        if course.get('prescription_timing_difference'):
            lines += ['- Различие источников: ' + course['prescription_timing_difference']]
        if course.get('history_file'):
            lines += ['- [Исторические записи и происхождение](' + Path(course['history_file']).name + ').']
        checkpoint = current['checkpoint']
        if checkpoint:
            lines += ['- Последнее подтверждение: **' + str(current['reported_total_units']) +
                      ' ед. принято**, состояние на **' + checkpoint['effective_date'] + '**.']
            if current['remaining_units_at_checkpoint'] is not None:
                lines += ['- По этому подтверждению остаётся **' + str(current['remaining_units_at_checkpoint']) +
                          ' ед.** до исходной цели. Это не подтверждённый остаток упаковки сегодня.']
            if checkpoint.get('expected_next'):
                nxt = checkpoint['expected_next']
                lines += ['- По последнему сообщению ожидался приём **№' + str(nxt['ordinal']) + ' ' +
                          nxt['date'] + '**; приём не подтверждён.']
        else:
            lines += ['- Суммарное количество принятого не сообщено; из числа прошедших дней не рассчитано.']
        if current['projection']:
            projection = current['projection']
            lines += ['- Расчёт при ежедневном приёме без пропусков: начало **' + projection['start_date'] +
                      '**, ' + str(course['planned_total_units']) + '-й/последний день по исходной цели **' + projection['last_day'] +
                      '**. Обе даты расчётные, не подтверждённая история.']
        lines += ['', 'Новые приёмы, пропуски и завершение автоматически не проставляются. '
                  'Сведения о пропусках и назначившем этот конкретный продукт враче не дополнены догадками.', '']
    reports = [(path, report) for path, report in user_reports() if report['recorded_date'] <= as_of]
    if reports:
        lines += ['## Дополнительные сообщения о назначениях и планах приёма', '']
        for path, report in reports:
            key = 'prescription' if report.get('prescription_material_id') else 'product'
            label = 'назначение' if key == 'prescription' else 'упаковка'
            lines += ['- **' + report['recorded_date'] + ', ' + report['medication'] + '**: ' +
                      report['summary'] + ' [Сообщение](' + path.name + '); [' + label + '](../../' +
                      report[key + '_card'] + ').']
        lines += ['']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('verify')
    for name in ['status', 'render']:
        p = commands.add_parser(name)
        p.add_argument('--as-of', required=True)
    p = commands.add_parser('add-event')
    p.add_argument('course_id')
    p.add_argument('event_json', type=Path)
    args = parser.parse_args()
    courses = load()
    if args.command == 'verify':
        user_reports()
        print('OK: course definitions, product sources and event histories verified.')
    elif args.command == 'status':
        print(json.dumps([state(c, e, args.as_of) for c, e in courses], ensure_ascii=False, indent=2))
    elif args.command == 'render':
        output = FOLDER / 'CURRENT.md'
        output.write_text(render(courses, args.as_of), encoding='utf-8')
        materials.check_links(output)
        print('OK: current view rebuilt from reported events; expected doses not confirmed.')
    else:
        course, events = next((c, e) for c, e in courses if c['id'] == args.course_id)
        event = read(materials.local(args.event_json))
        check_events(course, events + [event])
        path = materials.local(course['events_file'])
        # Recheck the full history immediately before append; never rewrite it.
        current = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
        materials.require(current == events, 'History changed before append')
        with path.open('ab') as target:
            if path.stat().st_size and not path.read_bytes().endswith(b'\n'):
                target.write(b'\n')
            target.write((json.dumps(event, ensure_ascii=False) + '\n').encode('utf-8'))
        print('OK: reviewed report appended; rebuild CURRENT with an explicit as-of date.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, StopIteration) as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        sys.exit(1)
