#!/usr/bin/env python3
"""
Pattern UI - local web interface for PatternComposer's auto layout.

Load a folder of SVG motifs, set roles (hero / medium / filler / off) and
upright flags per motif, tune the layout settings, generate scored layouts,
preview them as a live repeat in any palette and save the ones you like.

Usage:
    python pattern_ui.py
    python pattern_ui.py D:\\motifs\\cranes          (folder preloaded)

Then open http://127.0.0.1:8769 (opens automatically).
Another port: python pattern_ui.py --port 8770
Needs the same packages as the auto layout: numpy scipy pillow resvg-py
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

STATE = {'folder': None, 'motifs': [], 'layouts': {}}
LOCK = threading.Lock()


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------
def load_folder(folder_text):
    folder = Path(folder_text.strip().strip('"\''))
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
    words = pc.UPRIGHT_WORDS
    for motif in motifs:
        motif.upright = any(w in motif.name.lower() for w in words)
    STATE.update(folder=folder, motifs=motifs, layouts={})
    return {
        'folder': str(folder),
        'motifs': [{'id': m.index, 'name': m.name, 'role': m.role, 'upright': m.upright,
                    'group': str(m.path.parent.relative_to(folder)).replace('\\', '/')}
                   for m in motifs],
        'skipped': skipped,
        'palettes': palette_list(),
    }


def palette_list():
    result = [{'name': 'original', 'bg': '', 'colors': []}]
    for name, (bg, colors) in pc.PALETTES.items():
        result.append({'name': name, 'bg': bg, 'colors': colors})
    return result


def layout_args(s):
    return SimpleNamespace(
        hero_count=int(s['hero_count']), density=float(s['density']),
        filler_max=int(s['filler_max']), rotation=float(s['rotation']),
        upright_rotation=float(s['upright_rotation']), upright_words=[],
        hero_size=(float(s['hero_min']), float(s['hero_max'])),
        medium_size=(float(s['medium_min']), float(s['medium_max'])),
        filler_size=(float(s['filler_min']), float(s['filler_max_size'])),
        gap=float(s['gap']))


def apply_motif_flags(roles, upright):
    for motif in STATE['motifs']:
        motif.role = roles.get(str(motif.index), motif.role)
        motif.upright = bool(upright.get(str(motif.index), motif.upright))
    return [m for m in STATE['motifs'] if m.role != 'off']


def palette_mapping(palette, background, used):
    name = palette or 'original'
    choice = pc.choose_palettes([name], used, background or None)
    if not choice:
        choice = pc.choose_palettes(['original'], used, background or None)
    _, bg, mapping = choice[0]
    return (background or bg), mapping


def tile_svg(seed, palette, background):
    placements = STATE['layouts'][seed]['placements']
    instances, _ = pc.with_edge_copies(placements, pc.TILE_SIZE)
    used = sorted({p[0].index: p[0] for p in placements}.values(), key=lambda m: m.index)
    bg, mapping = palette_mapping(palette, background, used)
    return pc.build_tile_svg(instances, used, mapping, bg, pc.TILE_SIZE, f'layout {seed}'), bg


def generate(body):
    if not STATE['motifs']:
        raise ValueError('Load a motif folder first.')
    motifs = apply_motif_flags(body.get('roles', {}), body.get('upright', {}))
    if not any(m.role == 'hero' for m in motifs):
        raise ValueError('Mark at least one motif as Hero.')
    if pc.render_rgba('<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"/>') is None:
        raise ValueError('resvg-py is missing: pip install resvg-py')
    s = body['settings']
    args = layout_args(s)
    base = random.randrange(1, 10 ** 7) if body.get('new_seeds', True) else int(s.get('seed', 1))
    count, keep = int(s['candidates']), int(s['keep'])
    scored = []
    for n in range(count):
        seed = base + n
        placements, note = pc.auto_layout(motifs, pc.TILE_SIZE, args, random.Random(seed))
        result = pc.score_layout(placements, pc.TILE_SIZE)
        scored.append((result['score'], seed, placements, result))
    scored.sort(key=lambda item: item[0])
    STATE['layouts'] = {}
    out = []
    for rank, (score, seed, placements, result) in enumerate(scored[:max(1, keep)], start=1):
        STATE['layouts'][seed] = {'placements': placements, 'result': result, 'rank': rank}
        svg, bg = tile_svg(seed, body.get('palette'), body.get('background'))
        out.append({'seed': seed, 'rank': rank, 'metrics': result, 'svg': svg, 'bg': bg,
                    'count': len(placements)})
    return {'layouts': out, 'tried': count}


def recolor(body):
    out = []
    for seed in STATE['layouts']:
        svg, bg = tile_svg(seed, body.get('palette'), body.get('background'))
        out.append({'seed': seed, 'svg': svg, 'bg': bg})
    return {'layouts': out}


def save(body):
    seed = int(body['seed'])
    if seed not in STATE['layouts']:
        raise ValueError('This layout is no longer in memory - generate again.')
    folder = STATE['folder']
    out_dir = folder / 'composed_patterns' / 'auto'
    swatch_dir, preview_dir = out_dir / 'swatch', out_dir / 'previews'
    for directory in (out_dir, swatch_dir, preview_dir):
        pc.ensure_dir(directory)
    palette = body.get('palette') or 'original'
    name = f'{pc.safe_name(folder.name)}_auto_{seed}_{pc.safe_name(palette)}'
    svg, bg = tile_svg(seed, palette, body.get('background'))
    placements = STATE['layouts'][seed]['placements']
    instances, _ = pc.with_edge_copies(placements, pc.TILE_SIZE)
    used = sorted({p[0].index: p[0] for p in placements}.values(), key=lambda m: m.index)
    _, mapping = palette_mapping(palette, body.get('background'), used)
    (out_dir / f'{name}.svg').write_text(svg, encoding='utf-8')
    (swatch_dir / f'{name}_swatch.svg').write_text(
        pc.build_swatch_svg(instances, used, mapping, bg, pc.TILE_SIZE, name), encoding='utf-8')
    preview = pc.build_preview_svg(svg, pc.TILE_SIZE)
    (preview_dir / f'{name}_preview_3x3.svg').write_text(preview, encoding='utf-8')
    png = pc.render_png(preview, preview_dir / f'{name}_preview_3k.png')
    log = out_dir / 'saved_layouts.json'
    entries = json.loads(log.read_text(encoding='utf-8')) if log.exists() else []
    entries.append({'file': f'{name}.svg', 'seed': seed, 'palette': palette, 'background': bg,
                    'settings': body.get('settings'), **STATE['layouts'][seed]['result']})
    log.write_text(json.dumps(entries, indent=1), encoding='utf-8')
    return {'saved': str(out_dir / f'{name}.svg'), 'png': bool(png)}


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
            preload = json.dumps(STATE.get('preload') or '')
            return self.send(200, PAGE.replace('__PRELOAD__', preload), 'text/html')
        if self.path.startswith('/motif/'):
            index = int(self.path.rsplit('/', 1)[1])
            motifs = STATE['motifs']
            if 0 <= index < len(motifs):
                return self.send(200, motifs[index].path.read_bytes(), 'image/svg+xml')
        self.send(404, {'error': 'not found'})

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length) or b'{}')
        actions = {'/api/load': lambda: load_folder(body.get('folder', '')),
                   '/api/generate': lambda: generate(body),
                   '/api/recolor': lambda: recolor(body),
                   '/api/save': lambda: save(body)}
        if self.path not in actions:
            return self.send(404, {'error': 'unknown action'})
        try:
            with LOCK:
                self.send(200, actions[self.path]())
        except Exception as error:
            self.send(400, {'error': str(error)})


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Pattern UI</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#f4f1ec;--panel:#fff;--ink:#222;--muted:#777;--line:#e2ddd5;--accent:#2f6f5e;--accent-ink:#fff}
*{box-sizing:border-box}
body{margin:0;font:14px/1.4 system-ui,Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--ink);
  display:grid;grid-template-columns:340px 1fr;height:100vh;overflow:hidden}
aside{background:var(--panel);border-right:1px solid var(--line);overflow:auto;padding:14px}
main{display:grid;grid-template-rows:auto 1fr auto;overflow:hidden}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:18px 0 8px}
input[type=text]{width:100%;padding:7px 8px;border:1px solid var(--line);border-radius:6px;font:inherit}
button{font:inherit;border:1px solid var(--line);background:#fff;border-radius:6px;padding:7px 12px;cursor:pointer}
button.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}
button:disabled{opacity:.5;cursor:default}
.row{display:flex;gap:6px;align-items:center}
.tabs{display:flex;flex-wrap:wrap;gap:4px}
.tabs button{padding:5px 10px;border-radius:999px}
.tabs button.on{background:var(--ink);color:#fff;border-color:var(--ink)}
.motif{display:grid;grid-template-columns:52px 1fr;gap:8px;padding:6px 0;border-bottom:1px solid var(--line)}
.motif img{width:52px;height:52px;object-fit:contain;background:#fafafa;border-radius:6px}
.motif .name{font-size:12px;word-break:break-all}
.seg{display:flex;margin-top:4px}
.seg button{padding:3px 6px;font-size:11px;border-radius:0;margin-left:-1px}
.seg button:first-child{border-radius:6px 0 0 6px}.seg button:last-child{border-radius:0 6px 6px 0}
.seg button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.up{font-size:11px;color:var(--muted);margin-left:6px}
label.s{display:grid;grid-template-columns:1fr 54px;align-items:center;gap:6px;margin:6px 0;font-size:12px}
label.s span.v{text-align:right;color:var(--muted)}
label.s input{grid-column:1/3;width:100%}
.top{display:flex;gap:10px;align-items:center;padding:10px 14px;border-bottom:1px solid var(--line);background:var(--panel);flex-wrap:wrap}
.preview{position:relative;overflow:hidden}
#repeat{position:absolute;inset:0;background-repeat:repeat}
#tileFrame{position:absolute;border:2px dashed rgba(0,0,0,.55);pointer-events:none;display:none}
.strip{display:flex;gap:8px;overflow-x:auto;padding:10px 14px;background:var(--panel);border-top:1px solid var(--line);min-height:150px}
.cand{flex:0 0 auto;width:118px;cursor:pointer;border:2px solid transparent;border-radius:8px;padding:3px;background:#fafafa}
.cand.on{border-color:var(--accent)}
.cand .img{width:108px;height:108px;background-size:54px 54px;background-repeat:repeat;border-radius:4px}
.cand .lbl{font-size:11px;color:var(--muted);text-align:center;margin-top:3px}
#status{color:var(--muted);font-size:12px}
.metrics{font-size:12px;color:var(--muted)}
.empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--muted)}
</style></head><body>
<aside>
  <h2>Motif folder</h2>
  <div class="row"><input type="text" id="folder" placeholder="D:\motifs\cranes"><button id="loadBtn">Load</button></div>
  <div id="skipped" class="metrics"></div>

  <h2>Motifs</h2>
  <div id="groupTabs" class="tabs"></div>
  <div id="motifs"><div class="metrics">No folder loaded.</div></div>

  <h2>Layout</h2>
  <div id="sliders"></div>
</aside>
<main>
  <div class="top">
    <button class="primary" id="genBtn">Generate</button>
    <span id="status"></span>
    <span style="flex:1"></span>
    <span class="metrics">Palette</span><div id="palTabs" class="tabs"></div>
    <input type="color" id="bgColor" title="Background" value="#f6f1e7">
    <label class="metrics"><input type="checkbox" id="customBg"> own background</label>
  </div>
  <div class="preview" id="previewBox">
    <div id="repeat"></div><div id="tileFrame"></div>
    <div class="empty" id="emptyMsg">Load motifs and press Generate</div>
  </div>
  <div>
    <div class="top" style="border-top:1px solid var(--line);border-bottom:0">
      <span class="metrics">Tile size</span><input type="range" id="zoom" min="120" max="900" value="360">
      <label class="metrics"><input type="checkbox" id="showFrame" checked> show one tile</label>
      <span id="metrics" class="metrics"></span>
      <span style="flex:1"></span>
      <button id="saveBtn" disabled>Save selected</button>
    </div>
    <div class="strip" id="strip"></div>
  </div>
</main>
<script>
const SLIDERS = [
  ['candidates','Layouts to try',10,120,5,40],
  ['keep','Best layouts kept',1,16,1,8],
  ['hero_count','Hero motifs per tile',0,8,1,3],
  ['hero_min','Hero size min',0.15,0.6,0.01,0.30],
  ['hero_max','Hero size max',0.15,0.6,0.01,0.40],
  ['medium_min','Medium size min',0.06,0.4,0.01,0.15],
  ['medium_max','Medium size max',0.06,0.4,0.01,0.23],
  ['density','Coverage before fillers',0.15,0.7,0.01,0.42],
  ['filler_min','Filler size min',0.02,0.2,0.005,0.045],
  ['filler_max_size','Filler size max',0.02,0.2,0.005,0.11],
  ['filler_max','Max copies per filler',0,30,1,8],
  ['gap','Gap between motifs',0,0.05,0.002,0.012],
  ['rotation','Rotation range, degrees',0,360,5,360],
  ['upright_rotation','Upright motifs rotation',0,90,2,24],
];
const ROLES = ['hero','medium','filler','off'];
let motifs = [], layouts = [], selected = null, palette = 'original', palettes = [], group = 'all';
const $ = id => document.getElementById(id);

function settings(){const s={};SLIDERS.forEach(([k])=>s[k]=parseFloat($('s_'+k).value));return s}
function buildSliders(){
  let saved={};try{saved=JSON.parse(localStorage.getItem('pattern_ui_settings')||'{}')}catch(e){}
  $('sliders').innerHTML = SLIDERS.map(([k,l,mi,ma,st,d])=>{
    const v = saved[k]!==undefined?saved[k]:d;
    return `<label class="s">${l}<span class="v" id="v_${k}">${v}</span>
      <input type="range" id="s_${k}" min="${mi}" max="${ma}" step="${st}" value="${v}"></label>`}).join('');
  SLIDERS.forEach(([k])=>$('s_'+k).addEventListener('input',e=>{
    $('v_'+k).textContent=e.target.value;
    try{localStorage.setItem('pattern_ui_settings',JSON.stringify(settings()))}catch(err){}
  }));
}
async function api(path, body){
  const r = await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j = await r.json(); if(!r.ok) throw new Error(j.error||'error'); return j;
}
function status(t){$('status').textContent=t}

async function load(){
  status('Loading...');
  try{
    const j = await api('/api/load',{folder:$('folder').value});
    motifs = j.motifs; palettes = j.palettes;
    try{localStorage.setItem('pattern_ui_folder',j.folder)}catch(e){}
    $('skipped').textContent = j.skipped.length ? 'Skipped: '+j.skipped.join('; ') : '';
    group='all'; renderMotifs(); renderPalettes(); status(`${motifs.length} motifs loaded`);
  }catch(e){status(e.message)}
}
function renderMotifs(){
  const groups = ['all',...new Set(motifs.map(m=>m.group))];
  $('groupTabs').innerHTML = groups.length>2 ? groups.map(g=>
    `<button class="${g===group?'on':''}" data-g="${g}">${g==='.'?'root':g}</button>`).join('') : '';
  $('groupTabs').querySelectorAll('button').forEach(b=>b.onclick=()=>{group=b.dataset.g;renderMotifs()});
  const list = motifs.filter(m=>group==='all'||m.group===group);
  $('motifs').innerHTML = list.map(m=>`<div class="motif">
    <img src="/motif/${m.id}" loading="lazy">
    <div><div class="name">${m.name}</div>
      <div class="row"><div class="seg">${ROLES.map(r=>
        `<button data-id="${m.id}" data-role="${r}" class="${m.role===r?'on':''}">${r}</button>`).join('')}</div>
      <label class="up"><input type="checkbox" data-up="${m.id}" ${m.upright?'checked':''}> upright</label></div>
    </div></div>`).join('');
  $('motifs').querySelectorAll('.seg button').forEach(b=>b.onclick=()=>{
    motifs.find(m=>m.id==b.dataset.id).role=b.dataset.role; renderMotifs()});
  $('motifs').querySelectorAll('[data-up]').forEach(c=>c.onchange=()=>{
    motifs.find(m=>m.id==c.dataset.up).upright=c.checked});
}
function renderPalettes(){
  $('palTabs').innerHTML = palettes.map(p=>`<button class="${p.name===palette?'on':''}" data-p="${p.name}">${p.name}</button>`).join('');
  $('palTabs').querySelectorAll('button').forEach(b=>b.onclick=()=>{
    palette=b.dataset.p; const p=palettes.find(x=>x.name===palette);
    if(p.bg && !$('customBg').checked) $('bgColor').value=p.bg;
    renderPalettes(); recolor()});
}
function bgValue(){return $('customBg').checked ? $('bgColor').value : ''}
function svgUrl(svg){return 'url("data:image/svg+xml;charset=utf-8,'+encodeURIComponent(svg)+'")'}

async function gen(){
  $('genBtn').disabled=true; status('Generating... (a few seconds per layout)');
  const roles={}, upright={}; motifs.forEach(m=>{roles[m.id]=m.role;upright[m.id]=m.upright});
  try{
    const j = await api('/api/generate',{settings:settings(),roles,upright,palette,background:bgValue()});
    layouts = j.layouts; selected = layouts[0]?.seed; renderStrip(); show();
    status(`Best ${layouts.length} of ${j.tried} layouts`);
  }catch(e){status(e.message)}
  $('genBtn').disabled=false;
}
async function recolor(){
  if(!layouts.length) return;
  try{
    const j = await api('/api/recolor',{palette,background:bgValue()});
    j.layouts.forEach(u=>{const l=layouts.find(x=>x.seed===u.seed); if(l){l.svg=u.svg;l.bg=u.bg}});
    renderStrip(); show();
  }catch(e){status(e.message)}
}
function renderStrip(){
  $('strip').innerHTML = layouts.map(l=>`<div class="cand ${l.seed===selected?'on':''}" data-s="${l.seed}">
    <div class="img" style='background-image:${svgUrl(l.svg)}'></div>
    <div class="lbl">#${l.rank} &middot; ${l.metrics.score.toFixed(2)}</div></div>`).join('');
  $('strip').querySelectorAll('.cand').forEach(c=>c.onclick=()=>{selected=+c.dataset.s;renderStrip();show()});
}
function show(){
  const l = layouts.find(x=>x.seed===selected);
  $('emptyMsg').style.display = l?'none':'flex'; $('saveBtn').disabled=!l;
  if(!l) return;
  const z = +$('zoom').value, box=$('previewBox');
  const ox = Math.round((box.clientWidth - z)/2), oy = Math.round((box.clientHeight - z)/2);
  const r = $('repeat'); r.style.backgroundImage=svgUrl(l.svg); r.style.backgroundSize=z+'px '+z+'px';
  r.style.backgroundPosition=ox+'px '+oy+'px';
  const f=$('tileFrame'); f.style.display=$('showFrame').checked?'block':'none';
  Object.assign(f.style,{left:ox+'px',top:oy+'px',width:z+'px',height:z+'px'});
  const m=l.metrics;
  $('metrics').textContent=`#${l.rank}  score ${m.score}  ·  ${l.count} motifs  ·  coverage ${m.coverage}  ·  hole ${m.hole}  ·  stripes ${m.stripes}  ·  twins ${m.twins}`;
}
async function saveSel(){
  try{
    const j = await api('/api/save',{seed:selected,palette,background:bgValue(),settings:settings()});
    status('Saved: '+j.saved+(j.png?'':' (no PNG preview: pip install resvg-py)'));
  }catch(e){status(e.message)}
}
buildSliders();
$('loadBtn').onclick=load; $('genBtn').onclick=gen; $('saveBtn').onclick=saveSel;
$('folder').addEventListener('keydown',e=>{if(e.key==='Enter')load()});
['zoom','showFrame'].forEach(id=>$(id).addEventListener('input',show));
$('bgColor').addEventListener('change',()=>{$('customBg').checked=true;recolor()});
$('customBg').addEventListener('change',recolor);
window.addEventListener('resize',show);
const preload = __PRELOAD__;
let last=''; try{last=localStorage.getItem('pattern_ui_folder')||''}catch(e){}
$('folder').value = preload || last;
if($('folder').value) load();
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
