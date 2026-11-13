#!/usr/bin/env python3
"""
PatternComposer - build seamless VECTOR patterns from a folder of SVG motifs.

Input:  a folder with SVG motifs (one motif per file: a flower, a leaf, a
        berry...). Motifs drawn by hand or vectorised clipart both work.
Output: <folder>/composed_patterns/
          <collection>_<layout>_<palette>.svg      seamless tile (clipped)
          swatch/<...>_swatch.svg                  unclipped, for an
                                                   Illustrator pattern swatch
          previews/<...>_preview_3x3.svg           3 x 3 repeat, 1K per tile
          previews/<...>_preview_3k.png            same as PNG (needs resvg-py)
          report.csv

Layouts:  grid, halfdrop, brick, scatter (tossed motifs, random rotation),
          auto (hero / medium / filler motifs placed by their real silhouettes,
          many candidates scored, the best ones kept - see --layouts auto)
Palettes: original colours + built-in palettes; colours are remapped by
          lightness, so the light/dark structure of the motifs is kept.

Seamless by construction: every motif that crosses the tile edge is copied
to the opposite side, so the tile repeats without any visible seam.

Install (only the PNG preview needs an extra package):
    pip install resvg-py

Usage:
    python PatternComposer.py D:\\motifs\\nordic_flora
    python PatternComposer.py D:\\motifs\\nordic_flora --layouts scatter --variants 3
    python PatternComposer.py D:\\motifs\\nordic_flora --palettes original,sage,noir
    python PatternComposer.py D:\\motifs\\cranes --layouts auto --palettes original

Auto layout roles: put motifs into subfolders named hero / medium / filler
(also main, secondary, small...). Without them the most detailed motifs become
heroes and the simplest ones fillers. Subfolders are read recursively.
"""

import argparse
import copy
import csv
import io
import math
import random
import re
import xml.etree.ElementTree as ET
from pathlib import Path

SVG_NS = 'http://www.w3.org/2000/svg'
XLINK_NS = 'http://www.w3.org/1999/xlink'
ET.register_namespace('', SVG_NS)
ET.register_namespace('xlink', XLINK_NS)
for prefix, uri in (('inkscape', 'http://www.inkscape.org/namespaces/inkscape'),
                    ('sodipodi', 'http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd'),
                    ('rdf', 'http://www.w3.org/1999/02/22-rdf-syntax-ns#'),
                    ('cc', 'http://creativecommons.org/ns#'),
                    ('dc', 'http://purl.org/dc/elements/1.1/')):
    ET.register_namespace(prefix, uri)

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
TILE_SIZE = 1000            # tile size in SVG units (scales freely - vector)
GRID_CELLS = 4              # cells per tile side for grid / halfdrop / brick
CELL_FILL = 0.72            # motif size as share of a grid cell
SCATTER_COUNT = 14          # motifs per tile in the scatter layout
SCATTER_COVERAGE = 0.55     # share of the tile covered by scatter motifs
SCATTER_SIZE_JITTER = (0.8, 1.2)
SCATTER_SPACING = 1.0       # 1.0 = bounding circles just touch
PREVIEW_TILE_SIZE = 1024    # px per tile in previews
PREVIEW_REPEATS = 3
LAYOUTS = ('grid', 'halfdrop', 'brick', 'scatter', 'auto')
DEFAULT_LAYOUTS = ('grid', 'halfdrop', 'brick', 'scatter')
MOTIFS_PER_PATTERN = 6     # a big folder is split into sets of this size

PALETTES = {
    # name: (background, motif colours)
    'nordic':     ('#f3eee4', ['#1e2a2f', '#47676b', '#9fb7b0', '#c98a5e']),
    'noir':       ('#161412', ['#efe8da', '#b9ae98', '#7a6f5e']),
    'terracotta': ('#f5e9dc', ['#3e4a3d', '#7a3b26', '#c4643d', '#e3a36d']),
    'sage':       ('#eef0e6', ['#2f3e34', '#6b8a6e', '#a9bfa0', '#d9a46c']),
    'navy':       ('#0f2033', ['#6c8ebf', '#e0a96d', '#f1e6d2']),
    'blush':      ('#fbf0ec', ['#5b3a3a', '#b5656b', '#e3a5a0', '#9cae94']),
}
DEFAULT_PALETTES = ('original', 'nordic', 'noir', 'terracotta')

NAMED_COLORS = {
    'black': '#000000', 'white': '#ffffff', 'red': '#ff0000', 'green': '#008000',
    'blue': '#0000ff', 'gray': '#808080', 'grey': '#808080', 'yellow': '#ffff00',
    'orange': '#ffa500', 'purple': '#800080', 'brown': '#a52a2a', 'pink': '#ffc0cb',
    'silver': '#c0c0c0', 'navy': '#000080', 'teal': '#008080', 'olive': '#808000',
    'maroon': '#800000', 'beige': '#f5f5dc', 'ivory': '#fffff0', 'gold': '#ffd700',
}
DRAWABLE = {'path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon'}
DEF_TAGS = {'linearGradient', 'radialGradient', 'clipPath', 'mask', 'pattern',
            'filter', 'symbol', 'marker'}
SKIP_TAGS = {'title', 'desc', 'metadata', 'namedview', 'style', 'script'}
REPORT_FIELDS = ['file', 'set', 'motif_files', 'layout', 'palette', 'motifs', 'instances',
                 'edge_copies', 'svg_kb', 'preview_png', 'score', 'note']


# --------------------------------------------------------------------------
# Small geometry helpers (2D affine matrices as (a, b, c, d, e, f))
# --------------------------------------------------------------------------
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mat_mul(m, n):
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (a * a2 + c * b2, b * a2 + d * b2, a * c2 + c * d2, b * c2 + d * d2,
            a * e2 + c * f2 + e, b * e2 + d * f2 + f)


def apply(m, x, y):
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


def parse_transform(text):
    matrix = IDENTITY
    if not text:
        return matrix
    for name, args in re.findall(r'(\w+)\s*\(([^)]*)\)', text):
        v = [float(x) for x in re.findall(r'-?\d*\.?\d+(?:[eE][-+]?\d+)?', args)]
        if name == 'matrix' and len(v) == 6:
            m = tuple(v)
        elif name == 'translate':
            m = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0)
        elif name == 'scale':
            m = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == 'rotate':
            r = math.radians(v[0])
            m = (math.cos(r), math.sin(r), -math.sin(r), math.cos(r), 0, 0)
            if len(v) == 3:
                m = mat_mul(mat_mul((1, 0, 0, 1, v[1], v[2]), m),
                            (1, 0, 0, 1, -v[1], -v[2]))
        elif name == 'skewX':
            m = (1, 0, math.tan(math.radians(v[0])), 1, 0, 0)
        elif name == 'skewY':
            m = (1, math.tan(math.radians(v[0])), 0, 1, 0, 0)
        else:
            continue
        matrix = mat_mul(matrix, m)
    return matrix


NUMBER = re.compile(r'-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?')
PATH_TOKEN = re.compile(r'[MmLlHhVvCcSsQqTtAaZz]|-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?')
ARG_COUNT = {'M': 2, 'L': 2, 'H': 1, 'V': 1, 'C': 6, 'S': 4, 'Q': 4, 'T': 2,
             'A': 7, 'Z': 0}


def path_points(d):
    """Endpoints and control points of a path (enough for a bounding box)."""
    tokens = PATH_TOKEN.findall(d or '')
    points, x, y, start = [], 0.0, 0.0, (0.0, 0.0)
    command, i = None, 0
    while i < len(tokens):
        if re.match(r'[A-Za-z]', tokens[i]):
            command = tokens[i]
            i += 1
            if command in 'Zz':
                x, y = start
                continue
        if command is None:
            break
        upper = command.upper()
        count = ARG_COUNT[upper]
        if i + count > len(tokens):
            break
        try:
            values = [float(t) for t in tokens[i:i + count]]
        except ValueError:
            break
        i += count
        relative = command.islower()
        if upper == 'H':
            x = values[0] + (x if relative else 0)
            points.append((x, y))
        elif upper == 'V':
            y = values[0] + (y if relative else 0)
            points.append((x, y))
        elif upper == 'A':
            rx, ry = abs(values[0]), abs(values[1])
            nx = values[5] + (x if relative else 0)
            ny = values[6] + (y if relative else 0)
            mx, my = (x + nx) / 2, (y + ny) / 2
            points += [(nx, ny), (mx - rx, my - ry), (mx + rx, my + ry)]
            x, y = nx, ny
        else:
            for k in range(0, count, 2):
                px = values[k] + (x if relative else 0)
                py = values[k + 1] + (y if relative else 0)
                points.append((px, py))
            x, y = points[-1]
            if upper == 'M':
                start = (x, y)
                command = 'l' if relative else 'L'  # extra pairs are lineto
    return points


def element_points(element, tag):
    get = lambda name, default=0.0: float(
        NUMBER.findall(element.get(name, str(default)) or str(default))[0]
        if NUMBER.findall(element.get(name, str(default)) or str(default)) else default)
    if tag == 'path':
        return path_points(element.get('d'))
    if tag == 'rect':
        x, y, w, h = get('x'), get('y'), get('width'), get('height')
        return [(x, y), (x + w, y + h)]
    if tag == 'circle':
        cx, cy, r = get('cx'), get('cy'), get('r')
        return [(cx - r, cy - r), (cx + r, cy + r)]
    if tag == 'ellipse':
        cx, cy, rx, ry = get('cx'), get('cy'), get('rx'), get('ry')
        return [(cx - rx, cy - ry), (cx + rx, cy + ry)]
    if tag == 'line':
        return [(get('x1'), get('y1')), (get('x2'), get('y2'))]
    if tag in ('polyline', 'polygon'):
        values = [float(v) for v in NUMBER.findall(element.get('points', ''))]
        return list(zip(values[0::2], values[1::2]))
    return []


def local_tag(element):
    return element.tag.split('}')[-1] if isinstance(element.tag, str) else ''


# --------------------------------------------------------------------------
# Colours
# --------------------------------------------------------------------------
def normalize_color(value):
    value = value.strip().lower()
    if value.startswith('#') and len(value) == 4:
        return '#' + ''.join(ch * 2 for ch in value[1:])
    if re.fullmatch(r'#[0-9a-f]{6}', value):
        return value
    match = re.fullmatch(r'rgb\(\s*([\d.]+%?)\s*,\s*([\d.]+%?)\s*,\s*([\d.]+%?)\s*\)', value)
    if match:
        channels = []
        for part in match.groups():
            number = float(part.rstrip('%'))
            channels.append(round(number * 2.55) if part.endswith('%') else round(number))
        return '#{:02x}{:02x}{:02x}'.format(*[max(0, min(255, c)) for c in channels])
    return NAMED_COLORS.get(value)


def lightness(hex_color):
    r, g, b = [int(hex_color[i:i + 2], 16) / 255.0 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    y = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    return 116.0 * (y ** (1 / 3) if y > 0.008856 else 7.787 * y + 16 / 116) - 16.0


COLOR_PROPERTIES = ('fill', 'stroke', 'stop-color', 'color')


def normalize_element_colors(element, found):
    """Rewrite colour attributes/styles as #rrggbb and collect them."""
    for name in COLOR_PROPERTIES:
        value = element.get(name)
        if value:
            color = normalize_color(value)
            if color:
                element.set(name, color)
                found.add(color)
    style = element.get('style')
    if style:
        parts = []
        for declaration in style.split(';'):
            if ':' in declaration:
                key, value = declaration.split(':', 1)
                key = key.strip()
                if key in COLOR_PROPERTIES:
                    color = normalize_color(value)
                    if color:
                        value = color
                        found.add(color)
                parts.append(f'{key}:{value.strip()}')
        element.set('style', ';'.join(parts))


COLOR_IN_MARKUP = re.compile(
    r'((?:fill|stroke|stop-color|color)\s*(?:=\s*"|:)\s*)(#[0-9a-f]{6})')


def recolor_markup(markup, mapping):
    return COLOR_IN_MARKUP.sub(
        lambda m: m.group(1) + mapping.get(m.group(2), m.group(2)), markup)


def palette_mapping(motif_colors, palette_colors):
    """Map motif colours to palette colours by lightness rank."""
    source = sorted(motif_colors, key=lightness)
    target = sorted(palette_colors, key=lightness)
    if not source:
        return {}
    if len(source) == 1:
        return {source[0]: target[len(target) // 2]}
    return {color: target[round(i * (len(target) - 1) / (len(source) - 1))]
            for i, color in enumerate(source)}


# --------------------------------------------------------------------------
# Motif loading
# --------------------------------------------------------------------------
class Motif:
    def __init__(self, index, name, defs_markup, body_markup, bbox, colors):
        self.index = index
        self.name = name
        self.defs_markup = defs_markup      # gradients, clipPaths... (prefixed)
        self.body_markup = body_markup      # drawing elements
        self.bbox = bbox                    # (x0, y0, x1, y1) in motif units
        self.colors = colors

    @property
    def width(self):
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self):
        return self.bbox[3] - self.bbox[1]


def collect_bbox(element, matrix, points, removed):
    tag = local_tag(element)
    if element in removed or tag in SKIP_TAGS or tag in DEF_TAGS or tag == 'defs':
        return
    matrix = mat_mul(matrix, parse_transform(element.get('transform')))
    if tag in DRAWABLE:
        points.extend(apply(matrix, x, y) for x, y in element_points(element, tag))
    for child in element:
        collect_bbox(child, matrix, points, removed)


def first_drawables(element, matrix, limit, result):
    for child in element:
        if len(result) >= limit:
            return
        tag = local_tag(child)
        if tag in SKIP_TAGS or tag in DEF_TAGS or tag == 'defs':
            continue
        child_matrix = mat_mul(matrix, parse_transform(child.get('transform')))
        if tag in DRAWABLE:
            result.append((child, child_matrix))
        else:
            first_drawables(child, child_matrix, limit, result)


def find_background(root, canvas):
    """A light shape covering (almost) the whole motif canvas is a background."""
    x0, y0, x1, y1 = canvas
    canvas_area = max(1e-9, (x1 - x0) * (y1 - y0))
    candidates = []
    first_drawables(root, IDENTITY, 3, candidates)
    removed = set()
    for element, matrix in candidates:
        points = [apply(matrix, x, y) for x, y in element_points(element, local_tag(element))]
        if not points:
            continue
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        area = (max(xs) - min(xs)) * (max(ys) - min(ys))
        fill = element.get('fill') or ''
        style_fill = re.search(r'fill:\s*(#[0-9a-f]{6})', element.get('style') or '')
        color = normalize_color(style_fill.group(1) if style_fill else fill) if (style_fill or fill) else None
        if area >= 0.97 * canvas_area and color and lightness(color) >= 90:
            removed.add(element)
    return removed


def motif_canvas(root):
    view_box = root.get('viewBox') or root.get('viewbox')
    if view_box:
        values = [float(v) for v in NUMBER.findall(view_box)]
        if len(values) == 4:
            return values[0], values[1], values[0] + values[2], values[1] + values[3]
    width = NUMBER.findall(root.get('width', '0') or '0')
    height = NUMBER.findall(root.get('height', '0') or '0')
    return 0.0, 0.0, float(width[0]) if width else 0.0, float(height[0]) if height else 0.0


def load_motif(path, index, keep_background):
    tree = ET.parse(path)
    root = tree.getroot()
    canvas = motif_canvas(root)
    prefix = f'm{index}_'

    # Prefix every id and every reference so motifs never clash.
    ids = {el.get('id') for el in root.iter() if el.get('id')}
    for element in root.iter():
        if element.get('id'):
            element.set('id', prefix + element.get('id'))
        for key, value in list(element.attrib.items()):
            if 'url(#' in value:
                value = re.sub(r'url\(#([^)]+)\)',
                               lambda m: f'url(#{prefix}{m.group(1)})'
                               if m.group(1) in ids else m.group(0), value)
                element.set(key, value)
            if key.endswith('href') and value.startswith('#') and value[1:] in ids:
                element.set(key, '#' + prefix + value[1:])

    colors = set()
    for element in root.iter():
        normalize_element_colors(element, colors)

    removed = set() if keep_background else find_background(root, canvas)

    points = []
    collect_bbox(root, IDENTITY, points, removed)
    if not points:
        raise ValueError('no drawable shapes found')
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    bbox = (min(xs), min(ys), max(xs), max(ys))

    defs, body = [], []
    for child in list(root):
        tag = local_tag(child)
        if tag in SKIP_TAGS or child in removed:
            continue
        if tag == 'defs':
            defs.extend(ET.tostring(c, encoding='unicode') for c in child)
        elif tag in DEF_TAGS:
            defs.append(ET.tostring(child, encoding='unicode'))
        else:
            for element in list(child.iter()):
                for sub in list(element):
                    if sub in removed:
                        element.remove(sub)
            body.append(ET.tostring(child, encoding='unicode'))

    clean = lambda text: re.sub(r'\sxmlns(:\w+)?="[^"]*"', '', text)
    used_colors = set()
    body_markup = clean('\n'.join(body))
    for match in COLOR_IN_MARKUP.finditer(body_markup + '\n'.join(defs)):
        used_colors.add(match.group(2))
    return Motif(index, path.stem, clean('\n'.join(defs)), body_markup, bbox,
                 sorted(used_colors or colors))


# --------------------------------------------------------------------------
# Layouts -> list of placements (motif, x, y, scale, rotation, flip)
# --------------------------------------------------------------------------
def assign_motifs(count, positions, tile, cell, rng, iterations=4000):
    """Spread motifs so equal ones sit as far apart as possible.

    Every motif is used equally often; cells are then swapped while that
    lowers the penalty for equal motifs closer than ~2.2 cells (measured on
    the wrapped tile, so neighbours across the seam count too).
    """
    n = len(positions)
    assignment = [i % count for i in range(n)]
    rng.shuffle(assignment)
    reach = 2.2 * cell

    def distance(i, j):
        dx = abs(positions[i][0] - positions[j][0])
        dy = abs(positions[i][1] - positions[j][1])
        return math.hypot(min(dx, tile - dx), min(dy, tile - dy))

    near = [[(j, reach - distance(i, j)) for j in range(n)
             if j != i and distance(i, j) < reach] for i in range(n)]

    def cell_cost(i, motif):
        return sum(w for j, w in near[i] if assignment[j] == motif)

    for _ in range(iterations):
        i, j = rng.randrange(n), rng.randrange(n)
        if assignment[i] == assignment[j]:
            continue
        before = cell_cost(i, assignment[i]) + cell_cost(j, assignment[j])
        assignment[i], assignment[j] = assignment[j], assignment[i]
        after = cell_cost(i, assignment[i]) + cell_cost(j, assignment[j])
        if after > before:
            assignment[i], assignment[j] = assignment[j], assignment[i]
    return assignment


def grid_layout(motifs, tile, cells, kind, rng):
    cell = tile / cells
    positions = []
    for row in range(cells):
        for col in range(cells):
            x = (col + 0.5) * cell
            y = (row + 0.5) * cell
            if kind == 'halfdrop' and col % 2:
                y += cell / 2
            if kind == 'brick' and row % 2:
                x += cell / 2
            positions.append((x % tile, y % tile))
    assignment = assign_motifs(len(motifs), positions, tile, cell, rng)
    placements = []
    for (x, y), index in zip(positions, assignment):
        motif = motifs[index]
        scale = CELL_FILL * cell / max(motif.width, motif.height)
        placements.append((motif, x, y, scale, 0.0, 1))
    return placements


def torus_distance(a, b, tile):
    dx = abs(a[0] - b[0])
    dy = abs(a[1] - b[1])
    return math.hypot(min(dx, tile - dx), min(dy, tile - dy))


def scatter_layout(motifs, tile, count, rotation, rng):
    radius = math.sqrt(SCATTER_COVERAGE * tile * tile / (count * math.pi))
    for _ in range(12):
        placed, failed = [], False
        items = []
        for i in range(count):
            motif = motifs[i % len(motifs)]
            items.append((motif, radius * rng.uniform(*SCATTER_SIZE_JITTER)))
        items.sort(key=lambda item: -item[1])
        for motif, r in items:
            for _attempt in range(4000):
                point = (rng.uniform(0, tile), rng.uniform(0, tile))
                if all(torus_distance(point, (px, py), tile) >= (r + pr) * SCATTER_SPACING
                       for _, px, py, pr in placed):
                    placed.append((motif, point[0], point[1], r))
                    break
            else:
                failed = True
                break
        if not failed:
            break
        radius *= 0.93  # too crowded: shrink and try again
    placements = []
    for motif, x, y, r in placed:
        # The motif's longer side spans the circle's diameter; organic motifs
        # rarely fill their box, so this packs well without real overlaps.
        scale = r / max(max(motif.width, motif.height) / 2, 1e-9)
        angle = rng.uniform(-rotation / 2, rotation / 2) if rotation else 0.0
        flip = rng.choice((1, -1))
        placements.append((motif, x, y, scale, angle, flip))
    return placements


def instance_matrix(motif, x, y, scale, angle, flip):
    cx = (motif.bbox[0] + motif.bbox[2]) / 2
    cy = (motif.bbox[1] + motif.bbox[3]) / 2
    r = math.radians(angle)
    m = (1, 0, 0, 1, x, y)
    m = mat_mul(m, (math.cos(r), math.sin(r), -math.sin(r), math.cos(r), 0, 0))
    m = mat_mul(m, (scale * flip, 0, 0, scale, 0, 0))
    return mat_mul(m, (1, 0, 0, 1, -cx, -cy))


def tile_dims(tile):
    return (tile, tile) if isinstance(tile, (int, float)) else tuple(tile)


def with_edge_copies(placements, tile):
    """Add wrapped copies of every instance that crosses the tile edge."""
    tw, th = tile_dims(tile)
    result, copies = [], 0
    for motif, x, y, scale, angle, flip in placements:
        m = instance_matrix(motif, x, y, scale, angle, flip)
        x0, y0, x1, y1 = motif.bbox
        corners = [apply(m, px, py) for px, py in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
        bx0, bx1 = min(c[0] for c in corners), max(c[0] for c in corners)
        by0, by1 = min(c[1] for c in corners), max(c[1] for c in corners)
        shifts_x = [0.0] + ([tw] if bx0 < 0 else []) + ([-tw] if bx1 > tw else [])
        shifts_y = [0.0] + ([th] if by0 < 0 else []) + ([-th] if by1 > th else [])
        for sx in shifts_x:
            for sy in shifts_y:
                result.append((motif, mat_mul((1, 0, 0, 1, sx, sy), m)))
                copies += bool(sx or sy)
    return result, copies


# --------------------------------------------------------------------------
# SVG output
# --------------------------------------------------------------------------
def fmt_matrix(m):
    return 'matrix({})'.format(' '.join(f'{v:.6g}' for v in m))


def motif_defs(motifs, mapping):
    parts = []
    for motif in motifs:
        if motif.defs_markup:
            parts.append(recolor_markup(motif.defs_markup, mapping))
        parts.append(f'<g id="motif{motif.index}">\n'
                     f'{recolor_markup(motif.body_markup, mapping)}\n</g>')
    return '\n'.join(parts)


def build_tile_svg(instances, motifs, mapping, background, tile, name):
    tw, th = tile_dims(tile)
    uses = '\n'.join(
        f'<use href="#motif{motif.index}" xlink:href="#motif{motif.index}" '
        f'transform="{fmt_matrix(m)}"/>' for motif, m in instances)
    return (
        f'<svg xmlns="{SVG_NS}" xmlns:xlink="{XLINK_NS}" width="{tw:g}" '
        f'height="{th:g}" viewBox="0 0 {tw:g} {th:g}">\n<title>{name}</title>\n'
        f'<defs>\n{motif_defs(motifs, mapping)}\n'
        f'<clipPath id="tile-clip"><rect width="{tw:g}" height="{th:g}"/></clipPath>\n'
        f'</defs>\n<g clip-path="url(#tile-clip)">\n'
        f'<rect width="{tw:g}" height="{th:g}" fill="{background}"/>\n{uses}\n</g>\n</svg>\n')


def build_swatch_svg(instances, motifs, mapping, background, tile, name):
    """Unclipped, motifs inlined: drag into Illustrator's Swatches panel."""
    tw, th = tile_dims(tile)
    defs = '\n'.join(recolor_markup(m.defs_markup, mapping) for m in motifs if m.defs_markup)
    bodies = {m.index: recolor_markup(m.body_markup, mapping) for m in motifs}
    groups = '\n'.join(f'<g transform="{fmt_matrix(m)}">\n{bodies[motif.index]}\n</g>'
                       for motif, m in instances)
    return (
        f'<svg xmlns="{SVG_NS}" xmlns:xlink="{XLINK_NS}" width="{tw:g}" '
        f'height="{th:g}" viewBox="0 0 {tw:g} {th:g}">\n<title>{name} swatch</title>\n'
        + (f'<defs>\n{defs}\n</defs>\n' if defs else '') +
        f'<rect id="pattern-bounds" width="{tw:g}" height="{th:g}" fill="none" stroke="none"/>\n'
        f'<rect width="{tw:g}" height="{th:g}" fill="{background}"/>\n{groups}\n</svg>\n')


def build_preview_svg(tile_svg, tile):
    """3 x 3 repeat of the tile, each tile shown at PREVIEW_TILE_SIZE px."""
    inner = re.search(r'<svg[^>]*>(.*)</svg>', tile_svg, re.S).group(1)
    inner = re.sub(r'<title>.*?</title>', '', inner, flags=re.S)
    tw, th = tile_dims(tile)
    k = PREVIEW_TILE_SIZE / max(tw, th)
    n = PREVIEW_REPEATS
    uses = '\n'.join(
        f'<use href="#tile" xlink:href="#tile" x="{c * tw:g}" y="{r * th:g}"/>'
        for r in range(n) for c in range(n))
    return (
        f'<svg xmlns="{SVG_NS}" xmlns:xlink="{XLINK_NS}" width="{round(tw * k * n)}" '
        f'height="{round(th * k * n)}" viewBox="0 0 {tw * n:g} {th * n:g}">\n'
        f'<defs><g id="tile">{inner}</g></defs>\n{uses}\n</svg>\n')


def render_rgba(svg_text):
    """Render SVG to an RGBA PIL image (transparent background) or None."""
    try:
        import resvg_py
        from PIL import Image
    except ImportError:
        return None
    try:
        data = resvg_py.svg_to_bytes(svg_string=svg_text)
    except TypeError:
        data = resvg_py.svg_to_bytes(svg_text)
    return Image.open(io.BytesIO(bytes(data))).convert('RGBA')


GAP_MASK_SIZE = 480          # px, longer side of the coverage map
GAP_MIN_RADIUS = 0.05        # holes smaller than this (share of tile) are fine
FILLER_MAX_SIZE = 0.11       # largest filler, share of tile width
FILLER_MIN_SIZE = 0.045


def fill_gaps(placements, tile, fillers, rng, min_radius=GAP_MIN_RADIUS, limit=60):
    """Put filler motifs into empty spots of the tile (wrap-aware).

    fillers: list of dicts {motif, max, layer, min_size, max_size}. Big holes
    get the biggest filler that fits; every filler has a per-tile quota, so
    e.g. insects stay a few accents instead of a swarm. Fillers on layer 1
    go underneath everything (background blobs), others on top.
    """
    import numpy as np
    from scipy.ndimage import distance_transform_edt
    if not fillers:
        return placements, 0, 'no filler motifs'
    tw, th = tile_dims(tile)
    instances, _ = with_edge_copies(placements, tile)
    used = sorted({p[0].index: p[0] for p in placements}.values(), key=lambda m: m.index)
    k = GAP_MASK_SIZE / max(tw, th)
    svg = build_tile_svg(instances, used, {}, 'none', tile, 'mask')
    svg = re.sub(r'width="[^"]*" height="[^"]*"',
                 f'width="{round(tw * k)}" height="{round(th * k)}"', svg, count=1)
    image = render_rgba(svg)
    if image is None:
        return placements, 0, 'gap filling needs resvg-py'
    covered = np.asarray(image)[..., 3] > 24
    h, w = covered.shape
    yy, xx = np.mgrid[0:h, 0:w]
    counts = [0] * len(fillers)
    below, above = [], []
    for _ in range(limit):
        tiled = np.tile(covered, (3, 3))
        distance = distance_transform_edt(~tiled)[h:2 * h, w:2 * w]
        y, x = np.unravel_index(np.argmax(distance), distance.shape)
        diameter = 2 * distance[y, x] / k / tw          # share of tile width
        if diameter < 2 * min_radius:
            break
        options = [i for i, f in enumerate(fillers)
                   if counts[i] < f['max'] and f['min_size'] <= diameter * 0.9]
        if not options:
            covered[max(0, y - 2):y + 3, max(0, x - 2):x + 3] = True
            continue
        # Prefer the filler whose size range suits the hole, then the least used.
        i = min(options, key=lambda i: (abs(min(fillers[i]['max_size'], diameter * 0.85)
                                            - diameter * 0.85),
                                        counts[i] / fillers[i]['max'], rng.random()))
        filler = fillers[i]
        counts[i] += 1
        size = min(filler['max_size'], max(filler['min_size'], diameter * 0.8)) * tw
        motif = filler['motif']
        scale = size / max(motif.width, motif.height)
        angle = rng.uniform(-filler['rotation'], filler['rotation'])
        item = (motif, x / k, y / k, scale, angle, rng.choice((1, -1)))
        (below if filler['layer'] <= 1 else above).append(item)
        r_px = size * k / 2 * 1.1
        dx = np.minimum(abs(xx - x), w - abs(xx - x))
        dy = np.minimum(abs(yy - y), h - abs(yy - y))
        covered |= dx * dx + dy * dy <= r_px * r_px
    return below + placements + above, len(below) + len(above), ''


def render_png(svg_text, path):
    try:
        import resvg_py
    except ImportError:
        return False
    try:
        data = resvg_py.svg_to_bytes(svg_string=svg_text)
    except TypeError:
        data = resvg_py.svg_to_bytes(svg_text)
    path.write_bytes(bytes(data))
    return True


# --------------------------------------------------------------------------
# Auto layout: hero -> medium -> filler on a wrapped tile, scored, best kept
# --------------------------------------------------------------------------
ROLE_NAMES = {'hero': 'hero', 'main': 'hero', 'large': 'hero', 'big': 'hero',
              'medium': 'medium', 'secondary': 'medium', 'mid': 'medium',
              'filler': 'filler', 'fillers': 'filler', 'small': 'filler', 'accent': 'filler'}
HERO_SIZE = (0.30, 0.40)        # longest side as share of tile width
MEDIUM_SIZE = (0.15, 0.23)
MOTIF_GAP = 0.012               # free space around every motif, share of tile
MASK_BASE = 256                 # px, silhouette render size per motif
SCORE_CELLS = 8                 # cells per side for density / tone checks
UPRIGHT_WORDS = ('crane', 'heron', 'pine', 'tree', 'bottle', 'standing', 'snowman',
                 'owl', 'bear', 'house', 'vase', 'cup', 'mug', 'stem', 'grass', 'cattail')
OUTPUT_DIRS = {'composed_patterns', 'layout_brief'}

_silhouettes = {}


def assign_roles(motifs, root):
    """Role from the parent folder name (hero/medium/filler...), else by detail.

    Without role folders the most detailed third of the motifs become heroes,
    the simplest third fillers - detailed motifs carry a pattern, simple ones
    fill it.
    """
    for motif in motifs:
        parent = motif.path.parent
        motif.role = ROLE_NAMES.get(parent.name.lower()) if parent != root else None
    unassigned = [m for m in motifs if not m.role]
    if unassigned:
        ranked = sorted(unassigned, key=lambda m: -len(m.body_markup))
        n = len(ranked)
        n_hero = max(1, round(n * 0.3))
        n_filler = round(n * 0.3) if n >= 4 else 0
        for i, motif in enumerate(ranked):
            motif.role = 'hero' if i < n_hero else 'filler' if i >= n - n_filler else 'medium'
    if not any(m.role == 'hero' for m in motifs):
        max(motifs, key=lambda m: len(m.body_markup)).role = 'hero'


def rotation_range(motif, args):
    name = motif.name.lower()
    if any(word in name for word in args.upright_words):
        return args.upright_rotation
    return args.rotation


def motif_silhouette(motif):
    """Alpha mask (PIL 'L') of the motif alone, longest side MASK_BASE px."""
    if motif.index in _silhouettes:
        return _silhouettes[motif.index]
    from PIL import Image
    x0, y0, x1, y1 = motif.bbox
    w, h = max(motif.width, 1e-6), max(motif.height, 1e-6)
    k = MASK_BASE / max(w, h)
    svg = (f'<svg xmlns="{SVG_NS}" xmlns:xlink="{XLINK_NS}" width="{max(1, round(w * k))}" '
           f'height="{max(1, round(h * k))}" viewBox="{x0:g} {y0:g} {w:g} {h:g}">'
           f'<defs>{motif.defs_markup}</defs>{motif.body_markup}</svg>')
    image = render_rgba(svg)
    mask = image.getchannel('A') if image is not None else None
    _silhouettes[motif.index] = mask
    return mask


def instance_mask(motif, size_px, angle, flip, gap_px):
    """Boolean footprint of one placed motif (same geometry as instance_matrix)."""
    import numpy as np
    from PIL import Image, ImageOps
    from scipy.ndimage import binary_dilation
    image = motif_silhouette(motif)
    if flip < 0:
        image = ImageOps.mirror(image)
    s = size_px / max(image.size)
    image = image.resize((max(1, round(image.width * s)), max(1, round(image.height * s))),
                         Image.BILINEAR)
    image = image.rotate(-angle, resample=Image.BILINEAR, expand=True)  # SVG y points down
    mask = np.asarray(image) > 100
    if gap_px > 0:
        mask = binary_dilation(mask, iterations=int(gap_px))
    return mask


def torus_index(mask, cx, cy, size):
    import numpy as np
    h, w = mask.shape
    ys = (np.arange(h) + int(round(cy - h / 2))) % size
    xs = (np.arange(w) + int(round(cx - w / 2))) % size
    return np.ix_(ys, xs)


def torus_free_distance(covered):
    """Distance (px) from every pixel to the nearest covered pixel, wrap-aware."""
    import numpy as np
    from scipy.ndimage import distance_transform_edt
    h, w = covered.shape
    if not covered.any():
        return np.full(covered.shape, float(max(h, w)))
    return distance_transform_edt(~np.tile(covered, (3, 3)))[h:2 * h, w:2 * w]


def auto_layout(motifs, tile, args, rng):
    """Big motifs first, spread out; medium ones into the biggest holes; then fillers."""
    import numpy as np
    size = GAP_MASK_SIZE
    k = size / tile
    gap_px = max(1, round(MOTIF_GAP * size))
    covered = np.zeros((size, size), bool)
    placements, usage = [], {m.index: 0 for m in motifs}
    heroes = [m for m in motifs if m.role == 'hero']
    mediums = [m for m in motifs if m.role == 'medium'] or heroes
    fillers = [m for m in motifs if m.role == 'filler']

    def pick_motif(pool, x, y):
        near = {p[0].index for p in placements
                if torus_distance((x, y), (p[1], p[2]), tile) < 0.25 * tile}
        return min(pool, key=lambda m: (m.index in near, usage[m.index], rng.random()))

    def place(pool, size_range, hole_limited):
        distance = torus_free_distance(covered)
        best = distance.max()
        if best < gap_px * 2:
            return False
        ys, xs = np.nonzero(distance >= best * 0.75)       # among the biggest holes
        i = rng.randrange(len(ys))
        cy, cx = ys[i], xs[i]
        motif = pick_motif(pool, cx / k, cy / k)
        share = rng.uniform(*size_range)
        if hole_limited:   # a motif rarely fills its box: allow ~1.3x the hole
            share = min(share, 2.6 * distance[cy, cx] / size)
        r = rotation_range(motif, args)
        for _ in range(6):
            angle = rng.uniform(-r / 2, r / 2) if r else 0.0
            flip = rng.choice((1, -1))
            mask = instance_mask(motif, share * size, angle, flip, gap_px)
            index = torus_index(mask, cx, cy, size)
            if mask.shape[0] < size and mask.shape[1] < size and not (covered[index] & mask).any():
                covered[index] = covered[index] | mask
                placements.append((motif, cx / k, cy / k, share * tile / max(motif.width, motif.height),
                                   angle, flip))
                usage[motif.index] += 1
                return True
            share *= 0.88
            if share < size_range[0] * 0.6:
                break
        covered[max(0, cy - 2):cy + 3, max(0, cx - 2):cx + 3] = True   # skip this spot
        return False

    for _ in range(args.hero_count):
        place(heroes, HERO_SIZE, hole_limited=bool(placements))
    misses = 0
    while covered.mean() < args.density and misses < 25:
        misses = 0 if place(mediums, MEDIUM_SIZE, hole_limited=True) else misses + 1
    note = ''
    if fillers:
        filler_specs = [{'motif': m, 'max': args.filler_max, 'layer': 2,
                         'min_size': FILLER_MIN_SIZE, 'max_size': FILLER_MAX_SIZE,
                         'rotation': rotation_range(m, args) / 2} for m in fillers]
        placements, _, note = fill_gaps(placements, tile, filler_specs, rng)
    return placements, note


def score_layout(placements, tile):
    """Lower is better. Even density, no big holes, no stripes, no twins, even tone."""
    import numpy as np
    instances, _ = with_edge_copies(placements, tile)
    used = sorted({p[0].index: p[0] for p in placements}.values(), key=lambda m: m.index)
    size = GAP_MASK_SIZE
    svg = build_tile_svg(instances, used, {}, 'none', tile, 'score')
    svg = re.sub(r'width="[^"]*" height="[^"]*"', f'width="{size}" height="{size}"', svg, count=1)
    image = render_rgba(svg)
    if image is None:
        return None
    rgba = np.asarray(image).astype(float) / 255
    covered = rgba[..., 3] > 0.1
    c = SCORE_CELLS
    cells = covered.reshape(c, size // c, c, size // c).mean(axis=(1, 3))
    density = cells.std() / max(cells.mean(), 1e-6)
    hole = torus_free_distance(covered).max() / size
    # Ink tone per cell: dark/saturated motif pixels should be spread evenly too.
    lum = rgba[..., :3] @ np.array([0.299, 0.587, 0.114])
    ink = rgba[..., 3] * (1 - lum)
    ink_cells = ink.reshape(c, size // c, c, size // c).mean(axis=(1, 3))
    tone = ink_cells.std() / max(ink_cells.mean(), 1e-6)
    # Stripes: coverage profiles along rows, columns and both diagonals.
    win = max(1, size // 16)
    kernel = np.ones(win) / win

    def profile_spread(profile):
        smooth = np.convolve(np.concatenate([profile[-win:], profile, profile[:win]]),
                             kernel, mode='same')[win:-win]
        return smooth.std() / max(smooth.mean(), 1e-6)

    rows = np.arange(size)
    diag1 = np.array([np.roll(covered[i], -i) for i in rows]).mean(axis=0)
    diag2 = np.array([np.roll(covered[i], i) for i in rows]).mean(axis=0)
    stripes = max(profile_spread(p) for p in (covered.mean(1), covered.mean(0), diag1, diag2))
    twins = sum(1 for i, a in enumerate(placements) for b in placements[i + 1:]
                if a[0].index == b[0].index
                and torus_distance((a[1], a[2]), (b[1], b[2]), tile) < 0.22 * tile)
    total = density + 4 * hole + 1.5 * stripes + 0.6 * tone + 0.15 * twins
    return {'score': round(total, 3), 'density': round(density, 3), 'hole': round(hole, 3),
            'stripes': round(stripes, 3), 'tone': round(tone, 3), 'twins': twins,
            'coverage': round(float(covered.mean()), 3)}


def contact_sheet(tiles, path, cell=360, columns=4):
    """Each kept tile as a 2x2 repeat with its rank and score."""
    from PIL import Image, ImageDraw
    rows = math.ceil(len(tiles) / columns)
    sheet = Image.new('RGB', (columns * cell, rows * (cell + 26)), 'white')
    draw = ImageDraw.Draw(sheet)
    for i, (label, tile_svg) in enumerate(tiles):
        half = cell // 2
        svg = re.sub(r'width="[^"]*" height="[^"]*"', f'width="{half}" height="{half}"',
                     tile_svg, count=1)
        image = render_rgba(svg)
        if image is None:
            return False
        x, y = (i % columns) * cell, (i // columns) * (cell + 26)
        for dx in (0, half):
            for dy in (0, half):
                sheet.paste(image.convert('RGB'), (x + dx, y + 26 + dy))
        draw.text((x + 6, y + 6), label, fill='black')
    sheet.save(path)
    return True


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------
def ensure_dir(path):
    """mkdir that also works on Google Drive (spurious FileExistsError)."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        if path.is_file():
            raise


def safe_name(text):
    return re.sub(r'[^\w-]+', '_', text, flags=re.UNICODE).strip('_') or 'pattern'


def choose_palettes(requested, motifs, background):
    all_colors = sorted({c for m in motifs for c in m.colors})
    result = []
    for name in requested:
        if name == 'original':
            if background:
                bg = background
            else:
                mean_l = sum(lightness(c) for c in all_colors) / max(1, len(all_colors))
                bg = '#1f1d1a' if mean_l > 62 else '#f6f1e7'
            result.append(('original', bg, {}))
        elif name in PALETTES:
            bg, colors = PALETTES[name]
            result.append((name, bg, palette_mapping(all_colors, colors)))
        else:
            print(f'Unknown palette "{name}" skipped. Known: original, '
                  + ', '.join(PALETTES))
    return result


def make_sets(motifs, per_pattern, sets, rng):
    """Split motifs into sets; every motif is used in at least one set."""
    per_pattern = max(1, per_pattern)
    if len(motifs) <= per_pattern + 2 and sets <= 1:
        return [motifs]
    order = motifs[:]
    rng.shuffle(order)
    groups = [order[i:i + per_pattern] for i in range(0, len(order), per_pattern)]
    if len(groups) > 1 and len(groups[-1]) < per_pattern:
        tail = groups[-1]
        extra = [m for m in order if m not in tail]
        tail += rng.sample(extra, min(per_pattern - len(tail), len(extra)))
    while sets and len(groups) < sets:  # more patterns: extra random sets
        groups.append(rng.sample(motifs, min(per_pattern, len(motifs))))
    if sets:
        groups = groups[:sets]
    return [sorted(group, key=lambda m: m.index) for group in groups]


# --------------------------------------------------------------------------
# AI art-director mode: brief for an LLM, then build its JSON plan
# --------------------------------------------------------------------------
BRIEF_CELL = 320


def dominant_colors(motif, limit=3):
    counts = {}
    for color in COLOR_IN_MARKUP.findall(motif.body_markup):
        counts[color[1]] = counts.get(color[1], 0) + 1
    return [c for c, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:limit]]


def motif_sheet_svg(motifs, columns=4):
    rows = math.ceil(len(motifs) / columns)
    cell, pad, label = BRIEF_CELL, 28, 40
    width, height = columns * cell, rows * (cell + label)
    parts = [f'<svg xmlns="{SVG_NS}" xmlns:xlink="{XLINK_NS}" width="{width}" '
             f'height="{height}" viewBox="0 0 {width} {height}">',
             f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
             '<defs>' + motif_defs(motifs, {}) + '</defs>']
    for i, motif in enumerate(motifs):
        cx = (i % columns) * cell
        cy = (i // columns) * (cell + label)
        scale = (cell - 2 * pad) / max(motif.width, motif.height)
        m = instance_matrix(motif, cx + cell / 2, cy + label + (cell - pad) / 2, scale, 0, 1)
        parts.append(f'<rect x="{cx + 4}" y="{cy + 4}" width="{cell - 8}" '
                     f'height="{cell + label - 8}" fill="none" stroke="#dddddd"/>')
        parts.append(f'<text x="{cx + 14}" y="{cy + 30}" font-family="Arial, sans-serif" '
                     f'font-size="22" font-weight="bold" fill="#c0392b">#{i + 1}</text>')
        parts.append(f'<use href="#motif{motif.index}" xlink:href="#motif{motif.index}" '
                     f'transform="{fmt_matrix(m)}"/>')
    parts.append('</svg>')
    return '\n'.join(parts)


BRIEF_TEXT = """You are an experienced surface pattern designer (textiles, wallpaper,
wrapping paper). Design {count} different seamless repeating patterns using ONLY the
vector motifs below. The attached image "motif_sheet.png" shows each motif with its
number. A script will place the real vector motifs exactly where you say.

MOTIFS
{motif_list}

HOW TO THINK
- First decide the role of each motif: hero (largest, 2-5 per tile), secondary,
  filler (small: insects, dots, sprigs), background shapes (blobs, blocks: placed
  on a lower layer, can sit behind heroes).
- Size hierarchy makes a pattern look designed. Hero ~0.22-0.35, secondary
  ~0.12-0.2, fillers ~0.05-0.1 (size = longest side as a share of tile width).
- Even density: no big empty holes, no clumps; fillers go into gaps.
- Motifs must not touch edge-to-edge by accident: either a clear gap or an
  intentional overlap (e.g. a flower in front of a background blob).
- The same motif should not sit next to itself; vary rotation and flip of
  insects and loose elements; keep stems/grasses mostly upright unless the
  pattern is a tossed/scattered design.
- The tile repeats seamlessly: x and y may go slightly below 0 or above 1;
  the part outside the tile appears on the opposite side. Use this - a good
  repeat has motifs crossing the edges, otherwise the tile grid becomes visible.
- Typical tile: 18-40 items. Make the {count} patterns clearly different
  (e.g. dense tossed, airy with background blobs, half-drop-like rhythm).
- Background colour: pick one that suits the motifs' colours. Check the motif
  colours above: if motifs have dark outlines or stems (e.g. near-black), use a
  light or mid-tone background, otherwise the linework disappears.

ANSWER FORMAT: only JSON, no comments, exactly this structure:
{{
  "patterns": [
    {{
      "name": "short_name",
      "tile": {{"width": 1.0, "height": 1.0}},
      "background": "#f6efe2",
      "items": [
        {{"motif": 1, "x": 0.25, "y": 0.40, "size": 0.30, "rotation": 0, "flip": false, "layer": 2}}
      ]
    }}
  ]
}}
Fields: motif = number from the sheet; x, y = centre of the motif in tile units
(0..1, may slightly exceed); size = longest side / tile width; rotation in
degrees, clockwise; flip = mirror horizontally; layer = drawing order (1 =
bottom, background shapes; higher = on top). tile.height may differ from 1 for a
taller or wider repeat.
"""


def write_brief(motifs, out_dir, patterns_requested):
    ensure_dir(out_dir)
    lines = []
    for i, motif in enumerate(motifs, start=1):
        aspect = motif.width / max(motif.height, 1e-9)
        shape = 'wide' if aspect > 1.3 else 'tall' if aspect < 0.77 else 'compact'
        colors = ', '.join(dominant_colors(motif)) or 'n/a'
        lines.append(f'#{i}: file "{motif.name}", {shape} (w:h = {aspect:.2f}), '
                     f'main colours {colors}')
    text = BRIEF_TEXT.format(count=patterns_requested, motif_list='\n'.join(lines))
    (out_dir / 'layout_brief.txt').write_text(text, encoding='utf-8')
    sheet = motif_sheet_svg(motifs)
    (out_dir / 'motif_sheet.svg').write_text(sheet, encoding='utf-8')
    # Numbers are drawn with Pillow: the SVG renderer may have no fonts.
    png_path = out_dir / 'motif_sheet.png'
    png = render_png(re.sub(r'<text[^>]*>.*?</text>', '', sheet), png_path)
    if png:
        try:
            from PIL import Image, ImageDraw, ImageFont
            image = Image.open(png_path).convert('RGB')
            draw = ImageDraw.Draw(image)
            try:
                font = ImageFont.truetype('arialbd.ttf', 26)
            except OSError:
                try:
                    font = ImageFont.load_default(size=26)
                except TypeError:
                    font = ImageFont.load_default()
            for i in range(len(motifs)):
                x = (i % 4) * BRIEF_CELL + 14
                y = (i // 4) * (BRIEF_CELL + 40) + 8
                draw.text((x, y), f'#{i + 1}', fill=(192, 57, 43), font=font)
            image.save(png_path)
        except ImportError:
            pass
    return png


def read_plans(path):
    """Read one JSON file (or every .json in a folder); tolerate ``` fences."""
    files = sorted(path.glob('*.json')) if path.is_dir() else [path]
    plans = []
    for file in files:
        import json
        text = file.read_text(encoding='utf-8')
        text = re.sub(r'^\s*```(?:json)?|```\s*$', '', text.strip(), flags=re.M)
        data = json.loads(text)
        items = data.get('patterns', data) if isinstance(data, dict) else data
        if isinstance(items, dict):
            items = [items]
        for i, plan in enumerate(items, start=1):
            plan.setdefault('name', f'{file.stem}_{i}')
            plans.append(plan)
    return plans


def plan_placements(plan, motifs):
    tile = plan.get('tile') or {}
    tw = TILE_SIZE * float(tile.get('width', 1.0))
    th = TILE_SIZE * float(tile.get('height', 1.0))
    by_name = {m.name: m for m in motifs}
    items = sorted(plan.get('items', []), key=lambda it: float(it.get('layer', 1)))
    placements, problems = [], []
    for item in items:
        key = item.get('motif')
        motif = None
        if isinstance(key, (int, float)) or (isinstance(key, str) and key.strip('#').isdigit()):
            number = int(str(key).strip('#'))
            if 1 <= number <= len(motifs):
                motif = motifs[number - 1]
        elif isinstance(key, str):
            motif = by_name.get(key) or by_name.get(Path(key).stem)
        if motif is None:
            problems.append(f'unknown motif {key!r}')
            continue
        size = float(item.get('size', 0.15)) * TILE_SIZE
        scale = size / max(motif.width, motif.height, 1e-9)
        x = (float(item.get('x', 0.5)) * TILE_SIZE) % tw
        y = (float(item.get('y', 0.5)) * TILE_SIZE) % th
        flip = -1 if item.get('flip') in (True, 'true', 1, 'yes') else 1
        placements.append((motif, x, y, scale, float(item.get('rotation', 0)), flip))
    return placements, (tw, th), problems


def choose_fillers(plan, motifs, keys=None):
    """Filler specs from the plan ("fillers"), the CLI, or small plan motifs.

    Plan entries may be numbers or objects like
    {"motif": 14, "max": 4, "layer": 1, "min_size": 0.08, "max_size": 0.16}.
    """
    def lookup(key):
        key = str(key).strip().strip('#')
        if key.isdigit() and 1 <= int(key) <= len(motifs):
            return motifs[int(key) - 1]
        return next((m for m in motifs if m.name == key), None)

    def spec(entry):
        data = entry if isinstance(entry, dict) else {'motif': entry}
        motif = lookup(data.get('motif'))
        if motif is None:
            return None
        return {'motif': motif, 'max': int(data.get('max', 3)),
                'layer': float(data.get('layer', 4)),
                'min_size': float(data.get('min_size', FILLER_MIN_SIZE)),
                'max_size': float(data.get('max_size', FILLER_MAX_SIZE)),
                'rotation': float(data.get('rotation', 30))}

    if keys:
        if isinstance(keys, str):
            keys = [k for k in keys.split(',') if k.strip()]
        return [f for f in (spec(k) for k in keys) if f]
    small = {}
    for item in plan.get('items', []):
        motif = lookup(item.get('motif'))
        if motif and float(item.get('size', 1)) <= 0.1:
            small[motif.index] = item.get('motif')
    return [f for f in (spec(k) for k in small.values()) if f]


def build_from_plans(plan_path, motifs, args, out_dir):
    swatch_dir, preview_dir = out_dir / 'swatch', out_dir / 'previews'
    for directory in (out_dir, swatch_dir, preview_dir):
        ensure_dir(directory)
    rows = []
    for plan in read_plans(plan_path):
        placements, tile, problems = plan_placements(plan, motifs)
        if placements and not args.no_fill:
            filler_keys = plan.get('fillers') or args.fillers
            fillers = choose_fillers(plan, motifs, filler_keys)
            placements, added, note = fill_gaps(placements, tile, fillers,
                                                random.Random(args.seed))
            if note:
                problems.append(note)
            elif added:
                print(f'  plan "{plan["name"]}": +{added} fillers in empty spots')
        if not placements:
            print(f'  plan "{plan["name"]}": no usable items ({"; ".join(problems)})')
            continue
        used = sorted({p[0].index: p[0] for p in placements}.values(), key=lambda m: m.index)
        instances, copies = with_edge_copies(placements, tile)
        palettes = choose_palettes([p.strip() for p in args.palettes.split(',') if p.strip()],
                                   used, plan.get('background') or args.background)
        for palette_name, background, mapping in palettes:
            name = f'{safe_name(plan["name"])}_{palette_name}'
            tile_svg = build_tile_svg(instances, used, mapping, background, tile, name)
            (out_dir / f'{name}.svg').write_text(tile_svg, encoding='utf-8')
            (swatch_dir / f'{name}_swatch.svg').write_text(
                build_swatch_svg(instances, used, mapping, background, tile, name),
                encoding='utf-8')
            preview_svg = build_preview_svg(tile_svg, tile)
            (preview_dir / f'{name}_preview_3x3.svg').write_text(preview_svg, encoding='utf-8')
            png = False if args.no_render else render_png(
                preview_svg, preview_dir / f'{name}_preview_3k.png')
            rows.append({'file': f'{name}.svg', 'set': 'plan',
                         'motif_files': ' '.join(m.name for m in used), 'layout': 'ai_plan',
                         'palette': palette_name, 'motifs': len(used),
                         'instances': len(placements), 'edge_copies': copies,
                         'svg_kb': f'{len(tile_svg.encode("utf-8")) / 1024:.0f}',
                         'preview_png': png, 'note': '; '.join(problems)})
            print(f'  {name}.svg  ({len(placements)} items, +{copies} edge copies'
                  + (f'; {len(problems)} skipped' if problems else '') + ')')
    return rows


def run_auto(motifs, folder, requested_palettes, args):
    """Auto layout: try many arrangements, keep the best-scored ones."""
    try:
        import numpy  # noqa: F401
        import scipy  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        print('Auto layout needs numpy, scipy and pillow: pip install numpy scipy pillow')
        return
    if render_rgba('<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"/>') is None:
        print('Auto layout needs resvg-py: pip install resvg-py')
        return
    assign_roles(motifs, folder)
    for role in ('hero', 'medium', 'filler'):
        names = ', '.join(m.name for m in motifs if m.role == role) or '-'
        print(f'  {role}: {names}')
    tile = TILE_SIZE
    collection = safe_name(folder.name)
    out_dir = folder / 'composed_patterns' / 'auto'
    swatch_dir, preview_dir = out_dir / 'swatch', out_dir / 'previews'
    for directory in (out_dir, swatch_dir, preview_dir):
        ensure_dir(directory)

    print(f'Auto: trying {args.candidates} layouts...')
    scored = []
    for n in range(args.candidates):
        seed = args.seed * 100003 + n
        placements, note = auto_layout(motifs, tile, args, random.Random(seed))
        result = score_layout(placements, tile)
        scored.append((result['score'], seed, placements, result, note))
        print(f'  layout {n + 1:>3}/{args.candidates}: score {result["score"]:.3f} '
              f'({len(placements)} motifs, coverage {result["coverage"]:.2f})')
    scored.sort(key=lambda item: item[0])
    kept = scored[:max(1, args.keep)]

    palettes = choose_palettes(requested_palettes, motifs, args.background)
    rows, sheets, log = [], {}, []
    for rank, (score, seed, placements, result, note) in enumerate(kept, start=1):
        instances, copies = with_edge_copies(placements, tile)
        used = sorted({p[0].index: p[0] for p in placements}.values(), key=lambda m: m.index)
        log.append({'rank': rank, 'seed': seed, **result,
                    'motifs': sorted({p[0].name for p in placements})})
        for palette_name, background, mapping in palettes:
            name = f'{collection}_auto_r{rank:02d}_{palette_name}'
            tile_svg = build_tile_svg(instances, used, mapping, background, tile, name)
            (out_dir / f'{name}.svg').write_text(tile_svg, encoding='utf-8')
            (swatch_dir / f'{name}_swatch.svg').write_text(
                build_swatch_svg(instances, used, mapping, background, tile, name),
                encoding='utf-8')
            preview_svg = build_preview_svg(tile_svg, tile)
            (preview_dir / f'{name}_preview_3x3.svg').write_text(preview_svg, encoding='utf-8')
            png = False if args.no_render else render_png(
                preview_svg, preview_dir / f'{name}_preview_3k.png')
            sheets.setdefault(palette_name, []).append((f'#{rank}  score {score:.2f}', tile_svg))
            rows.append({'file': f'{name}.svg', 'set': 'auto',
                         'motif_files': ' '.join(m.name for m in used), 'layout': 'auto',
                         'palette': palette_name, 'motifs': len(used),
                         'instances': len(placements), 'edge_copies': copies,
                         'svg_kb': f'{len(tile_svg.encode("utf-8")) / 1024:.0f}',
                         'preview_png': png, 'score': score, 'note': note})
            print(f'  #{rank} {name}.svg  score {score:.3f}')
    for palette_name, tiles in sheets.items():
        contact_sheet(tiles, out_dir / f'{collection}_auto_{palette_name}_contact_sheet.png')
    import json
    (out_dir / 'auto_layouts.json').write_text(json.dumps(log, indent=1), encoding='utf-8')
    with open(out_dir / 'report.csv', 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f'Auto done: {len(kept)} best layouts in {out_dir} (see *_contact_sheet.png)')


def main():
    parser = argparse.ArgumentParser(
        description='Compose seamless vector patterns from a folder of SVG motifs.')
    parser.add_argument('input_folder', nargs='+', help='Folder with SVG motifs.')
    parser.add_argument('--layouts', default=','.join(DEFAULT_LAYOUTS),
                        help='Comma list: grid, halfdrop, brick, scatter, auto '
                             '(default: all except auto).')
    parser.add_argument('--palettes', default=','.join(DEFAULT_PALETTES),
                        help='Comma list: original, ' + ', '.join(PALETTES) + '.')
    parser.add_argument('--variants', type=int, default=1,
                        help='Different random arrangements per layout (default 1).')
    parser.add_argument('--cells', type=int, default=GRID_CELLS,
                        help='Cells per tile side for grid layouts (default 4, '
                             'use an even number for halfdrop/brick).')
    parser.add_argument('--count', type=int, default=SCATTER_COUNT,
                        help='Motifs per tile in scatter (default 14).')
    parser.add_argument('--rotation', type=float, default=360,
                        help='Max rotation range for scatter in degrees (default 360, '
                             '0 = upright, 30 = gentle tilt).')
    parser.add_argument('--background', default=None,
                        help='Background colour for the original palette, e.g. "#f3eee4".')
    parser.add_argument('--keep_motif_bg', action='store_true',
                        help='Do not remove white/light background rectangles in motifs.')
    parser.add_argument('--per_pattern', type=int, default=MOTIFS_PER_PATTERN,
                        help='Motifs per pattern; a larger folder is split into sets '
                             f'(default {MOTIFS_PER_PATTERN}).')
    parser.add_argument('--sets', type=int, default=0,
                        help='Number of motif sets (patterns per layout and palette). '
                             'Default: enough sets to use every motif once.')
    parser.add_argument('--make_brief', action='store_true',
                        help='Write motif_sheet.png + layout_brief.txt for ChatGPT/Claude/'
                             'Gemini, then stop.')
    parser.add_argument('--brief_patterns', type=int, default=3,
                        help='How many pattern plans to ask the AI for (default 3).')
    parser.add_argument('--no_fill', action='store_true',
                        help='Plan mode: do not fill empty spots with small motifs.')
    parser.add_argument('--fillers', default=None,
                        help='Plan mode: motif numbers used as fillers, e.g. "10,15,17,19" '
                             '(default: motifs the plan uses at size <= 0.1).')
    parser.add_argument('--plan', default=None,
                        help='JSON plan from the AI (file, or folder of .json files); '
                             'builds the patterns it describes.')
    parser.add_argument('--candidates', type=int, default=40,
                        help='Auto: layouts tried per palette set (default 40).')
    parser.add_argument('--keep', type=int, default=8,
                        help='Auto: best layouts saved (default 8).')
    parser.add_argument('--hero_count', type=int, default=3,
                        help='Auto: big hero motifs per tile (default 3).')
    parser.add_argument('--density', type=float, default=0.42,
                        help='Auto: tile share covered by hero + medium motifs before '
                             'fillers (default 0.42).')
    parser.add_argument('--filler_max', type=int, default=8,
                        help='Auto: max copies of each filler motif per tile (default 8).')
    parser.add_argument('--upright', default=','.join(UPRIGHT_WORDS),
                        help='Auto: name words of motifs that must stay upright.')
    parser.add_argument('--upright_rotation', type=float, default=24,
                        help='Auto: rotation range in degrees for upright motifs (default 24).')
    parser.add_argument('--seed', type=int, default=1, help='Random seed.')
    parser.add_argument('--no_render', action='store_true', help='Skip PNG previews.')
    args = parser.parse_args()

    folder = Path(' '.join(args.input_folder).strip().strip('"\''))
    if not folder.is_dir():
        raise FileNotFoundError(f'Input folder not found: {folder}')
    paths = sorted(p for p in folder.rglob('*.svg')
                   if p.is_file() and not OUTPUT_DIRS & set(p.relative_to(folder).parts))
    if not paths:
        print(f'No SVG motifs found in {folder}.')
        return

    motifs = []
    for path in paths:
        try:
            motif = load_motif(path, len(motifs), args.keep_motif_bg)
            motif.path = path
            motifs.append(motif)
            print(f'  motif {path.name}: {len(motif.colors)} colours, '
                  f'{motif.width:.0f}x{motif.height:.0f}')
        except Exception as error:
            print(f'  skipped {path.name}: {error}')
    if not motifs:
        print('No usable motifs.')
        return

    if args.make_brief:
        brief_dir = folder / 'layout_brief'
        png = write_brief(motifs, brief_dir, args.brief_patterns)
        print(f'Brief written to {brief_dir}')
        print('  1. Give the AI motif_sheet.' + ('png' if png else 'svg (install resvg-py for PNG)')
              + ' and the text of layout_brief.txt')
        print('  2. Save its JSON answer as plan.json')
        print(f'  3. Run: python PatternComposer.py "{folder}" --plan plan.json')
        return
    if args.plan:
        plan_path = Path(args.plan.strip('"\''))
        if not plan_path.is_absolute() and not plan_path.exists():
            plan_path = folder / plan_path
        rows = build_from_plans(plan_path, motifs, args, folder / 'composed_patterns' / 'ai_plans')
        with open(folder / 'composed_patterns' / 'ai_plans' / 'report.csv', 'w', newline='',
                  encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        print(f'Done: {len(rows)} patterns in {folder / "composed_patterns" / "ai_plans"}')
        return

    layouts = [l.strip() for l in args.layouts.split(',') if l.strip() in LAYOUTS]
    requested_palettes = [p.strip() for p in args.palettes.split(',') if p.strip()]
    args.upright_words = [w.strip().lower() for w in args.upright.split(',') if w.strip()]
    if 'auto' in layouts:
        layouts.remove('auto')
        run_auto(motifs, folder, requested_palettes, args)
        if not layouts:
            return
    motif_sets = make_sets(motifs, args.per_pattern, args.sets, random.Random(args.seed))
    collection = safe_name(folder.name)
    out_dir = folder / 'composed_patterns'
    swatch_dir = out_dir / 'swatch'
    preview_dir = out_dir / 'previews'
    for directory in (out_dir, swatch_dir, preview_dir):
        ensure_dir(directory)
    tile = TILE_SIZE
    cells = max(2, args.cells)
    total = len(motif_sets) * len(layouts) * args.variants * len(requested_palettes)
    print(f'Motifs: {len(motifs)} in {len(motif_sets)} set(s), layouts: {", ".join(layouts)}, '
          f'palettes: {", ".join(requested_palettes)} -> about {total} patterns')

    rows = []
    for set_number, set_motifs in enumerate(motif_sets, start=1):
        set_tag = f'_set{set_number:02d}' if len(motif_sets) > 1 else ''
        motif_files = ' '.join(m.name for m in set_motifs)
        if len(motif_sets) > 1:
            print(f'Set {set_number}/{len(motif_sets)}: {motif_files}')
        palettes = choose_palettes(requested_palettes, set_motifs, args.background)
        for layout in layouts:
            for variant in range(args.variants):
                rng = random.Random(args.seed * 1000 + set_number * 7919
                                    + variant * 31 + LAYOUTS.index(layout))
                if layout == 'scatter':
                    placements = scatter_layout(set_motifs, tile, args.count,
                                                args.rotation, rng)
                else:
                    layout_cells = cells
                    if layout in ('halfdrop', 'brick') and layout_cells % 2:
                        layout_cells += 1  # offsets need an even count to repeat
                    placements = grid_layout(set_motifs, tile, layout_cells, layout, rng)
                instances, copies = with_edge_copies(placements, tile)
                suffix = f'_v{variant + 1}' if args.variants > 1 else ''
                for palette_name, background, mapping in palettes:
                    name = f'{collection}{set_tag}_{layout}{suffix}_{palette_name}'
                    tile_svg = build_tile_svg(instances, set_motifs, mapping, background,
                                              tile, name)
                    (out_dir / f'{name}.svg').write_text(tile_svg, encoding='utf-8')
                    (swatch_dir / f'{name}_swatch.svg').write_text(
                        build_swatch_svg(instances, set_motifs, mapping, background,
                                         tile, name), encoding='utf-8')
                    preview_svg = build_preview_svg(tile_svg, tile)
                    (preview_dir / f'{name}_preview_3x3.svg').write_text(
                        preview_svg, encoding='utf-8')
                    png = False
                    if not args.no_render:
                        png = render_png(preview_svg, preview_dir / f'{name}_preview_3k.png')
                    rows.append({
                        'file': f'{name}.svg', 'set': set_number, 'motif_files': motif_files,
                        'layout': layout, 'palette': palette_name,
                        'motifs': len(set_motifs), 'instances': len(placements),
                        'edge_copies': copies,
                        'svg_kb': f'{len(tile_svg.encode("utf-8")) / 1024:.0f}',
                        'preview_png': png,
                        'note': '' if png or args.no_render else 'pip install resvg-py for PNG',
                    })
                    print(f'  {name}.svg  ({len(placements)} motifs, +{copies} edge copies)')

    with open(out_dir / 'report.csv', 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f'Done: {len(rows)} patterns in {out_dir}')


if __name__ == '__main__':
    main()
