"""Inline-SVG chart helpers. No matplotlib — charts are tiny and self-contained."""
from __future__ import annotations

from html import escape as _escape


def escape(text: str) -> str:
    return _escape(str(text))


def _frame(series_values: list[list[float]], width: int, height: int) -> tuple[float, float, float, float]:
    allv = [v for s in series_values for v in s]
    ymin = min(allv + [0.0])
    ymax = max(allv + [1.0])
    pad_l, pad_r, pad_t, pad_b = 40, 10, 10, 24
    x0, x1 = pad_l, width - pad_r
    y0, y1 = height - pad_b, pad_t
    return x0, x1, y0, y1, ymin, ymax


def _x_for(i: int, n: int, x0: float, x1: float) -> float:
    return x0 + (x1 - x0) * (i / max(1, n - 1))


def _y_for(v: float, ymin: float, ymax: float, y0: float, y1: float) -> float:
    return y0 - (y0 - y1) * ((v - ymin) / (ymax - ymin or 1.0))


def line_chart(series: dict[str, list[float]], *, width: int = 800, height: int = 240,
               x_labels: list[str] | None = None) -> str:
    vals = list(series.values())
    x0, x1, y0, y1, ymin, ymax = _frame(vals, width, height)
    parts = [f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">']
    parts.append(f'<text x="{x0}" y="{y1+16}" font-size="10" fill="#666">HQS over runs</text>')
    colors = {"authoritative": "#1f77b4", "rolling": "#2ca02c", "dashboard": "#ff7f0e",
              "feedback": "#d62728"}
    for name, ys in series.items():
        n = len(ys)
        pts = " ".join(f"{_x_for(i,n,x0,x1):.1f},{_y_for(ys[i],ymin,ymax,y0,y1):.1f}"
                       for i in range(n))
        c = colors.get(name, "#333")
        parts.append(f'<polyline fill="none" stroke="{c}" stroke-width="2" points="{pts}"/>')
        parts.append(f'<text x="{x1}" y="{_y_for(ys[-1],ymin,ymax,y0,y1):.1f}" '
                     f'font-size="10" fill="{c}">{escape(name)}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def scatter(xs: list[float], ys: list[float], *, width: int = 800, height: int = 240) -> str:
    x0, x1, y0, y1, ymin, ymax = _frame([ys], width, height)
    xmin, xmax = (min(xs), max(xs)) if xs else (0, 1)
    parts = [f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">']
    for x, y in zip(xs, ys):
        cx = x0 + (x1 - x0) * ((x - xmin) / (xmax - xmin or 1.0))
        cy = _y_for(y, ymin, ymax, y0, y1)
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" fill="#1f77b4"/>')
    parts.append("</svg>")
    return "\n".join(parts)


def bar(labels: list[str], values: list[float], *, width: int = 800, height: int = 240) -> str:
    x0, x1, y0, y1, ymin, ymax = _frame([values], width, height)
    n = len(labels)
    bw = (x1 - x0) / max(1, n) * 0.6
    parts = [f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">']
    for i, (lab, v) in enumerate(zip(labels, values)):
        cx = x0 + (x1 - x0) * (i / max(1, n)) + bw / 2
        cy = _y_for(v, ymin, ymax, y0, y1)
        h = y0 - cy
        parts.append(f'<rect x="{cx-bw/2:.1f}" y="{cy:.1f}" width="{bw:.1f}" height="{max(0,h):.1f}" fill="#ff7f0e"/>')
        parts.append(f'<text x="{cx:.1f}" y="{y0+16}" font-size="10" fill="#666" text-anchor="middle">{escape(lab)}</text>')
    parts.append("</svg>")
    return "\n".join(parts)