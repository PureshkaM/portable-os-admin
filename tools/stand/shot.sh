#!/bin/sh
# Снимок всего экрана.
DISPLAY=:99 import -window root /root/gui/shots/$1.png
echo /root/gui/shots/$1.png
