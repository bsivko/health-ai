"""Read-only XLSX intake: preserve cells, raw OOXML, formulas and cached values.

Usage: python scripts/xlsx_extract.py INPUT.xlsx OUTPUT_DIR
OUTPUT_DIR must exist with AGENTS.md. Produces workbook.json, numbered per-sheet
JSON/Markdown, and checks.json. Requires bundled openpyxl; never writes XLSX.
Only simple numeric arithmetic and same-sheet references are independently
evaluated for readable date chains. Unsupported formulas remain explicit.
"""
import argparse
import ast
from datetime import date, datetime, time, timedelta
import json
import operator
from pathlib import Path
import posixpath
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

import openpyxl
from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula
from openpyxl.styles.numbers import is_date_format
from openpyxl.utils.datetime import from_excel, to_excel

import materials


def serializable(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return {'seconds': value.total_seconds()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, ArrayFormula):
        return {'kind': 'array', 'text': value.text, 'attributes': dict(value)}
    if isinstance(value, DataTableFormula):
        return {'kind': 'data_table', 'attributes': dict(value)}
    return str(value)


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def arithmetic(formula, reference):
    tree = ast.parse(formula.lstrip('='), mode='eval')
    binary = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
              ast.Div: operator.truediv}

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.Name) and re.fullmatch('[A-Z]{1,3}[1-9][0-9]*', node.id):
            return reference(node.id)
        if isinstance(node, ast.BinOp) and type(node.op) in binary:
            return binary[type(node.op)](visit(node.left), visit(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            return visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        raise ValueError('Unsupported formula syntax')

    return visit(tree)


def part_path(owner, target):
    path = posixpath.normpath(target.lstrip('/') if target.startswith('/') else
                             posixpath.join(posixpath.dirname(owner), target))
    materials.require(path != '..' and not path.startswith('../'), 'Invalid OOXML part target')
    return path


def worksheet_parts(archive, ns):
    root_rels = ET.fromstring(archive.read('_rels/.rels'))
    workbook_rel = next(rel for rel in root_rels if rel.get('Type', '').endswith('/officeDocument'))
    workbook = part_path('', workbook_rel.attrib['Target'])
    folder, name = posixpath.split(workbook)
    rel_file = posixpath.join(folder, '_rels', name + '.rels')
    relationships = {r.attrib['Id']: r for r in ET.fromstring(archive.read(rel_file))}
    mapping = {}
    for node in ET.fromstring(archive.read(workbook)).findall('s:sheets/s:sheet', ns):
        relation = relationships[node.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']]
        if relation.attrib['Type'].endswith('/worksheet'):
            materials.require(relation.get('TargetMode') != 'External', 'External worksheet part')
            mapping[node.attrib['name']] = part_path(workbook, relation.attrib['Target'])
    return mapping


def source_cells(archive, name, ns):
    rows = ET.fromstring(archive.read(name)).findall('s:sheetData/s:row', ns)
    result = {}
    row_number = 0
    for row in rows:
        row_number = int(row.get('r', row_number + 1))
        column = 0
        for cell in row.findall('s:c', ns):
            address = cell.get('r')
            if address is None:
                column += 1
                address = openpyxl.utils.get_column_letter(column) + str(row_number)
            else:
                column = openpyxl.utils.cell.coordinate_from_string(address)[0]
                column = openpyxl.utils.column_index_from_string(column)
            formula = cell.find('s:f', ns)
            inline = cell.find('s:is', ns)
            result[address] = {
                'type': cell.get('t'), 'style': cell.get('s'),
                'v': cell.findtext('s:v', namespaces=ns),
                'formula': formula.text if formula is not None else None,
                'formula_attributes': dict(formula.attrib) if formula is not None else None,
                'inline_string_xml': ET.tostring(inline, encoding='unicode') if inline is not None else None,
            }
    return result


def extract(source, output):
    materials.require(output.is_dir() and (output / 'AGENTS.md').exists(), 'Read/create output AGENTS first')
    materials.require(not (output / 'workbook.json').exists(), 'Refusing to overwrite extraction')
    source_hash = materials.digest(source)
    wb = openpyxl.load_workbook(source, data_only=False, read_only=False)
    cached = openpyxl.load_workbook(source, data_only=True, read_only=False)
    ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(source) as archive:
        mapping = worksheet_parts(archive, ns)
        raw_sheets = {name: source_cells(archive, part, ns) for name, part in mapping.items()}
        package_parts = [{'path': i.filename, 'size': i.file_size, 'crc': i.CRC} for i in archive.infolist()]
    summary = dict(schema_version=1, source_sha256=source_hash,
                   source_received_name=source.name, epoch=wb.epoch.isoformat(),
                   created=serializable(wb.properties.created), modified=serializable(wb.properties.modified),
                   sheets=[], package_parts=package_parts,
                   formula_policy='Preserve source/cached values; independently evaluate only simple same-sheet arithmetic, never modify workbook')
    verification = []
    for index, sheet in enumerate(wb.worksheets, 1):
        raw = raw_sheets[sheet.title]
        populated = {address for address, record in raw.items() if
                     any(record[key] is not None for key in ('v', 'formula_attributes', 'inline_string_xml'))}
        cells = []
        resolved = {}
        active = set()

        def number(address):
            if address in resolved:
                return resolved[address]
            if address in active:
                raise ValueError('Cyclic formula')
            active.add(address)
            try:
                cell = sheet[address]
                value = cell.value
                if cell.data_type == 'f':
                    if not isinstance(value, str):
                        raise ValueError('Unsupported non-scalar formula')
                    value = arithmetic(value.replace('$', ''), number)
                elif isinstance(value, (datetime, date, time, timedelta)):
                    value = to_excel(value, wb.epoch)
                elif type(value) not in (int, float):
                    raise ValueError('Non-numeric or missing precedent')
                resolved[address] = value
                return value
            finally:
                active.remove(address)

        for row in sheet:
            for cell in row:
                if cell.value is None and cell.comment is None and cell.hyperlink is None and cell.coordinate not in populated:
                    continue
                record = dict(address=cell.coordinate, row=cell.row, column=cell.column,
                              value=serializable(cell.value), data_type=cell.data_type,
                              number_format=cell.number_format, raw_xml=raw.get(cell.coordinate),
                              cached_value=serializable(cached[sheet.title][cell.coordinate].value))
                if cell.comment:
                    record['comment'] = dict(text=cell.comment.text, author=cell.comment.author)
                if cell.hyperlink:
                    record['hyperlink'] = dict(target=cell.hyperlink.target, location=cell.hyperlink.location)
                if cell.data_type == 'f':
                    try:
                        result = number(cell.coordinate)
                        record['derived_value'] = serializable(from_excel(result, wb.epoch) if is_date_format(cell.number_format) else result)
                        record['derivation_status'] = 'supported_arithmetic_independent_of_excel'
                    except (ValueError, SyntaxError, TypeError, ZeroDivisionError, OverflowError) as error:
                        record['derivation_status'] = 'unresolved'
                        record['derivation_error'] = str(error)
                cells.append(record)
        filename = f'{index:02d}'
        data = dict(sheet=sheet.title, state=sheet.sheet_state, source_xml=mapping[sheet.title],
                    merged_ranges=[str(r) for r in sheet.merged_cells.ranges], cells=cells)
        save(output / (filename + '.json'), data)
        by_row = {}
        for cell in cells:
            by_row.setdefault(cell['row'], []).append(cell)
        lines = ['# ' + sheet.title, '', 'Ячейки сохранены с адресами. Формулы и их производные значения разделены; пустые календарные строки не являются измерениями.', '', '| Строка | Ячейки и значения |', '|---|---|']
        for row_number, values in by_row.items():
            # Calendar-only rows remain fully available in JSON; show useful data rows.
            if len(values) == 1 and values[0]['column'] == 1 and values[0]['data_type'] == 'f':
                continue
            parts = []
            for cell in values:
                value = cell.get('derived_value', cell['value'])
                display = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
                if cell['data_type'] == 'f':
                    display += ' [формула ' + str(cell['value']) + '; ' + cell['derivation_status'] + ']'
                parts.append(cell['address'] + ': ' + display.replace('|', '\\|').replace('\n', '<br>'))
            lines.append('| ' + str(row_number) + ' | ' + '<br>'.join(parts) + ' |')
        if not cells:
            lines.append('| — | Нет непустых ячеек; возможные графические объекты остаются в XLSX |')
        (output / (filename + '.md')).write_text('\n'.join(lines) + '\n', encoding='utf-8')
        missing = sorted(populated - {c['address'] for c in cells})
        materials.require(not missing, 'Source cells missing from extraction')
        info = dict(order=index, name=sheet.title, json=filename + '.json', markdown=filename + '.md',
                    populated_cells=len(cells), source_value_or_formula_cells=len(populated),
                    formulas=sum(c['data_type'] == 'f' for c in cells),
                    unresolved_formulas=[c['address'] for c in cells if c.get('derivation_status') == 'unresolved'])
        summary['sheets'].append(info)
        verification.append(dict(sheet=sheet.title, missing_source_cells=missing,
                                 json_sha256=materials.digest(output / (filename + '.json')),
                                 markdown_sha256=materials.digest(output / (filename + '.md'))))
    materials.require(materials.digest(source) == source_hash, 'Source changed during extraction')
    save(output / 'workbook.json', summary)
    save(output / 'checks.json', dict(source_sha256=source_hash, original_unchanged=True, sheets=verification))
    wb.close()
    cached.close()
    print('OK: all source cells, formulas, cached/raw values and sheet views extracted; source unchanged.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source')
    parser.add_argument('output')
    args = parser.parse_args()
    extract(materials.local(args.source), materials.local(args.output))
