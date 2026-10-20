"""Panel local de Halloween para focos WiZ (control directo por UDP, sin la app).

Uso:  python server.py      ->  abre http://localhost:8765
Solo usa la libreria estandar de Python 3.8+.
"""
import json
import math
import os
import random
import socket
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SEQ_FILE = os.path.join(HERE, "secuencias.json")
PORT = 8765
WIZ_PORT = 38899

BULBS = [
    {"ip": "192.168.1.95", "name": "Foco 1", "room": "Sala fiesta"},
    {"ip": "192.168.1.96", "name": "Foco 2", "room": "Sala fiesta"},
    {"ip": "192.168.1.98", "name": "Foco 3", "room": "Otro cuarto"},
]
IDX = {b["ip"]: i for i, b in enumerate(BULBS)}

# ---------------------------------------------------------------- UDP

_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def send(ip, params):
    msg = json.dumps({"method": "setPilot", "params": params}).encode()
    try:
        _sock.sendto(msg, (ip, WIZ_PORT))
    except OSError:
        pass


def get_pilot(ip):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.8)
    try:
        s.sendto(b'{"method":"getPilot","params":{}}', (ip, WIZ_PORT))
        data, _ = s.recvfrom(2048)
        return json.loads(data).get("result")
    except Exception:
        return None
    finally:
        s.close()


def clamp_dim(d):
    return max(10, min(100, int(round(d))))


# ---------------------------------------------------------------- catalogo

SCENES = {
    "calabaza": {"name": "Calabaza", "colors": [(255, 85, 0)], "dim": 100},
    "bruja": {"name": "Bruja morada", "colors": [(140, 0, 255)], "dim": 100},
    "toxico": {"name": "Veneno toxico", "colors": [(0, 255, 30)], "dim": 100},
    "sangre": {"name": "Sangre", "colors": [(255, 0, 0)], "dim": 100},
    "fantasma": {"name": "Fantasma", "colors": [(90, 180, 255)], "dim": 45},
    "noche": {"name": "Noche oscura", "colors": [(25, 0, 90)], "dim": 20},
    "mix": {"name": "Mezcla Halloween", "colors": [(255, 85, 0), (140, 0, 255), (0, 255, 30)], "dim": 100},
    "zombie": {"name": "Zombie", "colors": [(0, 255, 30), (255, 0, 0)], "dim": 80},
    "bruja_calabaza": {"name": "Bruja + Calabaza", "colors": [(140, 0, 255), (255, 85, 0)], "dim": 100},
    "neon_rosa": {"name": "Neon rosa", "colors": [(255, 0, 140)], "dim": 100},
    "neon_cian": {"name": "Neon cian", "colors": [(0, 255, 255)], "dim": 100},
    "neon": {"name": "Neon mix", "colors": [(255, 0, 140), (0, 255, 255), (140, 0, 255)], "dim": 100},
    "luz_negra": {"name": "Luz negra (blanco fosforescente)", "colors": [(70, 0, 255)], "dim": 100},
    "calido": {"name": "Luz normal", "temp": 2700, "dim": 100},
    "apagar": {"name": "Apagar", "off": True},
}

WIZ_SCENES = {
    28: "Halloween (WiZ)",
    5: "Chimenea",
    29: "Velas (WiZ)",
    26: "Club",
    31: "Pulso",
    4: "Fiesta",
    23: "Mar profundo",
    24: "Jungla",
}


# ---------------------------------------------------------------- motor

class Stop(Exception):
    pass


class StepEnd(Exception):
    pass


LOCK = threading.Lock()
RUNNERS = []


class Runner:
    """Un efecto o secuencia corriendo sobre un conjunto de focos."""

    def __init__(self, ips, label, bright, speed):
        self._ips = set(ips)
        self.label = label
        self.bright = bright
        self.speed = max(0.2, speed)
        self.deadline = None
        self.stop_ev = threading.Event()
        self.t0 = time.time()

    def T(self):
        """Reloj de la escena (segundos), ya escalado por la velocidad."""
        return (time.time() - self.t0) * self.speed

    def ips(self):
        with LOCK:
            out = sorted(self._ips, key=IDX.get)
        if not out:
            raise Stop
        return out

    def sleep(self, t):
        t = max(0.0, t / self.speed)
        if self.deadline is not None:
            rem = self.deadline - time.time()
            if t >= rem:
                if self.stop_ev.wait(max(0.0, rem)):
                    raise Stop
                raise StepEnd
        if self.stop_ev.wait(t):
            raise Stop
        self.ips()

    def rgb(self, ip, r, g, b, d=100):
        send(ip, {"state": True, "r": int(r), "g": int(g), "b": int(b),
                  "dimming": clamp_dim(d * self.bright / 100)})

    def run(self, fn):
        try:
            fn(self)
        except Stop:
            pass
        except Exception:
            traceback.print_exc()
        finally:
            with LOCK:
                if self in RUNNERS:
                    RUNNERS.remove(self)


def claim(ips):
    """Quita estos focos de cualquier efecto/secuencia que los este usando."""
    ips = set(ips)
    with LOCK:
        for r in RUNNERS:
            r._ips -= ips
            if not r._ips:
                r.stop_ev.set()
        RUNNERS[:] = [r for r in RUNNERS if r._ips]


def start_runner(ips, label, fn, bright, speed):
    claim(ips)
    r = Runner(ips, label, bright, speed)
    with LOCK:
        RUNNERS.append(r)
    threading.Thread(target=r.run, args=(fn,), daemon=True).start()
    return r


# ---------------------------------------------------------------- efectos

def lerp(a, b, f):
    return tuple(a[i] + (b[i] - a[i]) * f for i in range(3))


def fx_relampago(c):
    while True:
        for ip in c.ips():
            c.rgb(ip, 15, 15, 90, 12)
        c.sleep(random.uniform(1.5, 5))
        ips = c.ips()
        hit = ips if random.random() < 0.6 else [random.choice(ips)]
        for _ in range(random.choice((1, 2, 2, 3))):
            for ip in hit:
                c.rgb(ip, 255, 255, 255, 100)
            c.sleep(random.uniform(0.05, 0.12))
            for ip in hit:
                c.rgb(ip, 30, 30, 110, 15)
            c.sleep(random.uniform(0.06, 0.25))


def fx_vela(c):
    while True:
        for ip in c.ips():
            c.rgb(ip, 255, random.randint(70, 120), 0, random.randint(30, 80))
        c.sleep(random.uniform(0.08, 0.2))


def fx_latido(c):
    while True:
        for d, t in ((100, 0.12), (20, 0.15), (80, 0.12), (12, 0.9)):
            for ip in c.ips():
                c.rgb(ip, 255, 0, 0, d)
            c.sleep(t)


def fx_estrobo(c):
    on = False
    while True:
        on = not on
        for ip in c.ips():
            if on:
                c.rgb(ip, 255, 255, 255, 100)
            else:
                c.rgb(ip, 0, 0, 40, 10)
        c.sleep(0.09)


def fx_aura(c):
    pal = [(140, 0, 255), (0, 255, 30), (255, 80, 0)]
    t0 = time.time()
    while True:
        x0 = (time.time() - t0) * c.speed / 4
        for ip in c.ips():
            x = x0 + IDX[ip] * 0.66
            i, f = int(x) % 3, x - int(x)
            c.rgb(ip, *lerp(pal[i], pal[(i + 1) % 3], f), 85)
        c.sleep(0.25)


def fx_toxico(c):
    t0 = time.time()
    while True:
        ph = (time.time() - t0) * c.speed * 2 * math.pi / 4
        for ip in c.ips():
            d = 10 + 90 * (0.5 - 0.5 * math.cos(ph + IDX[ip]))
            c.rgb(ip, 30, 255, 0, d)
        c.sleep(0.2)


def fx_susto(c):
    while True:
        for ip in c.ips():
            c.rgb(ip, 120, 0, 0, 10)
        c.sleep(random.uniform(5, 10))
        for k in range(18):
            for ip in c.ips():
                if k % 2:
                    c.rgb(ip, 255, 0, 0, 100)
                else:
                    c.rgb(ip, 255, 255, 255, 100)
            c.sleep(0.08)


def fx_poltergeist(c):
    cols = [(0, 255, 30), (140, 0, 255), (255, 85, 0), (255, 255, 255)]
    while True:
        for ip in c.ips():
            x = random.random()
            if x < 0.6:
                c.rgb(ip, 0, 120, 20, 20)
            elif x < 0.8:
                c.rgb(ip, 0, 0, 30, 10)
            else:
                c.rgb(ip, *random.choice(cols), 100)
        c.sleep(random.uniform(0.07, 0.35))


def fx_maldito(c):
    pal = [(255, 85, 0), (140, 0, 255), (0, 255, 30), (255, 0, 0)]
    k = 0
    while True:
        for ip in c.ips():
            c.rgb(ip, *pal[(k + IDX[ip]) % len(pal)], 100)
        k += 1
        c.sleep(0.6)


EFFECTS = {
    "relampago": ("Relampagos", fx_relampago),
    "vela": ("Vela parpadeante", fx_vela),
    "latido": ("Latido", fx_latido),
    "aura": ("Aura embrujada", fx_aura),
    "toxico": ("Respiracion toxica", fx_toxico),
    "poltergeist": ("Poltergeist", fx_poltergeist),
    "maldito": ("Arcoiris maldito", fx_maldito),
    "susto": ("Susto", fx_susto),
    "estrobo": ("Estrobo", fx_estrobo),
}


# ---------------------------------------------------------------- escenas combinadas
# Cada foco tiene un "papel" segun su posicion entre los focos elegidos (1.o, 2.o, 3.o).
# Con 2 focos la escena se ve completa con los dos primeros papeles; con 1 solo hace el primero.
# Todos comparten el mismo reloj y los mismos eventos, asi se ven como una sola escena.

def paint(c, fn):
    """fn(k, n) -> (r, g, b, dim) para el foco numero k de n."""
    ips = c.ips()
    for k, ip in enumerate(ips):
        c.rgb(ip, *fn(k, len(ips)))


def ambient(c, secs, look, dt=0.15):
    """Mantiene el ambiente `secs` segundos de escena; look(k, n, T) -> (r, g, b, dim)."""
    end = c.T() + secs
    while c.T() < end:
        T = c.T()
        paint(c, lambda k, n: look(k, n, T))
        c.sleep(dt)


def combo_tormenta(c):
    def look(k, n, T):
        role = k % 3
        if role == 0:
            return 20, 30, 130, 14 + 6 * math.sin(T * 1.3)
        if role == 1:
            return 50, 10, 100, 10 + 5 * math.sin(T * 0.7 + 1)
        return 0, 70, 110, random.randint(8, 22)

    while True:
        ambient(c, random.uniform(2.5, 7), look)
        src = random.randrange(3)  # el rayo cae en un foco y los demas reflejan el destello
        for _ in range(random.choice((1, 2, 3))):
            paint(c, lambda k, n: (255, 255, 255, 100) if k == src % n else (25, 30, 110, 12))
            c.sleep(0.05)
            paint(c, lambda k, n: (255, 255, 255, 100) if k == src % n else (190, 210, 255, 60))
            c.sleep(0.08)
            paint(c, lambda k, n: (25, 30, 110, 10))
            c.sleep(random.uniform(0.07, 0.2))
        ambient(c, 1.4, lambda k, n, T: (80, 40, 180, 30 + 20 * math.sin(T * 14 + k)), dt=0.1)


def combo_caldero(c):
    def look(k, n, T):
        role = k % 3
        if role == 0:  # magia
            return 140, 0, 255, 55 + 40 * math.sin(T * 1.1)
        if role == 1:  # caldero burbujeando
            d = random.randint(25, 60) if random.random() < 0.7 else random.randint(70, 100)
            return 0, 255, 30, d
        return 255, random.randint(60, 110), 0, random.randint(35, 80)  # fuego

    while True:
        ambient(c, random.uniform(7, 14), look)
        # hechizo: la magia recorre los focos y todo estalla junto
        for i in range(len(c.ips())):
            paint(c, lambda k, n: (200, 100, 255, 100) if k == i else (60, 0, 110, 25))
            c.sleep(0.22)
        paint(c, lambda k, n: (255, 255, 255, 100))
        c.sleep(0.12)
        paint(c, lambda k, n: (0, 255, 30, 100))
        c.sleep(0.25)


def combo_fantasma(c):
    base = (70, 0, 25)
    ghost = (150, 200, 255)

    def casa(k, n, T):
        return base[0], base[1], base[2], 8 + 4 * math.sin(T * 0.8 + k)

    while True:
        ambient(c, random.uniform(4, 9), casa)
        n = len(c.ips())
        rev = random.random() < 0.5
        steps = 36
        for i in range(steps + 1):
            pos = -1 + (i / steps) * (n + 1)  # el fantasma cruza de un foco al siguiente
            if rev:
                pos = (n - 1) - pos

            def look(k, n, pos=pos):
                g = max(0.0, 1 - abs(k - pos))
                return (*lerp(base, ghost, g), 8 + 80 * g)

            paint(c, look)
            c.sleep(0.1)


def combo_monstruo(c):
    def look(k, n, T):
        role = k % 3
        if role == 0:  # corazon
            p = T % 1.2
            d = 100 if p < 0.12 else 25 if p < 0.24 else 80 if p < 0.36 else 12
            return 255, 0, 0, d
        if role == 1:  # respiracion
            return 20, 255, 40, 10 + 60 * (0.5 - 0.5 * math.cos(T * 2 * math.pi / 4.8))
        return 255, 150, 0, 5 if (T % 7) < 0.25 else 70  # ojos que parpadean

    while True:
        ambient(c, random.uniform(10, 18), look, dt=0.1)
        for _ in range(5):  # rugido
            paint(c, lambda k, n: (255, 0, 0, 100))
            c.sleep(0.1)
            paint(c, lambda k, n: (80, 0, 0, 30))
            c.sleep(0.1)


def combo_calabaza(c):
    pal = [(255, 85, 0), (140, 0, 255), (0, 255, 30), (255, 0, 0), (255, 255, 255)]

    def look(k, n, T):
        role = k % 3
        if role == 0:  # la calabaza con vela
            return 255, random.randint(55, 100), 0, random.randint(45, 90)
        if role == 1:
            return 140, 0, 255, 22 + 12 * math.sin(T * 0.9)
        return 0, 255, 30, 15 + 10 * math.sin(T * 0.9 + math.pi)  # en contrafase con el morado

    while True:
        ambient(c, random.uniform(9, 18), look, dt=0.12)
        # mirada: la calabaza se pone roja y el resto se apaga
        ambient(c, 1.3, lambda k, n, T: (255, 0, 0, 100) if k % 3 == 0 else (30, 0, 0, 10), dt=0.1)
        for _ in range(8):  # carcajada
            col = random.choice(pal)
            paint(c, lambda k, n: (*col, 100))
            c.sleep(0.1)


def combo_ronda(c):
    pal = [(255, 85, 0), (140, 0, 255), (0, 255, 30), (255, 0, 0)]
    while True:
        T = c.T()

        def look(k, n):
            pos = (T * 1.1) % n
            d = abs(k - pos)
            g = max(0.0, 1 - min(d, n - d))
            col = pal[int(T * 1.1 / n) % len(pal)]
            return (*lerp((20, 0, 40), col, g), 10 + 90 * g)

        paint(c, look)
        c.sleep(0.1)


COMBOS = {
    "tormenta": ("Tormenta eléctrica", combo_tormenta,
                 "1.º cielo con rayos · 2.º nubes moradas · 3.º lluvia fría. El rayo cae en un foco y los demás reflejan el destello."),
    "caldero": ("Caldero de la bruja", combo_caldero,
                "1.º magia morada · 2.º caldero verde burbujeando · 3.º fuego. De vez en cuando lanza un hechizo que recorre todos los focos."),
    "fantasma": ("Fantasma que pasa", combo_fantasma,
                 "Casa en penumbra roja; un fantasma azul cruza de un foco al siguiente."),
    "monstruo": ("Monstruo respirando", combo_monstruo,
                 "1.º corazón rojo latiendo · 2.º respiración verde · 3.º ojos ámbar. De pronto ruge y todo se pone rojo."),
    "calabaza": ("Calabaza embrujada", combo_calabaza,
                 "1.º calabaza con vela · 2.º y 3.º aura morada/verde en contrafase. Ocasionalmente la calabaza te mira en rojo y se oye una carcajada."),
    "ronda": ("Ronda maldita", combo_ronda,
              "Una luz gira de foco en foco cambiando de color; los demás quedan en penumbra."),
}


# ---------------------------------------------------------------- acciones

def anim_fn(a):
    """Funcion animada de una accion (efecto o combinada); None si es estatica."""
    k = a.get("kind")
    if k == "effect":
        return EFFECTS[a["id"]][1]
    if k == "combo":
        return COMBOS[a["id"]][1]
    return None


def action_label(a):
    k, i = a.get("kind"), a.get("id")
    if k == "scene" and i in SCENES:
        return SCENES[i]["name"]
    if k == "effect" and i in EFFECTS:
        return EFFECTS[i][0]
    if k == "combo" and i in COMBOS:
        return COMBOS[i][0]
    if k == "wiz":
        return WIZ_SCENES.get(int(i), "WiZ %s" % i)
    if k == "color":
        return "Color %s" % a.get("color")
    return "?"


def apply_static(ips, a, bright, speed):
    k = a.get("kind")
    for ip in ips:
        if k == "scene":
            s = SCENES[a["id"]]
            if s.get("off"):
                p = {"state": False}
            elif "temp" in s:
                p = {"state": True, "temp": s["temp"], "dimming": clamp_dim(s["dim"] * bright / 100)}
            else:
                r, g, b = s["colors"][IDX[ip] % len(s["colors"])]
                p = {"state": True, "r": r, "g": g, "b": b, "dimming": clamp_dim(s["dim"] * bright / 100)}
        elif k == "wiz":
            p = {"state": True, "sceneId": int(a["id"]), "dimming": clamp_dim(bright),
                 "speed": max(20, min(200, int(100 * speed)))}
        elif k == "color":
            r, g, b = a["color"]
            p = {"state": True, "r": int(r), "g": int(g), "b": int(b), "dimming": clamp_dim(bright)}
        else:
            raise ValueError("accion invalida")
        send(ip, p)
        send(ip, p)  # UDP: segundo envio por si se pierde el primero


def run_action(r, a):
    fn = anim_fn(a)
    if fn:
        fn(r)
    else:
        apply_static(r.ips(), a, r.bright, r.speed)
        while True:
            r.sleep(3600)


def do_apply(ips, a, bright, speed):
    fn = anim_fn(a)
    if fn:
        start_runner(ips, action_label(a), fn, bright, speed)
    else:
        claim(ips)
        apply_static(ips, a, bright, speed)


def do_sequence(ips, name, steps, loop, bright, speed):
    steps = [s for s in steps if float(s.get("duration", 0)) > 0]
    if not steps:
        raise ValueError("la secuencia no tiene pasos")

    def fn(r):
        while True:
            for i, st in enumerate(steps):
                r.label = "%s · paso %d/%d · %s" % (name, i + 1, len(steps), action_label(st))
                r.deadline = time.time() + float(st["duration"])
                try:
                    run_action(r, st)
                except StepEnd:
                    pass
            if not loop:
                return

    start_runner(ips, name, fn, bright, speed)


# ---------------------------------------------------------------- secuencias guardadas

DEFAULT_SEQS = [
    {"name": "Noche de terror", "loop": True, "steps": [
        {"kind": "scene", "id": "mix", "duration": 120},
        {"kind": "effect", "id": "relampago", "duration": 45},
        {"kind": "effect", "id": "aura", "duration": 120},
        {"kind": "effect", "id": "latido", "duration": 20},
        {"kind": "effect", "id": "susto", "duration": 15},
        {"kind": "wiz", "id": 28, "duration": 120},
    ]},
    {"name": "Cuarto tranquilo", "loop": True, "steps": [
        {"kind": "effect", "id": "vela", "duration": 300},
        {"kind": "scene", "id": "bruja", "duration": 180},
    ]},
]


def load_seqs():
    try:
        with open(SEQ_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return DEFAULT_SEQS


def save_seqs(seqs):
    with open(SEQ_FILE, "w", encoding="utf-8") as f:
        json.dump(seqs, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- HTTP

def status():
    res = {}
    threads = [threading.Thread(target=lambda ip=b["ip"]: res.__setitem__(ip, get_pilot(ip))) for b in BULBS]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with LOCK:
        running = {ip: r.label for r in RUNNERS for ip in r._ips}
    return [dict(b, online=res[b["ip"]] is not None, pilot=res[b["ip"]], running=running.get(b["ip"]))
            for b in BULBS]


def catalog():
    return {
        "bulbs": BULBS,
        "scenes": [{"id": k, "name": v["name"], "colors": v.get("colors"), "temp": v.get("temp"),
                    "off": v.get("off", False)} for k, v in SCENES.items()],
        "effects": [{"id": k, "name": v[0]} for k, v in EFFECTS.items()],
        "combos": [{"id": k, "name": v[0], "desc": v[2]} for k, v in COMBOS.items()],
        "wiz": [{"id": k, "name": v} for k, v in WIZ_SCENES.items()],
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/api/catalog":
            self._json(catalog())
        elif self.path == "/api/status":
            self._json(status())
        elif self.path == "/api/sequences":
            self._json(load_seqs())
        else:
            self._json({"error": "no encontrado"}, 404)

    def do_POST(self):
        try:
            b = self._body()
            ips = [ip for ip in b.get("targets", []) if ip in IDX]
            bright = float(b.get("bright", 100))
            speed = float(b.get("speed", 1))
            if self.path == "/api/sequences":
                save_seqs(b["sequences"])
                return self._json({"ok": True})
            if not ips:
                return self._json({"error": "elige al menos un foco"}, 400)
            if self.path == "/api/apply":
                do_apply(ips, b["action"], bright, speed)
            elif self.path == "/api/sequence/run":
                do_sequence(ips, b.get("name") or "Secuencia", b["steps"], bool(b.get("loop")), bright, speed)
            elif self.path == "/api/stop":
                claim(ips)
            else:
                return self._json({"error": "no encontrado"}, 404)
            self._json({"ok": True})
        except Exception as e:
            traceback.print_exc()
            self._json({"error": str(e)}, 400)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.1.1", 1))
        return s.getsockname()[0]
    except OSError:
        return "?"
    finally:
        s.close()


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("Panel Halloween listo:")
    print("  En esta PC:     http://localhost:%d" % PORT)
    print("  Desde el cel:   http://%s:%d  (misma red WiFi)" % (lan_ip(), PORT))
    print("Ctrl+C para salir.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
