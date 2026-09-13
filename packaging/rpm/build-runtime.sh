#!/bin/sh
# Собирает встроенный рантайм для .rpm: свой Python с Tk и cryptography.
#
# Зачем. Пакет должен ставиться на любую машину семейства RHEL, ничего не
# требуя от репозиториев. Ровно этого не получалось: `python3-tkinter` на DVD
# РЕД ОС 8 нет вовсе (проверено, 3274 пакета), а интерфейс написан на Tk. При
# этом сам дистрибутив такие пакеты собирает и держит в сетевом репозитории.
#
# Что делает. Берёт готовые пакеты дистрибутива и раскладывает их содержимое в
# собственный префикс пакета. Ни компиляции, ни сторонних двоичных файлов: всё
# то же самое, что стоит на обычной РЕД ОС, просто лежит не в /usr, а рядом с
# программой. Компилировать CPython из исходников рассматривалось и отвергнуто:
# часы работы под эмуляцией, а переносимости не прибавляет -- он всё равно
# слинкуется с glibc того хоста, где собран.
#
# Ограничение, которое надо называть прямо. Рантайм собран из пакетов РЕД ОС 8
# и требует glibc не ниже той, с которой они собраны (2.36). На RHEL 9 (2.34) и
# RHEL 8 такой рантайм не запустится. «Любая система семейства» здесь означает
# «РЕД ОС 8 и системы не старше её»; для более старой базы этот же сценарий
# надо прогнать на её репозитории, изменив REPO.
#
# Проверено 13.09.2026 в стенде: дерево 84 МБ, интерпретатор 3.11.0,
# cryptography 41.0.7, `_tkinter` загружается (Tcl/Tk 8.6).
set -eu

REPO=${REPO:-https://repo1.red-soft.ru/redos/8.0/x86_64/os}
PREFIX=${1:-runtime}
WORK=${WORK:-$(mktemp -d)}

# Что кладём. Первые шесть -- собственно рантайм; остальное -- то, без чего
# `_tkinter` не загружается: клиентские библиотеки X и шрифтов со своими
# зависимостями. Список получен не рассуждением, а последовательными запусками:
# каждый шаг называл недостающую библиотеку, пока `import _tkinter` не прошёл.
PACKAGES="python3 python3-libs python3-tkinter tcl tk python3-cryptography
glibc libXft libX11 libXrender libXext libXi libxcb libXau libXdmcp
fontconfig freetype libpng expat libbrotli zlib libxml2 xz-libs bzip2-libs
graphite2"
# `libbrotli`, а не `brotli`: второй -- это утилиты командной строки, а
# `libbrotlidec.so.1`, которую тянет fontconfig, лежит в первом. Проверка в
# конце сценария поймала это сразу; без неё пакет уехал бы сломанным.

echo "рабочий каталог: $WORK"
mkdir -p "$WORK/pkgs" "$PREFIX"

# Адреса берутся из метаданных репозитория, а не составляются по шаблону: имя
# файла содержит версию и релиз, и угадывать их -- значит ломаться при каждом
# обновлении репозитория. Метаданные сжаты zstd (не gzip, как во многих
# репозиториях), поэтому распаковываются утилитой, а не средствами python.
python3 - "$REPO" "$WORK" <<'PY'
import re, sys, urllib.request

repo, work = sys.argv[1], sys.argv[2]
md = urllib.request.urlopen(repo + "/repodata/repomd.xml", timeout=60).read().decode()
href = re.search(r'<data type="primary">.*?<location href="([^"]+)"', md, re.S).group(1)
open(work + "/primary.bin", "wb").write(
    urllib.request.urlopen(repo + "/" + href, timeout=300).read()
)
PY

if command -v zstd >/dev/null 2>&1; then
    zstd -dcf "$WORK/primary.bin" > "$WORK/primary.xml"
elif command -v unzstd >/dev/null 2>&1; then
    unzstd -c "$WORK/primary.bin" > "$WORK/primary.xml"
else
    echo "нужен zstd: метаданные этого репозитория сжаты им" >&2
    exit 1
fi

for name in $PACKAGES; do
    file=$(grep -oE "<location href=\"${name}-[0-9][^\"]*\.x86_64\.rpm\"" "$WORK/primary.xml" \
        | sed 's/.*href="//; s/"//' | sort -V | tail -1)
    if [ -z "$file" ]; then
        echo "в репозитории нет пакета $name" >&2
        exit 1
    fi
    echo "  $file"
    python3 - "$REPO/$file" "$WORK/pkgs/$file" <<'PY'
import sys, urllib.request
urllib.request.urlretrieve(sys.argv[1], sys.argv[2])
PY
done

# Распаковка в префикс. `-u` -- перезаписывать: пакеты пересекаются файлами
# (например, каталогами), и без этого cpio останавливается на первом же.
ABS=$(cd "$PREFIX" && pwd)
for rpm in "$WORK"/pkgs/*.rpm; do
    (cd "$ABS" && rpm2cpio "$rpm" | cpio -idmu --quiet)
done

# Проверка, без которой сборка не имеет смысла: рантайм, который не
# запускается, отличается от отсутствующего только размером. Отказ здесь стоит
# минуты, а обнаруженный у администратора -- рабочего дня.
echo "проверяю рантайм"

# Чем запускать проверку. На сборочном хосте RHEL это сам интерпретатор; на
# машине другой архитектуры (в стенде проекта -- arm64) он идёт через qemu, и
# тому нужно указать корень, где лежит загрузчик x86_64: в системе его нет, он
# только что распакован в префикс. Без этого проверка отказывала бы на
# полностью исправном рантайме -- то есть ровно тогда, когда всё хорошо.
RUN=""
if ! "$ABS/usr/bin/python3.11" -V >/dev/null 2>&1; then
    if command -v qemu-x86_64 >/dev/null 2>&1; then
        RUN="qemu-x86_64 -L $ABS"
    else
        echo "рантайм собран для x86_64, а эта машина его не исполняет;" >&2
        echo "поставьте qemu-user-static или собирайте на хосте x86_64" >&2
        exit 1
    fi
fi

if ! PYTHONHOME="$ABS/usr" LD_LIBRARY_PATH="$ABS/usr/lib64" \
        $RUN "$ABS/usr/bin/python3.11" -c "
import sys, cryptography, _tkinter
print('  интерпретатор', sys.version.split()[0])
print('  cryptography', cryptography.__version__)
print('  Tcl/Tk', _tkinter.TCL_VERSION, _tkinter.TK_VERSION)
"; then
    echo "рантайм собран, но не работает -- пакет с ним отдавать нельзя" >&2
    exit 1
fi

du -sh "$ABS"
echo "рантайм готов: $ABS"
