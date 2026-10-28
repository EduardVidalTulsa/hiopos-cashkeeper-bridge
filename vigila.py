#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal i Tulsà
#
# Watches the screen of a HioPOS scale/terminal (Android, over VNC with droidVNC-NG) and, when a
# cash payment is confirmed, sends the amount to a CashKeeper cash machine.
#
# HioPOS payment screen (1280x720): button bar "Cancel·lar / No imprimir / Imprimir", "TOTAL A
# PAGAR x,xx €" and "Mitjà de pagament". When Imprimir/No imprimir is pressed the screen dims
# ("Espereu si us plau") and then the shop assistants screen appears. Cancel·lar goes back to the
# sales screen (nothing is sent). The payment method is read at confirmation time: the payment
# screen always opens with cash selected.
#
# usage: vigila.py NAME HOST [--ckfast HOST[:PORT] | --sql MYCNF | --cmd "CMD {cents}"] [options]
#        without a sender it only logs (dry run)
import argparse, os, re, socket, subprocess, sys, tempfile, time, uuid, warnings, logging
from datetime import date
from PIL import ImageChops, ImageStat
from vncdotool import api

ap = argparse.ArgumentParser(description="Send HioPOS cash payments to a CashKeeper")
ap.add_argument("nom", metavar="NAME", help="name of this terminal (logs, state file)")
ap.add_argument("host", metavar="HOST", help="terminal running droidVNC-NG")
ap.add_argument("--vnc-port", type=int, default=5900)
ap.add_argument("--vncpass", default=".vncpass", help="file with the VNC password (default .vncpass)")
env = ap.add_mutually_exclusive_group()
env.add_argument("--ckfast", metavar="HOST[:PORT]",
                 help="send straight to the CKMonitor 'CKFast' connector (port 16501 by default)")
env.add_argument("--sql", metavar="MYCNF",
                 help="insert into a MariaDB/MySQL queue table instead (client options file)")
env.add_argument("--cmd", help="run a command instead; {cents} and {text} are replaced")
ap.add_argument("--sql-db", default="cashkeeper", help="database for --sql (default cashkeeper)")
ap.add_argument("--sql-table", default="cashkeeper", help="queue table for --sql: (uuid, total)")
ap.add_argument("--no-1cent", action="store_true",
                help="the cash machine has no 1 cent coins: amounts ending in 1, 6 or 9 cents go 1 cent down")
ap.add_argument("--state", default="estat",
                help="directory where the shop assistant's name is left for hiopos-virtual-printer")
ap.add_argument("--telegram", metavar="FILE", help="file with 'BOT_TOKEN CHAT_ID' for alerts")
args = ap.parse_args()

warnings.filterwarnings("ignore")
nom = args.nom
logging.basicConfig(level=logging.INFO, format=f"%(asctime)s {nom} %(message)s")
log = logging.info
logging.getLogger("vncdotool").setLevel(logging.WARNING)
logging.getLogger("twisted").setLevel(logging.WARNING)

# screen regions on HioPOS at 1280x720
BARRA = (780, 8, 1280, 48)        # top right button bar
TOTAL = (400, 90, 650, 145)       # "TOTAL A PAGAR" amount
MITJA = (30, 185, 290, 222)       # payment method
TIQUET = (700, 60, 1140, 660)     # ticket preview: ~238 normal, <200 dimmed (= confirmed)
VENEDOR = (805, 250, 1110, 277)   # "Venedor: Name" in the ticket preview
EFECTIU = ("efectiu", "efectivo")                 # cash, Catalan and Spanish UI
VENDA = ("tiquet", "ticket", "albar")             # sales screen buttons
TMP = os.path.join(tempfile.gettempdir(), f"vigila-{nom}.png")


def ocr(im, box, escala=3):
    c = im.crop(box).convert("L")
    c.resize((c.width * escala, c.height * escala)).save(TMP)
    return subprocess.run(["tesseract", TMP, "-", "--psm", "7"], capture_output=True, text=True).stdout.strip()


def telegram(text):
    if not args.telegram:
        return
    tok, chat = open(args.telegram).read().split()
    subprocess.run(["curl", "-s", "-m", "5", f"https://api.telegram.org/bot{tok}/sendMessage",
                    "-d", f"chat_id={chat}", "--data-urlencode", f"text={text}"], capture_output=True)


def centims(text):
    m = re.search(r"(\d+)[,.](\d{2})", text.replace(" ", ""))
    return int(m.group(1)) * 100 + int(m.group(2)) if m else None


def ckfast(total):
    # same message a POS integration sends to the CKFast connector of CKMonitor
    host, _, port = args.ckfast.partition(":")
    msg = f"C|m{date.today():%Y-%m-%d}:UUID:{uuid.uuid4()}|{total}"
    with socket.create_connection((host, int(port or 16501)), 3) as s:
        s.settimeout(3)
        s.sendall(msg.encode())
        resposta = s.recv(256).decode(errors="replace").strip()
    if not resposta.startswith("OK"):
        raise RuntimeError(f"CashKeeper answered {resposta!r}")
    return resposta


def envia(total, text):
    if args.no_1cent and total % 10 in (1, 6, 9):
        total -= 1
    try:
        if args.ckfast:
            log(f"SENT to the CashKeeper {total} cents ({text}): {ckfast(total)}")
        elif args.sql:
            subprocess.run(["mariadb", f"--defaults-extra-file={args.sql}", "-N", "-D", args.sql_db, "-e",
                            f"INSERT INTO `{args.sql_table}` (uuid,total) VALUES (UUID(),{total})"],
                           capture_output=True, text=True, check=True)
            log(f"QUEUED for the CashKeeper {total} cents ({text})")
        elif args.cmd:
            subprocess.run(args.cmd.format(cents=total, text=text), shell=True, check=True)
            log(f"command run for {total} cents ({text})")
        else:
            log(f"DRY RUN: would send {total} cents ({text})")
    except Exception as e:
        log(f"ERROR sending {total} cents: {e!r}")
        telegram(f"{nom}: could not send the cash payment {text} to the CashKeeper: {e}")


def apunta_venedor(im):
    # hiopos-virtual-printer uses it for the declined card banner
    v = ocr(im, VENEDOR)
    if v:
        os.makedirs(args.state, exist_ok=True)
        with open(os.path.join(args.state, f"{nom}.venedor"), "w") as f:
            f.write(v)


def classifica(im):
    barra = ocr(im, BARRA, 2).lower()
    if "imprimir" in barra:
        fosc = ImageStat.Stat(im.crop(TIQUET).convert("L")).mean[0] < 200
        return ("confirmat" if fosc else "cobrament"), ocr(im, TOTAL), ocr(im, MITJA)
    if any(p in barra for p in VENDA):
        return "venda", None, None
    if ImageStat.Stat(im.crop((990, 60, 1270, 110)).convert("L")).mean[0] > 200 and "apagar" in ocr(im, (80, 8, 600, 48), 2).lower():
        return "venedors", None, None
    return "altre", None, None


def bucle(client, st):
    # st keeps the pending payment across reconnections: if the connection drops in the middle
    # of a payment and comes back once it is confirmed, it is still sent
    anterior = None
    while True:
        pendent, enviat = st["pendent"], st["enviat"]
        client.refreshScreen()
        im = client.screen.copy().convert("RGB")
        if anterior is not None and not ImageChops.difference(im.crop((0, 60, 1280, 670)), anterior.crop((0, 60, 1280, 670))).getbbox():
            time.sleep(0.4)
            continue
        anterior = im
        estat, total, mitja = classifica(im)
        if estat == "cobrament":
            pendent, enviat = (total, mitja), False
            apunta_venedor(im)
        elif estat in ("confirmat", "venedors") and pendent and not enviat:
            text, mitja_ara = (total or pendent[0], mitja or pendent[1]) if estat == "confirmat" else pendent
            log(f"confirmed: {text} {mitja_ara} ({estat})")
            if any(p in (mitja_ara or "").lower() for p in EFECTIU):
                c = centims(text or "") or centims(pendent[0] or "")
                if c:
                    envia(c, text or pendent[0])
                else:
                    telegram(f"{nom}: cash payment confirmed but the amount could not be read ({text!r})")
            enviat = True
        elif estat == "venda":
            if pendent and not enviat:
                log(f"cancelled: {pendent}")
            pendent, enviat = None, False
        if estat == "venedors":
            pendent, enviat = None, False
        st["pendent"], st["enviat"] = pendent, enviat
        time.sleep(0.4)


mode = "ckfast" if args.ckfast else "sql" if args.sql else "cmd" if args.cmd else "dry run"
st = {"pendent": None, "enviat": False}   # (amount text, method) of the last payment screen seen
while True:
    try:
        # timeout: sometimes the terminal stops answering on an open connection and
        # refreshScreen() would wait forever; this way it raises and we reconnect
        client = api.connect(f"{args.host}::{args.vnc_port}", password=open(args.vncpass).read().strip(), timeout=10)
        log(f"connected ({mode})")
        bucle(client, st)
    except Exception as e:
        log(f"error: {e!r}, reconnecting in 2 s")
        try:
            client.disconnect()
        except Exception:
            pass
        time.sleep(2)
