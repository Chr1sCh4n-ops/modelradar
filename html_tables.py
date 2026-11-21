#!/usr/bin/env python3
"""标准库版 HTML 表格解析：替代 lxml，让目标机器零 pip 依赖就能跑。

只需要原来那几 xpath 表达式的能力，所以只做这些：
  ``//table``                 → find_tables()
  ``tbl.xpath('.//tbody/tr')``→ tbody_rows()
  ``tr.xpath('.//td')``       → cells()
  ``' '.join(el.itertext())`` → text_of()
  ``td.xpath('.//img/@src')`` → img_srcs()

**刻意复刻 lxml 的两个细节**（不复刻就会静默改数据，逐字对比测试卡的就是这个）：
  · ``itertext()`` 会把**尾随文本**（tail）也算进去，且原代码是用空格 join 所有文本块，
    所以 ``"GPT-6 Luna"`` 与 ``"(low)"`` 两个 span 会变成 ``"GPT-6 Luna  (low)"``
    （两个空格）。这里必须同样 join，不能在 join 后再压缩空白 —— 一压就与 lxml 不等价，
    而 ``num()``/分词都会受影响。
  · ``.//td`` 是**后代**匹配（嵌套表格里的 td 也在内，文档序），不是直接子节点。
"""
from __future__ import annotations

from html.parser import HTMLParser

# HTML 里不闭合的标签：不入栈，否则整棵树的层级会错位
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "text_before", "texts")

    def __init__(self, tag: str, attrs: dict | None = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[Node] = []
        self.parent: Node | None = None
        self.texts: list[str] = []        # 本节点自身的文本块（含兄弟节点的 tail 归到父节点）

    def walk(self):
        for c in self.children:
            yield c
            yield from c.walk()

    def find_all(self, tag: str) -> list[Node]:
        """后代替换：.//tag 的语义（含嵌套，文档序）。"""
        return [n for n in self.walk() if n.tag == tag]

    def text(self) -> str:
        """等价于 ``' '.join(el.itertext())``：自身与后代的文本块按文档序、用空格拼接。"""
        parts: list[str] = []

        def rec(n: Node):
            parts.extend(t for t in n.texts if t.strip())
            for c in n.children:
                rec(c)
        rec(self)
        return " ".join(parts).strip()

    def __repr__(self):
        return f"<{self.tag} {len(self.children)}>"


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#doc")
        self.stack = [self.root]
        self.skip_depth = 0     # <script>/<style> 里的文本不算 itertext 内容吗？算——但表格里没有它们

    @property
    def cur(self) -> Node:
        return self.stack[-1]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        node.parent = self.cur
        self.cur.children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        node.parent = self.cur
        self.cur.children.append(node)

    def handle_endtag(self, tag):
        # 容忍不闭合：往上找最近同名标签再弹（浏览器/库的常见行为）
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if data:
            self.cur.texts.append(data)


def parse(html: str) -> Node:
    b = _Builder()
    b.feed(html)
    return b.root


def find_tables(root: Node) -> list[Node]:
    return root.find_all("table")


def tbody_rows(table: Node) -> list[Node]:
    """``tbl.xpath('.//tbody/tr')``：tbody 里的直接 tr 行。"""
    out: list[Node] = []
    for tb in table.find_all("tbody"):
        out.extend(c for c in tb.children if c.tag == "tr")
    return out


def rows_any(table: Node) -> list[Node]:
    """``tbl.xpath('.//tr')``：所有后代 tr（tbody 有没有都算）。"""
    return table.find_all("tr")


def cells(tr: Node) -> list[Node]:
    """``tr.xpath('.//td')``：后代 td，文档序。"""
    return tr.find_all("td")


def cell_text(td: Node) -> str:
    return td.text()


def img_srcs(td: Node) -> list[str]:
    return [n.attrs.get("src", "") for n in td.find_all("img") if n.attrs.get("src")]
