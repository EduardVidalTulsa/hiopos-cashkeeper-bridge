#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal i Tulsà
#
# Saves a screenshot of the terminal every time its screen changes, to study the payment screens
# of a new HioPOS setup. usage: grava.py HOST VNCPASS_FILE OUTPUT_DIR [SECONDS]
import sys, time, os, warnings
from datetime import datetime
from PIL import ImageChops
from vncdotool import api

warnings.filterwarnings("ignore")
ip, fpass, outdir = sys.argv[1:4]
durada = float(sys.argv[4]) if len(sys.argv) > 4 else 3 * 3600
os.makedirs(outdir, exist_ok=True)

client = api.connect(f"{ip}::5900", password=open(fpass).read().strip(), timeout=10)
anterior = None
fi = time.time() + durada
try:
    while time.time() < fi:
        client.refreshScreen()
        img = client.screen.copy().convert("RGB")
        # ignore the top bar (clock, wifi) so it does not count as a change
        if anterior is None or ImageChops.difference(img.crop((0, 60, 1280, 670)), anterior.crop((0, 60, 1280, 670))).getbbox():
            img.save(os.path.join(outdir, datetime.now().strftime("%H%M%S_%f")[:-3] + ".png"))
            anterior = img
        time.sleep(0.5)
finally:
    client.disconnect()
