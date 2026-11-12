"""Local document-image previews and lossless orientation copies (Pillow).

preview INPUT_DIR OUTPUT_DIR --prefix PREFIX writes temporary thumbnails plus
PREFIX-inputs.json. orient CONFIG_JSON writes full-size PNGs and a manifest;
config contains output_dir, manifest_name and images with source, source_sha256,
archived_source, rotation_degrees (0/90/180/270 counterclockwise), view_name.
The archived source must already exist under raw and match the input SHA-256.
Neither operation changes originals. Read applicable AGENTS before invoking.
inspect INPUT OUTPUT --rotation 180 --contrast 4 --autocontrast-cutoff 1
creates a temporary readability preview and a transformation receipt, not evidence
of restored characters. Enhanced images are restricted to the project temp folder.
"""
import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageOps, ImageEnhance

import materials


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def preview(input_dir, output_dir, prefix, pattern='*'):
    materials.require(Path(prefix).name == prefix and '/' not in prefix and '\\' not in prefix, 'Invalid prefix')
    materials.require('/' not in pattern and '\\' not in pattern, 'Preview pattern must match filenames only')
    items = []
    for path in sorted(input_dir.glob(pattern)):
        if path.suffix.lower() not in ('.jpeg', '.jpg', '.png') or not path.is_file():
            continue
        with Image.open(path) as original:
            im = original.copy()
            record = dict(name=path.name, sha256=materials.digest(path), dimensions=list(im.size))
            im.thumbnail((1300, 1800))
            target = output_dir / (prefix + '-' + path.name + '.png')
            materials.require(not target.exists(), 'Preview already exists')
            im.save(target)
            record['preview'] = target.relative_to(materials.ROOT).as_posix()
            items.append(record)
    target = output_dir / (prefix + '-inputs.json')
    materials.require(not target.exists(), 'Snapshot already exists')
    save(target, items)
    print('OK: temporary previews and original SHA-256 snapshot created.')


def orient(config):
    folder = materials.local(config['output_dir'])
    materials.require(folder.is_dir() and (folder / 'AGENTS.md').is_file(), 'Read/create output AGENTS first')
    manifest = (folder / config.get('manifest_name', 'images.json')).resolve()
    materials.require(manifest.parent == folder and not manifest.exists(), 'Invalid/existing image manifest')
    jobs = []
    targets = {manifest}
    archive = materials.local('raw')
    for item in config['images']:
        source = materials.local(item['source'])
        archived = materials.local(item['archived_source'])
        materials.require(materials.digest(source) == item['source_sha256'], 'Source changed')
        materials.require(archived.is_relative_to(archive) and archived.is_file(), 'Archived image source missing/outside raw')
        materials.require(materials.digest(archived) == item['source_sha256'], 'Archived image differs from input')
        name = item['view_name']
        materials.require(Path(name).name == name and name.lower().endswith('.png'), 'Invalid view filename')
        target = (folder / name).resolve()
        materials.require(target.parent == folder and target not in targets and not target.exists(), 'Invalid/existing view path')
        targets.add(target)
        rotation = item['rotation_degrees']
        materials.require(rotation in (0, 90, 180, 270), 'Unsupported rotation')
        jobs.append((source, archived, target, rotation, item['source_sha256']))
    records = []
    transpose = {90: Image.Transpose.ROTATE_90, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_270}
    for source, archived, target, rotation, expected_hash in jobs:
        with Image.open(source) as original:
            expected = original.copy() if rotation == 0 else original.transpose(transpose[rotation])
            with target.open('xb') as output:
                expected.save(output, format='PNG')
            with Image.open(target) as reopened:
                materials.require(reopened.mode == expected.mode and reopened.size == expected.size,
                                  'View mode/size mismatch')
                materials.require(reopened.tobytes() == expected.tobytes() and
                                  reopened.convert('RGBA').tobytes() == expected.convert('RGBA').tobytes(), 'Pixel mismatch')
        materials.require(materials.digest(source) == materials.digest(archived) == expected_hash, 'Image source changed during orientation')
        records.append(dict(source_path=archived.relative_to(materials.ROOT).as_posix(), source_sha256=expected_hash,
                            view_path=target.relative_to(materials.ROOT).as_posix(),
                            view_sha256=materials.digest(target), rotation_degrees=rotation,
                            resize=False, crop=False, pixel_check='identical_to_decoded_original_with_documented_rotation'))
    with manifest.open('x', encoding='utf-8') as output:
        json.dump(dict(schema_version=1, images=records), output, ensure_ascii=False, indent=2)
        output.write('\n')
    print('OK: full-size orientation copies verified pixel-for-pixel; source files unchanged.')


def inspect_image(source, target, rotation, contrast, cutoff):
    materials.require(target.is_relative_to(materials.local('temp')), 'Enhanced preview must be temporary')
    receipt = target.with_suffix(target.suffix + '.json')
    materials.require(target.suffix.lower() == '.png' and not target.exists() and not receipt.exists(),
                      'Expected unused PNG and receipt paths')
    materials.require(0 < contrast <= 10 and 0 <= cutoff < 50, 'Invalid contrast/cutoff')
    before = materials.digest(source)
    transpose = {90: Image.Transpose.ROTATE_90, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_270}
    with Image.open(source) as original:
        im = original.copy() if rotation == 0 else original.transpose(transpose[rotation])
        im = ImageEnhance.Contrast(ImageOps.autocontrast(im.convert('L'), cutoff=cutoff)).enhance(contrast)
        im.thumbnail((1500, 2100))
        im.save(target)
    materials.require(materials.digest(source) == before, 'Original changed during inspection')
    save(receipt, dict(source_path=source.relative_to(materials.ROOT).as_posix(), source_sha256=before,
                       preview_path=target.relative_to(materials.ROOT).as_posix(), preview_sha256=materials.digest(target),
                       rotation_degrees=rotation, mode='L', contrast_factor=contrast, autocontrast_cutoff=cutoff,
                       maximum_dimensions=[1500, 2100], purpose='readability_only_not_restoration'))
    print('OK: temporary enhanced preview and transformation receipt saved; original hash unchanged.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('preview')
    p.add_argument('input_dir', type=Path)
    p.add_argument('output_dir', type=Path)
    p.add_argument('--prefix', required=True)
    p.add_argument('--pattern', default='*')
    p = commands.add_parser('orient')
    p.add_argument('config', type=Path)
    p = commands.add_parser('inspect')
    p.add_argument('input', type=Path)
    p.add_argument('output', type=Path)
    p.add_argument('--rotation', type=int, choices=[0, 90, 180, 270], default=0)
    p.add_argument('--contrast', type=float, default=1)
    p.add_argument('--autocontrast-cutoff', type=float, default=0)
    args = parser.parse_args()
    if args.command == 'preview':
        preview(materials.local(args.input_dir), materials.local(args.output_dir), args.prefix, args.pattern)
    elif args.command == 'orient':
        orient(json.loads(materials.local(args.config).read_text(encoding='utf-8')))
    else:
        inspect_image(materials.local(args.input), materials.local(args.output), args.rotation,
                      args.contrast, args.autocontrast_cutoff)


if __name__ == '__main__':
    main()
