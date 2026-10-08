"""Мини-библиотека для генерации многостраничного .drawio (mxGraphModel).

Стиль сущностей повторяет исходную схему Максима («Архитектура диплома.drawio»):
заголовок — скруглённая рамка с крупным шрифтом, описание — жёлтая заметка.
"""
from __future__ import annotations

import html
import itertools

TITLE = "rounded=1;whiteSpace=wrap;html=1;fontSize=17;fillColor=#FFFFFF;"
NOTE = ("shape=note;whiteSpace=wrap;html=1;backgroundOutline=1;fontColor=#000000;darkOpacity=0.05;"
        "fillColor=#FFF9B2;strokeColor=none;fillStyle=solid;direction=west;gradientDirection=north;"
        "gradientColor=#FFF2A1;shadow=1;size=20;pointerEvents=1;align=left;verticalAlign=top;"
        "spacingLeft=8;spacingRight=8;spacingTop=6;fontSize=12;")
PROPOSED_NOTE = ("rounded=1;whiteSpace=wrap;html=1;align=left;verticalAlign=top;spacing=6;fontSize=11;"
                 "fillColor=#FDECEA;strokeColor=#C0392B;dashed=1;fontColor=#7B241C;")
PROPOSED_EDGE = "strokeColor=#C0392B;dashed=1;fontColor=#C0392B;"
HEADER = "text;html=1;align=left;verticalAlign=middle;fontSize=22;fontStyle=1;"
SUBTLE = "text;html=1;align=left;verticalAlign=top;fontSize=12;fontColor=#555555;whiteSpace=wrap;"
EDGE = "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;fontSize=11;labelBackgroundColor=#FFFFFF;"

GROUP_COLORS = {
    "trust": ("#E1D5E7", "#9673A6"),
    "admin": ("#DAE8FC", "#6C8EBF"),
    "system": ("#D5E8D4", "#82B366"),
    "medium": ("#FFE6CC", "#D79B00"),
    "access": ("#FFF2CC", "#D6B656"),
    "logs": ("#F5F5F5", "#666666"),
    "external": ("#F8CECC", "#B85450"),
}


def esc(text: str) -> str:
    return html.escape(text, quote=True)


def para(*lines: str) -> str:
    """Текст заметки: каждая строка — отдельный абзац <div>."""
    return "".join(f"<div>{line}</div>" for line in lines)


def note_height(lines, width: int = 315, font: int = 12, extra: int = 30) -> int:
    chars = max(20, int((width - 24) / (font * 0.56)))
    total = 0
    for line in lines:
        plain = line.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
        total += max(1, -(-len(plain) // chars))
    return int(total * font * 1.32) + extra


class Page:
    def __init__(self, name: str, width: int = 3000, height: int = 2000) -> None:
        self.name = name
        self.width = width
        self.height = height
        self.cells: list[str] = []
        self._ids = itertools.count(1)

    def nid(self, prefix: str = "c") -> str:
        return f"{prefix}{next(self._ids)}"

    def vertex(self, value: str, style: str, x: float, y: float, w: float, h: float,
               parent: str = "1", cid: str | None = None) -> str:
        cid = cid or self.nid("v")
        self.cells.append(
            f'<mxCell id="{cid}" value="{esc(value)}" style="{esc(style)}" vertex="1" parent="{parent}">'
            f'<mxGeometry x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" as="geometry"/></mxCell>')
        return cid

    def edge(self, src: str, dst: str, label: str = "", style: str = EDGE, parent: str = "1",
             cid: str | None = None) -> str:
        cid = cid or self.nid("e")
        self.cells.append(
            f'<mxCell id="{cid}" value="{esc(label)}" style="{esc(style)}" edge="1" parent="{parent}" '
            f'source="{src}" target="{dst}"><mxGeometry relative="1" as="geometry"/></mxCell>')
        return cid

    def xml(self, index: int) -> str:
        body = "".join(self.cells)
        return (f'<diagram name="{esc(self.name)}" id="p{index}">'
                f'<mxGraphModel dx="1600" dy="1000" grid="1" gridSize="10" guides="1" tooltips="1" '
                f'connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="{self.width}" '
                f'pageHeight="{self.height}" math="0" shadow="0" adaptiveColors="auto">'
                f'<root><mxCell id="0"/><mxCell id="1" parent="0"/>{body}</root></mxGraphModel></diagram>')


def write(path: str, pages: "list[Page]") -> None:
    data = '<mxfile host="Electron">' + "".join(p.xml(i) for i, p in enumerate(pages)) + "</mxfile>"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(data)


# -- процессы с дорожками ----------------------------------------------------------
LANE_COLORS = ["#F5F5F5", "#E8F4F8", "#FFF0E6", "#E8F5E9", "#FFF9E6", "#FCE4EC", "#EDE7F6"]
TASK = "rounded=1;whiteSpace=wrap;html=1;fontSize=11;"
DECISION = "rhombus;whiteSpace=wrap;html=1;fontSize=10;spacing=14;"
START = "ellipse;html=1;fillColor=#000000;strokeColor=#000000;"
END = "ellipse;html=1;shape=doubleEllipse;fillColor=#000000;strokeColor=#000000;"


def process_page(name: str, title: str, lanes: "list[str]", steps: list, edges: list,
                 notes: list | None = None, col_w: int = 190, lane_h: int = 150,
                 head_w: int = 150) -> Page:
    """steps: (ключ, дорожка, колонка, текст, вид[, proposed]); edges: (откуда, куда, подпись[, proposed]).

    Вид: task, decision, start, end. Колонка — номер шага по горизонтали.
    notes: (дорожка, колонка, текст, proposed) — заметки, привязанные к ячейке.
    """
    # Каждому шагу — своя колонка в порядке списка: так вертикальные участки стрелок не
    # проходят сквозь блоки соседних дорожек. Номер колонки в описании шага не используется.
    order = {st[0]: i for i, st in enumerate(steps)}
    max_col = len(steps) - 1
    width = head_w + (max_col + 1) * col_w + 80
    page = Page(name, width=width + 80, height=110 + len(lanes) * lane_h + 300)
    page.vertex(f"<b>{title}</b>", HEADER, 20, 20, width, 50)
    lane_ids = {}
    top = 90
    for i, lane in enumerate(lanes):
        lane_ids[lane] = page.vertex(
            lane, f"swimlane;horizontal=0;startSize={head_w};fillColor={LANE_COLORS[i % len(LANE_COLORS)]};"
                  f"html=1;fontSize=13;fontStyle=1;whiteSpace=wrap;",
            20, top + i * lane_h, width, lane_h)
    keys = {}
    for st in steps:
        key, lane, _col, text, kind = st[:5]
        proposed = len(st) > 5 and st[5]
        col = order[key]
        x = head_w + col * col_w + 15
        if kind == "task":
            w, h = col_w - 30, 80
            style = TASK + (PROPOSED_EDGE.replace("fontColor=#C0392B;", "") + "fillColor=#FDECEA;" if proposed else "")
        elif kind == "decision":
            w, h = col_w - 20, 110
            style = DECISION
            x -= 5
        elif kind == "start":
            w, h = 30, 30
            style = START
        else:
            w, h = 34, 34
            style = END
        y = (lane_h - h) / 2
        if kind in ("start", "end"):
            x += (col_w - 30 - w) / 2
        keys[key] = page.vertex(text if kind in ("task", "decision") else "", style, x, y, w, h,
                                parent=lane_ids[lane])
    for ed in edges:
        src, dst, label = ed[:3]
        extra = ed[3] if len(ed) > 3 else ""
        style = EDGE + (PROPOSED_EDGE if extra is True else (extra or ""))
        if order[dst] < order[src] and not extra:
            style += "exitX=0.5;exitY=0;entryX=0.5;entryY=0;"
        page.edge(keys[src], keys[dst], label, style)
    y = top + len(lanes) * lane_h + 20
    x = 20
    for nt in notes or []:
        text, proposed = nt
        h = note_height([text], width=420, font=11, extra=24)
        page.vertex(text, PROPOSED_NOTE if proposed else NOTE.replace("fontSize=12;", "fontSize=11;"),
                    x, y, 420, h)
        x += 440
        if x + 420 > width:
            x, y = 20, y + h + 20
    return page
