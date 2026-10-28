# hiopos-cashkeeper-bridge

A light bridge between **HioPOS** scales/terminals (Android) and a **CashKeeper** automatic cash
machine. It watches the POS screen over VNC and, when a **cash** payment is confirmed, reads the
amount and sends it to the CashKeeper, which then opens and takes the money.

We wrote it because the POS vendor had not delivered the CashKeeper integration after months. It
does not touch the POS at all: it only looks at the screen.

## How it works

1. [droidVNC-NG](https://github.com/bk138/droidVNC-NG) runs on each terminal (start on boot,
   accessibility permission). Over Ethernet, ADB is not an option: wireless debugging needs Wi-Fi
   and `adb tcpip` is lost on every reboot.
2. `vigila.py` keeps a VNC connection to the terminal, grabs the screen twice a second and runs
   OCR (tesseract) only when it changes, on a few fixed regions of the HioPOS 1280x720 layout:
   - payment screen: button bar `Cancel·lar / No imprimir / Imprimir`, `TOTAL A PAGAR` amount and
     payment method;
   - confirmation: when `Imprimir` or `No imprimir` is pressed the ticket preview dims (mean
     brightness below 200); the payment method is read at that moment, because the payment screen
     always opens with cash preselected;
   - cancellation: going back to the sales screen sends nothing.
3. A confirmed cash payment is sent:
   - `--ckfast HOST[:PORT]`: straight to the **CKFast** connector of CKMonitor (port 16501),
     message `C|mYYYY-MM-DD:UUID:<uuid>|<cents>`, answer `OK|Added`;
   - `--sql MYCNF`: inserted into a MariaDB/MySQL queue table `(uuid, total)` processed by
     something else;
   - `--cmd "program {cents}"`: any command;
   - with none of them it only logs (dry run).
4. If the VNC connection stalls (it happens), a 10 s timeout reconnects; a payment seen before
   the drop is still sent if it is found confirmed afterwards.

It also writes the shop assistant's name (`Venedor:` in the ticket preview) to
`--state DIR/<name>.venedor`, which
[hiopos-virtual-printer](https://github.com/EduardVidalTulsa/hiopos-virtual-printer) prints on the
declined card banner.

The screen regions and texts match the Catalan HioPOS UI at 1280x720 (Spanish words for cash and
the sales buttons are accepted too). Another resolution or language may need new coordinates in
`vigila.py`: use `grava.py` to record the screens of a few real sales and adjust.

## Install

Python 3.10+, `tesseract` with the English data (`tesseract-data-eng` / `tesseract-ocr-eng`;
without it OCR silently returns nothing) and `curl` if Telegram alerts are wanted.

```sh
python3 -m venv venv
venv/bin/pip install -r requirements.txt
printf '%s' 'your-vnc-password' > .vncpass; chmod 600 .vncpass
venv/bin/python vigila.py scale1 192.0.2.10                    # dry run first
venv/bin/python vigila.py scale1 192.0.2.10 --ckfast 192.0.2.99
```

Run the watcher close to the terminals (same LAN): every screen grab is a few MB of raw pixels.
`deploy/hiopos-cashkeeper-bridge@.service` is an example systemd template, one instance per
terminal.

Options:

```
--ckfast HOST[:PORT]   CKMonitor CKFast connector (default port 16501)
--sql MYCNF            queue in MariaDB instead (--sql-db, --sql-table)
--cmd "CMD {cents}"    run a command instead
--no-1cent             no 1 cent coins: amounts ending in 1, 6 or 9 cents go 1 cent down
--vncpass FILE         VNC password file (default .vncpass)
--state DIR            where the shop assistant's name is left (default estat/)
--telegram FILE        'BOT_TOKEN CHAT_ID', alert when a payment cannot be read or sent
```

`grava.py HOST VNCPASS_FILE OUTPUT_DIR [SECONDS]` saves a screenshot every time the screen changes.

## Notes

- If CKMonitor answers `OK|Added` but the CashKeeper shows a timeout (error 92, "Límite de tiempo
  excedido…") on the first payment after a few idle minutes, the link between CKMonitor and the
  cash machine is dropping when idle (power saving, Wi-Fi). It is not this program.
- Always read the amount from the screen; never assume it.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
