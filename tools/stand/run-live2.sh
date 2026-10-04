#!/bin/sh
# Запускает приложение из СИНХРОНИЗИРОВАННОГО кода (/root/poa-live2),
# а не из /root/poa и не из установленного пакета: в обоих лежит старая версия
# с тремя мастерами выпуска, и прогон проверил бы код, которого в репозитории
# больше нет.
size=${1:-1280x900}
pkill -f "Xvfb :99" 2>/dev/null
pkill -f portable_os_admin 2>/dev/null
sleep 1
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 ${size}x24 > /root/gui/xvfb.log 2>&1 &
sleep 2
cd /root/poa-live2
DISPLAY=:99 POA_ALLOW_NON_REMOVABLE=1 PYTHONPATH=/root/poa-live2/src setsid nohup \
  python3 -c "from portable_os_admin.gui.app import run; run()" > /root/gui/app.log 2>&1 < /dev/null &
sleep 6
DISPLAY=:99 xdotool search --name "Portable OS Admin" getwindowgeometry
