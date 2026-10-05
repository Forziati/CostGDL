"""Planta, alzados y vista isométrica (SVG) a partir de un layout JSON, y renders con Gemini.

El layout lo propone Claude a partir del presupuesto; aquí solo se valida y se dibuja a escala.
Convenciones (metros): x crece al Este, y crece al Sur; el origen es la esquina NO del espacio.
  muro N: y=0 · muro S: y=ancho_y · muro O: x=0 · muro E: x=ancho_x
  aperturas: "pos" = distancia al inicio del muro (N/S desde el Oeste, E/O desde el Norte).
"""

import base64
import colorsys
import json
import math
import re
from xml.sax.saxutils import escape

import requests

LAYOUT_PROMPT = """\
Con el presupuesto y las decisiones de esta conversación, define la distribución de cada espacio
remodelado para dibujar planta, alzados y vista isométrica a escala. Responde SOLO con un bloque
```json (JSON válido, sin comentarios) con este esquema (metros):

{"espacios": [{
  "nombre": "Cocina",
  "ancho": 4.0,            // dimensión Este-Oeste
  "largo": 4.0,            // dimensión Norte-Sur
  "alto": 2.6,
  "piso": {"material": "Porcelanato gris 60x60", "color": "#B9B6B0"},
  "muro_color": "#F4E9D8",
  "aperturas": [{"tipo": "puerta" | "ventana", "muro": "N"|"S"|"E"|"O", "pos": 0.5,
                 "ancho": 0.9, "alto": 2.1, "antepecho": 0.0}],
  "elementos": [{"etiqueta": "Isla", "tipo": "isla", "x": 1.0, "y": 1.5, "w": 2.0, "d": 0.9,
                 "z": 0.0, "h": 0.9, "color": "#C9B79C"}],
  "renders": [{"titulo": "Vista general", "prompt": "<prompt en inglés para un generador de imágenes>"}]
}]}

Reglas:
- x,y = esquina Noroeste de la huella del elemento; w a lo largo de x, d a lo largo de y; z = altura
  desde el piso; h = alto del elemento. Todo dentro de [0,ancho]x[0,largo]; sin traslapes.
- Respeta las dimensiones y los muros demolidos del presupuesto; deja pasillos de >= 0.9 m
  (>= 1.0 m alrededor de una isla).
- Elementos pegados a un muro deben tocarlo (x=0, y=0, x+w=ancho o y+d=largo) para salir en su alzado.
- Alturas típicas: gabinete bajo h=0.9; alacena z=1.4 h=0.7; refrigerador h=1.8; isla h=0.9;
  sanitario h=0.4; regadera/mampara h=2.0; lavabo h=0.85.
- tipo ∈ gabinete_bajo, alacena, isla, electrodomestico, mueble, sanitario, banqueta, otro.
- Usa los colores y materiales elegidos en el presupuesto (hex).
- "renders": 2 o 3 vistas; cada prompt describe en inglés el espacio con medidas, materiales,
  colores, iluminación y estilo (arquitectura mexicana contemporánea, fotografía realista de interiores).
"""

KIND_COLORS = {
    "gabinete_bajo": "#C9B79C", "alacena": "#D9CBB4", "isla": "#BFA98A", "electrodomestico": "#B8BEC4",
    "mueble": "#CDB89A", "sanitario": "#F3F3F0", "banqueta": "#8C6A4A", "otro": "#CFC6B8",
}


# ---------- validación ----------
def _f(v, default, lo=None, hi=None):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(v):
        return default
    if lo is not None:
        v = max(v, lo)
    if hi is not None:
        v = min(v, hi)
    return v


def _color(c, default):
    return c if isinstance(c, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", c) else default


def parse_layout(text):
    """Extrae y valida el JSON de layout. Devuelve (espacios, avisos). Lanza ValueError si no hay nada usable."""
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S) or re.search(r"(\{.*\})", text, re.S)
    if not m:
        raise ValueError("La respuesta no trae un layout JSON.")
    try:
        raw = json.loads(m.group(1))["espacios"]
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError(f"Layout JSON inválido: {e}")
    spaces, warns = [], []
    for s in raw:
        W, D = _f(s.get("ancho"), 0, 0.5, 30), _f(s.get("largo"), 0, 0.5, 30)
        if not W or not D:
            continue
        sp = {
            "nombre": str(s.get("nombre", "Espacio"))[:40],
            "ancho": W, "largo": D, "alto": _f(s.get("alto"), 2.6, 2.0, 5.0),
            "piso": {"material": str((s.get("piso") or {}).get("material", ""))[:60],
                     "color": _color((s.get("piso") or {}).get("color"), "#D8CDBB")},
            "muro_color": _color(s.get("muro_color"), "#F4E9D8"),
            "aperturas": [], "elementos": [], "renders": [],
        }
        for a in s.get("aperturas") or []:
            wall = str(a.get("muro", "")).upper()[:1]
            if wall not in "NSEO" or not wall:
                continue
            L = W if wall in "NS" else D
            w = _f(a.get("ancho"), 0.9, 0.3, L)
            sp["aperturas"].append({
                "tipo": "ventana" if str(a.get("tipo")).lower().startswith("v") else "puerta",
                "muro": wall, "pos": _f(a.get("pos"), 0, 0, L - w), "ancho": w,
                "alto": _f(a.get("alto"), 2.1, 0.3, sp["alto"]), "antepecho": _f(a.get("antepecho"), 0, 0, sp["alto"]),
            })
        for e in s.get("elementos") or []:
            w, d = _f(e.get("w"), 0, 0.05, W), _f(e.get("d"), 0, 0.05, D)
            if not w or not d:
                continue
            x, y = _f(e.get("x"), 0, 0, W - w), _f(e.get("y"), 0, 0, D - d)
            if (x, y) != (_f(e.get("x"), 0), _f(e.get("y"), 0)):
                warns.append(f"{sp['nombre']}: «{e.get('etiqueta')}» se ajustó para caber en el espacio.")
            kind = e.get("tipo") if e.get("tipo") in KIND_COLORS else "otro"
            z = _f(e.get("z"), 0, 0, sp["alto"])
            sp["elementos"].append({
                "etiqueta": str(e.get("etiqueta", ""))[:28], "tipo": kind, "x": x, "y": y, "w": w, "d": d,
                "z": z, "h": _f(e.get("h"), 0.9, 0.02, sp["alto"] - z),
                "color": _color(e.get("color"), KIND_COLORS[kind]),
            })
        els = sp["elementos"]
        for i, p in enumerate(els):  # traslapes en planta a la misma altura
            for q in els[i + 1:]:
                if p["x"] < q["x"] + q["w"] - .02 and q["x"] < p["x"] + p["w"] - .02 and \
                   p["y"] < q["y"] + q["d"] - .02 and q["y"] < p["y"] + p["d"] - .02 and \
                   p["z"] < q["z"] + q["h"] and q["z"] < p["z"] + p["h"]:
                    warns.append(f"{sp['nombre']}: «{p['etiqueta']}» y «{q['etiqueta']}» se traslapan; revisa la distribución.")
        for r in s.get("renders") or []:
            if r.get("prompt"):
                sp["renders"].append({"titulo": str(r.get("titulo", "Vista"))[:50], "prompt": str(r["prompt"])[:1500]})
        spaces.append(sp)
    if not spaces:
        raise ValueError("El layout no contiene espacios con medidas válidas.")
    return spaces, warns


# ---------- utilidades de dibujo ----------
def _shade(hex_color, k):
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    r, g, b = colorsys.hls_to_rgb(h, max(0, min(1, l * k)), s)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _svg(w, h, body, bg="#FFFDF8"):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.0f} {h:.0f}" width="{w:.0f}" height="{h:.0f}" '
            f'font-family="Nunito, Arial, sans-serif"><rect width="100%" height="100%" fill="{bg}"/>{body}</svg>')


def _text(x, y, s, size=12, anchor="middle", fill="#3A2A20", weight="400", rot=None):
    t = f' transform="rotate({rot} {x:.1f} {y:.1f})"' if rot else ""
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" text-anchor="{anchor}" fill="{fill}" '
            f'font-weight="{weight}"{t}>{escape(s)}</text>')


def _dim_h(x1, x2, y, label):
    return (f'<line x1="{x1:.1f}" y1="{y}" x2="{x2:.1f}" y2="{y}" stroke="#7A6350" stroke-width="1"/>'
            f'<line x1="{x1:.1f}" y1="{y - 5}" x2="{x1:.1f}" y2="{y + 5}" stroke="#7A6350"/>'
            f'<line x1="{x2:.1f}" y1="{y - 5}" x2="{x2:.1f}" y2="{y + 5}" stroke="#7A6350"/>'
            + _text((x1 + x2) / 2, y - 6, label, 12, fill="#7A6350"))


def _dim_v(y1, y2, x, label):
    return (f'<line x1="{x}" y1="{y1:.1f}" x2="{x}" y2="{y2:.1f}" stroke="#7A6350"/>'
            f'<line x1="{x - 5}" y1="{y1:.1f}" x2="{x + 5}" y2="{y1:.1f}" stroke="#7A6350"/>'
            f'<line x1="{x - 5}" y1="{y2:.1f}" x2="{x + 5}" y2="{y2:.1f}" stroke="#7A6350"/>'
            + _text(x - 8, (y1 + y2) / 2, label, 12, fill="#7A6350", rot=-90))


# ---------- planta ----------
def plan_svg(sp, px=110):
    W, D, T = sp["ancho"], sp["largo"], 0.14
    mx, my = 70, 60
    w, h = W * px + 2 * mx, D * px + 2 * my + 20
    o = [f'<rect x="{mx}" y="{my}" width="{W * px:.1f}" height="{D * px:.1f}" fill="{sp["piso"]["color"]}"/>']
    # baldosas sugeridas
    step = 0.6 * px
    for i in range(1, int(W * px / step) + 1):
        o.append(f'<line x1="{mx + i * step:.1f}" y1="{my}" x2="{mx + i * step:.1f}" y2="{my + D * px:.1f}" stroke="#00000012"/>')
    for j in range(1, int(D * px / step) + 1):
        o.append(f'<line x1="{mx}" y1="{my + j * step:.1f}" x2="{mx + W * px:.1f}" y2="{my + j * step:.1f}" stroke="#00000012"/>')
    for e in sorted(sp["elementos"], key=lambda e: e["z"]):
        ex, ey, ew, ed = mx + e["x"] * px, my + e["y"] * px, e["w"] * px, e["d"] * px
        dash = ' stroke-dasharray="5 3"' if e["z"] >= 1.0 else ""
        o.append(f'<rect x="{ex:.1f}" y="{ey:.1f}" width="{ew:.1f}" height="{ed:.1f}" fill="{e["color"]}" '
                 f'fill-opacity="{0.55 if e["z"] >= 1.0 else 0.95}" stroke="#5A4636" stroke-width="1.4"{dash}/>')
        size = max(8, min(12, ew / max(len(e["etiqueta"]), 1) * 1.7))
        o.append(_text(ex + ew / 2, ey + ed / 2 + size / 3, e["etiqueta"], size, fill="#3A2A20", weight="600"))
    # muros con huecos
    def wall_rects(wall):
        L = W if wall in "NS" else D
        gaps = sorted((a["pos"], a["pos"] + a["ancho"]) for a in sp["aperturas"] if a["muro"] == wall)
        segs, cur = [], 0.0
        for g0, g1 in gaps:
            if g0 > cur:
                segs.append((cur, g0))
            cur = max(cur, g1)
        if cur < L:
            segs.append((cur, L))
        return segs
    wc = "#7A5A44"
    for wall in "NSEO":
        for s0, s1 in wall_rects(wall):
            if wall == "N":
                r = (mx + s0 * px, my - T * px, (s1 - s0) * px, T * px)
            elif wall == "S":
                r = (mx + s0 * px, my + D * px, (s1 - s0) * px, T * px)
            elif wall == "O":
                r = (mx - T * px, my + s0 * px, T * px, (s1 - s0) * px)
            else:
                r = (mx + W * px, my + s0 * px, T * px, (s1 - s0) * px)
            o.append(f'<rect x="{r[0]:.1f}" y="{r[1]:.1f}" width="{r[2]:.1f}" height="{r[3]:.1f}" fill="{wc}"/>')
    o.append(f'<rect x="{mx - T * px:.1f}" y="{my - T * px:.1f}" width="{T * px:.1f}" height="{T * px:.1f}" fill="{wc}"/>'
             f'<rect x="{mx + W * px:.1f}" y="{my - T * px:.1f}" width="{T * px:.1f}" height="{T * px:.1f}" fill="{wc}"/>'
             f'<rect x="{mx - T * px:.1f}" y="{my + D * px:.1f}" width="{T * px:.1f}" height="{T * px:.1f}" fill="{wc}"/>'
             f'<rect x="{mx + W * px:.1f}" y="{my + D * px:.1f}" width="{T * px:.1f}" height="{T * px:.1f}" fill="{wc}"/>')
    for a in sp["aperturas"]:
        L0, L1 = a["pos"] * px, (a["pos"] + a["ancho"]) * px
        if a["tipo"] == "ventana":
            if a["muro"] in "NS":
                yy = my - T * px if a["muro"] == "N" else my + D * px
                o.append(f'<rect x="{mx + L0:.1f}" y="{yy:.1f}" width="{L1 - L0:.1f}" height="{T * px:.1f}" fill="#BFE3EA" stroke="#1F7F8C" stroke-width="1.5"/>')
            else:
                xx = mx - T * px if a["muro"] == "O" else mx + W * px
                o.append(f'<rect x="{xx:.1f}" y="{my + L0:.1f}" width="{T * px:.1f}" height="{L1 - L0:.1f}" fill="#BFE3EA" stroke="#1F7F8C" stroke-width="1.5"/>')
        else:  # puerta: hoja + arco de giro hacia el interior
            r = L1 - L0
            if a["muro"] == "N":
                x0, y0 = mx + L0, my
                o.append(f'<path d="M{x0:.1f},{y0:.1f} L{x0:.1f},{y0 + r:.1f} A{r:.1f},{r:.1f} 0 0 0 {x0 + r:.1f},{y0:.1f}" fill="#1F7F8C18" stroke="#1F7F8C" stroke-width="1.5"/>')
            elif a["muro"] == "S":
                x0, y0 = mx + L0, my + D * px
                o.append(f'<path d="M{x0:.1f},{y0:.1f} L{x0:.1f},{y0 - r:.1f} A{r:.1f},{r:.1f} 0 0 1 {x0 + r:.1f},{y0:.1f}" fill="#1F7F8C18" stroke="#1F7F8C" stroke-width="1.5"/>')
            elif a["muro"] == "O":
                x0, y0 = mx, my + L0
                o.append(f'<path d="M{x0:.1f},{y0:.1f} L{x0 + r:.1f},{y0:.1f} A{r:.1f},{r:.1f} 0 0 1 {x0:.1f},{y0 + r:.1f}" fill="#1F7F8C18" stroke="#1F7F8C" stroke-width="1.5"/>')
            else:
                x0, y0 = mx + W * px, my + L0
                o.append(f'<path d="M{x0:.1f},{y0:.1f} L{x0 - r:.1f},{y0:.1f} A{r:.1f},{r:.1f} 0 0 0 {x0:.1f},{y0 + r:.1f}" fill="#1F7F8C18" stroke="#1F7F8C" stroke-width="1.5"/>')
    o.append(_dim_h(mx, mx + W * px, my - 22, f"{W:.2f} m"))
    o.append(_dim_v(my, my + D * px, mx - 28, f"{D:.2f} m"))
    o.append(_text(w / 2, h - 12, f"Planta · {sp['nombre']} · {W * D:.1f} m² · piso: {sp['piso']['material'] or '—'}", 13, weight="700", fill="#A8431F"))
    o.append(_text(w - 30, 30, "N ↑", 14, weight="700", fill="#1F7F8C"))
    return _svg(w, h, "".join(o))


# ---------- alzados ----------
def _wall_view(sp, wall):
    """Elementos y aperturas proyectados sobre un muro, vistos desde dentro. Devuelve (largo, items, openings)."""
    W, D, tol = sp["ancho"], sp["largo"], 0.16
    L = W if wall in "NS" else D
    items = []
    for e in sp["elementos"]:
        if wall == "N" and e["y"] <= tol:
            a, b = e["x"], e["x"] + e["w"]
        elif wall == "S" and e["y"] + e["d"] >= D - tol:
            a, b = W - (e["x"] + e["w"]), W - e["x"]
        elif wall == "E" and e["x"] + e["w"] >= W - tol:
            a, b = e["y"], e["y"] + e["d"]
        elif wall == "O" and e["x"] <= tol:
            a, b = D - (e["y"] + e["d"]), D - e["y"]
        else:
            continue
        items.append((a, b, e))
    ops = []
    for p in sp["aperturas"]:
        if p["muro"] != wall:
            continue
        a = p["pos"] if wall in "NE" else L - (p["pos"] + p["ancho"])
        ops.append((a, a + p["ancho"], p))
    return L, items, ops


def elevation_svgs(sp, px=100):
    out = []
    names = {"N": "Muro Norte", "S": "Muro Sur", "E": "Muro Este", "O": "Muro Oeste"}
    for wall in "NSEO":
        L, items, ops = _wall_view(sp, wall)
        if not items and not ops:
            continue
        H, mx, my = sp["alto"], 70, 50
        w, h = L * px + 2 * mx, H * px + 2 * my + 40
        base = my + H * px
        o = [f'<rect x="{mx}" y="{my}" width="{L * px:.1f}" height="{H * px:.1f}" fill="{sp["muro_color"]}" stroke="#7A5A44" stroke-width="3"/>',
             f'<rect x="{mx}" y="{base:.1f}" width="{L * px:.1f}" height="10" fill="{sp["piso"]["color"]}" stroke="#7A5A44"/>']
        for a, b, p in ops:
            top = my + (H - p["antepecho"] - p["alto"]) * px
            hh = p["alto"] * px
            fill, stroke = ("#BFE3EA", "#1F7F8C") if p["tipo"] == "ventana" else ("#7A5A44", "#4A3626")
            o.append(f'<rect x="{mx + a * px:.1f}" y="{top:.1f}" width="{(b - a) * px:.1f}" height="{hh:.1f}" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
            if p["tipo"] == "ventana":
                o.append(f'<line x1="{mx + (a + b) / 2 * px:.1f}" y1="{top:.1f}" x2="{mx + (a + b) / 2 * px:.1f}" y2="{top + hh:.1f}" stroke="{stroke}" stroke-width="1.5"/>')
        for a, b, e in sorted(items, key=lambda t: t[2]["d"] if wall in "NS" else t[2]["w"], reverse=True):
            top = my + (H - e["z"] - e["h"]) * px
            o.append(f'<rect x="{mx + a * px:.1f}" y="{top:.1f}" width="{(b - a) * px:.1f}" height="{e["h"] * px:.1f}" fill="{e["color"]}" stroke="#5A4636" stroke-width="1.5"/>')
            size = max(8, min(12, (b - a) * px / max(len(e["etiqueta"]), 1) * 1.7))
            o.append(_text(mx + (a + b) / 2 * px, top + e["h"] * px / 2 + size / 3, e["etiqueta"], size, weight="600"))
        o.append(_dim_h(mx, mx + L * px, my - 18, f"{L:.2f} m"))
        o.append(_dim_v(my, base, mx - 24, f"{H:.2f} m"))
        o.append(_text(w / 2, h - 12, f"Alzado · {sp['nombre']} · {names[wall]} (visto desde dentro)", 13, weight="700", fill="#A8431F"))
        out.append((names[wall], _svg(w, h, "".join(o))))
    return out


# ---------- isométrica ----------
def iso_svg(sp, px=70):
    W, D, H = sp["ancho"], sp["largo"], sp["alto"]
    c, s = math.cos(math.radians(30)), 0.5

    def P(x, y, z):
        return ((x - y) * c * px, (x + y) * s * px - z * px)

    def poly(pts, fill, stroke="#4A3626", sw=1.2, op=1):
        d = " ".join(f"{P(*p)[0]:.1f},{P(*p)[1]:.1f}" for p in pts)
        return f'<polygon points="{d}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}" fill-opacity="{op}" stroke-linejoin="round"/>'

    body = [poly([(0, 0, 0), (W, 0, 0), (W, D, 0), (0, D, 0)], sp["piso"]["color"])]
    mc = sp["muro_color"]
    body.append(poly([(0, 0, 0), (W, 0, 0), (W, 0, H), (0, 0, H)], _shade(mc, 0.95)))          # muro N
    body.append(poly([(0, 0, 0), (0, D, 0), (0, D, H), (0, 0, H)], _shade(mc, 0.82)))          # muro O
    for p in sp["aperturas"]:
        a, b, z0, z1 = p["pos"], p["pos"] + p["ancho"], p["antepecho"], p["antepecho"] + p["alto"]
        fill = "#BFE3EA" if p["tipo"] == "ventana" else "#7A5A44"
        if p["muro"] == "N":
            body.append(poly([(a, 0, z0), (b, 0, z0), (b, 0, z1), (a, 0, z1)], fill, "#1F7F8C", 1.6))
        elif p["muro"] == "O":
            body.append(poly([(0, a, z0), (0, b, z0), (0, b, z1), (0, a, z1)], fill, "#1F7F8C", 1.6))
    for e in sorted(sp["elementos"], key=lambda e: (e["x"] + e["w"] / 2) + (e["y"] + e["d"] / 2) + e["z"] * 0.01):
        x0, y0, x1, y1, z0, z1 = e["x"], e["y"], e["x"] + e["w"], e["y"] + e["d"], e["z"], e["z"] + e["h"]
        col = e["color"]
        body.append(poly([(x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)], _shade(col, 0.82)))  # cara Este
        body.append(poly([(x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)], _shade(col, 0.68)))  # cara Sur
        body.append(poly([(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)], _shade(col, 1.08)))  # tapa
        tx, ty = P((x0 + x1) / 2, (y0 + y1) / 2, z1)
        body.append(_text(tx, ty + 4, e["etiqueta"], 10, weight="600"))
    xs = [P(x, y, z)[0] for x in (0, W) for y in (0, D) for z in (0, H)]
    ys = [P(x, y, z)[1] for x in (0, W) for y in (0, D) for z in (0, H)]
    pad = 40
    ox, oy = pad - min(xs), pad - min(ys)
    w, h = max(xs) - min(xs) + 2 * pad, max(ys) - min(ys) + 2 * pad + 20
    inner = f'<g transform="translate({ox:.1f},{oy:.1f})">{"".join(body)}</g>'
    inner += _text(w / 2, h - 10, f"Vista isométrica · {sp['nombre']} (muros Norte y Oeste)", 13, weight="700", fill="#A8431F")
    return _svg(w, h, inner)


def svg_data_uri(svg):
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


# ---------- renders con Gemini (opcional) ----------
def gemini_render(api_key, model, prompt, timeout=120):
    """Devuelve bytes de imagen. Lanza RuntimeError con el motivo si falla."""
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}},
        timeout=timeout,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Gemini respondió {r.status_code}: {r.text[:300]}")
    for cand in r.json().get("candidates", []):
        for part in (cand.get("content") or {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                return base64.b64decode(inline["data"])
    raise RuntimeError("Gemini no devolvió imagen (¿filtro de seguridad o modelo sin salida de imagen?).")
