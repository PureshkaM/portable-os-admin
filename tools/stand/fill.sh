#!/bin/sh
# Ввод в поле мастера: клик, очистка, набор.
#
#   fill.sh X Y "текст"
#
# Очистка идёт End + BackSpace, а не Ctrl+A: в Tk Entry Ctrl+A — это «в начало
# строки», выделения он не делает, и набор после него дописывает текст к
# старому вместо замены. Так уже получилось «25» вместо «2» и
# «/root/gui/binding-run.container/root/gui/admin.container» вместо пути —
# оба раза молча, потому что поле выглядело заполненным.
set -eu
export DISPLAY=:99
x=$1; y=$2; text=$3
xdotool mousemove "$x" "$y" click 1
sleep 0.3
xdotool key End
xdotool key --repeat 80 --delay 6 BackSpace
sleep 0.2
[ -n "$text" ] && xdotool type --delay 45 "$text"
sleep 0.2
echo "поле ($x,$y) = $text"
