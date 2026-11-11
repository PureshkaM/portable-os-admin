"""Экспорт атласа через установленный draw.io Desktop, без дополнительных пакетов."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--png-dir', type=Path, help='Каталог PNG для визуальной проверки')
    parser.add_argument('--only', nargs='*', help='Номера страниц для повторного экспорта')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    folder = root / 'docs' / 'architecture'
    cli = shutil.which('drawio') or '/Applications/draw.io.app/Contents/MacOS/draw.io'
    if not Path(cli).is_file():
        raise SystemExit('Нужен установленный draw.io Desktop; XML-исходники уже доступны.')
    pages = json.loads((folder / 'manifest.json').read_text())
    if args.png_dir:
        args.png_dir.mkdir(parents=True, exist_ok=True)
    for i, page in enumerate(pages, 1):
        if args.only and page['slug'][:2] not in args.only:
            continue
        source = folder / 'diagrams' / (page['slug'] + '.drawio')
        svg = folder / 'svg' / (page['slug'] + '.drawio.svg')
        subprocess.run([cli, '--disable-gpu', '-x', '-f', 'svg', '-e', '-o', str(svg), str(source)],
                       check=True, capture_output=True, timeout=60)
        doc = ET.parse(svg).getroot()
        assert '<mxfile' in doc.get('content', ''), f'Нет редактируемого XML: {svg}'
        if args.png_dir:
            subprocess.run([cli, '--disable-gpu', '-x', '-f', 'png', '-o',
                            str(args.png_dir / (page['slug'] + '.png')), str(source)],
                           check=True, capture_output=True, timeout=60)
        print(f'{i}/{len(pages)} · {page["slug"]}', flush=True)
    atlas = ET.Element('mxfile', host='Electron')
    for page in pages:
        doc = ET.parse(folder / 'diagrams' / (page['slug'] + '.drawio')).getroot()
        diagram = doc.find('diagram')
        diagram.set('id', page['slug'])
        atlas.append(diagram)
    ET.ElementTree(atlas).write(folder / 'shleif-architecture.drawio', encoding='utf-8', xml_declaration=True)
    print('Экспорт завершён: SVG проверены парсером, встроенный XML присутствует.', flush=True)


if __name__ == '__main__':
    main()
