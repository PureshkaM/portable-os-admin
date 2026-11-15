"""Экспорт листов атласа во внешний каталог без дублирования файлов в проекте."""
import argparse
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output_dir', type=Path)
    parser.add_argument('--format', choices=('svg', 'png'), default='svg')
    parser.add_argument('--page', type=int, action='append', help='Номер листа, начиная с 1')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    atlas = root / 'docs/architecture/yakor-architecture.drawio'
    count = len(ET.parse(atlas).getroot().findall('diagram'))
    pages = args.page if args.page is not None else range(1, count + 1)
    if any(i < 1 or i > count for i in pages):
        parser.error(f'Номер листа должен быть от 1 до {count}')
    cli = shutil.which('drawio') or '/Applications/draw.io.app/Contents/MacOS/draw.io'
    if not Path(cli).is_file():
        raise SystemExit('Для экспорта нужен установленный draw.io Desktop')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for i in pages:
        output = args.output_dir / f'{i:02d}.drawio.{args.format}'
        command = [cli, '--disable-gpu', '-x', '-f', args.format, '-p', str(i), '-o', str(output)]
        if args.format == 'svg':
            command.append('-e')
        try:
            subprocess.run([*command, str(atlas)], check=True, capture_output=True, timeout=60)
        except subprocess.CalledProcessError as exc:
            raise SystemExit(exc.stderr.decode(errors='replace') or exc.stdout.decode(errors='replace')) from exc
        print(f'{i}/{count} · {output.name}', flush=True)


if __name__ == '__main__':
    main()
