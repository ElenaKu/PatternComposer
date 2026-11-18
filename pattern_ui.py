#!/usr/bin/env python3
"""
Pattern UI - local web editor for seamless vector patterns (PatternComposer).

Load a folder of SVG motifs, pick a layout scheme (half-drop, brick, diamond,
bouquets, vine, ditsy, toss, your own saved schemes, or scored auto layouts),
then move, rotate, scale, flip, swap, add and delete motifs by hand on a live
wrap-around tile. Layers: each layer has its own motifs and its own free or
block mode (e.g. a ditsy background layer under half-drop bouquets); show, lock,
reorder, merge; saved SVGs keep one group per layer. Colour: built-in and saved palettes, palettes from an image,
imported palettes (hex lists, QuiltFlow JSON, .ase, .gpl), OKLCH colour harmonies,
recolouring by lightness with manual per-colour changes, a B/W check and a
colourways sheet. Block mode: build one block (optionally mirror-symmetric)
and it repeats live on a half-drop, brick or grid lattice. A reference tile (any PNG/JPG, e.g. a generated pattern) can
be shown underneath as a guide. Fill empty spots with filler motifs, recolour
with palettes and save the tile, an Illustrator swatch and 3x3 previews.

Usage:
    python pattern_ui.py
    python pattern_ui.py D:\\motifs\\cranes          (folder preloaded)
    python pattern_ui.py D:\\motifs\\cranes --port 8770

Opens http://127.0.0.1:8769 by default.
Needs: numpy scipy pillow resvg-py (auto layouts, gap filling, PNG previews)

Editor keys: drag = move, wheel = rotate, Shift+wheel = scale, Delete = remove,
D = duplicate, F = flip, N = next motif of the same role, arrows = nudge,
[ and ] = move the motif to the layer below / above,
Ctrl+Z = undo, Ctrl+Y = redo.
"""

import json
import random
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import PatternComposer as pc

HOST, PORT = '127.0.0.1', 8769
SCHEME_DIR = Path(__file__).resolve().parent / 'schemes'
PALETTE_DIR = Path(__file__).resolve().parent / 'palettes'
T = pc.TILE_SIZE

STATE = {'folder': None, 'motifs': [], 'preload': ''}
LOCK = threading.Lock()


# --------------------------------------------------------------------------
# Placement conversion: client {id, x, y, s, angle, flip} <-> PatternComposer
# s = longest side of the motif as a share of the tile width
# --------------------------------------------------------------------------
def motif_by_id(index):
    motifs = STATE['motifs']
    if not 0 <= int(index) < len(motifs):
        raise ValueError('Unknown motif - reload the folder.')
    return motifs[int(index)]


def to_tuple(p):
    m = motif_by_id(p['id'])
    scale = float(p['s']) * T / max(m.width, m.height, 1e-9)
    return (m, float(p['x']) % T, float(p['y']) % T, scale, float(p.get('angle', 0)),
            -1 if p.get('flip', 1) < 0 else 1)


def to_client(t):
    m, x, y, scale, angle, flip = t
    return {'id': m.index, 'x': round(x % T, 2), 'y': round(y % T, 2),
            's': round(scale * max(m.width, m.height) / T, 4),
            'angle': round(angle, 2), 'flip': flip}


def apply_flags(body):
    roles, upright = body.get('roles', {}), body.get('upright', {})
    for m in STATE['motifs']:
        m.role = roles.get(str(m.index), m.role)
        m.upright = bool(upright.get(str(m.index), m.upright))


def palette_name(spec):
    if isinstance(spec, dict):
        return pc.safe_name(spec.get('name') or 'custom') or 'custom'
    return spec or 'original'


def palette_of(spec, background, motifs, remap=None):
    """Palette spec = built-in name or {'name', 'bg', 'colors'}; remap = {original: new}.

    Motif colours go to palette colours by lightness rank, so the drawing keeps
    its light-to-dark structure.
    """
    if isinstance(spec, dict):
        colors = [c for c in (pc.normalize_color(x) for x in spec.get('colors', [])) if c]
        if colors:
            all_colors = sorted({c for m in motifs for c in m.colors})
            mapping = pc.palette_mapping(all_colors, colors)
            bg = pc.normalize_color(spec.get('bg') or '') or '#f6f1e7'
        else:
            _, bg, mapping = pc.choose_palettes(['original'], motifs, spec.get('bg') or None)[0]
    else:
        choice = (pc.choose_palettes([spec or 'original'], motifs, background or None)
                  or pc.choose_palettes(['original'], motifs, background or None))
        _, bg, mapping = choice[0]
    mapping = dict(mapping)
    for src, dst in (remap or {}).items():
        src, dst = pc.normalize_color(src), pc.normalize_color(dst)
        if src and dst:
            mapping[src] = dst
    return (background or bg), mapping


def needs_renderer():
    if pc.render_rgba('<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"/>') is None:
        raise ValueError('This needs resvg-py: pip install resvg-py')


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------
def load_folder(body):
    folder = Path(body.get('folder', '').strip().strip('"\''))
    if not folder.is_dir():
        raise ValueError(f'Folder not found: {folder}')
    paths = sorted(p for p in folder.rglob('*.svg')
                   if p.is_file() and not pc.OUTPUT_DIRS & set(p.relative_to(folder).parts))
    motifs, skipped = [], []
    for path in paths:
        try:
            motif = pc.load_motif(path, len(motifs), False)
            motif.path = path
            motifs.append(motif)
        except Exception as error:
            skipped.append(f'{path.name}: {error}')
    if not motifs:
        raise ValueError('No usable SVG motifs in this folder.')
    pc._silhouettes.clear()
    pc.assign_roles(motifs, folder)
    for m in motifs:
        m.upright = any(w in m.name.lower() for w in pc.UPRIGHT_WORDS)
    STATE.update(folder=folder, motifs=motifs)
    return {
        'folder': str(folder), 'skipped': skipped, 'tile': T,
        'motifs': [{'id': m.index, 'name': m.name, 'role': m.role, 'upright': m.upright,
                    'w': m.width, 'h': m.height,
                    'cx': (m.bbox[0] + m.bbox[2]) / 2, 'cy': (m.bbox[1] + m.bbox[3]) / 2,
                    'group': str(m.path.parent.relative_to(folder)).replace('\\', '/')}
                   for m in motifs],
        'palettes': [{'name': 'original', 'bg': '', 'colors': []}]
                    + [{'name': n, 'bg': bg, 'colors': c} for n, (bg, c) in pc.PALETTES.items()],
        'saved_palettes': list_palettes(),
        'schemes': list_schemes(),
    }


def markup(body):
    """Motif drawings recoloured for the palette, ready for the editor <defs>."""
    motifs = STATE['motifs']
    bg, mapping = palette_of(body.get('palette'), body.get('background'), motifs, body.get('remap'))
    originals = sorted({c for m in motifs for c in m.colors}, key=pc.lightness)
    return {'bg': bg, 'colors': [{'orig': c, 'now': mapping.get(c, c)} for c in originals],
            'motifs': [
        {'id': m.index, 'defs': pc.recolor_markup(m.defs_markup, mapping) if m.defs_markup else '',
         'body': pc.recolor_markup(m.body_markup, mapping)} for m in motifs]}


def fill(body):
    needs_renderer()
    apply_flags(body)
    s = body['settings']
    placements = [to_tuple(p) for p in body.get('placements', [])]
    fillers = [m for m in STATE['motifs'] if m.role == 'filler']
    if not fillers:
        raise ValueError('Mark some motifs as filler first.')
    args = SimpleNamespace(rotation=float(s['rotation']), upright_rotation=float(s['upright_rotation']),
                           upright_words=[])
    specs = [{'motif': m, 'max': int(s['filler_max']), 'layer': 2,
              'min_size': float(s['filler_min']), 'max_size': float(s['filler_max_size']),
              'rotation': pc.rotation_range(m, args) / 2} for m in fillers]
    rng = random.Random(random.randrange(10 ** 7))
    result, added, note = pc.fill_gaps(placements, T, specs, rng,
                                       min_radius=float(s['filler_min']) / 2)
    return {'placements': [to_client(t) for t in result], 'added': added, 'note': note}


def auto(body):
    needs_renderer()
    apply_flags(body)
    motifs = [m for m in STATE['motifs'] if m.role != 'off']
    if not any(m.role == 'hero' for m in motifs):
        raise ValueError('Mark at least one motif as hero.')
    s = body['settings']
    args = SimpleNamespace(
        hero_count=int(s['hero_count']), density=float(s['density']),
        filler_max=int(s['filler_max']), rotation=float(s['rotation']),
        upright_rotation=float(s['upright_rotation']), upright_words=[],
        hero_size=(float(s['hero_min']), float(s['hero_max'])),
        medium_size=(float(s['medium_min']), float(s['medium_max'])),
        filler_size=(float(s['filler_min']), float(s['filler_max_size'])), gap=float(s['gap']))
    base = random.randrange(1, 10 ** 7)
    scored = []
    for n in range(int(s['candidates'])):
        placements, _ = pc.auto_layout(motifs, T, args, random.Random(base + n))
        result = pc.score_layout(placements, T)
        scored.append((result['score'], base + n, placements, result))
    scored.sort(key=lambda item: item[0])
    return {'layouts': [{'seed': seed, 'score': score, 'metrics': result,
                         'placements': [to_client(t) for t in placements]}
                        for score, seed, placements, result in scored[:int(s['keep'])]]}


def layered_tile_svg(layer_list, mapping, background, name):
    """Tile SVG with one group per layer, bottom layer first."""
    tw, th = pc.tile_dims(T)
    all_motifs = {}
    groups = []
    for n, layer in enumerate(layer_list, start=1):
        placements = [to_tuple(p) for p in layer.get('placements', [])]
        instances, _ = pc.with_edge_copies(placements, T)
        for m, _ in instances:
            all_motifs[m.index] = m
        label = (layer.get('name') or f'Layer {n}').replace('&', '&amp;').replace('<', '').replace('"', '')
        uses = '\n'.join(f'<use href="#motif{m.index}" xlink:href="#motif{m.index}" '
                          f'transform="{pc.fmt_matrix(mx)}"/>' for m, mx in instances)
        groups.append(f'<g id="layer{n}_{pc.safe_name(label)}" inkscape:groupmode="layer" '
                      f'inkscape:label="{label}">\n{uses}\n</g>')
    used = sorted(all_motifs.values(), key=lambda m: m.index)
    return (f'<svg xmlns="{pc.SVG_NS}" xmlns:xlink="{pc.XLINK_NS}" '
            f'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" width="{tw:g}" '
            f'height="{th:g}" viewBox="0 0 {tw:g} {th:g}">\n<title>{name}</title>\n'
            f'<defs>\n{pc.motif_defs(used, mapping)}\n'
            f'<clipPath id="tile-clip"><rect width="{tw:g}" height="{th:g}"/></clipPath>\n'
            f'</defs>\n<g clip-path="url(#tile-clip)">\n'
            f'<rect id="background" width="{tw:g}" height="{th:g}" fill="{background}"/>\n'
            + '\n'.join(groups) + '\n</g>\n</svg>\n')


def save(body):
    placements = [to_tuple(p) for p in body.get('placements', [])]
    if not placements:
        raise ValueError('The tile is empty.')
    folder = STATE['folder']
    out_dir = folder / 'composed_patterns' / 'edited'
    swatch_dir, preview_dir = out_dir / 'swatch', out_dir / 'previews'
    for directory in (out_dir, swatch_dir, preview_dir):
        pc.ensure_dir(directory)
    used = sorted({p[0].index: p[0] for p in placements}.values(), key=lambda m: m.index)
    palette = palette_name(body.get('palette'))
    bg, mapping = palette_of(body.get('palette'), body.get('background'), STATE['motifs'],
                             body.get('remap'))
    name = pc.safe_name(body['name']) if (body.get('name') or '').strip() else pc.safe_name(folder.name)
    name = f'{name}_{pc.safe_name(palette)}'
    stem, n = name, 2
    while (out_dir / f'{name}.svg').exists():
        name, n = f'{stem}_{n}', n + 1
    instances, copies = pc.with_edge_copies(placements, T)
    layer_list = [l for l in body.get('layers') or [] if l.get('placements')]
    if len(layer_list) > 1:
        svg = layered_tile_svg(layer_list, mapping, bg, name)
    else:
        svg = pc.build_tile_svg(instances, used, mapping, bg, T, name)
    (out_dir / f'{name}.svg').write_text(svg, encoding='utf-8')
    (swatch_dir / f'{name}_swatch.svg').write_text(
        pc.build_swatch_svg(instances, used, mapping, bg, T, name), encoding='utf-8')
    preview = pc.build_preview_svg(svg, T)
    (preview_dir / f'{name}_preview_3x3.svg').write_text(preview, encoding='utf-8')
    png = pc.render_png(preview, preview_dir / f'{name}_preview_3k.png')
    (out_dir / f'{name}.layout.json').write_text(json.dumps(
        {'palette': palette, 'background': bg, 'colour_map': mapping, 'motif_folder': str(folder),
         'placements': [dict(to_client(t), name=t[0].name) for t in placements],
         'layers': [{'name': l.get('name'), 'placements': l.get('placements')} for l in layer_list]},
        indent=1),
        encoding='utf-8')
    return {'saved': str(out_dir / f'{name}.svg'), 'png': bool(png), 'edge_copies': copies}


def colors(body):
    """Area share of each colour on the tile, checked against the 60-30-10 rule."""
    needs_renderer()
    import re
    from PIL import Image
    placements = [to_tuple(p) for p in body.get('placements', [])]
    if not placements:
        raise ValueError('The tile is empty.')
    used = sorted({p[0].index: p[0] for p in placements}.values(), key=lambda m: m.index)
    bg, mapping = palette_of(body.get('palette'), body.get('background'), STATE['motifs'],
                             body.get('remap'))
    instances, _ = pc.with_edge_copies(placements, T)
    svg = pc.build_tile_svg(instances, used, mapping, bg, T, 'colors')
    svg = re.sub(r'width="[^"]*" height="[^"]*"', 'width="320" height="320"', svg, count=1)
    image = pc.render_rgba(svg)
    flat = Image.new('RGB', image.size, bg if bg.startswith('#') else '#ffffff')
    flat.paste(image, mask=image.getchannel('A'))
    quant = flat.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
    pal = quant.getpalette()
    counts = sorted(quant.getcolors(), reverse=True)
    total = sum(c for c, _ in counts)
    groups = []   # merge near-identical colours (anti-aliasing)
    for count, index in counts:
        rgb = tuple(pal[index * 3:index * 3 + 3])
        for g in groups:
            if sum((a - b) ** 2 for a, b in zip(g['rgb'], rgb)) < 40 ** 2:
                g['count'] += count
                break
        else:
            groups.append({'rgb': rgb, 'count': count})
    groups.sort(key=lambda g: -g['count'])
    shares = [{'hex': '#%02x%02x%02x' % g['rgb'], 'share': round(g['count'] / total, 3)}
              for g in groups if g['count'] / total >= 0.005]
    main = shares[0]['share'] if shares else 0
    second = shares[1]['share'] if len(shares) > 1 else 0
    rest = sum(x['share'] for x in shares[2:])
    tips = []
    if main < 0.5:
        tips.append('No colour dominates (main is under 50%): make the background or the main motifs one '
                    'calmer colour, or add space between motifs.')
    elif main > 0.75:
        tips.append('The main colour takes over 75%: the tile may look empty - enlarge motifs or add some.')
    if 0 < second < 0.15 and main >= 0.5:
        tips.append('The second colour is weak (under 15%): give the main motifs one shared colour '
                    'so they read as a group against the background.')
    if second > 0.4:
        tips.append('The second colour competes with the main one (over 40%): shrink or recolour some motifs.')
    if rest > 0.2:
        tips.append('Accent colours cover over 20%: too many small bright spots - fewer or smaller accents read better.')
    elif len(shares) > 2 and rest < 0.04:
        tips.append('Almost no accent colour: a small bright detail (about 10%) would liven the tile.')
    if not tips:
        tips.append('Close to 60-30-10: balanced.')
    return {'shares': shares, 'tips': tips, 'main': main, 'second': second, 'accents': round(rest, 3)}


def colorways(body):
    """The same tile in several palettes, as standalone SVG tiles."""
    placements = [to_tuple(p) for p in body.get('placements', [])]
    if not placements:
        raise ValueError('The tile is empty.')
    used = sorted({p[0].index: p[0] for p in placements}.values(), key=lambda m: m.index)
    instances, _ = pc.with_edge_copies(placements, T)
    out = []
    for i, spec in enumerate(body.get('palettes', [])):
        first = i == 0   # the current palette keeps your manual colour changes and background
        bg, mapping = palette_of(spec, body.get('background', '') if first else '', STATE['motifs'],
                                 body.get('remap') if first else None)
        out.append({'name': palette_name(spec), 'spec': spec, 'bg': bg,
                    'svg': pc.build_tile_svg(instances, used, mapping, bg, T, palette_name(spec))})
    return {'colorways': out}


def extract_palette(body):
    """Main colours of an image (data URL): most common one as background."""
    import base64
    import io
    from PIL import Image
    data = body.get('image', '')
    if ',' in data:
        data = data.split(',', 1)[1]
    image = Image.open(io.BytesIO(base64.b64decode(data))).convert('RGB')
    image.thumbnail((240, 240))
    n = max(3, min(10, int(body.get('count', 6))))
    quant = image.quantize(colors=n + 6, method=Image.Quantize.MEDIANCUT)
    pal = quant.getpalette()
    groups = []
    for count, index in sorted(quant.getcolors(), reverse=True):
        rgb = tuple(pal[index * 3:index * 3 + 3])
        for g in groups:
            if sum((a - b) ** 2 for a, b in zip(g['rgb'], rgb)) < 30 ** 2:
                g['count'] += count
                break
        else:
            groups.append({'rgb': rgb, 'count': count})
    groups.sort(key=lambda g: -g['count'])
    hexes = ['#%02x%02x%02x' % g['rgb'] for g in groups[:n + 1]]
    return {'bg': hexes[0], 'colors': hexes[1:] or hexes}


def list_palettes():
    result = []
    if PALETTE_DIR.is_dir():
        for path in sorted(PALETTE_DIR.glob('*.json')):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                result.append({'name': path.stem, 'bg': data.get('bg', ''), 'colors': data['colors'],
                               'saved': True})
            except Exception:
                pass
    return result


def save_palette(body):
    spec = body.get('palette') or {}
    name = pc.safe_name(spec.get('name') or '')
    colors = [c for c in (pc.normalize_color(x) for x in spec.get('colors', [])) if c]
    if not name or not colors:
        raise ValueError('A palette needs a name and at least one colour.')
    pc.ensure_dir(PALETTE_DIR)
    (PALETTE_DIR / f'{name}.json').write_text(
        json.dumps({'bg': pc.normalize_color(spec.get('bg') or '') or '', 'colors': colors}, indent=1),
        encoding='utf-8')
    return {'palettes': list_palettes()}


def list_schemes():
    result = []
    if SCHEME_DIR.is_dir():
        for path in sorted(SCHEME_DIR.glob('*.json')):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                result.append({'name': path.stem, 'slots': data['slots']})
            except Exception:
                pass
    return result


def save_scheme(body):
    name = pc.safe_name(body.get('name') or '')
    if not name:
        raise ValueError('Give the scheme a name.')
    slots = body.get('slots') or []
    if not slots:
        raise ValueError('The tile is empty.')
    pc.ensure_dir(SCHEME_DIR)
    (SCHEME_DIR / f'{name}.json').write_text(json.dumps({'slots': slots}, indent=1),
                                             encoding='utf-8')
    return {'schemes': list_schemes()}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, data, ctype='application/json'):
        payload = data if isinstance(data, bytes) else (
            data.encode('utf-8') if isinstance(data, str) else json.dumps(data).encode('utf-8'))
        self.send_response(code)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            return self.send(200, PAGE.replace('__PRELOAD__', json.dumps(STATE['preload'])),
                             'text/html')
        if self.path.startswith('/motif/'):
            try:
                return self.send(200, motif_by_id(self.path.rsplit('/', 1)[1]).path.read_bytes(),
                                 'image/svg+xml')
            except Exception:
                pass
        self.send(404, {'error': 'not found'})

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length) or b'{}')
        actions = {'/api/load': load_folder, '/api/markup': markup, '/api/fill': fill,
                   '/api/colors': colors, '/api/colorways': colorways,
                   '/api/extract': extract_palette, '/api/save_palette': save_palette,
                   '/api/auto': auto, '/api/save': save, '/api/save_scheme': save_scheme}
        if self.path not in actions:
            return self.send(404, {'error': 'unknown action'})
        try:
            with LOCK:
                self.send(200, actions[self.path](body))
        except Exception as error:
            self.send(400, {'error': str(error)})


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Pattern UI</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#f4f1ec;--panel:#fff;--ink:#222;--muted:#777;--line:#e2ddd5;--accent:#2f6f5e}
*{box-sizing:border-box}
body{margin:0;font:14px/1.4 system-ui,Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--ink);
  display:grid;grid-template-columns:330px 1fr;height:100vh;overflow:hidden}
aside{background:var(--panel);border-right:1px solid var(--line);overflow:auto;padding:12px 14px}
main{display:grid;grid-template-rows:auto auto 1fr auto;overflow:hidden;min-width:0}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:16px 0 7px}
input[type=text]{width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:6px;font:inherit}
button{font:inherit;border:1px solid var(--line);background:#fff;border-radius:6px;padding:6px 11px;cursor:pointer;white-space:nowrap}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
button:disabled{opacity:.45;cursor:default}
.row{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.tabs{display:flex;flex-wrap:wrap;gap:4px}
.tabs button{padding:4px 10px;border-radius:999px;font-size:13px}
.tabs button.on{background:var(--ink);color:#fff;border-color:var(--ink)}
.motif{display:grid;grid-template-columns:50px 1fr;gap:8px;padding:5px 0;border-bottom:1px solid var(--line)}
.motif img{width:50px;height:50px;object-fit:contain;background:#fafafa;border-radius:6px;cursor:copy}
.motif img:hover{outline:2px solid var(--accent)}
.motif .name{font-size:12px;word-break:break-all}
.seg{display:flex;margin-top:3px}
.seg button{padding:2px 6px;font-size:11px;border-radius:0;margin-left:-1px}
.seg button:first-child{border-radius:6px 0 0 6px}.seg button:last-child{border-radius:0 6px 6px 0}
.seg button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.up{font-size:11px;color:var(--muted);margin-left:4px}
label.s{display:grid;grid-template-columns:1fr 50px;align-items:center;gap:4px;margin:5px 0;font-size:12px}
label.s span.v{text-align:right;color:var(--muted)}
label.s input{grid-column:1/3;width:100%}
.bar{display:flex;gap:8px;align-items:center;padding:8px 12px;border-bottom:1px solid var(--line);background:var(--panel);flex-wrap:wrap}
.muted{color:var(--muted);font-size:12px}
#edBox{position:relative;overflow:hidden;background:#ddd}
#ed{width:100%;height:100%;display:block;touch-action:none}
#ed use{cursor:grab}
.strip{display:flex;gap:8px;overflow-x:auto;padding:8px 12px;background:var(--panel);border-top:1px solid var(--line)}
.strip:empty{display:none}
.cand{flex:0 0 auto;cursor:pointer;border:2px solid transparent;border-radius:8px;padding:2px;background:#fafafa;text-align:center;font-size:11px;color:var(--muted)}
.cand.on{border-color:var(--accent)}
.cand svg{width:96px;height:96px;display:block}
details{margin-top:6px}
.sw{display:inline-block;width:18px;height:18px;border:1px solid rgba(0,0,0,.15);border-radius:3px;vertical-align:middle}
.palbtn{display:flex;align-items:center;gap:6px;width:100%;justify-content:space-between;margin:3px 0}
.palbtn.on{outline:2px solid var(--accent)}
.strip4{display:flex;gap:2px}
.cmap{display:flex;align-items:center;gap:4px;margin:2px 8px 2px 0}
.cmap input[type=color]{width:26px;height:22px;padding:0;border:1px solid var(--line);border-radius:4px}
textarea{width:100%;height:70px;font:12px monospace;border:1px solid var(--line);border-radius:6px;padding:6px}
#cwOverlay{position:fixed;inset:0;background:rgba(0,0,0,.55);display:none;z-index:10;overflow:auto;padding:24px}
#cwBox{background:#fff;border-radius:10px;padding:14px;max-width:1300px;margin:0 auto}
#cwGrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;margin-top:10px}
.cw .tile{height:240px;border-radius:6px;background-repeat:repeat;background-size:120px 120px;border:1px solid var(--line)}
#ed.gray{filter:grayscale(1)}
.layer{display:flex;align-items:center;gap:4px;padding:3px 4px;border:1px solid var(--line);border-radius:6px;margin:3px 0;cursor:pointer;font-size:12px}
.layer.on{border-color:var(--accent);background:#eef6f3}
.layer input[type=text]{flex:1;padding:3px 5px;font-size:12px}
.layer label{display:flex;align-items:center;gap:1px;font-size:12px;cursor:pointer}
summary{cursor:pointer;color:var(--muted);font-size:12px}
.hint{font-size:11px;color:var(--muted);margin-top:6px;line-height:1.5}
</style></head><body>
<aside>
  <h2>Motif folder</h2>
  <div class="row" style="flex-wrap:nowrap"><input type="text" id="folder" placeholder="D:\motifs\cranes"><button id="loadBtn">Load</button></div>
  <div id="skipped" class="muted"></div>

  <h2>Layers <span class="muted" style="text-transform:none;letter-spacing:0">top layer first</span></h2>
  <div id="layerList"></div>
  <div class="row" style="margin-top:4px;gap:4px">
    <button data-layer="add">+ Add</button><button data-layer="dup">Duplicate</button>
    <button data-layer="up" title="Move layer up">&uarr;</button><button data-layer="down" title="Move layer down">&darr;</button>
    <button data-layer="merge" title="Merge into the layer below">Merge down</button><button data-layer="del">Delete</button></div>
  <label class="muted"><input type="checkbox" id="dimLayers" checked> dim other layers</label>

  <h2>Reference tile</h2>
  <div class="row"><input type="file" id="refFile" accept="image/*" style="max-width:100%"></div>
  <label class="s">Reference opacity<span class="v" id="v_refOp">0.35</span>
    <input type="range" id="refOp" min="0" max="1" step="0.05" value="0.35"></label>
  <div class="row"><label class="muted"><input type="checkbox" id="refRepeat" checked> repeat around</label>
    <button id="refClear">Remove</button></div>

  <h2>Colour</h2>
  <div id="colTabs" class="tabs"></div>
  <div id="colPane" style="margin-top:6px"></div>
  <div class="row" style="margin-top:6px"><span class="muted">Background</span><input type="color" id="bgColor" value="#f6f1e7">
    <label class="muted"><input type="checkbox" id="customBg"> own background</label></div>
  <div class="muted" style="margin-top:6px">Colour map - original &rarr; now (click to change):</div>
  <div id="colorMap" class="row" style="gap:0"></div>
  <div class="row" style="margin-top:6px;flex-wrap:nowrap"><input type="text" id="palName" placeholder="palette name"><button id="palSave">Save palette</button></div>

  <h2>Motifs <span class="muted" style="text-transform:none;letter-spacing:0">click a picture to add it</span></h2>
  <div id="groupTabs" class="tabs"></div>
  <div id="motifs"><div class="muted">No folder loaded.</div></div>

  <h2>Composition</h2>
  <label class="muted"><input type="checkbox" id="useRatio"> sizes by ratio: hero, hero x r, hero x r&sup2;</label>
  <label class="s">Size ratio r (golden = 0.618)<span class="v" id="v_ratio">0.618</span>
    <input type="range" id="ratio" min="0.4" max="0.9" step="0.002" value="0.618"></label>
  <label class="s">Hero size for the ratio<span class="v" id="v_ratioBase">0.36</span>
    <input type="range" id="ratioBase" min="0.12" max="0.6" step="0.01" value="0.36"></label>
  <div class="row"><button id="ratioApply" title="Resize every motif on the tile by its role">Apply ratio to tile</button></div>
  <label class="s">Golden ring motifs<span class="v" id="v_ringN">8</span>
    <input type="range" id="ringN" min="3" max="24" step="1" value="8"></label>
  <div class="muted">Guides on the tile:</div>
  <div class="row"><label class="muted"><input type="checkbox" id="gThirds"> thirds</label>
    <label class="muted"><input type="checkbox" id="gGolden"> golden section</label>
    <label class="muted"><input type="checkbox" id="gDiag"> diagonals</label></div>
  <div class="row" style="margin-top:6px"><button id="colorBtn">Check colours 60-30-10</button></div>
  <div id="colorOut" class="hint"></div>

  <h2>Sizes and fillers</h2>
  <div id="slidersMain"></div>
  <details><summary>Auto layout settings</summary><div id="slidersAuto"></div></details>
</aside>
<main>
  <div class="bar">
    <span class="muted">Scheme</span><div id="schemeTabs" class="tabs"></div>
    <button id="applyBtn" class="primary">Apply scheme</button>
    <button id="autoBtn">Auto</button>
    <button id="fillBtn">Fill gaps</button>
    <button id="clearBtn">Clear</button>
  </div>
  <div class="bar">
    <label title="Build one block, it repeats on a lattice"><input type="checkbox" id="blockOn"> <b>Block mode</b></label>
    <div id="latTabs" class="tabs"></div>
    <span class="muted">Columns</span><input type="range" id="bNc" min="1" max="6" step="1" value="2" style="width:70px"><span id="v_bNc" class="muted">2</span>
    <span class="muted">Rows</span><input type="range" id="bNr" min="1" max="6" step="1" value="1" style="width:70px"><span id="v_bNr" class="muted">1</span>
    <span class="muted">Block scale</span><input type="range" id="bK" min="0.4" max="1.8" step="0.02" value="1" style="width:90px"><span id="v_bK" class="muted">1</span>
    <label class="muted" title="Left half mirrors the right half"><input type="checkbox" id="bMirror"> mirror</label>
    <button id="bakeBtn" title="Turn the repeated blocks into free motifs">Bake</button>
  </div>
  <div class="bar">
    <button id="undoBtn" title="Ctrl+Z">Undo</button><button id="redoBtn" title="Ctrl+Y">Redo</button>
    <span class="muted">Selected:</span>
    <button data-act="flip" title="F">Flip</button><button data-act="dup" title="D">Duplicate</button>
    <button data-act="next" title="N">Next motif</button><button data-act="front">To front</button>
    <button data-act="back">To back</button><button data-act="ring" title="Fillers around the selected motif by the golden angle">Golden ring</button>
    <button id="toUp" title="Move to the layer above ( ] )">&uarr; layer</button><button id="toDown" title="Move to the layer below ( [ )">&darr; layer</button>
    <button data-act="del" title="Delete">Delete</button>
    <span id="selInfo" class="muted"></span>
    <span style="flex:1"></span>
    <span class="muted">Context</span><input type="range" id="ctx" min="0.1" max="1" step="0.05" value="0.5" style="width:90px">
    <label class="muted"><input type="checkbox" id="fade" checked> fade repeats</label>
  </div>
  <div id="edBox"><svg id="ed" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">
    <defs id="mdefs"></defs>
    <rect id="bgRect" x="-1000" y="-1000" width="3000" height="3000" fill="#f6f1e7"/>
    <g id="refLayer"></g><g id="inst"></g>
    <path id="fadeMask" fill="#ffffff" fill-opacity="0.5" fill-rule="evenodd" pointer-events="none"/>
    <rect id="tileFrame" x="0" y="0" width="1000" height="1000" fill="none" stroke="#000" stroke-opacity=".6"
      stroke-dasharray="8 6" vector-effect="non-scaling-stroke" pointer-events="none"/>
    <g id="guides" pointer-events="none"></g>
    <g id="selLayer" pointer-events="none"></g>
  </svg></div>
  <div id="cwOverlay"><div id="cwBox"><div class="row"><b>Colourways</b><span class="muted">the same tile in every palette - Use to edit, Save to export</span>
    <span style="flex:1"></span><button id="cwSaveAll">Save all</button><button id="cwClose">Close</button></div><div id="cwGrid"></div></div></div>
  <div>
    <div class="strip" id="strip"></div>
    <div class="bar" style="border-top:1px solid var(--line);border-bottom:0">
      <span class="muted">Palette:</span><span id="curPal" class="row" style="gap:3px"></span>
      <label class="muted" title="Check the light-dark structure"><input type="checkbox" id="gray"> B/W check</label>
      <button id="cwBtn">Colourways</button>
      <span style="flex:1"></span>
      <input type="text" id="saveName" placeholder="name (optional)" style="width:160px">
      <button id="schemeSaveBtn">Save as scheme</button>
      <button id="saveBtn" class="primary">Save pattern</button>
    </div>
    <div class="bar" style="border-bottom:0"><span id="status" class="muted">Load a motif folder to start.</span></div>
  </div>
</main>
<script>
const T = 1000;
const MAIN = [
  ['hero_min','Hero size min',0.12,0.6,0.01,0.30],['hero_max','Hero size max',0.12,0.6,0.01,0.40],
  ['medium_min','Medium size min',0.05,0.4,0.01,0.15],['medium_max','Medium size max',0.05,0.4,0.01,0.23],
  ['filler_min','Filler size min',0.02,0.2,0.005,0.045],['filler_max_size','Filler size max',0.02,0.2,0.005,0.11],
  ['filler_max','Max copies per filler',0,30,1,8],
  ['rotation','Rotation range, degrees',0,360,5,360],['upright_rotation','Upright motifs rotation',0,90,2,24],
];
const AUTO = [
  ['candidates','Layouts to try',5,120,5,30],['keep','Best layouts kept',1,16,1,8],
  ['hero_count','Hero motifs per tile',0,8,1,3],['density','Coverage before fillers',0.15,0.7,0.01,0.42],
  ['gap','Gap between motifs',0,0.05,0.002,0.012],
];
const ROLES = ['hero','medium','filler','off'];

// Built-in schemes: slots in tile shares (x, y, role, angle). Size comes from the role sliders.
function vine(){const s=[];for(let i=0;i<8;i++){const y=i/8+1/16;s.push([0.5+0.2*Math.sin(2*Math.PI*y),y,i%2?'filler':'medium',(i%2?1:-1)*35]);}
  s.push([0.12,0.3,'hero',0],[0.88,0.8,'hero',0]);return s}
function ditsy(){const s=[];for(let r=0;r<6;r++)for(let c=0;c<6;c++){const j=((r*7+c*13)%10)/10-0.5;
  s.push([(c+0.5+(r%2)*0.5+j*0.25)/6,(r+0.5+j*0.2)/6,(r+c)%3?'filler':'medium',((r*5+c*3)%12)*30]);}return s}
const SCHEMES = {
  'Half-drop':[[0.25,0.25,'hero',0],[0.75,0.75,'hero',0],[0.25,0.75,'medium',-20],[0.75,0.25,'medium',20],
    [0.5,0.5,'filler',0],[0,0,'filler',30],[0.5,0,'filler',-30],[0,0.5,'filler',60]],
  'Brick':[[0.25,0.25,'hero',0],[0.75,0.25,'hero',0],[0,0.75,'medium',15],[0.5,0.75,'medium',-15],
    [0,0.25,'filler',0],[0.5,0.25,'filler',0],[0.25,0.75,'filler',45],[0.75,0.75,'filler',-45]],
  'Diamond':[[0,0,'hero',0],[0.5,0.5,'hero',0],[0.5,0,'medium',0],[0,0.5,'medium',0],
    [0.25,0.25,'filler',45],[0.75,0.25,'filler',-45],[0.25,0.75,'filler',-45],[0.75,0.75,'filler',45]],
  'Bouquets':[[0.3,0.3,'hero',0],[0.47,0.24,'medium',40],[0.2,0.45,'medium',-50],[0.36,0.48,'medium',160],
    [0.8,0.8,'hero',0],[0.63,0.86,'medium',-140],[0.9,0.65,'medium',130],[0.74,0.62,'medium',-20],
    [0.55,0.55,'filler',0],[0.05,0.8,'filler',30],[0.8,0.1,'filler',-30],[0.05,0.1,'filler',0]],
  'Vine':vine(),
  'Ditsy':ditsy(),
  'Golden spiral':(()=>{const s=[[0.5,0.5,'hero',0]];  // phyllotaxis: 137.5 degrees between neighbours
    for(let i=1;i<=13;i++){const a=i*137.508*Math.PI/180,r=0.13*Math.sqrt(i);
      s.push([0.5+r*Math.cos(a),0.5+r*Math.sin(a),i<=4?'medium':'filler',i*137.508%360])}return s})(),
  'Toss':[[0.22,0.3,'hero',-10],[0.72,0.78,'hero',12],[0.68,0.22,'medium',35],[0.18,0.78,'medium',-30],
    [0.47,0.52,'medium',150],[0.92,0.47,'medium',-120],[0.45,0.06,'medium',80],
    [0.42,0.3,'filler',0],[0.88,0.12,'filler',40],[0.08,0.55,'filler',-40],[0.6,0.6,'filler',20],
    [0.32,0.62,'filler',60],[0.9,0.95,'filler',-20],[0.05,0.05,'filler',10],[0.55,0.92,'filler',0]],
};
let motifs=[], palettes=[], savedPalettes=[], customSchemes=[], scheme='Half-drop', group='all';
let curPal={name:'original',bg:'',colors:[]}, remap={}, colTab='Palettes', draftPal=null, colorways=[];
let P=[], sel=-1, hist=[], future=[], refImg=null, autoLayouts=[], autoSel=null;
// Block mode: P holds one block (coordinates relative to the block centre); it repeats on a lattice.
let block={on:false,lattice:'halfdrop',nc:2,nr:1,k:1,mirror:false};
const LATTICES={halfdrop:'Half-drop',brick:'Brick',grid:'Grid'};
const $=id=>document.getElementById(id), byId=id=>motifs.find(m=>m.id===id);
const mod=v=>((v%T)+T)%T;
function latticePoints(b){
  const L=[],w=T/b.nc,h=T/b.nr;
  for(let c=0;c<b.nc;c++)for(let r=0;r<b.nr;r++){let x=c*w+w/2,y=r*h+h/2;
    if(b.lattice==='halfdrop'&&c%2) y+=h/2;
    if(b.lattice==='brick'&&r%2) x+=w/2;
    L.push([x,y]);}
  return L;
}
function blockItems(items,b){
  const out=[];
  items.forEach((p,i)=>{out.push({p,src:i,m:false});
    if(b.mirror&&Math.abs(p.x)>4) out.push({p:{...p,x:-p.x,angle:-p.angle,flip:-p.flip},src:i,m:true});});
  return out;
}
// Every motif a layer actually draws on the tile, with a link back to the editable item.
function expandLayer(items,b){
  if(!b.on) return items.map((p,i)=>({p,src:i,m:false}));
  const k=b.k,out=[],list=blockItems(items,b);
  for(const [lx,ly] of latticePoints(b)) for(const u of list){const q=u.p;
    out.push({p:{id:q.id,x:mod(lx+q.x*k),y:mod(ly+q.y*k),s:q.s*k,angle:q.angle,flip:q.flip},src:u.src,m:u.m});}
  return out;
}
const expand=()=>expandLayer(P,block);

/* ---------- layers: P and block always belong to the active layer ---------- */
const newBlock=()=>({on:false,lattice:'halfdrop',nc:2,nr:1,k:1,mirror:false});
let layers=[{name:'Layer 1',visible:true,locked:false,P:[],block:newBlock()}], L=0;
function commit(){layers[L].P=P;layers[L].block=block}
const layerPlacements=l=>expandLayer(l.P,l.block).map(e=>e.p);
// All visible layers, bottom to top - what gets saved, filled around and colour-checked.
const concrete=()=>{commit();return layers.filter(l=>l.visible).flatMap(layerPlacements)};
function switchLayer(i){commit();L=i;P=layers[L].P;block=layers[L].block;sel=-1;syncBlockUi();renderLayers();render()}
function renderLayers(){
  $('layerList').innerHTML=layers.map((l,i)=>i).reverse().map(i=>{const l=layers[i];
    return `<div class="layer ${i===L?'on':''}" data-l="${i}">
      <label title="Show"><input type="checkbox" data-vis="${i}" ${l.visible?'checked':''}>&#128065;</label>
      <label title="Lock"><input type="checkbox" data-lock="${i}" ${l.locked?'checked':''}>&#128274;</label>
      <input type="text" data-name="${i}" value="${l.name.replace(/"/g,'&quot;')}">
      <span class="muted">${(i===L?P:l.P).length}${l.block.on?' &#9638;':''}</span></div>`}).join('');
  $('layerList').querySelectorAll('.layer').forEach(d=>d.onclick=e=>{if(e.target.tagName!=='INPUT'||e.target.type==='text'){const i=+d.dataset.l;if(i!==L)switchLayer(i)}});
  $('layerList').querySelectorAll('[data-vis]').forEach(c=>c.onchange=()=>{layers[+c.dataset.vis].visible=c.checked;render()});
  $('layerList').querySelectorAll('[data-lock]').forEach(c=>c.onchange=()=>{layers[+c.dataset.lock].locked=c.checked;sel=-1;render()});
  $('layerList').querySelectorAll('[data-name]').forEach(t=>t.onchange=()=>{layers[+t.dataset.name].name=t.value.trim()||'Layer'});
}
function layerAct(a){
  snapshot(); commit();
  if(a==='add'){layers.splice(L+1,0,{name:`Layer ${layers.length+1}`,visible:true,locked:false,P:[],block:newBlock()});L++}
  if(a==='dup'){const c=JSON.parse(JSON.stringify(layers[L]));c.name+=' copy';layers.splice(L+1,0,c);L++}
  if(a==='del'){if(layers.length===1){layers[0].P=[];}else{layers.splice(L,1);L=Math.max(0,L-1)}}
  if(a==='up'&&L<layers.length-1){[layers[L],layers[L+1]]=[layers[L+1],layers[L]];L++}
  if(a==='down'&&L>0){[layers[L],layers[L-1]]=[layers[L-1],layers[L]];L--}
  if(a==='merge'&&L>0){const below=layers[L-1];   // baked copies of both layers end up in the lower one
    below.P=[...layerPlacements(below),...layerPlacements(layers[L])];below.block=newBlock();layers.splice(L,1);L--}
  P=layers[L].P;block=layers[L].block;sel=-1;syncBlockUi();renderLayers();render();
}
function moveToLayer(d){   // move the selected motif to the layer above (d=1) or below (d=-1)
  const t=L+d; if(sel<0||!P[sel]||t<0||t>=layers.length){status('No layer there - add one first.');return}
  if(block.on||layers[t].block.on){status('Bake block layers before moving motifs between them.');return}
  snapshot(); const p=P.splice(sel,1)[0]; layers[t].P.push(p); commit(); sel=-1; renderLayers(); render();
  status(`Moved to "${layers[t].name}".`);
}

/* ---------- settings ---------- */
function sliders(list, el){
  let saved={};try{saved=JSON.parse(localStorage.getItem('pui_settings')||'{}')}catch(e){}
  $(el).innerHTML=list.map(([k,l,mi,ma,st,d])=>{const v=saved[k]!==undefined?saved[k]:d;
    return `<label class="s">${l}<span class="v" id="v_${k}">${v}</span><input type="range" id="s_${k}" min="${mi}" max="${ma}" step="${st}" value="${v}"></label>`}).join('');
  list.forEach(([k])=>$('s_'+k).addEventListener('input',e=>{$('v_'+k).textContent=e.target.value;
    try{localStorage.setItem('pui_settings',JSON.stringify(settings()))}catch(err){}}));
}
function settings(){const s={};[...MAIN,...AUTO].forEach(([k])=>s[k]=parseFloat($('s_'+k).value));return s}
async function api(path, body){
  const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
  const j=await r.json(); if(!r.ok) throw new Error(j.error||'error'); return j;
}
const status=t=>$('status').textContent=t;
const layerPayload=()=>{commit();return layers.filter(l=>l.visible).map(l=>({name:l.name,placements:layerPlacements(l)}))};
function flags(){const roles={},upright={};motifs.forEach(m=>{roles[m.id]=m.role;upright[m.id]=m.upright});return {roles,upright}}

/* ---------- loading ---------- */
async function load(){
  status('Loading...');
  try{
    const j=await api('/api/load',{folder:$('folder').value});
    motifs=j.motifs; palettes=j.palettes; savedPalettes=j.saved_palettes; customSchemes=j.schemes; group='all';
    try{localStorage.setItem('pui_folder',j.folder)}catch(e){}
    $('skipped').textContent=j.skipped.length?'Skipped: '+j.skipped.join('; '):'';
    layers=[{name:'Layer 1',visible:true,locked:false,P:[],block:newBlock()}];L=0;block=layers[0].block;
    P=[];sel=-1;hist=[];future=[];autoLayouts=[];renderLayers();syncBlockUi();
    renderMotifs(); renderSchemes(); renderColour(); await loadMarkup(); applyScheme();
    status(`${motifs.length} motifs loaded. Pick a scheme, then drag motifs to adjust.`);
  }catch(e){status(e.message)}
}
async function loadMarkup(){
  const j=await api('/api/markup',{palette:spec(),remap,background:bgValue()});
  $('mdefs').innerHTML=j.motifs.map(m=>`${m.defs}<g id="m${m.id}">${m.body}</g>`).join('');
  $('bgRect').setAttribute('fill',j.bg); if(!$('customBg').checked) $('bgColor').value=toHex(j.bg);
  renderColorMap(j.colors); renderCurPal(); render(); renderStrip();
}
function toHex(c){return /^#[0-9a-f]{6}$/i.test(c)?c:'#f6f1e7'}
function bgValue(){return $('customBg').checked?$('bgColor').value:''}

/* ---------- side panel ---------- */
function renderMotifs(){
  const groups=['all',...new Set(motifs.map(m=>m.group))];
  $('groupTabs').innerHTML=groups.length>2?groups.map(g=>`<button class="${g===group?'on':''}" data-g="${g}">${g==='.'?'root':g}</button>`).join(''):'';
  $('groupTabs').querySelectorAll('button').forEach(b=>b.onclick=()=>{group=b.dataset.g;renderMotifs()});
  $('motifs').innerHTML=motifs.filter(m=>group==='all'||m.group===group).map(m=>`<div class="motif">
    <img src="/motif/${m.id}" data-add="${m.id}" title="Add to the tile" loading="lazy">
    <div><div class="name">${m.name}</div><div class="row" style="gap:2px"><div class="seg">${ROLES.map(r=>
      `<button data-id="${m.id}" data-role="${r}" class="${m.role===r?'on':''}">${r}</button>`).join('')}</div>
      <label class="up"><input type="checkbox" data-up="${m.id}" ${m.upright?'checked':''}> upright</label></div></div></div>`).join('');
  $('motifs').querySelectorAll('.seg button').forEach(b=>b.onclick=()=>{byId(+b.dataset.id).role=b.dataset.role;renderMotifs()});
  $('motifs').querySelectorAll('[data-up]').forEach(c=>c.onchange=()=>{byId(+c.dataset.up).upright=c.checked});
  $('motifs').querySelectorAll('[data-add]').forEach(i=>i.onclick=()=>addMotif(+i.dataset.add));
}
function renderSchemes(){
  const names=[...Object.keys(SCHEMES),...customSchemes.map(s=>s.name)];
  if(!names.includes(scheme)) scheme='Half-drop';
  $('schemeTabs').innerHTML=names.map(n=>`<button class="${n===scheme?'on':''}" data-s="${n}">${n}</button>`).join('');
  $('schemeTabs').querySelectorAll('button').forEach(b=>b.onclick=()=>{scheme=b.dataset.s;renderSchemes();applyScheme()});
}


/* ---------- placement helpers ---------- */
function ratioSize(role){const b=+$('ratioBase').value,q=+$('ratio').value;
  return role==='hero'?b:role==='medium'?b*q:b*q*q}
function roleSize(role){const s=settings(),r=Math.random();
  if($('useRatio').checked) return ratioSize(role)*(0.95+0.1*r);
  if(role==='hero') return s.hero_min+r*(s.hero_max-s.hero_min);
  if(role==='medium') return s.medium_min+r*(s.medium_max-s.medium_min);
  return s.filler_min+r*(s.filler_max_size-s.filler_min)}
function angleFor(m, base){const s=settings();const lim=(m.upright?s.upright_rotation:s.rotation)/2;
  let a=base+(Math.random()-0.5)*Math.min(lim,30); if(m.upright) a=Math.max(-lim,Math.min(lim,a)); return a}
function pool(role){const order={hero:['hero','medium','filler'],medium:['medium','hero','filler'],filler:['filler','medium','hero']}[role]||[role];
  for(const r of order){const p=motifs.filter(m=>m.role===r);if(p.length) return p}return []}
function tdist(a,b){const dx=Math.abs(a.x-b.x),dy=Math.abs(a.y-b.y);return Math.hypot(Math.min(dx,T-dx),Math.min(dy,T-dy))}
const stateStr=()=>{commit();return JSON.stringify({layers,L})};
function restore(str){const o=JSON.parse(str);layers=o.layers;L=o.L;P=layers[L].P;block=layers[L].block;syncBlockUi();renderLayers()}
function snapshot(){hist.push(stateStr());if(hist.length>200)hist.shift();future=[]}

function applyScheme(){
  if(!motifs.length) return;
  const custom=customSchemes.find(s=>s.name===scheme);
  const slots=custom?custom.slots.map(s=>[s.x,s.y,s.role,s.angle,s.s]):SCHEMES[scheme];
  snapshot(); block.on=false; syncBlockUi(); const used={}; P=[];
  for(const [x,y,role,ang,size] of slots){
    const options=pool(role); if(!options.length) continue;
    const here={x:x*T,y:y*T};
    const near=new Set(P.filter(p=>tdist(p,here)<0.3*T).map(p=>p.id));
    const m=[...options].sort((a,b)=>((near.has(a.id)-near.has(b.id))||((used[a.id]||0)-(used[b.id]||0))||(Math.random()-0.5)))[0];
    used[m.id]=(used[m.id]||0)+1;
    P.push({id:m.id,x:here.x,y:here.y,s:size||roleSize(role),angle:angleFor(m,ang||0),flip:m.upright||Math.random()<0.5?1:-1,role});
  }
  sel=-1; autoSel=null; render(); renderStrip();
  status(`Scheme "${scheme}" applied: ${P.length} motifs. Drag, rotate (wheel) or scale (Shift+wheel) to adjust.`);
}
function addMotif(id){
  const m=byId(id); if(!m) return; snapshot();
  P.push({id,x:block.on?0:T/2,y:block.on?0:T/2,s:roleSize(m.role==='off'?'medium':m.role),angle:block.on?0:angleFor(m,0),flip:1});
  sel=P.length-1; render();
}

/* ---------- editor rendering ---------- */
function xform(p,dx,dy){const m=byId(p.id);const k=p.s*T/Math.max(m.w,m.h);
  return `translate(${p.x+dx} ${p.y+dy}) rotate(${p.angle}) scale(${k*p.flip} ${k}) translate(${-m.cx} ${-m.cy})`}
const OFFS=[];for(const dy of [-T,0,T])for(const dx of [-T,0,T])OFFS.push([dx,dy]);
function render(){
  const ctx=parseFloat($('ctx').value)*T;
  $('ed').setAttribute('viewBox',`${-ctx} ${-ctx} ${T+2*ctx} ${T+2*ctx}`);
  commit();
  const dim=$('dimLayers').checked;
  $('inst').innerHTML=layers.map((l,li)=>{
    if(!l.visible) return '';
    const live=li===L&&!l.locked;
    const uses=expandLayer(l.P,l.block).map(e=>OFFS.map(([dx,dy])=>
      `<use href="#m${e.p.id}" xlink:href="#m${e.p.id}" ${live?`data-i="${e.src}" data-m="${e.m?1:0}"`:''} transform="${xform(e.p,dx,dy)}"/>`).join('')).join('');
    return `<g ${live?'':'pointer-events="none"'} opacity="${dim&&li!==L?0.35:1}">${uses}</g>`;}).join('');
  if($('layerList')) for(const el of $('layerList').querySelectorAll('.layer .muted')){const i=+el.parentElement.dataset.l;
    el.innerHTML=layers[i].P.length+(layers[i].block.on?' &#9638;':'')}
  const big=`M${-T} ${-T}H${2*T}V${2*T}H${-T}Z`, hole=`M0 0V${T}H${T}V0Z`;
  $('fadeMask').setAttribute('d',big+hole); $('fadeMask').style.display=$('fade').checked?'':'none';
  renderRef(); renderSel();
}
function renderSel(){
  const p=P[sel];
  if(!p){$('selLayer').innerHTML='';$('selInfo').textContent='';return}
  const m=byId(p.id);
  const copies=expand().filter(e=>e.src===sel);
  $('selLayer').innerHTML=copies.map((e,n)=>(block.on?[[0,0]]:OFFS).map(([dx,dy])=>`<g transform="${xform(e.p,dx,dy)}"><rect x="${m.cx-m.w/2}" y="${m.cy-m.h/2}" width="${m.w}" height="${m.h}"
    fill="none" stroke="#1a73e8" stroke-width="2" vector-effect="non-scaling-stroke" stroke-dasharray="${(dx||dy||n)?'4 4':'none'}"/></g>`).join('')).join('');
  $('selInfo').textContent=`${m.name} · size ${p.s.toFixed(3)} · ${Math.round(p.angle)}°`;
}
function renderRef(){
  if(!refImg){$('refLayer').innerHTML='';return}
  const op=$('refOp').value, offs=$('refRepeat').checked?OFFS:[[0,0]];
  $('refLayer').innerHTML=offs.map(([dx,dy])=>`<image href="${refImg}" x="${dx}" y="${dy}" width="${T}" height="${T}" preserveAspectRatio="none" opacity="${op}" pointer-events="none"/>`).join('');
}

/* ---------- mouse and keyboard ---------- */
function svgPoint(e){const svg=$('ed'),pt=svg.createSVGPoint();pt.x=e.clientX;pt.y=e.clientY;return pt.matrixTransform(svg.getScreenCTM().inverse())}
let drag=null;
$('ed').addEventListener('pointerdown',e=>{
  const i=e.target.dataset?e.target.dataset.i:undefined;
  if(i===undefined){sel=-1;renderSel();return}
  sel=+i; const pt=svgPoint(e); snapshot();
  const k=block.on?block.k:1, sx=e.target.dataset.m==='1'?-1:1;
  drag={x0:pt.x,y0:pt.y,px:P[sel].x,py:P[sel].y,moved:false,k,sx};
  $('ed').setPointerCapture(e.pointerId); render();
});
$('ed').addEventListener('pointermove',e=>{
  if(!drag) return; const pt=svgPoint(e);
  P[sel].x=drag.px+drag.sx*(pt.x-drag.x0)/drag.k; P[sel].y=drag.py+(pt.y-drag.y0)/drag.k; drag.moved=true; render();
});
$('ed').addEventListener('pointerup',()=>{
  if(!drag) return; if(!drag.moved) hist.pop();
  const p=P[sel]; if(!block.on){p.x=mod(p.x);p.y=mod(p.y)} drag=null; render();
});
let wheelTimer=null;
$('ed').addEventListener('wheel',e=>{
  if(sel<0) return; e.preventDefault();
  if(!wheelTimer) snapshot(); clearTimeout(wheelTimer); wheelTimer=setTimeout(()=>wheelTimer=null,500);
  const p=P[sel], up=e.deltaY<0;
  if(e.shiftKey) p.s=Math.max(0.01,Math.min(0.9,p.s*(up?1.05:1/1.05)));
  else p.angle+=(up?-1:1)*(e.altKey?1:5);
  render();
},{passive:false});
function act(a){
  if(sel<0||!P[sel]) return; snapshot(); const p=P[sel];
  if(a==='flip') p.flip*=-1;
  if(a==='del'){P.splice(sel,1);sel=-1}
  if(a==='dup'){P.push({...p,x:block.on?p.x+40:(p.x+60)%T,y:block.on?p.y+40:(p.y+60)%T});sel=P.length-1}
  if(a==='front'){P.push(P.splice(sel,1)[0]);sel=P.length-1}
  if(a==='back'){P.unshift(P.splice(sel,1)[0]);sel=0}
  if(a==='ring'){const fill=pool('filler');if(!fill.length){hist.pop();status('Mark some motifs as filler first.');return}
    const n=+$('ringN').value, base=p.s*T/2, step=137.508*Math.PI/180;   // golden angle around the motif
    const s0=settings(), fs=(s0.filler_min+s0.filler_max_size)/2, d=fs*T;
    for(let i=0;i<n;i++){const a=i*step, r=base*0.8+d*0.6+d*0.62*Math.sqrt(i);
      const m=fill[i%fill.length];
      P.push({id:m.id,x:p.x+r*Math.cos(a),y:p.y+r*Math.sin(a),s:fs*(1-0.25*i/n),
        angle:m.upright?0:(a*180/Math.PI+90)%360,flip:1});}
    if(!block.on) P.forEach(q=>{q.x=mod(q.x);q.y=mod(q.y)});
  }
  if(a==='next'){const m=byId(p.id);const same=motifs.filter(x=>x.role===m.role&&x.role!=='off');
    if(same.length>1){const k=same.findIndex(x=>x.id===p.id);p.id=same[(k+1)%same.length].id}}
  render();
}
document.querySelectorAll('[data-act]').forEach(b=>b.onclick=()=>act(b.dataset.act));
function undo(){if(!hist.length)return;future.push(stateStr());restore(hist.pop());sel=-1;render()}
function redo(){if(!future.length)return;hist.push(stateStr());restore(future.pop());sel=-1;render()}
document.addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT') return;
  const k=e.key.toLowerCase();
  if((e.ctrlKey||e.metaKey)&&k==='z'){e.preventDefault();e.shiftKey?redo():undo();return}
  if((e.ctrlKey||e.metaKey)&&k==='y'){e.preventDefault();redo();return}
  if(k==='delete'||k==='backspace'){e.preventDefault();act('del')}
  else if(k===']') moveToLayer(1); else if(k==='[') moveToLayer(-1);
  else if(k==='d') act('dup'); else if(k==='f') act('flip'); else if(k==='n') act('next');
  else if(k.startsWith('arrow')&&sel>=0){e.preventDefault();snapshot();const d=e.shiftKey?25:5,p=P[sel];
    if(k==='arrowleft')p.x-=d;if(k==='arrowright')p.x+=d;if(k==='arrowup')p.y-=d;if(k==='arrowdown')p.y+=d;
    if(!block.on){p.x=mod(p.x);p.y=mod(p.y)}render()}
});

/* ---------- server actions ---------- */
async function fillGaps(){
  if(block.on) bake();
  status('Filling gaps...');
  const all=concrete();
  try{const j=await api('/api/fill',{placements:all,settings:settings(),...flags()});
    snapshot(); P.push(...j.placements.slice(all.length)); sel=-1; render(); renderLayers();
    status(`Added ${j.added} fillers to "${layers[L].name}". ${j.note||''}`)}catch(e){status(e.message)}
}
async function runAuto(){
  $('autoBtn').disabled=true; status('Auto: trying layouts (a few seconds each)...');
  try{const j=await api('/api/auto',{settings:settings(),...flags()});
    autoLayouts=j.layouts; if(autoLayouts.length){snapshot();block.on=false;syncBlockUi();P=JSON.parse(JSON.stringify(autoLayouts[0].placements));autoSel=0;sel=-1;render()}
    renderStrip(); status(`Best ${autoLayouts.length} auto layouts below - click one to edit it.`)}catch(e){status(e.message)}
  $('autoBtn').disabled=false;
}
function renderStrip(){
  $('strip').innerHTML=autoLayouts.map((l,i)=>`<div class="cand ${i===autoSel?'on':''}" data-k="${i}">
    <svg viewBox="0 0 ${T} ${T}" xmlns="http://www.w3.org/2000/svg"><rect width="${T}" height="${T}" fill="${$('bgRect').getAttribute('fill')}"/>
    ${l.placements.map(p=>OFFS.map(([dx,dy])=>`<use href="#m${p.id}" transform="${xform(p,dx,dy)}"/>`).join('')).join('')}</svg>
    #${i+1} · ${l.score.toFixed(2)}</div>`).join('');
  $('strip').querySelectorAll('.cand').forEach(c=>c.onclick=()=>{autoSel=+c.dataset.k;snapshot();
    block.on=false;syncBlockUi();P=JSON.parse(JSON.stringify(autoLayouts[autoSel].placements));sel=-1;render();renderStrip()});
}
async function savePattern(){
  status('Saving...');
  try{const j=await api('/api/save',{placements:concrete(),layers:layerPayload(),palette:spec(),remap,background:bgValue(),name:$('saveName').value});
    status('Saved: '+j.saved+(j.png?'':' (PNG preview needs resvg-py)'))}catch(e){status(e.message)}
}
async function saveScheme(){
  const name=$('saveName').value.trim()||prompt('Scheme name:'); if(!name) return;
  const slots=concrete().map(p=>({x:+(p.x/T).toFixed(4),y:+(p.y/T).toFixed(4),role:byId(p.id).role==='off'?'medium':byId(p.id).role,
    angle:Math.round(p.angle),s:+p.s.toFixed(4)}));
  try{const j=await api('/api/save_scheme',{name,slots});customSchemes=j.schemes;scheme=name.replace(/[^\w-]+/g,'_');renderSchemes();
    status(`Scheme "${name}" saved - it works with any motif folder.`)}catch(e){status(e.message)}
}

/* ---------- colour ---------- */
const spec=()=>curPal.colors.length?{name:curPal.name,bg:curPal.bg,colors:curPal.colors}:(curPal.name==='original'?'original':{name:curPal.name,bg:curPal.bg,colors:[]});
const swatches=cs=>`<span class="strip4">${cs.map(c=>`<span class="sw" style="background:${c}" title="${c}"></span>`).join('')}</span>`;
function usePalette(p){curPal={name:p.name,bg:p.bg||'',colors:[...(p.colors||[])]};remap={};
  if(p.bg&&!$('customBg').checked) $('bgColor').value=toHex(p.bg); $('palName').value=p.name==='original'?'':p.name;
  renderColour(); loadMarkup();}
function renderCurPal(){$('curPal').innerHTML=`<b style="font-size:12px">${curPal.name}</b> `+swatches([$('bgRect').getAttribute('fill'),...curPal.colors])}
function renderColorMap(list){
  $('colorMap').innerHTML=list.map(c=>`<span class="cmap"><span class="sw" style="background:${c.orig}"></span>&rarr;<input type="color" data-orig="${c.orig}" value="${toHex(c.now)}"></span>`).join('');
  $('colorMap').querySelectorAll('input').forEach(i=>i.onchange=()=>{remap[i.dataset.orig]=i.value;loadMarkup()});
}
const COLTABS=['Palettes','From image','Import','Harmony'];
function renderColour(){
  $('colTabs').innerHTML=COLTABS.map(t=>`<button class="${t===colTab?'on':''}" data-t="${t}">${t}</button>`).join('');
  $('colTabs').querySelectorAll('button').forEach(b=>b.onclick=()=>{colTab=b.dataset.t;draftPal=null;renderColour()});
  const pane=$('colPane');
  if(colTab==='Palettes'){
    const all=[...palettes,...savedPalettes];
    pane.innerHTML=all.map((p,i)=>`<button class="palbtn ${p.name===curPal.name?'on':''}" data-i="${i}"><span>${p.name}${p.saved?' ★':''}</span>${swatches([p.bg||'#ffffff',...p.colors].filter(Boolean))}</button>`).join('');
    pane.querySelectorAll('.palbtn').forEach(b=>b.onclick=()=>usePalette(all[+b.dataset.i]));
  } else if(colTab==='From image'){
    pane.innerHTML=`<div class="row"><button id="exRef">From reference tile</button><input type="file" id="exFile" accept="image/*" style="max-width:100%"></div>
      <label class="s">Colours<span class="v" id="v_exN">6</span><input type="range" id="exN" min="3" max="10" step="1" value="6"></label><div id="draft"></div>`;
    $('exN').oninput=e=>$('v_exN').textContent=e.target.value;
    $('exRef').onclick=()=>refImg?extract(refImg):status('Load a reference tile first (Reference tile section).');
    $('exFile').onchange=e=>{const f=e.target.files[0];if(!f)return;const r=new FileReader();r.onload=()=>extract(r.result);r.readAsDataURL(f)};
  } else if(colTab==='Import'){
    pane.innerHTML=`<textarea id="impText" placeholder="#DA8B34 #2E1C2B #F1E4CB ...  or a QuiltFlow JSON"></textarea>
      <div class="row"><button id="impParse">Read text</button><input type="file" id="impFile" accept=".ase,.gpl,.json,.txt,.hex,.css" style="max-width:100%"></div>
      <div class="hint">Hex lists, QuiltFlow JSON, Adobe swatches (.ase), GIMP / Inkscape palettes (.gpl). The first colour becomes the background - change it below.</div><div id="draft"></div>`;
    $('impParse').onclick=()=>showDraft(parseText($('impText').value),'imported');
    $('impFile').onchange=e=>{const f=e.target.files[0];if(!f)return;const r=new FileReader();
      if(/\.ase$/i.test(f.name)){r.onload=()=>showDraft(parseAse(r.result),f.name.replace(/\.\w+$/,''));r.readAsArrayBuffer(f)}
      else{r.onload=()=>showDraft(parseText(r.result),f.name.replace(/\.\w+$/,''));r.readAsText(f)}};
  } else {
    pane.innerHTML=`<div class="row"><span class="muted">Base colour</span><input type="color" id="hBase" value="#c0542f"></div>
      <div id="hTypes" class="tabs" style="margin-top:4px"></div>
      <label class="s">Colours<span class="v" id="v_hN">5</span><input type="range" id="hN" min="3" max="7" step="1" value="5"></label>
      <div id="hBg" class="tabs"></div><div id="draft"></div>`;
    harmonyUi();
  }
}
function showDraft(colors,name,bg){
  colors=[...new Set(colors.map(c=>c.toLowerCase()))];
  if(!colors.length){status('No colours found.');return}
  draftPal={name:(name||'custom').replace(/[^\w-]+/g,'_'),bg:bg||colors[0],colors:bg?colors:colors.slice(1).length?colors.slice(1):colors};
  $('draft').innerHTML=`<div class="row" style="margin-top:6px"><span class="muted">bg</span>${swatches([draftPal.bg])}<span class="muted">colours</span>${swatches(draftPal.colors)}
    <button id="draftUse" class="primary">Use</button></div>`;
  $('draftUse').onclick=()=>usePalette(draftPal);
}
async function extract(dataUrl){
  try{const j=await api('/api/extract',{image:dataUrl,count:+$('exN').value});showDraft(j.colors,'from_image',j.bg)}catch(e){status(e.message)}
}
function parseText(t){
  try{const j=JSON.parse(t);const hx=j?.collection?.palette?.allHexCodes||j.allHexCodes||j.colors;
    if(Array.isArray(hx)){const out=hx.map(x=>typeof x==='string'?x:x.hex).filter(Boolean);
      const bg=j?.collection?.palette?.mainColors?.map(c=>c.hex).sort((a,b)=>lum(b)-lum(a))[0];
      return bg?[bg,...out.filter(c=>c.toLowerCase()!==bg.toLowerCase())]:out}}catch(e){}
  const hex=[...t.matchAll(/#?\b([0-9a-f]{6})\b/gi)].map(m=>'#'+m[1]);
  if(hex.length) return hex;
  return t.split('\n').map(l=>l.trim().match(/^(\d+)\s+(\d+)\s+(\d+)/)).filter(Boolean)   // GIMP .gpl lines
    .map(m=>'#'+[m[1],m[2],m[3]].map(v=>(+v).toString(16).padStart(2,'0')).join(''));
}
function parseAse(buf){   // Adobe Swatch Exchange: RGB, CMYK, Gray and LAB colour entries
  const v=new DataView(buf),out=[];let o=12;const n=v.getUint32(8);
  const hex=(r,g,b)=>'#'+[r,g,b].map(x=>Math.round(Math.max(0,Math.min(1,x))*255).toString(16).padStart(2,'0')).join('');
  for(let i=0;i<n&&o<buf.byteLength;i++){const type=v.getUint16(o),len=v.getUint32(o+2);let q=o+6;
    if(type===1){const nl=v.getUint16(q);q+=2+nl*2;const model=String.fromCharCode(...new Uint8Array(buf,q,4));q+=4;
      const f=k=>v.getFloat32(q+4*k);
      if(model==='RGB ') out.push(hex(f(0),f(1),f(2)));
      else if(model==='CMYK') out.push(hex((1-f(0))*(1-f(3)),(1-f(1))*(1-f(3)),(1-f(2))*(1-f(3))));
      else if(model==='Gray') out.push(hex(f(0),f(0),f(0)));
      else if(model==='LAB '){const [L,a,b]=[f(0)*100,f(1),f(2)];out.push(labHex(L,a,b))}}
    o+=6+len;}
  return out;
}
function labHex(L,a,b){let y=(L+16)/116,x=a/500+y,z=y-b/200;const f=t=>t**3>0.008856?t**3:(t-16/116)/7.787;
  x=0.95047*f(x);y=f(y);z=1.08883*f(z);
  const r=3.2406*x-1.5372*y-0.4986*z,g=-0.9689*x+1.8758*y+0.0415*z,bb=0.0557*x-0.204*y+1.057*z;
  const gm=c=>c>0.0031308?1.055*c**(1/2.4)-0.055:12.92*c;
  return '#'+[r,g,bb].map(c=>Math.round(Math.max(0,Math.min(1,gm(c)))*255).toString(16).padStart(2,'0')).join('')}
// OKLCH: perceptual colour space - equal lightness numbers look equally light
function hexToOklch(h){const c=[1,3,5].map(i=>parseInt(h.slice(i,i+2),16)/255).map(x=>x<=0.04045?x/12.92:((x+0.055)/1.055)**2.4);
  const l=Math.cbrt(0.4122214708*c[0]+0.5363325363*c[1]+0.0514459929*c[2]),m=Math.cbrt(0.2119034982*c[0]+0.6806995451*c[1]+0.1073969566*c[2]),
    s=Math.cbrt(0.0883024619*c[0]+0.2817188376*c[1]+0.6299787005*c[2]);
  const L=0.2104542553*l+0.7936177850*m-0.0040720468*s,a=1.9779984951*l-2.4285922050*m+0.4505937099*s,b=0.0259040371*l+0.7827717662*m-0.8086757660*s;
  return [L,Math.hypot(a,b),(Math.atan2(b,a)*180/Math.PI+360)%360]}
function oklchRgb(L,C,H){const a=C*Math.cos(H*Math.PI/180),b=C*Math.sin(H*Math.PI/180);
  const l=(L+0.3963377774*a+0.2158037573*b)**3,m=(L-0.1055613458*a-0.0638541728*b)**3,s=(L-0.0894841775*a-1.2914855480*b)**3;
  return [4.0767416621*l-3.3077115913*m+0.2309699292*s,-1.2684380046*l+2.6097574011*m-0.3413193965*s,-0.0041960863*l-0.7034186147*m+1.7076147010*s]}
function oklchHex(L,C,H){let lo=0,hi=C;   // shrink chroma until the colour fits sRGB
  const ok=c=>oklchRgb(L,c,H).every(v=>v>=-0.001&&v<=1.001);
  if(!ok(C)){for(let i=0;i<20;i++){const mid=(lo+hi)/2;ok(mid)?lo=mid:hi=mid}C=lo}
  const gm=c=>{c=Math.max(0,Math.min(1,c));return c>0.0031308?1.055*c**(1/2.4)-0.055:12.92*c};
  return '#'+oklchRgb(L,C,H).map(c=>Math.round(gm(c)*255).toString(16).padStart(2,'0')).join('')}
const lum=h=>hexToOklch(h)[0];
const HARMONIES={Analogous:[0,30,-30,60,-60],Complementary:[0,180,0,180,0],Split:[0,150,210,0,150],Triad:[0,120,240,0,120],Tetrad:[0,90,180,270,0],Monochrome:[0,0,0,0,0]};
let hType='Analogous', hBgMode='Light';
function harmonyUi(){
  $('hTypes').innerHTML=Object.keys(HARMONIES).map(t=>`<button class="${t===hType?'on':''}" data-t="${t}">${t}</button>`).join('');
  $('hBg').innerHTML=['Light','Dark'].map(t=>`<button class="${t===hBgMode?'on':''}" data-b="${t}">${t} background</button>`).join('');
  $('hTypes').querySelectorAll('button').forEach(b=>b.onclick=()=>{hType=b.dataset.t;harmonyUi()});
  $('hBg').querySelectorAll('button').forEach(b=>b.onclick=()=>{hBgMode=b.dataset.b;harmonyUi()});
  $('hN').oninput=e=>{$('v_hN').textContent=e.target.value;makeHarmony()}; $('hBase').oninput=makeHarmony;
  makeHarmony();
}
function makeHarmony(){
  const [L0,C0,H0]=hexToOklch($('hBase').value), n=+$('hN').value, offs=HARMONIES[hType];
  const C=Math.max(0.04,Math.min(C0,0.17)), colors=[];
  for(let i=0;i<n;i++){const L=0.32+0.56*i/(n-1);   // even light-to-dark ladder
    const taper=1-0.65*Math.max(0,(L-0.68)/0.2)-0.2*Math.max(0,(0.42-L)/0.1);   // very light/dark: less saturated
    colors.push(oklchHex(L,C*taper*(hType==='Monochrome'?1:(i%2?0.8:1)),(H0+offs[i%offs.length]+360)%360))}
  const bg=hBgMode==='Light'?oklchHex(0.96,0.015,H0):oklchHex(0.22,0.03,H0);
  showDraft(colors,`${hType.toLowerCase()}_${$('hBase').value.slice(1)}`,bg);
}
async function savePalette(){
  const name=$('palName').value.trim(); if(!name){status('Type a palette name first.');return}
  const colors=curPal.colors.length?curPal.colors.map(c=>remap[c]||c):[...new Set(Object.values(remap))];
  const bg=$('bgRect').getAttribute('fill');
  try{const j=await api('/api/save_palette',{palette:{name,bg,colors:colors.length?colors:[...document.querySelectorAll('#colorMap input')].map(i=>i.value)}});
    savedPalettes=j.palettes; colTab='Palettes'; renderColour(); status(`Palette "${name}" saved.`)}catch(e){status(e.message)}
}
async function openColorways(){
  const specs=[spec(),...palettes.filter(p=>p.colors.length&&p.name!==curPal.name),...savedPalettes.filter(p=>p.name!==curPal.name)].slice(0,9)
    .map(p=>typeof p==='string'?p:{name:p.name,bg:p.bg,colors:p.colors});
  status('Building colourways...');
  try{const j=await api('/api/colorways',{placements:concrete(),palettes:specs,remap,background:bgValue()});colorways=j.colorways;
    $('cwGrid').innerHTML=colorways.map((c,i)=>`<div class="cw"><div class="tile" style='background-image:url("data:image/svg+xml;charset=utf-8,${encodeURIComponent(c.svg)}")'></div>
      <div class="row" style="margin-top:4px"><b style="font-size:12px">${c.name}</b><span style="flex:1"></span><button data-use="${i}">Use</button><button data-save="${i}">Save</button></div></div>`).join('');
    $('cwGrid').querySelectorAll('[data-use]').forEach(b=>b.onclick=()=>{const c=colorways[+b.dataset.use];
      usePalette(typeof c.spec==='string'?{name:c.spec,bg:'',colors:[]}:c.spec);$('cwOverlay').style.display='none'});
    $('cwGrid').querySelectorAll('[data-save]').forEach(b=>b.onclick=()=>saveColorway(+b.dataset.save));
    $('cwOverlay').style.display='block'; status('');
  }catch(e){status(e.message)}
}
async function saveColorway(i){
  const c=colorways[i];
  try{const j=await api('/api/save',{placements:concrete(),layers:layerPayload(),palette:c.spec,remap:i===0?remap:{},background:i===0?bgValue():'',name:$('saveName').value});
    status('Saved: '+j.saved)}catch(e){status(e.message)}
}
$('palSave').onclick=savePalette; $('cwBtn').onclick=openColorways;
$('cwClose').onclick=()=>$('cwOverlay').style.display='none';
$('cwSaveAll').onclick=async()=>{for(let i=0;i<colorways.length;i++) await saveColorway(i); status(`Saved ${colorways.length} colourways.`)};
$('gray').onchange=()=>$('ed').classList.toggle('gray',$('gray').checked);

/* ---------- composition helpers ---------- */
function renderGuides(){
  const lines=[], add=(x1,y1,x2,y2,c)=>lines.push(`<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${c}" stroke-width="1.5" vector-effect="non-scaling-stroke" stroke-opacity=".8"/>`);
  if($('gThirds').checked) for(const f of [1/3,2/3]){add(f*T,0,f*T,T,'#1a73e8');add(0,f*T,T,f*T,'#1a73e8')}
  if($('gGolden').checked) for(const f of [0.382,0.618]){add(f*T,0,f*T,T,'#d4a017');add(0,f*T,T,f*T,'#d4a017')}
  if($('gDiag').checked){add(0,0,T,T,'#c0392b');add(T,0,0,T,'#c0392b')}
  $('guides').innerHTML=lines.join('');
}
async function checkColors(){
  $('colorOut').textContent='Checking...';
  try{const j=await api('/api/colors',{placements:concrete(),palette:spec(),remap,background:bgValue()});
    $('colorOut').innerHTML=j.shares.map(c=>`<div class="row" style="flex-wrap:nowrap"><span style="display:inline-block;width:14px;height:14px;border:1px solid #ccc;background:${c.hex}"></span>
      <span style="flex:1;height:8px;background:#eee;border-radius:4px"><span style="display:block;height:8px;border-radius:4px;width:${Math.round(c.share*100)}%;background:${c.hex};border:1px solid #bbb"></span></span>
      <span>${Math.round(c.share*100)}%</span></div>`).join('')+
      `<div style="margin-top:4px">Main ${Math.round(j.main*100)}% · second ${Math.round(j.second*100)}% · accents ${Math.round(j.accents*100)}% (aim 60 · 30 · 10)</div>`+
      j.tips.map(t=>`<div>• ${t}</div>`).join('');
  }catch(e){$('colorOut').textContent=e.message}
}
function applyRatio(){
  if(!P.length) return; snapshot();
  P.forEach(p=>{const role=byId(p.id).role;p.s=ratioSize(role==='off'?'medium':role)});
  $('useRatio').checked=true; render();
  status(`Sizes set by ratio ${$('ratio').value}: hero ${ratioSize('hero').toFixed(3)}, medium ${ratioSize('medium').toFixed(3)}, filler ${ratioSize('filler').toFixed(3)}.`);
}
['ratio','ratioBase','ringN'].forEach(id=>$(id).oninput=e=>{$('v_'+id).textContent=e.target.value});
['gThirds','gGolden','gDiag'].forEach(id=>$(id).onchange=renderGuides);
$('ratioApply').onclick=applyRatio; $('colorBtn').onclick=checkColors;

/* ---------- block mode ---------- */
function syncBlockUi(){
  $('blockOn').checked=block.on; $('bMirror').checked=block.mirror;
  $('bNc').value=block.nc; $('v_bNc').textContent=block.nc;
  $('bNr').value=block.nr; $('v_bNr').textContent=block.nr;
  $('bK').value=block.k; $('v_bK').textContent=(+block.k).toFixed(2);
  $('latTabs').innerHTML=Object.entries(LATTICES).map(([k,l])=>`<button class="${k===block.lattice?'on':''}" data-l="${k}">${l}</button>`).join('');
  $('latTabs').querySelectorAll('button').forEach(b=>b.onclick=()=>{snapshot();block.lattice=b.dataset.l;fixLattice();syncBlockUi();render()});
}
function fixLattice(){  // a half-drop needs an even column count, a brick an even row count
  if(block.lattice==='halfdrop'&&block.nc%2) block.nc=Math.min(6,block.nc+1);
  if(block.lattice==='brick'&&block.nr%2) block.nr=Math.min(6,block.nr+1);
}
function enterBlock(){
  snapshot();
  let unit;
  if(sel>=0&&P[sel]) unit=[{...P[sel],x:0,y:0,angle:0}];
  else{const heroes=pool('hero');if(!heroes.length){status('Load motifs first.');return syncBlockUi()}
    unit=[{id:heroes[0].id,x:0,y:0,s:0.45,angle:0,flip:1}]}
  P=unit; sel=0; block.on=true; fixLattice(); syncBlockUi(); render();
  status('Block mode: edit one block - every copy follows. Click motif pictures to add them to the block, turn on mirror for symmetry.');
}
function bake(){
  if(!block.on) return; snapshot(); P=expand().map(e=>e.p); block.on=false; sel=-1; syncBlockUi(); render();
  status('Blocks baked into free motifs - you can now edit every copy on its own.');
}
$('blockOn').onchange=()=>{$('blockOn').checked?enterBlock():bake()};
$('bakeBtn').onclick=bake;
$('bMirror').onchange=()=>{snapshot();block.mirror=$('bMirror').checked;render()};
['bNc','bNr'].forEach(id=>$(id).oninput=e=>{snapshot();block[id==='bNc'?'nc':'nr']=+e.target.value;fixLattice();syncBlockUi();render()});
$('bK').oninput=e=>{block.k=+e.target.value;$('v_bK').textContent=block.k.toFixed(2);render()};
syncBlockUi();

/* ---------- wiring ---------- */
document.querySelectorAll('[data-layer]').forEach(b=>b.onclick=()=>layerAct(b.dataset.layer));
$('dimLayers').onchange=render; $('toUp').onclick=()=>moveToLayer(1); $('toDown').onclick=()=>moveToLayer(-1);
renderLayers();
sliders(MAIN,'slidersMain'); sliders(AUTO,'slidersAuto');
$('loadBtn').onclick=load; $('folder').addEventListener('keydown',e=>{if(e.key==='Enter')load()});
$('applyBtn').onclick=applyScheme; $('autoBtn').onclick=runAuto; $('fillBtn').onclick=fillGaps;
$('clearBtn').onclick=()=>{snapshot();P=[];sel=-1;render()};
$('undoBtn').onclick=undo; $('redoBtn').onclick=redo;
$('saveBtn').onclick=savePattern; $('schemeSaveBtn').onclick=saveScheme;
$('ctx').oninput=render; $('fade').onchange=render;
$('bgColor').onchange=()=>{$('customBg').checked=true;loadMarkup()}; $('customBg').onchange=loadMarkup;
$('refFile').onchange=e=>{const f=e.target.files[0];if(!f)return;const r=new FileReader();
  r.onload=()=>{refImg=r.result;renderRef();status('Reference tile shown under the motifs.')};r.readAsDataURL(f)};
$('refOp').oninput=e=>{$('v_refOp').textContent=e.target.value;renderRef()};
$('refRepeat').onchange=renderRef; $('refClear').onclick=()=>{refImg=null;$('refFile').value='';renderRef()};
render();
const preload=__PRELOAD__; let last='';try{last=localStorage.getItem('pui_folder')||''}catch(e){}
$('folder').value=preload||last; if($('folder').value) load();
</script></body></html>'''


def main():
    argv, port = sys.argv[1:], PORT
    if '--port' in argv:
        i = argv.index('--port')
        port = int(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    if argv:
        STATE['preload'] = ' '.join(argv).strip().strip('"\'')
    server = ThreadingHTTPServer((HOST, port), Handler)
    url = f'http://{HOST}:{port}'
    print(f'Pattern UI running at {url}  (Ctrl+C to stop)')
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('Stopped.')


if __name__ == '__main__':
    main()
