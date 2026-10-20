#!/usr/bin/env python3
"""
LED BLE Tester — prueba de encendido/apagado de luces LED RGB por Bluetooth LE.

Protocolos incluidos (los más comunes en controladores baratos y luces "permanentes"):
  govee       Servicio 00010203-...-1910, write ...2b11. Paquetes de 20 bytes,
              byte 0x33 = comando, último byte = XOR de los 19 anteriores.
  elk-bledom  Servicio FFF0, write FFF3. Tramas de 9 bytes 7E ... EF
              (apps: Lotus Lantern, duoCo Strip, Lotus Lamp X).
  triones     Servicio FFD5, write FFD9. CC 23 33 = on, CC 24 33 = off
              (apps: HappyLighting, Triones).
  magic-blue  Servicio FFE5, write FFE9. Mismos paquetes que triones
              (controladores viejos tipo "LEDBlue"/Magic Blue).
  dmrrba      Controlador DMRRBA-007 (app illumiHome; luces permanentes MuliVid).
              Nordic UART: write 6E400002. 5A 01 02 01 = on, 5A 01 02 00 = off,
              5A 07 01 RR GG BB = color.

Uso (CLI):
  python ble_led_tester.py scan              # busca dispositivos BLE
  python ble_led_tester.py inspect <MAC>     # lista servicios/características
  python ble_led_tester.py on  <MAC>         # auto-detecta protocolo y enciende
  python ble_led_tester.py off <MAC>
  python ble_led_tester.py on <MAC> --protocol elk-bledom --variant 1
  python ble_led_tester.py color <MAC> 255 0 0   # rojo (dmrrba / elk-bledom)
  python ble_led_tester.py raw <MAC> fff3 "7e 00 04 01 00 00 00 00 ef"
  python ble_led_tester.py gui               # interfaz gráfica sencilla

Importante: cierra la app del teléfono (o apaga el Bluetooth del teléfono).
La mayoría de estos controladores aceptan UNA sola conexión BLE a la vez.
"""
from __future__ import annotations

import argparse
import asyncio
import queue
import sys
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # pragma: no cover
    sys.exit("Falta la librería 'bleak'. Instálala con:  pip install bleak")


# --------------------------------------------------------------------------- #
# Protocolos
# --------------------------------------------------------------------------- #
def u16(short: str) -> str:
    """Convierte un UUID corto de 16 bits (p. ej. 'fff3') a UUID completo."""
    return f"0000{short.lower()}-0000-1000-8000-00805f9b34fb"


def expand_uuid(u: str) -> str:
    u = u.strip().lower()
    return u16(u) if len(u) == 4 else u


def parse_hex(s: str) -> bytes:
    s = s.lower().replace("0x", "").replace(",", " ").replace(":", " ")
    return bytes.fromhex("".join(s.split()))


def govee_packet(*payload: int) -> bytes:
    """Paquete Govee: 19 bytes de datos (rellenos con 0) + 1 byte XOR."""
    data = bytearray(19)
    data[: len(payload)] = bytes(payload)
    chk = 0
    for b in data:
        chk ^= b
    return bytes(data) + bytes([chk])


@dataclass
class Profile:
    name: str
    service: str
    write_char: str
    on: list[bytes]   # variantes de "encender" (la 0 es la principal)
    off: list[bytes]  # variantes de "apagar"
    keepalive: Optional[bytes] = None
    name_hints: tuple[str, ...] = field(default_factory=tuple)
    color: Optional[Callable[[int, int, int], bytes]] = None  # (r, g, b) -> paquete


PROFILES: list[Profile] = [
    Profile(
        name="govee",
        service="00010203-0405-0607-0809-0a0b0c0d1910",
        write_char="00010203-0405-0607-0809-0a0b0c0d2b11",
        on=[govee_packet(0x33, 0x01, 0x01)],
        off=[govee_packet(0x33, 0x01, 0x00)],
        keepalive=govee_packet(0xAA, 0x01),
        name_hints=("govee", "ihoment", "minger"),
    ),
    Profile(
        name="elk-bledom",
        service=u16("fff0"),
        write_char=u16("fff3"),
        on=[parse_hex("7e 00 04 01 00 00 00 00 ef"),
            parse_hex("7e 00 04 f0 00 01 ff 00 ef")],
        off=[parse_hex("7e 00 04 00 00 00 00 00 ef"),
             parse_hex("7e 00 04 00 00 00 ff 00 ef")],
        name_hints=("elk", "bledom", "melk", "ledble", "lotus", "duoco"),
        color=lambda r, g, b: bytes([0x7E, 0x00, 0x05, 0x03, r, g, b, 0x00, 0xEF]),
    ),
    Profile(
        name="triones",
        service=u16("ffd5"),
        write_char=u16("ffd9"),
        on=[parse_hex("cc 23 33")],
        off=[parse_hex("cc 24 33")],
        name_hints=("triones", "happylighting", "qhm", "dream~"),
    ),
    Profile(
        name="magic-blue",
        service=u16("ffe5"),
        write_char=u16("ffe9"),
        on=[parse_hex("cc 23 33")],
        off=[parse_hex("cc 24 33")],
        name_hints=("ledblue", "magic"),
    ),
    # Controlador DMRRBA-007 (app illumiHome, p. ej. luces permanentes MuliVid).
    # Usa el Nordic UART Service: write 6e400002, notify 6e400003.
    # Comandos sin marco de inicio/fin: 0x5A (=90), longitud/acción, ...
    Profile(
        name="dmrrba",
        service="6e400001-b5a3-f393-e0a9-e50e24dcca9e",
        write_char="6e400002-b5a3-f393-e0a9-e50e24dcca9e",
        on=[bytes([0x5A, 0x01, 0x02, 0x01])],
        off=[bytes([0x5A, 0x01, 0x02, 0x00])],
        color=lambda r, g, b: bytes([0x5A, 0x07, 0x01, r, g, b]),
        name_hints=("dmrrba",),
    ),
]


def find_profile(name: str) -> Profile:
    for p in PROFILES:
        if p.name == name:
            return p
    raise ValueError(f"Protocolo desconocido: {name}. Opciones: {[p.name for p in PROFILES]}")


def guess_from_adv(name: Optional[str], service_uuids: list[str]) -> Optional[Profile]:
    n = (name or "").lower()
    uuids = {u.lower() for u in service_uuids or []}
    for p in PROFILES:
        if p.service in uuids or any(h in n for h in p.name_hints):
            return p
    return None


def detect_profile(client: BleakClient) -> Optional[Profile]:
    """Detecta el protocolo buscando la característica de escritura conocida."""
    for p in PROFILES:
        if client.services.get_characteristic(p.write_char) is not None:
            return p
    return None


# --------------------------------------------------------------------------- #
# Operaciones BLE
# --------------------------------------------------------------------------- #
LogFn = Callable[[str], None]


async def write(client: BleakClient, char_uuid: str, data: bytes,
                log: Optional[LogFn] = print) -> None:
    ch = client.services.get_characteristic(expand_uuid(char_uuid))
    if ch is None:
        raise RuntimeError(f"La característica {char_uuid} no existe en este dispositivo")
    # Si soporta write-without-response lo usamos (es lo que hacen las apps).
    with_response = "write-without-response" not in ch.properties
    await client.write_gatt_char(ch, data, response=with_response)
    if log:
        log(f"TX {ch.uuid[:8]} -> {data.hex(' ')}")


async def subscribe_all(client: BleakClient, log: LogFn) -> None:
    """Se suscribe a todas las notificaciones para ver qué responde el equipo."""
    for s in client.services:
        # 1800/1801 (GAP/GATT) deniegan el acceso en Windows; no aportan nada.
        if s.uuid.lower() in (u16("1800"), u16("1801")):
            continue
        for c in s.characteristics:
            if not client.is_connected:
                log("(se perdió la conexión al suscribirse)")
                return
            if "notify" in c.properties or "indicate" in c.properties:
                try:
                    await client.start_notify(
                        c, lambda ch, d: log(f"RX {ch.uuid[:8]} <- {bytes(d).hex(' ')}"))
                except Exception as e:  # noqa: BLE001
                    log(f"(no se pudo suscribir a {c.uuid}: {e})")


async def dump_services(client: BleakClient, log: LogFn) -> None:
    for s in client.services:
        log(f"[Servicio] {s.uuid}  {s.description}")
        for c in s.characteristics:
            line = f"    [Char] {c.uuid}  ({','.join(c.properties)})  handle={c.handle}"
            if "read" in c.properties:
                try:
                    v = await client.read_gatt_char(c)
                    line += f"  valor={v.hex(' ')}"
                    if v and all(32 <= b < 127 for b in v):
                        line += f"  '{v.decode()}'"
                except Exception as e:  # noqa: BLE001
                    line += f"  (lectura falló: {e.__class__.__name__})"
            log(line)
    p = detect_profile(client)
    log(f"\nProtocolo detectado: {p.name if p else 'NINGUNO conocido'}")


async def scan(timeout: float = 8.0, show_all: bool = False) -> list:
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    items = sorted(found.values(), key=lambda da: da[1].rssi, reverse=True)
    rows = []
    for dev, adv in items:
        name = adv.local_name or dev.name
        guess = guess_from_adv(name, adv.service_uuids)
        if not show_all and not name and not guess:
            continue
        rows.append((dev, adv, name, guess))
    return rows


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
async def cmd_scan(args) -> int:
    print(f"Escaneando {args.timeout:.0f} s...\n")
    rows = await scan(args.timeout, args.all)
    if not rows:
        print("No se encontró nada. ¿Bluetooth encendido? ¿App del teléfono cerrada?")
        return 1
    for dev, adv, name, guess in rows:
        tag = f"  <-- posible {guess.name}" if guess else ""
        print(f"{dev.address}  RSSI {adv.rssi:>4}  {name or '(sin nombre)'}{tag}")
        if args.verbose and adv.service_uuids:
            for u in adv.service_uuids:
                print(f"        servicio anunciado: {u}")
        if args.verbose and adv.manufacturer_data:
            for k, v in adv.manufacturer_data.items():
                print(f"        manufacturer 0x{k:04x}: {v.hex(' ')}")
    return 0


async def cmd_inspect(args) -> int:
    async with BleakClient(args.address, timeout=20) as c:
        print(f"Conectado a {args.address}\n")
        await dump_services(c, print)
    return 0


async def cmd_power(args, state: bool) -> int:
    async with BleakClient(args.address, timeout=20) as c:
        await subscribe_all(c, print)
        p = find_profile(args.protocol) if args.protocol else detect_profile(c)
        if p is None:
            print("No reconocí el protocolo. Corre 'inspect' y prueba con 'raw'.")
            return 2
        pkts = p.on if state else p.off
        v = min(args.variant, len(pkts) - 1)
        print(f"Protocolo: {p.name}  (variante {v}, hay {len(pkts)})")
        if p.keepalive:
            await write(c, p.write_char, p.keepalive)
        await write(c, p.write_char, pkts[v])
        await asyncio.sleep(1.0)  # tiempo para ver respuestas (RX)
    return 0


async def cmd_color(args) -> int:
    async with BleakClient(args.address, timeout=20) as c:
        await subscribe_all(c, print)
        p = find_profile(args.protocol) if args.protocol else detect_profile(c)
        if p is None or p.color is None:
            print("Este protocolo no tiene comando de color definido.")
            return 2
        print(f"Protocolo: {p.name}")
        await write(c, p.write_char, p.color(args.r, args.g, args.b))
        await asyncio.sleep(1.0)
    return 0


async def cmd_raw(args) -> int:
    async with BleakClient(args.address, timeout=20) as c:
        await subscribe_all(c, print)
        await write(c, args.char, parse_hex(args.hex))
        await asyncio.sleep(1.0)
    return 0


# --------------------------------------------------------------------------- #
# GUI (Tkinter)
# --------------------------------------------------------------------------- #
class AsyncRunner:
    """Event loop de asyncio en un hilo aparte para no congelar Tkinter."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


def run_gui() -> None:
    import tkinter as tk
    from tkinter import ttk
    from tkinter.scrolledtext import ScrolledText

    runner = AsyncRunner()
    ui_q: "queue.Queue[tuple[str, object]]" = queue.Queue()
    st: dict = {"client": None, "profile": None, "rows": [], "ka": None}

    def log(msg: str) -> None:
        ui_q.put(("log", msg))

    # ---------- coroutines (corren en el hilo de asyncio) ----------
    async def a_scan():
        log("Escaneando 6 s...")
        rows = await scan(6.0)
        ui_q.put(("rows", rows))
        log(f"{len(rows)} dispositivo(s).")

    async def a_disconnect():
        if st["ka"]:
            st["ka"].cancel()
            st["ka"] = None
        c = st["client"]
        if c and c.is_connected:
            await c.disconnect()
        st["client"] = None
        st["profile"] = None

    async def a_keepalive(c: BleakClient, p: Profile):
        while c.is_connected:
            try:
                await write(c, p.write_char, p.keepalive, log=None)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(2)

    async def a_connect(addr: str, proto: str):
        await a_disconnect()
        log(f"Conectando a {addr}...")
        c = BleakClient(addr, timeout=20,
                        disconnected_callback=lambda _c: log("Desconectado."))
        await c.connect()
        await subscribe_all(c, log)
        p = detect_profile(c) if proto == "auto" else find_profile(proto)
        st["client"], st["profile"] = c, p
        log(f"Conectado. Protocolo: {p.name if p else 'DESCONOCIDO (usa Inspeccionar y Raw)'}")
        if p and p.keepalive:
            st["ka"] = asyncio.ensure_future(a_keepalive(c, p))

    async def a_power(on: bool, variant: int):
        c, p = st["client"], st["profile"]
        if not c or not c.is_connected:
            log("Primero conecta.")
            return
        if not p:
            log("Protocolo desconocido: elige uno en la lista o usa Raw.")
            return
        pkts = p.on if on else p.off
        await write(c, p.write_char, pkts[min(variant, len(pkts) - 1)], log)

    async def a_color(r: int, g: int, b: int):
        c, p = st["client"], st["profile"]
        if not c or not c.is_connected:
            log("Primero conecta.")
            return
        if not p or not p.color:
            log("Este protocolo no tiene comando de color definido.")
            return
        await write(c, p.write_char, p.color(r, g, b), log)

    async def a_inspect():
        c = st["client"]
        if not c or not c.is_connected:
            log("Primero conecta.")
            return
        await dump_services(c, log)

    async def a_raw(char: str, hexs: str):
        c = st["client"]
        if not c or not c.is_connected:
            log("Primero conecta.")
            return
        await write(c, char, parse_hex(hexs), log)

    def go(coro):
        fut = runner.submit(coro)
        fut.add_done_callback(
            lambda f: f.exception() and log(f"ERROR: {f.exception()!r}"))

    # ---------- widgets ----------
    root = tk.Tk()
    root.title("LED BLE Tester")
    root.geometry("760x560")

    top = ttk.Frame(root, padding=6)
    top.pack(fill="x")
    ttk.Button(top, text="Escanear", command=lambda: go(a_scan())).pack(side="left")
    ttk.Label(top, text="  Protocolo:").pack(side="left")
    proto_var = tk.StringVar(value="auto")
    ttk.Combobox(top, textvariable=proto_var, width=12, state="readonly",
                 values=["auto"] + [p.name for p in PROFILES]).pack(side="left")
    ttk.Label(top, text="  Variante:").pack(side="left")
    var_var = tk.IntVar(value=0)
    ttk.Spinbox(top, from_=0, to=3, width=3, textvariable=var_var).pack(side="left")

    devs = tk.Listbox(root, height=8, font=("Consolas", 10))
    devs.pack(fill="x", padx=6)

    def selected_addr() -> Optional[str]:
        sel = devs.curselection()
        return st["rows"][sel[0]][0].address if sel else None

    def on_connect():
        addr = selected_addr()
        if addr:
            go(a_connect(addr, proto_var.get()))
        else:
            log("Selecciona un dispositivo de la lista.")

    btns = ttk.Frame(root, padding=6)
    btns.pack(fill="x")
    ttk.Button(btns, text="Conectar", command=on_connect).pack(side="left")
    ttk.Button(btns, text="Desconectar", command=lambda: go(a_disconnect())).pack(side="left")
    ttk.Button(btns, text="Inspeccionar", command=lambda: go(a_inspect())).pack(side="left")
    ttk.Button(btns, text="  ENCENDER  ",
               command=lambda: go(a_power(True, var_var.get()))).pack(side="left", padx=(20, 4))
    ttk.Button(btns, text="  APAGAR  ",
               command=lambda: go(a_power(False, var_var.get()))).pack(side="left")

    def pick_color():
        from tkinter import colorchooser
        rgb, _hex = colorchooser.askcolor(title="Color de las luces")
        if rgb:
            go(a_color(*(int(v) for v in rgb)))

    ttk.Button(btns, text="Color...", command=pick_color).pack(side="left", padx=(8, 0))

    raw = ttk.Frame(root, padding=6)
    raw.pack(fill="x")
    ttk.Label(raw, text="Raw  char:").pack(side="left")
    char_e = ttk.Entry(raw, width=12)
    char_e.insert(0, "fff3")
    char_e.pack(side="left")
    ttk.Label(raw, text=" hex:").pack(side="left")
    hex_e = ttk.Entry(raw, width=40)
    hex_e.insert(0, "7e 00 04 01 00 00 00 00 ef")
    hex_e.pack(side="left", fill="x", expand=True)
    ttk.Button(raw, text="Enviar",
               command=lambda: go(a_raw(char_e.get(), hex_e.get()))).pack(side="left")

    out = ScrolledText(root, font=("Consolas", 9))
    out.pack(fill="both", expand=True, padx=6, pady=6)

    def poll():
        try:
            while True:
                kind, payload = ui_q.get_nowait()
                if kind == "log":
                    out.insert("end", f"{payload}\n")
                    out.see("end")
                elif kind == "rows":
                    st["rows"] = payload
                    devs.delete(0, "end")
                    for dev, adv, name, guess in payload:
                        tag = f"   <-- posible {guess.name}" if guess else ""
                        devs.insert("end", f"{dev.address}  {adv.rssi:>4} dBm  "
                                           f"{name or '(sin nombre)'}{tag}")
        except queue.Empty:
            pass
        root.after(100, poll)

    def on_close():
        try:
            runner.submit(a_disconnect()).result(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    log("Cierra la app del teléfono antes de conectar. Escanea, selecciona, conecta, enciende.")
    poll()
    root.mainloop()


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Probador BLE para luces LED RGB")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="buscar dispositivos BLE")
    s.add_argument("--timeout", type=float, default=8.0)
    s.add_argument("--all", action="store_true", help="incluir dispositivos sin nombre")
    s.add_argument("-v", "--verbose", action="store_true",
                   help="mostrar servicios y manufacturer data anunciados")

    s = sub.add_parser("inspect", help="listar servicios/características")
    s.add_argument("address")

    for name in ("on", "off"):
        s = sub.add_parser(name, help=f"{'encender' if name == 'on' else 'apagar'}")
        s.add_argument("address")
        s.add_argument("--protocol", choices=[p.name for p in PROFILES])
        s.add_argument("--variant", type=int, default=0,
                       help="variante del paquete (elk-bledom tiene 0 y 1)")

    s = sub.add_parser("color", help="poner un color RGB (0-255 cada uno)")
    s.add_argument("address")
    for ch in ("r", "g", "b"):
        s.add_argument(ch, type=lambda x: max(0, min(255, int(x))))
    s.add_argument("--protocol", choices=[p.name for p in PROFILES])

    s = sub.add_parser("raw", help="escribir bytes arbitrarios a una característica")
    s.add_argument("address")
    s.add_argument("char", help="UUID completo o corto, p. ej. fff3")
    s.add_argument("hex", help='bytes en hex, p. ej. "7e 00 04 01 00 00 00 00 ef"')

    sub.add_parser("gui", help="interfaz gráfica")

    args = ap.parse_args()
    if args.cmd == "gui":
        run_gui()
        return 0
    handlers = {
        "scan": cmd_scan,
        "inspect": cmd_inspect,
        "on": lambda a: cmd_power(a, True),
        "off": lambda a: cmd_power(a, False),
        "color": cmd_color,
        "raw": cmd_raw,
    }
    return asyncio.run(handlers[args.cmd](args))


if __name__ == "__main__":
    sys.exit(main())
