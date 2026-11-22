#!/usr/bin/env python3
"""
LogiBatt — % de batterie des périphériques Logitech dans la zone de notification.

Parle HID++ 2.0 directement (récepteurs Unifying / Bolt / Lightspeed, USB filaire,
Bluetooth). Fonctionne en parallèle de G HUB / Logi Options+.

Features HID++ gérées :
  0x1004 UNIFIED_BATTERY   (souris récentes : MX, Lift, Pro X Superlight 2…)
  0x1000 BATTERY_STATUS    (anciens modèles)
  0x1001 BATTERY_VOLTAGE   (gamme G Lightspeed — % estimé depuis la tension)

Usage :
  pythonw logibatt.py          -> icône systray
  python  logibatt.py --once   -> scan unique, résultat dans la console (debug)
"""
from __future__ import annotations

import argparse
import logging
import threading
import time
from dataclasses import dataclass

import hid  # pip install hidapi

LOGI_VID = 0x046D
REPORT_SHORT, REPORT_LONG = 0x10, 0x11
LONG_LEN = 20
SW_ID = 0x0A                     # identifiant logiciel (1..15) pour filtrer nos réponses
TIMEOUT_S = 0.5

POLL_S = 60                      # intervalle de rafraîchissement
LOW_THRESHOLD = 15               # seuil de notification "batterie faible"

F_ROOT = 0x0000
F_DEVICE_NAME = 0x0005
F_BATTERY_STATUS = 0x1000
F_BATTERY_VOLTAGE = 0x1001
F_UNIFIED_BATTERY = 0x1004

DEVICE_TYPES = {0: "Clavier", 1: "Télécommande", 2: "Pavé num.", 3: "Souris",
                4: "Trackpad", 5: "Trackball", 6: "Présentateur", 8: "Casque"}

# Courbe tension (mV) -> % pour Li-ion (même table que Solaar)
VOLTAGE_CURVE = [(4186, 100), (4067, 90), (3989, 80), (3922, 70), (3859, 60),
                 (3811, 50), (3778, 40), (3751, 30), (3717, 20), (3671, 10),
                 (3646, 5), (3579, 2), (3500, 0)]

log = logging.getLogger("logibatt")


# --------------------------------------------------------------------------- HID++

class Hidpp:
    """Canal HID++ sur la collection 'long report' (0x11) d'une interface Logitech."""

    def __init__(self, path: bytes):
        self.dev = hid.device()
        self.dev.open_path(path)

    def close(self):
        try:
            self.dev.close()
        except Exception:
            pass

    def request(self, idx: int, feat: int, func: int, params=(), timeout=TIMEOUT_S):
        func_sw = ((func & 0x0F) << 4) | SW_ID
        msg = bytes([REPORT_LONG, idx, feat, func_sw, *params]).ljust(LONG_LEN, b"\0")
        self.dev.write(msg)

        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            data = self.dev.read(LONG_LEN, max(1, int(remaining * 1000)))
            if not data or len(data) < 5 or data[1] != idx:
                continue
            # Erreur HID++ 2.0 : [11, idx, FF, feat, func|sw, code]
            # Erreur HID++ 1.0 : [10, idx, 8F, feat, func|sw, code]
            if data[2] in (0xFF, 0x8F) and data[3] == feat and data[4] == func_sw:
                log.debug("erreur HID++ idx=%#x feat=%#x code=%#x", idx, feat, data[5])
                return None
            if data[0] == REPORT_LONG and data[2] == feat and data[3] == func_sw:
                return bytes(data[4:])
            # sinon : notification ou réponse destinée à une autre appli -> on ignore
        return None

    # ---- helpers protocole

    def ping(self, idx: int) -> bool:
        r = self.request(idx, 0x00, 1, (0, 0, 0x5A))
        return r is not None and r[0] >= 2 and r[2] == 0x5A

    def feature_index(self, idx: int, feature_id: int) -> int | None:
        r = self.request(idx, 0x00, 0, (feature_id >> 8, feature_id & 0xFF))
        return r[0] if r and r[0] else None


@dataclass
class Battery:
    percent: int | None
    charging: bool
    estimated: bool = False      # True si dérivé d'une tension ou d'un niveau grossier


@dataclass
class Device:
    name: str
    kind: str | None
    battery: Battery

    @property
    def is_mouse(self) -> bool:
        return self.kind in ("Souris", "Trackball", "Trackpad")

    def label(self) -> str:
        b = self.battery
        pct = "?" if b.percent is None else f"{'~' if b.estimated else ''}{b.percent}%"
        return f"{self.name} : {pct}{' ⚡' if b.charging else ''}"


def _voltage_to_percent(mv: int) -> int:
    if mv >= VOLTAGE_CURVE[0][0]:
        return 100
    for (v_hi, p_hi), (v_lo, p_lo) in zip(VOLTAGE_CURVE, VOLTAGE_CURVE[1:]):
        if v_lo <= mv < v_hi:
            return round(p_lo + (p_hi - p_lo) * (mv - v_lo) / (v_hi - v_lo))
    return 0


def read_battery(ch: Hidpp, idx: int) -> Battery | None:
    if fi := ch.feature_index(idx, F_UNIFIED_BATTERY):
        r = ch.request(idx, fi, 1)                     # get_status
        if r:
            pct, level, charge = r[0], r[1], r[2]
            charging = charge in (1, 2)
            if pct:
                return Battery(pct, charging)
            # Certains modèles ne donnent qu'un niveau grossier
            for bit, approx in ((8, 90), (4, 50), (2, 20), (1, 5)):
                if level & bit:
                    return Battery(approx, charging, estimated=True)
            return Battery(None, charging)

    if fi := ch.feature_index(idx, F_BATTERY_STATUS):
        r = ch.request(idx, fi, 0)
        if r:
            return Battery(r[0] or None, r[2] in (1, 2, 4))

    if fi := ch.feature_index(idx, F_BATTERY_VOLTAGE):
        r = ch.request(idx, fi, 0)
        if r:
            mv = (r[0] << 8) | r[1]
            return Battery(_voltage_to_percent(mv), bool(r[2] & 0x80), estimated=True)

    return None


def read_name(ch: Hidpp, idx: int) -> tuple[str, str | None]:
    fi = ch.feature_index(idx, F_DEVICE_NAME)
    if not fi:
        return "Périphérique Logitech", None
    r = ch.request(idx, fi, 0)
    length = r[0] if r else 0
    name = b""
    while len(name) < length:
        r = ch.request(idx, fi, 1, (len(name),))
        if not r:
            break
        name += r[: length - len(name)]
    t = ch.request(idx, fi, 2)
    kind = DEVICE_TYPES.get(t[0]) if t else None
    return name.decode("utf-8", "replace").strip("\0 ") or "Périphérique Logitech", kind


def hidpp_paths() -> list[bytes]:
    """Collections HID++ 'long' des interfaces Logitech."""
    paths, seen = [], set()
    for d in hid.enumerate(LOGI_VID, 0):
        up, u = d.get("usage_page", 0), d.get("usage", 0)
        ok = (up == 0xFF00 and u == 0x0002) \
            or (up == 0xFF43 and u == 0x0202) \
            or (up == 0 and d.get("interface_number") == 2)   # vieux hidapi Linux
        if ok and d["path"] not in seen:
            seen.add(d["path"])
            paths.append(d["path"])
    return paths


def scan() -> list[Device]:
    devices: list[Device] = []
    for path in hidpp_paths():
        try:
            ch = Hidpp(path)
        except (OSError, IOError) as e:
            log.debug("ouverture impossible %s : %s", path, e)
            continue
        try:
            # 0xFF = périphérique en direct (USB/BT) ; 1..6 = appairés sur récepteur
            for idx in (0xFF, 1, 2, 3, 4, 5, 6):
                if not ch.ping(idx):
                    continue
                batt = read_battery(ch, idx)
                if batt is None:
                    continue
                name, kind = read_name(ch, idx)
                log.debug("trouvé %s (%s) idx=%#x : %s", name, kind, idx, batt)
                if all(d.name != name for d in devices):
                    devices.append(Device(name, kind, batt))
                if idx == 0xFF:
                    break
        except Exception as e:                      # périph. débranché en cours, etc.
            log.debug("erreur sur %s : %s", path, e)
        finally:
            ch.close()
    return devices


# --------------------------------------------------------------------------- UI

def _font(size: int):
    from PIL import ImageFont
    for f in ("arialbd.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def make_icon(percent: int | None, charging: bool, stale: bool = False):
    from PIL import Image, ImageDraw
    S = 64
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    if percent is None or stale:
        bg = (110, 110, 110)
    elif percent <= LOW_THRESHOLD:
        bg = (215, 45, 45)
    elif percent <= 40:
        bg = (230, 140, 20)
    else:
        bg = (40, 160, 70)
    d.rounded_rectangle((0, 0, S - 1, S - 1), radius=12, fill=bg)

    text = "?" if percent is None else str(percent)
    font = _font(44 if len(text) <= 2 else 32)
    x0, y0, x1, y1 = d.textbbox((0, 0), text, font=font)
    d.text(((S - (x1 - x0)) / 2 - x0, (S - (y1 - y0)) / 2 - y0), text,
           font=font, fill="white")

    if charging:                                   # petit éclair en haut à droite
        d.polygon([(50, 2), (40, 18), (47, 18), (42, 30), (58, 12), (50, 12), (56, 2)],
                  fill=(255, 220, 0), outline=(60, 60, 60))
    return img


class App:
    def __init__(self):
        import pystray
        self.pystray = pystray
        self.devices: list[Device] = []
        self.last_primary: Device | None = None
        self.notified_low: set[str] = set()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.icon = pystray.Icon("logibatt", make_icon(None, False),
                                 "LogiBatt — recherche…",
                                 menu=pystray.Menu(self._menu_items))

    def _menu_items(self):
        MI, Menu = self.pystray.MenuItem, self.pystray.Menu
        items = [MI(d.label(), None, enabled=False) for d in self.devices]
        if not items:
            items = [MI("Aucun périphérique en ligne", None, enabled=False)]
        return [*items, Menu.SEPARATOR,
                MI("Rafraîchir", lambda: self.wake.set(), default=True),
                MI("Quitter", self.quit)]

    def _update_ui(self):
        primary = next((d for d in self.devices if d.is_mouse), None) \
            or (self.devices[0] if self.devices else None)
        if primary:
            self.last_primary = primary
            b = primary.battery
            self.icon.icon = make_icon(b.percent, b.charging)
            tip = " | ".join(d.label() for d in self.devices)
        elif self.last_primary:
            b = self.last_primary.battery
            self.icon.icon = make_icon(b.percent, False, stale=True)
            tip = f"{self.last_primary.name} : hors ligne (dernier : {b.percent}%)"
        else:
            self.icon.icon = make_icon(None, False)
            tip = "LogiBatt — aucun périphérique Logitech trouvé"
        self.icon.title = tip[:127]                # limite Windows
        self.icon.update_menu()

        for d in self.devices:                    # notification batterie faible
            p, key = d.battery.percent, d.name
            if p is not None and p <= LOW_THRESHOLD and not d.battery.charging:
                if key not in self.notified_low:
                    self.notified_low.add(key)
                    try:
                        self.icon.notify(f"{d.name} : {p}% restant", "Batterie faible")
                    except Exception:
                        pass
            else:
                self.notified_low.discard(key)

    def _worker(self):
        while not self.stop.is_set():
            try:
                self.devices = scan()
            except Exception:
                log.exception("scan en échec")
                self.devices = []
            self._update_ui()
            self.wake.wait(POLL_S)
            self.wake.clear()

    def _setup(self, icon):
        icon.visible = True
        threading.Thread(target=self._worker, daemon=True).start()

    def quit(self, icon=None, item=None):
        self.stop.set()
        self.wake.set()
        self.icon.stop()

    def run(self):
        self.icon.run(setup=self._setup)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="scan unique en console")
    ap.add_argument("--debug", action="store_true", help="logs détaillés")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING,
                        format="%(levelname)s %(message)s")

    if args.once:
        devs = scan()
        if not devs:
            print("Aucun périphérique Logitech HID++ avec batterie trouvé.")
        for d in devs:
            print(f"{d.kind or '?':<10} {d.label()}")
        return

    App().run()


if __name__ == "__main__":
    main()
