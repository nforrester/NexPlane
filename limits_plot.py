'''
Display of the safe area for teach_limits.py:
- render_ascii(): small map in the terminal
- write_svg():    graphic as SVG file (open in a browser or image viewer)

Axes: horizontal axis 1 (RA), vertical axis 2 (Dec), each in degrees
relative to the home position.
'''

import math

from html import escape

import axis_limits

from axis_limits import deg

Point = tuple[float, float]

def _label(i: int) -> str:
    '''Point label in the terminal: 1-9, then A-Z, then *.'''
    if i < 9:
        return str(i + 1)
    if i < 9 + 26:
        return chr(ord('A') + i - 9)
    return '*'

def _rel(points: list[Point], home: Point) -> list[Point]:
    return [(deg(p[0] - home[0]), deg(p[1] - home[1])) for p in points]

def _bounds(pts: list[Point], min_span: float = 10.0) -> tuple[float, float, float, float]:
    '''Plot range around the points, at least min_span degrees wide, with a small border.'''
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    if x1 - x0 < min_span:
        c = (x0 + x1) / 2
        x0, x1 = c - min_span / 2, c + min_span / 2
    if y1 - y0 < min_span:
        c = (y0 + y1) / 2
        y0, y1 = c - min_span / 2, c + min_span / 2
    pad_x = (x1 - x0) * 0.08
    pad_y = (y1 - y0) * 0.08
    return x0 - pad_x, x1 + pad_x, y0 - pad_y, y1 + pad_y

def _edge_kinds(n: int, crossings: list[tuple[int, int]]) -> list[str]:
    '''Per edge: "normal", "closing" (closing line) or "crossing".'''
    bad = {i for pair in crossings for i in pair}
    kinds = []
    for i in range(n):
        if i in bad:
            kinds.append('crossing')
        elif i == n - 1:
            kinds.append('closing')
        else:
            kinds.append('normal')
    return kinds

# ---------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------

def render_ascii(home: Point, boundary: list[Point], current: Point, width: int, height: int) -> list[str]:
    '''
    Returns height lines with at most width characters each.
    Characters: digits/letters = boundary points, H = home, @ = current position,
    . = boundary, : = closing line to point 1, ! = edge that crosses another one.
    '''
    width = max(width, 20)
    height = max(height, 6)
    rel_b = _rel(boundary, home)
    rel_c = _rel([current], home)[0]
    x0, x1, y0, y1 = _bounds(rel_b + [(0.0, 0.0), rel_c])

    plot_h = height - 1   # last line for the axis labels
    grid = [[' '] * width for _ in range(plot_h)]

    def cell(p: Point) -> tuple[int, int]:
        cx = round((p[0] - x0) / (x1 - x0) * (width - 1))
        cy = round((y1 - p[1]) / (y1 - y0) * (plot_h - 1))
        return max(0, min(width - 1, cx)), max(0, min(plot_h - 1, cy))

    def put(c: tuple[int, int], ch: str) -> None:
        grid[c[1]][c[0]] = ch

    n = len(rel_b)
    if n >= 2:
        crossings = axis_limits.crossing_edges(boundary) if n >= 4 else []
        kinds = _edge_kinds(n, crossings)
        edges = range(n) if n >= 3 else range(1)
        for i in edges:
            a, b = cell(rel_b[i]), cell(rel_b[(i + 1) % n])
            ch = {'normal': '.', 'closing': ':', 'crossing': '!'}[kinds[i]]
            steps = max(abs(b[0] - a[0]), abs(b[1] - a[1]), 1)
            for s in range(steps + 1):
                x = round(a[0] + (b[0] - a[0]) * s / steps)
                y = round(a[1] + (b[1] - a[1]) * s / steps)
                if grid[y][x] in ' .:':
                    grid[y][x] = ch
    for i, p in enumerate(rel_b):
        put(cell(p), _label(i))
    put(cell((0.0, 0.0)), 'H')
    put(cell(rel_c), '@')

    lines = [''.join(row) for row in grid]
    lines.append(f'Axis 1: {x0:+.0f} .. {x1:+.0f} degrees  |  axis 2: {y0:+.0f} (bottom) .. {y1:+.0f} (top) degrees')
    return [line[:width] for line in lines]

# ---------------------------------------------------------------------------
# SVG
# ---------------------------------------------------------------------------

def _nice_step(span: float) -> float:
    raw = span / 8
    mag = 10 ** math.floor(math.log10(raw))
    for m in [1, 2, 5, 10]:
        if raw <= m * mag:
            return float(m * mag)
    return float(10 * mag)

def write_svg(path: str, home: Point, boundary: list[Point], current: Point, margin: float, status: str) -> None:
    '''Write an SVG preview of the outline and the allowed area (incl. safety margin).'''
    W, H = 900, 700
    L, R, T, B = 70, 30, 100, 60
    rel_b = _rel(boundary, home)
    rel_c = _rel([current], home)[0]
    x0, x1, y0, y1 = _bounds(rel_b + [(0.0, 0.0), rel_c])

    def sx(x: float) -> float:
        return L + (x - x0) / (x1 - x0) * (W - L - R)

    def sy(y: float) -> float:
        return T + (y1 - y) / (y1 - y0) * (H - T - B)

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
           'font-family="sans-serif" font-size="13">',
           f'<rect width="{W}" height="{H}" fill="#ffffff"/>']

    # Grid
    for lo, hi, horiz in [(x0, x1, False), (y0, y1, True)]:
        step = _nice_step(hi - lo)
        v = math.ceil(lo / step) * step
        while v <= hi:
            if horiz:
                out.append(f'<line x1="{L}" x2="{W - R}" y1="{sy(v):.1f}" y2="{sy(v):.1f}" stroke="#e4e4e4"/>')
                out.append(f'<text x="{L - 8}" y="{sy(v) + 4:.1f}" text-anchor="end" fill="#666">{v:g}</text>')
            else:
                out.append(f'<line y1="{T}" y2="{H - B}" x1="{sx(v):.1f}" x2="{sx(v):.1f}" stroke="#e4e4e4"/>')
                out.append(f'<text y="{H - B + 18}" x="{sx(v):.1f}" text-anchor="middle" fill="#666">{v:g}</text>')
            v += step
    out.append(f'<rect x="{L}" y="{T}" width="{W - L - R}" height="{H - T - B}" fill="none" stroke="#999"/>')
    out.append(f'<text x="{(L + W - R) / 2}" y="{H - 15}" text-anchor="middle" fill="#333">'
               'Axis 1 (RA), degrees relative to home</text>')
    out.append(f'<text transform="translate(18,{(T + H - B) / 2}) rotate(-90)" text-anchor="middle" fill="#333">'
               'Axis 2 (Dec), degrees relative to home</text>')

    n = len(boundary)
    crossings = axis_limits.crossing_edges(boundary) if n >= 4 else []
    valid = n >= 3 and not axis_limits.polygon_problems(boundary)

    # Permitted area incl. safety margin as a grid
    if valid:
        nx, ny = 150, 115
        cw = (W - L - R) / nx
        ch = (H - T - B) / ny
        for j in range(ny):
            for i in range(nx):
                x = x0 + (i + 0.5) / nx * (x1 - x0)
                y = y1 - (j + 0.5) / ny * (y1 - y0)
                p = (home[0] + math.radians(x), home[1] + math.radians(y))
                if axis_limits.clearance(p, boundary, margin) >= 0:
                    out.append(f'<rect x="{L + i * cw:.1f}" y="{T + j * ch:.1f}" width="{cw + 0.4:.1f}" '
                               f'height="{ch + 0.4:.1f}" fill="#cfe8d2"/>')
        poly_pts = ' '.join(f'{sx(p[0]):.1f},{sy(p[1]):.1f}' for p in rel_b)
        out.append(f'<polygon points="{poly_pts}" fill="none" stroke="none"/>')

    # Edges
    if n >= 2:
        kinds = _edge_kinds(n, crossings)
        for i in (range(n) if n >= 3 else range(1)):
            a, b = rel_b[i], rel_b[(i + 1) % n]
            style = {'normal': 'stroke="#2b5797" stroke-width="2"',
                     'closing': 'stroke="#888" stroke-width="2" stroke-dasharray="6 5"',
                     'crossing': 'stroke="#d0021b" stroke-width="3.5"'}[kinds[i]]
            out.append(f'<line x1="{sx(a[0]):.1f}" y1="{sy(a[1]):.1f}" x2="{sx(b[0]):.1f}" y2="{sy(b[1]):.1f}" {style}/>')

    # Points
    for i, p in enumerate(rel_b):
        out.append(f'<circle cx="{sx(p[0]):.1f}" cy="{sy(p[1]):.1f}" r="5" fill="#2b5797"/>')
        out.append(f'<text x="{sx(p[0]) + 8:.1f}" y="{sy(p[1]) - 7:.1f}" fill="#2b5797" font-weight="bold">{i + 1}</text>')
    out.append(f'<rect x="{sx(0) - 6:.1f}" y="{sy(0) - 6:.1f}" width="12" height="12" fill="#111"/>')
    out.append(f'<text x="{sx(0) + 9:.1f}" y="{sy(0) + 16:.1f}" fill="#111">Home</text>')
    out.append(f'<circle cx="{sx(rel_c[0]):.1f}" cy="{sy(rel_c[1]):.1f}" r="7" fill="none" stroke="#e67e00" stroke-width="3"/>')

    # Title and legend
    out.append(f'<text x="{L}" y="26" font-size="17" font-weight="bold" fill="#111">'
               f'Safe area ({n} boundary points, safety margin {deg(margin):.1f} degrees)</text>')
    out.append(f'<text x="{L}" y="48" fill="{"#d0021b" if not valid else "#1e7b34"}">{escape(status)}</text>')
    legend = [('#2b5797', 'boundary'), ('#888', 'closing line to point 1'),
              ('#d0021b', 'crossing edges'), ('#cfe8d2', 'allowed area'),
              ('#e67e00', 'current position')]
    lx = float(L)
    for color, text in legend:
        out.append(f'<rect x="{lx}" y="66" width="14" height="10" fill="{color}"/>')
        out.append(f'<text x="{lx + 19}" y="76" fill="#333">{text}</text>')
        lx += 19 + 6.6 * len(text) + 20
    out.append('</svg>')
    with open(path, 'w') as f:
        f.write('\n'.join(out))
