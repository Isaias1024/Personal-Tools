#!/usr/bin/env python3
"""
Control de luces permanentes (DMRRBA-007 / illumiHome) por Bluetooth LE.

  ON / OFF   busca automáticamente el controlador (nombre DMRRBA-...), se conecta
             y enciende / apaga ("OFF suave" = color negro).
  Colores    rueda de color + brillo.
  Halloween  animaciones de la festividad (latido, fuego, tormenta, fantasma...).
             Velocidad y brillo ajustables en vivo.

Requiere ble_led_tester.py en la misma carpeta y:  pip install bleak
Cierra la app illumiHome del teléfono antes de usar esto.
"""
from __future__ import annotations

import asyncio
import colorsys
import math
import queue
import random
import tkinter as tk
from tkinter import ttk
from typing import Callable, Iterator, Optional

from bleak import BleakClient, BleakScanner

from ble_led_tester import (AsyncRunner, Profile, detect_profile, find_profile,
                            subscribe_all, write)

TARGET_PREFIX = "dmrrba"   # nombre que anuncia el controlador
SCAN_SECONDS = 5.0         # duración de cada escaneo
FIND_TIMEOUT = 40.0        # tiempo máximo buscando el dispositivo
FRAME_DT = 0.07            # segundos entre cuadros de animación (~12 fps)
WHEEL_SIZE = 240

# --- paleta -------------------------------------------------------------
BG = "#0e0c14"
CARD = "#181523"
CARD_HI = "#231f33"
LINE = "#2a2540"
FG = "#ece8f5"
MUTED = "#8d86a6"
ORANGE = "#ff7a1a"
PURPLE = "#9b6bff"
GREEN = "#38d27a"
RED = "#e5484d"

RGB = tuple[float, float, float]   # componentes 0..1


def hsv_to_rgb255(h: float, s: float, v: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return round(r * 255), round(g * 255), round(b * 255)


def build_wheel_image(size: int) -> tk.PhotoImage:
    """Rueda de color (matiz = ángulo, saturación = radio) como PhotoImage."""
    img = tk.PhotoImage(width=size, height=size)
    radius = size / 2
    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            dx, dy = x + 0.5 - radius, y + 0.5 - radius
            dist = math.hypot(dx, dy)
            if dist <= radius - 1:
                hue = (math.atan2(-dy, dx) / (2 * math.pi)) % 1.0
                r, g, b = hsv_to_rgb255(hue, dist / radius, 1.0)
                row.append(f"#{r:02x}{g:02x}{b:02x}")
            else:
                row.append(CARD)
        rows.append("{" + " ".join(row) + "}")
    img.put(" ".join(rows))
    return img


# ======================================================================
#  Animaciones de Halloween
#  Cada una es un generador que recibe speed() y produce un color
#  "perceptual" (0..1) por cuadro. Al enviar se aplica corrección gamma
#  (los LEDs son lineales, el ojo no) y después el brillo global.
# ======================================================================
SpeedFn = Callable[[], float]
GAMMA = 2.0


def _mix(a: RGB, b: RGB, t: float) -> RGB:
    t = max(0.0, min(1.0, t))
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


def _scale(c: RGB, k: float) -> RGB:
    return (c[0] * k, c[1] * k, c[2] * k)


def _ease(x: float) -> float:
    """smoothstep: arranca y termina suave."""
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def _noise(t: float, seed: float = 0.0) -> float:
    """Ruido suave 0..1 (suma de senos incomensurables): orgánico y sin saltos."""
    return 0.5 + 0.5 * (math.sin(t * 1.00 + seed) + math.sin(t * 2.31 + seed * 1.7 + 1.3)
                        + math.sin(t * 4.77 + seed * 2.9 + 2.1)) / 3.0


def _clock(speed: SpeedFn) -> Iterator[float]:
    t = 0.0
    while True:
        t += FRAME_DT * speed()
        yield t


def _chance(speed: SpeedFn, per_second: float) -> bool:
    """True con probabilidad 'per_second' eventos/seg (escala con la velocidad)."""
    return random.random() < per_second * FRAME_DT * speed()


def anim_heartbeat(speed: SpeedFn) -> Iterator[RGB]:
    """Latido: dos golpes y reposo, rojo sangre."""
    for t in _clock(speed):
        p = (t / 1.15) % 1.0
        beat = (math.exp(-((p - 0.08) / 0.035) ** 2)
                + 0.65 * math.exp(-((p - 0.27) / 0.045) ** 2))
        k = 0.06 + 0.94 * min(beat, 1.0)
        yield _scale(_mix((0.9, 0.0, 0.0), (1.0, 0.05, 0.03), k), k)


def anim_fire(speed: SpeedFn) -> Iterator[RGB]:
    """Antorcha: llama con parpadeo orgánico y ráfagas de viento."""
    gust = 0.0
    for t in _clock(speed):
        if _chance(speed, 0.8):
            gust = random.uniform(0.4, 0.9)
        gust *= 0.85
        h = 0.55 + 0.35 * _noise(t * 2.2) + random.uniform(-0.08, 0.08) - gust * 0.5
        h = max(0.12, min(1.0, h))
        yield (min(1.0, h * 1.15), 0.22 * h ** 1.4 + 0.1 * h ** 3, 0.0)


def anim_hell(speed: SpeedFn) -> Iterator[RGB]:
    """Infierno: lava roja que respira, ráfagas naranjas y chispas amarillas."""
    flare = 0.0
    spark = 0.0
    for t in _clock(speed):
        if _chance(speed, 1.1):
            flare = random.uniform(0.6, 1.0)
        if _chance(speed, 0.7):
            spark = 1.0
        flare *= 0.90
        spark *= 0.62
        heat = 0.30 + 0.38 * _noise(t * 0.9, 3.0) + 0.45 * flare
        base = _mix((0.35, 0.0, 0.0), (1.0, 0.06, 0.0), heat * 1.2)
        orange = _mix(base, (1.0, 0.38, 0.0), flare)
        yield _mix(orange, (1.0, 0.75, 0.15), spark * 0.8)


def anim_underworld(speed: SpeedFn) -> Iterator[RGB]:
    """Inframundo: marea índigo/turquesa y almas verdes que cruzan."""
    soul = 0.0
    for t in _clock(speed):
        if _chance(speed, 0.5):
            soul = 1.0
        soul *= 0.88
        tide = _noise(t * 0.45, 7.0)
        base = _mix((0.10, 0.0, 0.40), (0.0, 0.40, 0.45), tide)
        base = _scale(base, 0.35 + 0.45 * _noise(t * 0.7, 1.0))
        yield _mix(base, (0.35, 1.0, 0.55), soul * 0.85)


def anim_storm(speed: SpeedFn) -> Iterator[RGB]:
    """Tormenta: nubes azules y relámpagos con destellos múltiples y eco."""
    queue_: list[float] = []
    glow = 0.0
    for t in _clock(speed):
        base = _scale(_mix((0.12, 0.05, 0.55), (0.05, 0.18, 0.55), _noise(t * 0.6)),
                      0.18 + 0.22 * _noise(t * 0.9, 4.0))
        if not queue_ and _chance(speed, 0.28):
            queue_ = random.choice([[1.0, 0.0, 0.9, 0.3, 0.0, 0.0, 1.0],
                                    [1.0, 0.6, 0.0, 1.0],
                                    [0.7, 0.0, 1.0, 0.0, 0.5, 0.0, 0.0, 1.0, 0.4],
                                    [1.0]])
        if queue_:
            glow = max(glow, queue_.pop(0))
        glow *= 0.55
        yield _mix(base, (0.85, 0.92, 1.0), glow)


def anim_ghost(speed: SpeedFn) -> Iterator[RGB]:
    """Fantasma: respira lento (inhala suave, exhala más rápido) con escalofríos."""
    for t in _clock(speed):
        p = (t / 3.2) % 1.0
        k = _ease(p / 0.6) if p < 0.6 else 1 - _ease((p - 0.6) / 0.4)
        k = 0.06 + 0.94 * k * (0.9 + 0.1 * _noise(t * 5))
        yield _mix((0.0, 0.25, 0.12), (0.55, 1.0, 0.80), k ** 1.3)


def anim_pumpkin(speed: SpeedFn) -> Iterator[RGB]:
    """Calabaza: brasa naranja que titila y ondas moradas de magia."""
    for t in _clock(speed):
        cyc = (t / 7.0) % 1.0
        magic = _ease((cyc - 0.55) / 0.15) * (1 - _ease((cyc - 0.85) / 0.15))
        flick = 0.72 + 0.28 * _noise(t * 3.0, 2.0) + random.uniform(-0.04, 0.04)
        orange = _scale((1.0, 0.30, 0.0), min(1.0, flick))
        yield _mix(orange, (0.55, 0.0, 1.0), magic)


def anim_potion(speed: SpeedFn) -> Iterator[RGB]:
    """Poción: verde ácido que hierve, con burbujas que revientan."""
    bubble = 0.0
    for t in _clock(speed):
        if _chance(speed, 1.6):
            bubble = random.uniform(0.6, 1.0)
        bubble *= 0.72
        base = 0.40 + 0.25 * _noise(t * 1.6, 5.0)
        yield _mix(_scale((0.15, 1.0, 0.0), base), (0.75, 1.0, 0.45), bubble)


def anim_witch(speed: SpeedFn) -> Iterator[RGB]:
    """Hechizo: morado → magenta → verde tóxico con pausas y destellos."""
    stops = [(0.45, 0.0, 1.0), (1.0, 0.0, 0.55), (0.1, 1.0, 0.25)]
    spark = 0.0
    for t in _clock(speed):
        x = (t * 0.30) % len(stops)
        i = int(x)
        f = _ease((x - i) / 0.6)            # sostiene el color y luego transiciona
        if _chance(speed, 0.9):
            spark = 0.9
        spark *= 0.6
        yield _mix(_mix(stops[i], stops[(i + 1) % len(stops)], f), (1, 1, 1), spark * 0.5)


def anim_panic(speed: SpeedFn) -> Iterator[RGB]:
    """Pánico: ráfagas de destellos rojo/naranja entre silencios tensos."""
    burst = 0
    for _ in _clock(speed):
        if burst == 0 and _chance(speed, 0.6):
            burst = random.randint(8, 24)
        if burst:
            burst -= 1
            r = random.random()
            yield (0.0, 0.0, 0.0) if r < 0.35 else (1.0, 0.28, 0.0) if r < 0.7 else (1.0, 0.0, 0.0)
        else:
            yield (0.10, 0.0, 0.0)


def anim_moon(speed: SpeedFn) -> Iterator[RGB]:
    """Luna llena: luz pálida que respira; pasan nubes que la tapan."""
    for t in _clock(speed):
        cloud = _ease((_noise(t * 0.35, 9.0) - 0.55) / 0.2)
        k = (0.78 + 0.22 * math.sin(t * 0.8)) * (1 - 0.75 * cloud)
        yield _scale(_mix((0.55, 0.68, 1.0), (0.85, 0.9, 1.0), k), k)


def anim_fog(speed: SpeedFn) -> Iterator[RGB]:
    """Niebla del cementerio: gris-verdoso que deriva despacio."""
    for t in _clock(speed):
        c = _mix((0.12, 0.30, 0.22), (0.30, 0.50, 0.62), _noise(t * 0.30, 11.0))
        yield _scale(c, 0.35 + 0.45 * _noise(t * 0.5, 13.0))


def anim_blood(speed: SpeedFn) -> Iterator[RGB]:
    """Sangre: gotas — golpe de luz roja que se desvanece lento."""
    level = 0.0
    wait = 0.0
    for _ in _clock(speed):
        wait -= FRAME_DT * speed()
        if wait <= 0:
            level = 1.0
            wait = random.uniform(1.4, 3.2)
        level *= 0.93
        yield _scale((0.9, 0.0, 0.01), 0.08 + 0.92 * level)


def anim_neon(speed: SpeedFn) -> Iterator[RGB]:
    """Neón roto: verde tóxico que chisporrotea y se apaga a ratos."""
    pattern: list[float] = []
    for t in _clock(speed):
        if not pattern and _chance(speed, 0.55):
            pattern = random.choice([[0, 1, 0, 1, 0, 0, 1], [0.2, 0.8, 0.1, 0.9, 0.3],
                                     [0] * random.randint(4, 12) + [1, 0, 1]])
        if pattern:
            v = pattern.pop(0)
        else:
            v = 0.92 + 0.08 * _noise(t * 8)
        yield _scale((0.15, 1.0, 0.45), v)


def anim_possession(speed: SpeedFn) -> Iterator[RGB]:
    """Posesión: rojo demoníaco que late y se corrompe con glitches."""
    glitch: list[RGB] = []
    for t in _clock(speed):
        if not glitch and _chance(speed, 0.5):
            glitch = [random.choice([(0, 0, 0), (0.6, 0.0, 1.0), (1.0, 1.0, 1.0), (0, 0, 0)])
                      for _ in range(random.randint(2, 6))]
        if glitch:
            yield glitch.pop(0)
        else:
            yield _scale((1.0, 0.0, 0.02), 0.25 + 0.75 * (0.5 + 0.5 * math.sin(t * 2.4)) ** 2)


def anim_coven(speed: SpeedFn) -> Iterator[RGB]:
    """Aquelarre: morado, verde y naranja en rotación con chispas mágicas."""
    stops = [(0.55, 0.0, 1.0), (0.1, 1.0, 0.2), (1.0, 0.33, 0.0)]
    spark = 0.0
    for t in _clock(speed):
        x = (t * 0.45) % len(stops)
        i = int(x)
        base = _mix(stops[i], stops[(i + 1) % len(stops)], _ease((x - i) / 0.35))
        if _chance(speed, 2.0):
            spark = 1.0
        spark *= 0.55
        yield _mix(_scale(base, 0.6 + 0.4 * _noise(t * 2.0)), (1, 1, 1), spark * 0.6)


def anim_embers(speed: SpeedFn) -> Iterator[RGB]:
    """Brasas del infierno: negro y rojo — brasas que se encienden y mueren."""
    heat = 0.0
    for t in _clock(speed):
        if _chance(speed, 2.2):
            heat = random.uniform(0.5, 1.0)
        heat *= 0.90
        slow = max(0.0, _noise(t * 0.8, 21.0) - 0.45) * 1.2   # el fondo casi se apaga
        k = min(1.0, slow * 0.55 + heat)
        yield (k, 0.05 * k ** 3, 0.0)


def anim_abyss(speed: SpeedFn) -> Iterator[RGB]:
    """Abismo: oscuridad total rota por latidos rojos lejanos."""
    level = 0.0
    wait = 0.0
    for _ in _clock(speed):
        wait -= FRAME_DT * speed()
        if wait <= 0:
            level = 1.0
            wait = random.uniform(2.0, 5.0)
        level *= 0.95
        yield (level ** 1.5, 0.0, 0.0)


ANIMATIONS: list[tuple[str, str, str, Callable[[SpeedFn], Iterator[RGB]]]] = [
    ("hell",        "Infierno",    "#ff3b0a", anim_hell),
    ("embers",      "Brasas",      "#c01000", anim_embers),
    ("abyss",       "Abismo",      "#600008", anim_abyss),
    ("underworld",  "Inframundo",  "#18c8b8", anim_underworld),
    ("fire",        "Antorcha",    "#ff7a1a", anim_fire),
    ("heartbeat",   "Latido",      "#e5484d", anim_heartbeat),
    ("storm",       "Tormenta",    "#7c6cff", anim_storm),
    ("ghost",       "Fantasma",    "#6dffb0", anim_ghost),
    ("pumpkin",     "Calabaza",    "#ff8a00", anim_pumpkin),
    ("potion",      "Poción",      "#7dff2a", anim_potion),
    ("witch",       "Hechizo",     "#c04dff", anim_witch),
    ("coven",       "Aquelarre",   "#ff5fd0", anim_coven),
    ("moon",        "Luna llena",  "#b8c8ff", anim_moon),
    ("fog",         "Niebla",      "#78a8a0", anim_fog),
    ("blood",       "Sangre",      "#b00014", anim_blood),
    ("neon",        "Neón roto",   "#3dff8a", anim_neon),
    ("possession",  "Posesión",    "#ff0030", anim_possession),
    ("panic",       "Pánico",      "#ff3d1f", anim_panic),
]


class LedApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.runner = AsyncRunner()
        self.ui_q: "queue.Queue[tuple[str, object]]" = queue.Queue()

        # estado (se toca desde el hilo de asyncio, salvo donde se indica)
        self.client: Optional[BleakClient] = None
        self.profile: Optional[Profile] = None
        self.address: Optional[str] = None
        self.busy = False
        self._pending_rgb: Optional[tuple[int, int, int]] = None
        self._last_rgb: tuple[int, int, int] = (255, 255, 255)
        self._sender_task: Optional[asyncio.Task] = None
        self._anim_task: Optional[asyncio.Task] = None
        # valores escritos por el hilo de Tk y leídos por el de asyncio
        self._bright = 1.0
        self._speed = 1.0

        # estado de la UI (hilo de Tk)
        self.hue, self.sat = 0.0, 0.0
        self.soft_off = tk.BooleanVar(value=False)
        self.active_anim: Optional[str] = None
        self.anim_buttons: dict[str, tk.Label] = {}
        self._pulse_phase = 0.0

        self._build_ui()
        self._poll()
        self._pulse()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        r = self.root
        r.title("Luces · Halloween")
        r.configure(bg=BG)
        r.resizable(False, False)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=FG, font=("Segoe UI", 10))
        style.configure("Card.TLabel", background=CARD, foreground=FG)
        style.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("TCheckbutton", background=BG, foreground=MUTED, font=("Segoe UI", 9),
                        focuscolor=BG)
        style.map("TCheckbutton", background=[("active", BG)], foreground=[("active", FG)])
        style.configure("Horizontal.TScale", background=CARD, troughcolor=LINE,
                        bordercolor=CARD, lightcolor=ORANGE, darkcolor=ORANGE)
        style.map("Horizontal.TScale", background=[("active", CARD)])

        pad = tk.Frame(r, bg=BG, padx=18, pady=16)
        pad.pack()

        # --- encabezado: título + estado
        head = tk.Frame(pad, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text="🎃", bg=BG, font=("Segoe UI Emoji", 26)).pack(side="left")
        titles = tk.Frame(head, bg=BG)
        titles.pack(side="left", padx=10)
        tk.Label(titles, text="Luces Halloween", bg=BG, fg=FG,
                 font=("Segoe UI Semibold", 16)).pack(anchor="w")
        self.mac_var = tk.StringVar(value="MAC: —")
        tk.Label(titles, textvariable=self.mac_var, bg=BG, fg=MUTED,
                 font=("Consolas", 9)).pack(anchor="w")

        self.status_var = tk.StringVar(value="Desconectado")
        pill = tk.Frame(head, bg=CARD, padx=10, pady=5)
        pill.pack(side="right")
        self.dot = tk.Canvas(pill, width=10, height=10, bg=CARD, highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 6))
        self.dot_item = self.dot.create_oval(1, 1, 9, 9, fill=MUTED, outline="")
        tk.Label(pill, textvariable=self.status_var, bg=CARD, fg=FG,
                 font=("Segoe UI", 9, "bold")).pack(side="left")

        # --- ON / OFF
        btns = tk.Frame(pad, bg=BG)
        btns.pack(fill="x", pady=(14, 0))
        self.btn_on = self._flat_button(btns, "ENCENDER", GREEN, "#0b1f13", self.on_click_on)
        self.btn_off = self._flat_button(btns, "APAGAR", RED, "white", self.on_click_off)
        self.btn_on.pack(side="left", expand=True, fill="x", padx=(0, 6))
        self.btn_off.pack(side="left", expand=True, fill="x", padx=(6, 0))
        ttk.Checkbutton(pad, text="OFF suave (color negro; mantiene el Bluetooth despierto)",
                        variable=self.soft_off).pack(anchor="w", pady=(8, 10))

        # --- pestañas
        tabs = tk.Frame(pad, bg=BG)
        tabs.pack(fill="x")
        self.tab_btns: dict[str, tk.Label] = {}
        for key, label in (("halloween", "🎃  Halloween"), ("colors", "🎨  Colores")):
            b = tk.Label(tabs, text=label, bg=BG, fg=MUTED, cursor="hand2",
                         font=("Segoe UI Semibold", 11), padx=14, pady=8)
            b.pack(side="left")
            b.bind("<Button-1>", lambda _e, k=key: self._show_tab(k))
            self.tab_btns[key] = b
        tk.Frame(pad, bg=LINE, height=1).pack(fill="x")

        body = tk.Frame(pad, bg=BG)
        body.pack(fill="x", pady=(12, 0))
        self.pages = {"halloween": self._build_halloween(body),
                      "colors": self._build_colors(body)}

        # --- brillo (común)
        card = tk.Frame(pad, bg=CARD, padx=14, pady=10)
        card.pack(fill="x", pady=(12, 0))
        self.bright = tk.DoubleVar(value=100)
        self.speed = tk.DoubleVar(value=1.0)
        self._slider_row(card, "Brillo", self.bright, 0, 100, self._on_bright)
        self._slider_row(card, "Velocidad", self.speed, 0.2, 3.0, self._on_speed)
        sw = tk.Frame(card, bg=CARD)
        sw.pack(fill="x", pady=(6, 0))
        self.swatch = tk.Frame(sw, bg="#ffffff", width=28, height=14)
        self.swatch.pack(side="left")
        self.hex_var = tk.StringVar(value="#ffffff")
        tk.Label(sw, textvariable=self.hex_var, bg=CARD, fg=MUTED,
                 font=("Consolas", 9)).pack(side="left", padx=8)

        self.log_box = tk.Text(pad, height=4, width=52, bg="#0a0910", fg=MUTED,
                               font=("Consolas", 8), relief="flat", state="disabled",
                               padx=8, pady=6)
        self.log_box.pack(pady=(12, 0), fill="x")

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._show_tab("halloween")
        self._color_changed(send=False)

    def _flat_button(self, parent, text, bg, fg, cmd) -> tk.Label:
        b = tk.Label(parent, text=text, bg=bg, fg=fg, cursor="hand2",
                     font=("Segoe UI Semibold", 12), pady=11)
        b._base = bg  # type: ignore[attr-defined]
        b._enabled = True  # type: ignore[attr-defined]
        b.bind("<Button-1>", lambda _e: cmd() if b._enabled else None)  # type: ignore[attr-defined]
        b.bind("<Enter>", lambda _e: b._enabled and b.configure(bg=self._lighten(bg)))  # type: ignore[attr-defined]
        b.bind("<Leave>", lambda _e: b._enabled and b.configure(bg=bg))  # type: ignore[attr-defined]
        return b

    @staticmethod
    def _lighten(hexcolor: str, k: float = 0.15) -> str:
        r, g, b = (int(hexcolor[i:i + 2], 16) for i in (1, 3, 5))
        r, g, b = (round(c + (255 - c) * k) for c in (r, g, b))
        return f"#{r:02x}{g:02x}{b:02x}"

    def _set_buttons_enabled(self, enabled: bool) -> None:
        for b in (self.btn_on, self.btn_off):
            b._enabled = enabled  # type: ignore[attr-defined]
            b.configure(bg=b._base if enabled else LINE,  # type: ignore[attr-defined]
                        cursor="hand2" if enabled else "arrow")

    def _slider_row(self, parent, label, var, lo, hi, cmd) -> None:
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=2)
        tk.Label(row, text=label, bg=CARD, fg=FG, width=9, anchor="w",
                 font=("Segoe UI", 10)).pack(side="left")
        ttk.Scale(row, from_=lo, to=hi, variable=var, orient="horizontal",
                  command=lambda _v: cmd()).pack(side="left", fill="x", expand=True)

    def _build_halloween(self, parent) -> tk.Frame:
        page = tk.Frame(parent, bg=BG)
        grid = tk.Frame(page, bg=BG)
        grid.pack()
        for i, (key, name, color, _fn) in enumerate(ANIMATIONS):
            tile = tk.Frame(grid, bg=CARD, width=118, height=58, cursor="hand2",
                            highlightthickness=2, highlightbackground=CARD)
            tile.grid(row=i // 4, column=i % 4, padx=4, pady=4)
            tile.grid_propagate(False)
            tile.pack_propagate(False)
            bar = tk.Frame(tile, bg=color, height=4)
            bar.pack(fill="x", side="top")
            lbl = tk.Label(tile, text=name, bg=CARD, fg=FG, cursor="hand2",
                           font=("Segoe UI Semibold", 10))
            lbl.pack(expand=True)
            for w in (tile, bar, lbl):
                w.bind("<Button-1>", lambda _e, k=key: self._toggle_anim(k))
            tile._lbl = lbl  # type: ignore[attr-defined]
            tile._color = color  # type: ignore[attr-defined]
            self.anim_buttons[key] = tile  # type: ignore[assignment]
        stop = tk.Label(page, text="■  Detener animación", bg=BG, fg=MUTED, cursor="hand2",
                        font=("Segoe UI", 9))
        stop.pack(pady=(4, 0))
        stop.bind("<Button-1>", lambda _e: self._stop_anim_ui())
        return page

    def _build_colors(self, parent) -> tk.Frame:
        page = tk.Frame(parent, bg=BG)
        self.wheel_img = build_wheel_image(WHEEL_SIZE)
        self.canvas = tk.Canvas(page, width=WHEEL_SIZE, height=WHEEL_SIZE, bg=BG,
                                highlightthickness=0, cursor="crosshair")
        self.canvas.pack()
        self.canvas.create_image(0, 0, image=self.wheel_img, anchor="nw")
        c = WHEEL_SIZE / 2
        self.marker = self.canvas.create_oval(c - 7, c - 7, c + 7, c + 7,
                                              outline="black", width=2)
        self.marker_in = self.canvas.create_oval(c - 5, c - 5, c + 5, c + 5,
                                                 outline="white", width=2)
        self.canvas.bind("<Button-1>", self._on_wheel)
        self.canvas.bind("<B1-Motion>", self._on_wheel)
        return page

    def _show_tab(self, key: str) -> None:
        for k, page in self.pages.items():
            page.pack_forget()
            self.tab_btns[k].configure(fg=MUTED, bg=BG)
        self.pages[key].pack()
        self.tab_btns[key].configure(fg=ORANGE)

    # ------------------------------------------------------------- eventos UI
    def _on_bright(self) -> None:
        self._bright = self.bright.get() / 100.0
        if self.active_anim is None:
            self._color_changed()

    def _on_speed(self) -> None:
        self._speed = float(self.speed.get())

    def _on_wheel(self, ev: tk.Event) -> None:
        self._stop_anim_ui()
        c = WHEEL_SIZE / 2
        dx, dy = ev.x - c, ev.y - c
        dist = math.hypot(dx, dy)
        if dist > c - 1:  # limitar al borde de la rueda
            dx, dy = dx * (c - 1) / dist, dy * (c - 1) / dist
            dist = c - 1
        self.hue = (math.atan2(-dy, dx) / (2 * math.pi)) % 1.0
        self.sat = dist / c
        x, y = c + dx, c + dy
        self.canvas.coords(self.marker, x - 7, y - 7, x + 7, y + 7)
        self.canvas.coords(self.marker_in, x - 5, y - 5, x + 5, y + 5)
        self._color_changed()

    def _current_rgb(self) -> tuple[int, int, int]:
        return hsv_to_rgb255(self.hue, self.sat, self.bright.get() / 100.0)

    def _color_changed(self, send: bool = True) -> None:
        rgb = self._current_rgb()
        self._show_rgb(rgb)
        if send:
            self.runner.loop.call_soon_threadsafe(self._queue_color, rgb)

    def _show_rgb(self, rgb: tuple[int, int, int]) -> None:
        hx = "#%02x%02x%02x" % rgb
        self.swatch.configure(bg=hx)
        self.hex_var.set(hx)

    # ------------------------------------------------ selección de animación
    def _toggle_anim(self, key: str) -> None:
        if self.active_anim == key:
            self._stop_anim_ui()
            return
        self._set_active(key)
        self.runner.loop.call_soon_threadsafe(self._start_anim, key)

    def _stop_anim_ui(self) -> None:
        if self.active_anim is None:
            return
        self._set_active(None)
        self.runner.loop.call_soon_threadsafe(self._cancel_anim)

    def _set_active(self, key: Optional[str]) -> None:
        self.active_anim = key
        for k, tile in self.anim_buttons.items():
            on = k == key
            tile.configure(highlightbackground=tile._color if on else CARD,  # type: ignore[attr-defined]
                           bg=CARD_HI if on else CARD)
            tile._lbl.configure(bg=CARD_HI if on else CARD)  # type: ignore[attr-defined]

    def _pulse(self) -> None:
        """Pequeño latido en el punto de estado."""
        self._pulse_phase += 0.25
        live = self.status_var.get().startswith("Conectado")
        base = GREEN if live else MUTED
        if self.active_anim:
            base = ORANGE
        self.dot.itemconfigure(self.dot_item, fill=base)
        self.root.after(200, self._pulse)

    # --------------------------------------------- mensajes hacia la UI (thread-safe)
    def log(self, msg: str) -> None:
        self.ui_q.put(("log", msg))

    def set_status(self, msg: str) -> None:
        self.ui_q.put(("status", msg))

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.ui_q.get_nowait()
                if kind == "log":
                    self.log_box.configure(state="normal")
                    self.log_box.insert("end", f"{payload}\n")
                    self.log_box.see("end")
                    self.log_box.configure(state="disabled")
                elif kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "mac":
                    self.mac_var.set(f"MAC: {payload}")
                elif kind == "busy":
                    self._set_buttons_enabled(not payload)
                elif kind == "rgb":
                    self._show_rgb(payload)  # type: ignore[arg-type]
                elif kind == "anim_ended":
                    if self.active_anim == payload:
                        self._set_active(None)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _on_close(self) -> None:
        try:
            self.runner.submit(self._disconnect()).result(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        self.root.destroy()

    # --------------------------------------------------------------- botones
    def on_click_on(self) -> None:
        self._stop_anim_ui()
        self._run_action(self._do_power(True, False))

    def on_click_off(self) -> None:
        self._stop_anim_ui()
        self._run_action(self._do_power(False, self.soft_off.get()))

    def _run_action(self, coro) -> None:
        if self.busy:
            coro.close()
            return
        self.busy = True
        self.ui_q.put(("busy", True))

        async def wrapper():
            try:
                await coro
            except Exception as e:  # noqa: BLE001
                self.log(f"ERROR: {e!r}")
                self.set_status("Error (ver registro)")
            finally:
                self.busy = False
                self.ui_q.put(("busy", False))

        self.runner.submit(wrapper())

    # ------------------------------------------------- lógica BLE (hilo asyncio)
    @property
    def connected(self) -> bool:
        return self.client is not None and self.client.is_connected

    def _on_disconnect(self, _client) -> None:
        self.client = None
        self.set_status("Desconectado")
        self.log("Se perdió la conexión.")

    async def _find_device(self):
        """Escanea hasta encontrar un dispositivo cuyo nombre empiece con DMRRBA.
        Si hay varios, elige el de mejor señal."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + FIND_TIMEOUT
        attempt = 0
        while True:
            attempt += 1
            self.set_status(f"Buscando luces... (intento {attempt})")
            found = await BleakScanner.discover(timeout=SCAN_SECONDS, return_adv=True)
            cands = []
            for dev, adv in found.values():
                name = adv.local_name or dev.name or ""
                if name.lower().startswith(TARGET_PREFIX):
                    cands.append((adv.rssi, dev, name))
            if cands:
                cands.sort(key=lambda t: t[0], reverse=True)
                rssi, dev, name = cands[0]
                if len(cands) > 1:
                    self.log(f"{len(cands)} controladores cerca; uso el de mejor señal.")
                self.log(f"Encontrado {name} ({dev.address}, {rssi} dBm)")
                return dev
            if loop.time() >= deadline:
                raise RuntimeError(
                    "No se encontró ningún DMRRBA. ¿Luces con corriente? "
                    "¿illumiHome cerrada en el teléfono? Si las apagaste, espera unos segundos.")

    async def _ensure_connected(self) -> None:
        if self.connected:
            return
        dev = None
        if self.address:  # ya la conocemos: intenta directo antes de escanear
            self.set_status("Reconectando...")
            dev = await BleakScanner.find_device_by_address(self.address, timeout=SCAN_SECONDS)
        if dev is None:
            dev = await self._find_device()
        self.address = dev.address
        self.ui_q.put(("mac", dev.address))

        self.set_status("Conectando...")
        client = BleakClient(dev, timeout=20, disconnected_callback=self._on_disconnect)
        try:
            await client.connect()
        except Exception:
            self.log("Primer intento de conexión falló; reintentando...")
            await asyncio.sleep(2)
            await client.connect()
        self.client = client
        await subscribe_all(client, self.log)
        self.profile = detect_profile(client) or find_profile("dmrrba")
        self.log(f"Conectado. Protocolo: {self.profile.name}")

    async def _do_power(self, on: bool, soft: bool) -> None:
        await self._ensure_connected()
        p = self.profile
        assert self.client is not None and p is not None
        if on:
            await write(self.client, p.write_char, p.on[0], self.log)
            self.set_status("Conectado · ON")
            # vuelve a aplicar el color elegido por si el controlador no lo recuerda
            await asyncio.sleep(0.15)
            await write(self.client, p.write_char, p.color(*self._last_rgb), self.log)
        elif soft and p.color:
            await write(self.client, p.write_char, p.color(0, 0, 0), self.log)
            self.set_status("Conectado · OFF suave")
        else:
            await write(self.client, p.write_char, p.off[0], self.log)
            self.set_status("Conectado · OFF")

    # ------------------------------------------------ color (limitado en frecuencia)
    def _queue_color(self, rgb: tuple[int, int, int]) -> None:
        """Se ejecuta en el hilo asyncio. Guarda el último color y lanza un
        único enviador que manda solo el valor más reciente (~12 envíos/s)."""
        self._cancel_anim()
        self._last_rgb = rgb
        self._pending_rgb = rgb
        if self._sender_task is None or self._sender_task.done():
            self._sender_task = asyncio.ensure_future(self._send_loop())

    async def _send_loop(self) -> None:
        try:
            while self._pending_rgb is not None:
                rgb, self._pending_rgb = self._pending_rgb, None
                if not self.connected or self.profile is None or self.profile.color is None:
                    self.log("Presiona ENCENDER para conectar antes de elegir color.")
                    self._pending_rgb = None
                    return
                await write(self.client, self.profile.write_char,
                            self.profile.color(*rgb), None)
                await asyncio.sleep(0.08)
        except Exception as e:  # noqa: BLE001
            self.log(f"ERROR enviando color: {e!r}")

    # ------------------------------------------------ animaciones (hilo asyncio)
    def _cancel_anim(self) -> None:
        if self._anim_task is not None and not self._anim_task.done():
            self._anim_task.cancel()
        self._anim_task = None

    def _start_anim(self, key: str) -> None:
        self._cancel_anim()
        self._pending_rgb = None
        self._anim_task = asyncio.ensure_future(self._run_anim(key))

    async def _run_anim(self, key: str) -> None:
        factory = next(fn for k, _n, _c, fn in ANIMATIONS if k == key)
        try:
            await self._ensure_connected()
            p = self.profile
            assert self.client is not None and p is not None and p.color is not None
            await write(self.client, p.write_char, p.on[0], None)
            await asyncio.sleep(0.15)
            self.set_status("Conectado · Animación")
            self.log(f"Animación: {key}")
            frames = factory(lambda: self._speed)
            ui_tick = 0
            loop = asyncio.get_running_loop()
            for color in frames:
                t0 = loop.time()
                k = self._bright
                rgb = tuple(max(0, min(255, round(max(0.0, min(1.0, c)) ** GAMMA * k * 255)))
                        for c in color)
                if not any(rgb):   # algunos controladores ignoran (0,0,0): usa el rojo más bajo
                    rgb = (1, 0, 0) if color[0] > 0 or color[1] + color[2] == 0 else (0, 0, 1)
                if not self.connected:
                    self.log("Conexión perdida; animación detenida.")
                    return
                await write(self.client, p.write_char, p.color(*rgb), None)
                self._last_rgb = rgb  # type: ignore[assignment]
                ui_tick += 1
                if ui_tick % 3 == 0:
                    self.ui_q.put(("rgb", rgb))
                await asyncio.sleep(max(0.0, FRAME_DT - (loop.time() - t0)))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.log(f"ERROR en animación: {e!r}")
            self.set_status("Error (ver registro)")
        finally:
            self.ui_q.put(("anim_ended", key))

    async def _disconnect(self) -> None:
        self._cancel_anim()
        if self.client is not None and self.client.is_connected:
            await self.client.disconnect()
        self.client = None


def main() -> None:
    root = tk.Tk()
    LedApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
