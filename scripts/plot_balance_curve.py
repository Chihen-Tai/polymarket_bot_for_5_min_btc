from __future__ import annotations

import argparse
import html
import math
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path


LINE_RE = re.compile(
    r"^\[(?P<ts>[0-9:\- ]+)\].*?\bequity=(?P<equity>-?\d+(?:\.\d+)?)\s+cash=(?P<cash>-?\d+(?:\.\d+)?)"
)


def parse_points(log_path: Path) -> list[tuple[datetime, float, float]]:
    points: list[tuple[datetime, float, float]] = []
    for raw_line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = LINE_RE.search(raw_line)
        if not match:
            continue
        ts = datetime.strptime(match.group("ts"), "%Y-%m-%d %H:%M:%S")
        equity = float(match.group("equity"))
        cash = float(match.group("cash"))
        points.append((ts, equity, cash))
    points.sort(key=lambda item: item[0])
    return points


def fmt_money(value: float) -> str:
    return f"{value:.2f}"


def svg_polyline(points: list[tuple[float, float]], *, color: str, width: float) -> str:
    payload = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    return (
        f'<polyline fill="none" stroke="{color}" stroke-width="{width:.2f}" '
        f'stroke-linecap="round" stroke-linejoin="round" points="{payload}" />'
    )


def _axis_positions(count: int, start: float, end: float) -> list[float]:
    if count <= 1:
        return [start]
    step = (end - start) / (count - 1)
    return [start + (step * idx) for idx in range(count)]


def build_svg(points: list[tuple[datetime, float, float]], *, title: str, width: int = 1280, height: int = 720) -> str:
    if not points:
        raise ValueError("No equity/cash points found in log file.")

    pad_left = 90
    pad_right = 40
    pad_top = 70
    pad_bottom = 80
    chart_w = width - pad_left - pad_right
    chart_h = height - pad_top - pad_bottom

    min_ts = points[0][0]
    max_ts = points[-1][0]
    span_sec = max(1.0, (max_ts - min_ts).total_seconds())

    values = [equity for _, equity, _ in points] + [cash for _, _, cash in points]
    min_val = min(values)
    max_val = max(values)
    if math.isclose(min_val, max_val):
        min_val -= 1.0
        max_val += 1.0
    y_pad = max(0.25, (max_val - min_val) * 0.08)
    min_y = min_val - y_pad
    max_y = max_val + y_pad
    value_span = max_y - min_y

    def x_pos(ts: datetime) -> float:
        return pad_left + (((ts - min_ts).total_seconds() / span_sec) * chart_w)

    def y_pos(val: float) -> float:
        return pad_top + (chart_h * (1.0 - ((val - min_y) / value_span)))

    equity_points = [(x_pos(ts), y_pos(equity)) for ts, equity, _ in points]
    cash_points = [(x_pos(ts), y_pos(cash)) for ts, _, cash in points]

    y_ticks = 6
    x_ticks = min(6, max(2, len(points)))
    y_values = _axis_positions(y_ticks, min_y, max_y)
    x_values = _axis_positions(x_ticks, 0.0, span_sec)

    svg: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">',
        '<style>',
        'text { font-family: Menlo, Monaco, Consolas, "Liberation Mono", monospace; fill: #cbd5e1; }',
        '.small { font-size: 12px; }',
        '.label { font-size: 14px; font-weight: 600; }',
        '.title { font-size: 22px; font-weight: 700; fill: #f8fafc; }',
        '.grid { stroke: #334155; stroke-width: 1; stroke-dasharray: 4 6; }',
        '.axis { stroke: #64748b; stroke-width: 1.2; }',
        '</style>',
        '<rect x="0" y="0" width="100%" height="100%" fill="#0f172a" />',
        f'<text class="title" x="{pad_left}" y="38">{html.escape(title)}</text>',
        f'<text class="small" x="{pad_left}" y="58">source: {html.escape(points[0][0].strftime("%Y-%m-%d %H:%M:%S"))} to {html.escape(points[-1][0].strftime("%Y-%m-%d %H:%M:%S"))}</text>',
    ]

    for tick in y_values:
        y = y_pos(tick)
        svg.append(f'<line class="grid" x1="{pad_left}" y1="{y:.2f}" x2="{width - pad_right}" y2="{y:.2f}" />')
        svg.append(f'<text class="small" x="{pad_left - 12}" y="{y + 4:.2f}" text-anchor="end">{fmt_money(tick)}</text>')

    for sec in x_values:
        x = pad_left + ((sec / span_sec) * chart_w)
        tick_ts = min_ts.timestamp() + sec
        label = datetime.fromtimestamp(tick_ts).strftime("%H:%M:%S")
        svg.append(f'<line class="grid" x1="{x:.2f}" y1="{pad_top}" x2="{x:.2f}" y2="{pad_top + chart_h}" />')
        svg.append(f'<text class="small" x="{x:.2f}" y="{height - pad_bottom + 24}" text-anchor="middle">{label}</text>')

    svg.extend([
        f'<line class="axis" x1="{pad_left}" y1="{pad_top}" x2="{pad_left}" y2="{pad_top + chart_h}" />',
        f'<line class="axis" x1="{pad_left}" y1="{pad_top + chart_h}" x2="{width - pad_right}" y2="{pad_top + chart_h}" />',
        svg_polyline(equity_points, color="#38bdf8", width=3.2),
        svg_polyline(cash_points, color="#f59e0b", width=3.2),
    ])

    last_ts, last_equity, last_cash = points[-1]
    last_equity_x, last_equity_y = equity_points[-1]
    last_cash_x, last_cash_y = cash_points[-1]
    svg.extend([
        f'<circle cx="{last_equity_x:.2f}" cy="{last_equity_y:.2f}" r="4.5" fill="#38bdf8" />',
        f'<circle cx="{last_cash_x:.2f}" cy="{last_cash_y:.2f}" r="4.5" fill="#f59e0b" />',
        f'<text class="small" x="{last_equity_x + 8:.2f}" y="{last_equity_y - 8:.2f}">equity {fmt_money(last_equity)}</text>',
        f'<text class="small" x="{last_cash_x + 8:.2f}" y="{last_cash_y + 18:.2f}">cash {fmt_money(last_cash)}</text>',
        f'<rect x="{width - 210}" y="24" width="160" height="48" rx="10" fill="#111827" stroke="#334155" />',
        f'<line x1="{width - 192}" y1="42" x2="{width - 156}" y2="42" stroke="#38bdf8" stroke-width="3.2" stroke-linecap="round" />',
        f'<text class="label" x="{width - 146}" y="47">equity</text>',
        f'<line x1="{width - 192}" y1="62" x2="{width - 156}" y2="62" stroke="#f59e0b" stroke-width="3.2" stroke-linecap="round" />',
        f'<text class="label" x="{width - 146}" y="67">cash</text>',
        f'<text class="small" x="{pad_left}" y="{height - 18}">last point: {last_ts.strftime("%Y-%m-%d %H:%M:%S")}</text>',
        '</svg>',
    ])
    return "\n".join(svg)


def render_svg_to_png(svg_text: str, output_path: Path, *, width: int = 1280, height: int = 720) -> None:
    try:
        import cairosvg  # type: ignore

        cairosvg.svg2png(bytestring=svg_text.encode("utf-8"), write_to=str(output_path), output_width=width, output_height=height)
        return
    except Exception:
        pass

    qlmanage = shutil.which("qlmanage")
    if qlmanage:
        with tempfile.TemporaryDirectory(prefix="balance_curve_") as tmpdir:
            tmpdir_path = Path(tmpdir)
            svg_path = tmpdir_path / "chart.svg"
            svg_path.write_text(svg_text, encoding="utf-8")
            size = max(width, height)
            proc = subprocess.run(
                [qlmanage, "-t", "-s", str(size), "-o", str(tmpdir_path), str(svg_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            png_path = tmpdir_path / "chart.svg.png"
            if proc.returncode == 0 and png_path.exists():
                output_path.write_bytes(png_path.read_bytes())
                return

    raise RuntimeError("PNG output requires cairosvg or a macOS Quick Look renderer (qlmanage). Use SVG output instead.")


def write_chart(points: list[tuple[datetime, float, float]], output_path: Path, *, title: str, width: int = 1280, height: int = 720) -> None:
    svg_text = build_svg(points, title=title, width=width, height=height)
    suffix = output_path.suffix.lower()
    if suffix == ".svg":
        output_path.write_text(svg_text, encoding="utf-8")
        return
    if suffix == ".png":
        render_svg_to_png(svg_text, output_path, width=width, height=height)
        return
    raise SystemExit(f"Unsupported output extension: {output_path.suffix}. Use .png or .svg")


def main() -> None:
    ap = argparse.ArgumentParser(description="Plot equity/cash over time from a bot log file.")
    ap.add_argument("log_file", help="Path to log-dryrun-*.txt or log-live-*.txt")
    ap.add_argument("--output", help="Output chart path (defaults to <log_file>.balance.svg)")
    ap.add_argument("--title", help="Optional chart title")
    args = ap.parse_args()

    log_path = Path(args.log_file).expanduser().resolve()
    if not log_path.exists():
        raise SystemExit(f"log file not found: {log_path}")

    points = parse_points(log_path)
    if not points:
        raise SystemExit("No lines with equity/cash were found in the log.")

    output_path = Path(args.output).expanduser().resolve() if args.output else log_path.with_suffix(".balance.svg")
    title = args.title or f"Balance Curve: {log_path.name}"
    write_chart(points, output_path, title=title)
    print(f"parsed points: {len(points)}")
    print(f"output: {output_path}")


if __name__ == "__main__":
    main()
