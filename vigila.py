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
# sales screen (nothing is sent). The payment screen opens with cash ("Efectiu") in the method field
# and the list of methods dropped down: the field is only read once the list is closed. If the
# terminal prints a ticket (hiopos-virtual-printer, --tickets), the ticket decides.
#
# usage: vigila.py NAME HOST [--ckfast HOST[:PORT] | --sql MYCNF | --cmd "CMD {cents}"] [options]
#        without a sender it only logs (dry run)
import argparse, glob, os, re, socket, subprocess, sys, tempfile, time, uuid, warnings, logging
from collections import Counter
from datetime import date
from PIL import Image, ImageChops, ImageOps, ImageStat
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
ap.add_argument("--tickets", metavar="DIR",
                help="tiquets/ directory of hiopos-virtual-printer: printed tickets and card receipts "
                     "of this terminal confirm the payment method and amount")
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
LLIURAT = (420, 190, 545, 217)    # inside of the "Lliurat" (tendered) field
ETIQUETA_TOTAL = (25, 100, 260, 140)  # "TOTAL A PAGAR" label
MENU = (60, 260, 380, 540)        # where the payment methods list drops down
FONS = (420, 260, 650, 540)       # plain background next to it, brightness reference
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
    m = re.search(r"(\d{1,5})[,.](\d{2})(?!\d)", (text or "").replace(" ", ""))
    return int(m.group(1)) * 100 + int(m.group(2)) if m else None


def tess(img, psm, whitelist=None):
    img.save(TMP)
    cmd = ["tesseract", TMP, "-", "--psm", str(psm)]
    if whitelist:
        cmd += ["-c", f"tessedit_char_whitelist={whitelist}"]
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def llegeix_import(im):
    """Amount in cents, or None if the readings disagree, and the detail for the log.
    Tesseract now and then mixes up 5/9 and 9/0, so "TOTAL A PAGAR" is read in 4 variants (scale
    3/4, grey/black and white, digits only), plus the "Lliurat" field and the "Total" of the ticket
    preview; the amount is only good if two independent sources agree."""
    c = im.crop(TOTAL).convert("L")
    vt = []
    for esc in (3, 4):
        g = c.resize((c.width * esc, c.height * esc), Image.LANCZOS)
        vt.append(centims(tess(g, 7, "0123456789,.€")))
        vt.append(centims(tess(g.point(lambda p: 255 if p > 150 else 0), 7, "0123456789,.€")))
    vt = [v for v in vt if v]
    p = im.crop(TIQUET).convert("L")
    p = ImageOps.autocontrast(p.resize((p.width * 2, p.height * 2), Image.LANCZOS))
    m = re.search(r"(?im)^\s*total\b[^\n]*?(\d{1,4}[,.]\d{2})\s*€?", tess(p, 6))
    vp = centims(m.group(1)) if m else None
    # "Lliurat" holds the total by default (if the tendered amount is typed it just won't match)
    l = im.crop(LLIURAT).convert("L")
    vl = []
    for esc in (3, 4):
        g = l.resize((l.width * esc, l.height * esc), Image.LANCZOS)
        vl.append(centims(tess(g, 7, "0123456789,.")))
    vl = [v for v in vl if v]
    fonts = [set(vt), set(vl), {vp} if vp else set()]
    vots = Counter(v for f in fonts for v in f)
    det = f"total={vt} lliurat={vl} preview={vp}"
    bons = [v for v, n in vots.items() if n >= 2]
    if len(bons) == 1:
        return bons[0], det
    # no second source: only if the 4 TOTAL variants agree and nothing contradicts them
    altres = set(vl) | ({vp} if vp else set())
    if len(vt) == 4 and len(set(vt)) == 1 and altres <= set(vt):
        return vt[0], det
    return None, det


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


def es_efectiu(text):
    return any(p in (text or "").lower() for p in EFECTIU)


def menu_obert(im):
    # the payment screen opens with the methods list dropped down (dark grey) and "Efectiu" in the
    # field until one is picked: while the list is open the field means nothing. Compared with the
    # background next to it so that it also works on the dimmed screen
    g = im.convert("L")
    return ImageStat.Stat(g.crop(MENU)).mean[0] < 0.7 * ImageStat.Stat(g.crop(FONS)).mean[0]


def llegeix_tiquet(f):
    """(total, cash, methods) in cents of a sales ticket saved by hiopos-virtual-printer, or None"""
    t = re.sub(r"\x1b(!.|[@a-zA-Z].?)", "", open(f, "rb").read().decode("cp858", "replace"))
    linies = [l.strip() for l in t.replace("\r", "").split("\n")]
    i = next((k for k, l in enumerate(linies) if re.search(r"\bTOTAL\b", l)), None)
    if i is None:
        return None
    total = centims(linies[i].split("TOTAL")[-1])
    mitjans, nom_m = [], None
    # after TOTAL: each payment method with its amount (same line or the next one)
    for l in linies[i + 1:]:
        if l.startswith(("Impostos", "Base")):
            break
        if re.search(r"[A-Za-z]{3}", l):
            nom_m = re.sub(r"\s*-?[\d.,]+\s*€?$", "", l)
        if nom_m and centims(l) is not None:
            mitjans.append((nom_m, centims(l)))
            nom_m = None
    if total is None or not mitjans:
        return None
    altres = sum(c for n, c in mitjans if not re.search("efectiu|efectivo|canvi|cambio", n, re.I))
    return total, (total - altres if any(es_efectiu(n) for n, _ in mitjans) else 0), mitjans


def papers(t0):
    """The sales ticket, and whether there is an accepted card terminal receipt, printed by this
    terminal since t0. No ticket when the customer does not want one: then the screen decides."""
    venda, rebut = None, False
    if not args.tickets:
        return venda, rebut
    for d in sorted({time.strftime("%Y%m%d", time.localtime(t0)), time.strftime("%Y%m%d")}):
        for f in sorted(glob.glob(os.path.join(args.tickets, d, f"*_{IP}.bin"))):
            if os.path.getmtime(f) < t0:
                continue
            r = llegeix_tiquet(f)
            if r:
                venda = r
            else:
                b = open(f, "rb").read()
                if re.search(rb"Ejemplar para|VENTA", b) and b"DENEGADA" not in b:
                    rebut = True
    return venda, rebut


def classifica(im):
    barra = ocr(im, BARRA, 2).lower()
    # the same bar shows up on other screens (viewing a past ticket): it is only a payment
    # screen if the "TOTAL A PAGAR" label is there
    if "imprimir" in barra and "total" in ocr(im, ETIQUETA_TOTAL, 2).lower():
        fosc = ImageStat.Stat(im.crop(TIQUET).convert("L")).mean[0] < 200
        return "confirmat" if fosc else "cobrament"
    if any(p in barra for p in VENDA):
        return "venda"
    if ImageStat.Stat(im.crop((990, 60, 1270, 110)).convert("L")).mean[0] > 200 and "apagar" in ocr(im, (80, 8, 600, 48), 2).lower():
        return "venedors"
    return "altre"


def nou_cobrament():
    return {"t0": time.time(), "import": None, "det": "", "text": None, "mitja": None, "fet": False,
            "barra": None, "k_imp": None, "k_mitja": None}


def actualitza(p, im, estat):
    # Only what changed is read again: the amount (8 OCR runs) once, and the method only with the
    # list closed. Reading everything on every frame was too slow to catch the moment between
    # picking the method and confirming, and card payments went out as the default cash.
    if estat == "cobrament":
        p["barra"] = im.crop(BARRA).tobytes()
        k = im.crop(TOTAL).tobytes() + im.crop(LLIURAT).tobytes()
        if k != p["k_imp"]:
            p["import"], p["det"] = llegeix_import(im)
            p["text"], p["k_imp"] = ocr(im, TOTAL), k
            apunta_venedor(im)
    if not menu_obert(im):
        k = im.crop(MITJA).tobytes()
        if k != p["k_mitja"]:
            p["mitja"], p["k_mitja"] = ocr(im, MITJA) or None, k
            log(f"method: {p['mitja']} ({estat})")


def decideix(p, estat):
    p["fet"] = True
    # With the method read on screen there is no waiting (no ticket if the customer doesn't want
    # one); without it, wait a little for the ticket or the card receipt. A ticket, if any, decides.
    venda, rebut = papers(p["t0"])
    fi = time.time() + (0 if p["mitja"] or not args.tickets else 6)
    while venda is None and not rebut and time.time() < fi:
        time.sleep(0.5)
        venda, rebut = papers(p["t0"])
    c, det = p["import"], p["det"]
    if venda:
        total, ef, mitjans = venda
        font = f"ticket {', '.join(f'{n} {v / 100:.2f}' for n, v in mitjans)}"
        if ef and c != ef:
            log(f"ticket amount ({ef}) differs from the screen ({c} {det}): using the ticket")
        efectiu, c = ef > 0, ef
    elif rebut:
        font, efectiu = "card terminal receipt", False
    elif p["mitja"]:
        font, efectiu = f"screen {p['mitja']}", es_efectiu(p["mitja"])
    else:
        font, efectiu = "nothing", None
    log(f"confirmed ({estat}): {p['text']} amount={c} {det} method from {font}")
    if efectiu is None:
        telegram(f"{nom}: payment of {p['text']} confirmed but the payment method could not be seen: if it was cash, take it by hand")
        log("NOT SENT: payment method not seen")
    elif efectiu:
        if c:
            envia(c, f"{c // 100},{c % 100:02d} €")
        else:
            telegram(f"{nom}: cash payment confirmed but the amount readings disagree ({det}): take it by hand")
            log(f"NOT SENT: amount readings disagree ({det})")


def bucle(client, st):
    # st["p"] keeps the payment in progress across reconnections: if the connection drops in the
    # middle of a payment and comes back once it is confirmed, it is still sent
    anterior = None
    while True:
        p = st["p"]
        client.refreshScreen()
        im = client.screen.copy().convert("RGB")
        if anterior is not None and not ImageChops.difference(im.crop((0, 0, 1280, 670)), anterior.crop((0, 0, 1280, 670))).getbbox():
            time.sleep(0.1 if p else 0.4)
            continue
        anterior = im
        # within a payment, an unchanged button bar means the same screen: skip the OCR of
        # classifica() so that no change is missed
        if p and not p["fet"] and p["barra"] == im.crop(BARRA).tobytes():
            estat = "cobrament"
        else:
            estat = classifica(im)
        if estat == "cobrament":
            if p is None or p["fet"]:
                p = st["p"] = nou_cobrament()
            actualitza(p, im, estat)
        elif estat == "confirmat" and p and not p["fet"]:
            actualitza(p, im, estat)
            decideix(p, estat)
        elif estat == "venedors" and p and not p["fet"]:
            decideix(p, estat)
        elif estat == "venda":
            if p and not p["fet"]:
                # Cancel·lar goes back to the sales screen, but sometimes so does a finished
                # payment: a ticket or card receipt means it was paid
                time.sleep(1.5)
                if any(papers(p["t0"])):
                    decideix(p, estat)
                else:
                    log(f"cancelled: {p['text']} {p['mitja']}")
            st["p"] = None
        if estat == "venedors":
            st["p"] = None
        time.sleep(0.1 if st["p"] else 0.4)


mode = "ckfast" if args.ckfast else "sql" if args.sql else "cmd" if args.cmd else "dry run"
IP = socket.gethostbyname(args.host)   # hiopos-virtual-printer names the jobs by the terminal IP
st = {"p": None}   # payment in progress
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
