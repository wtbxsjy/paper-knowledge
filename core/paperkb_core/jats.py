"""JATS (PMC) XML -> Markdown, stdlib only. Deterministic, no heuristics on content.

Covers what PMC OA articles use in practice: title, authors, abstract(s),
nested sections, paragraphs, lists, figures (graphic + label + caption),
tables (kept as HTML — Markdown pipe tables cannot express spans), display and
inline formulas (TeX when present, else MathML text), xref citations, inline
emphasis, footnotes, acknowledgements, appendices, supplementary material and
the reference list.

Figures are written as ``![label](images/<file>)``; ``graphic_files`` maps the
``xlink:href`` (often extension-less) to the files actually present.
"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Optional

XLINK = "{http://www.w3.org/1999/xlink}href"
MML = "{http://www.w3.org/1998/Math/MathML}"
IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".svg", ".webp")


def _local(tag) -> str:
    return tag.split("}", 1)[1] if isinstance(tag, str) and "}" in tag else (tag or "")


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "")


class Converter:
    def __init__(self, resolve_graphic: Callable[[str], Optional[str]]):
        self.resolve_graphic = resolve_graphic
        self.missing_graphics: list[str] = []

    # ---------- inline ----------
    def inline(self, el) -> str:
        out = [_ws(html.escape(el.text or "", quote=False))] if el.text else []
        for ch in el:
            out.append(self.inline_el(ch))
            if ch.tail:
                out.append(_ws(html.escape(ch.tail, quote=False)))
        return "".join(out)

    def inline_el(self, el) -> str:
        t = _local(el.tag)
        if t in ("italic", "it"):
            s = self.inline(el).strip()
            return f"*{s}*" if s else ""
        if t == "bold":
            s = self.inline(el).strip()
            return f"**{s}**" if s else ""
        if t == "sup":
            return f"<sup>{self.inline(el)}</sup>"
        if t == "sub":
            return f"<sub>{self.inline(el)}</sub>"
        if t in ("monospace", "code"):
            return f"`{''.join(el.itertext()).strip()}`"
        if t == "sc":
            return self.inline(el)
        if t == "ext-link" or t == "uri":
            href = el.get(XLINK) or "".join(el.itertext()).strip()
            label = self.inline(el).strip() or href
            return f"[{label}]({href})" if href else label
        if t == "xref":
            return self.inline(el)
        if t == "inline-formula":
            return self.formula(el, display=False)
        if t in ("inline-graphic",):
            f = self.resolve_graphic(el.get(XLINK) or "")
            return f"![](images/{f})" if f else ""
        if t in ("fn", "table-wrap-foot"):
            return ""
        if t == "break":
            return "<br>"
        return self.inline(el)

    def formula(self, el, display: bool) -> str:
        tex = el.find(".//tex-math")
        if tex is not None and "".join(tex.itertext()).strip():
            s = "".join(tex.itertext()).strip()
            s = re.sub(r"^\\documentclass.*?\\begin\{document\}", "", s, flags=re.S)
            s = re.sub(r"\\end\{document\}\s*$", "", s).strip().strip("$").strip()
            return f"\n\n$$\n{s}\n$$\n\n" if display else f"${s}$"
        math = el.find(f".//{MML}math")
        if math is not None:
            alt = math.get("alttext")
            s = alt or _ws("".join(math.itertext())).strip()
            return f"\n\n$$\n{s}\n$$\n\n" if display else f"${s}$"
        g = el.find(".//graphic")
        if g is not None:
            f = self.resolve_graphic(g.get(XLINK) or "")
            return f"\n\n![](images/{f})\n\n" if f else ""
        return self.inline(el)

    # ---------- blocks ----------
    def blocks(self, el, depth: int) -> list[str]:
        out: list[str] = []
        for ch in el:
            out.extend(self.block(ch, depth))
        return out

    def block(self, el, depth: int) -> list[str]:
        t = _local(el.tag)
        if t == "sec":
            out = []
            title = el.find("title")
            label = el.find("label")
            if title is not None:
                head = self.inline(title).strip()
                if label is not None and "".join(label.itertext()).strip():
                    head = f"{''.join(label.itertext()).strip()} {head}"
                out.append(f"{'#' * min(depth, 6)} {head}")
            for ch in el:
                if _local(ch.tag) not in ("title", "label"):
                    out.extend(self.block(ch, depth + 1))
            return out
        if t == "p":
            # a <p> may contain block children (fig, table-wrap, disp-formula, list)
            parts, buf = [], [html.escape(_ws(el.text or ""), quote=False)] if el.text else []
            for ch in el:
                ct = _local(ch.tag)
                if ct in ("fig", "table-wrap", "list", "disp-formula", "disp-quote", "boxed-text"):
                    if "".join(buf).strip():
                        parts.append("".join(buf).strip())
                    buf = []
                    parts.extend(self.block(ch, depth))
                else:
                    buf.append(self.inline_el(ch))
                if ch.tail:
                    buf.append(html.escape(_ws(ch.tail), quote=False))
            if "".join(buf).strip():
                parts.append("".join(buf).strip())
            return parts
        if t == "list":
            ordered = el.get("list-type") in ("order", "arabic", "alpha-lower", "alpha-upper", "roman-lower", "roman-upper")
            items = []
            for i, li in enumerate(el.findall("list-item"), 1):
                body = " ".join(" ".join(self.block(p, depth)) if _local(p.tag) != "label" else ""
                                for p in li).strip()
                lab = li.find("label")
                mark = f"{i}." if ordered else "-"
                if lab is not None and "".join(lab.itertext()).strip():
                    mark = f"- {''.join(lab.itertext()).strip()}"
                items.append(f"{mark} {body}")
            return ["\n".join(items)] if items else []
        if t == "fig":
            return self.figure(el)
        if t == "fig-group":
            return [b for f in el.findall("fig") for b in self.figure(f)]
        if t == "table-wrap":
            return self.table(el)
        if t == "disp-formula":
            return [self.formula(el, display=True).strip()]
        if t in ("disp-quote",):
            return ["> " + " ".join(self.blocks(el, depth))]
        if t == "boxed-text":
            return self.blocks(el, depth)
        if t == "supplementary-material":
            return [self.supplement(el)]
        if t in ("title", "label", "object-id"):
            return []
        if t in ("def-list",):
            return [f"- **{self.inline(d.find('term')) if d.find('term') is not None else ''}**: "
                    f"{' '.join(self.blocks(d.find('def'), depth)) if d.find('def') is not None else ''}"
                    for d in el.findall("def-item")]
        if t in ("ack", "app", "notes", "glossary", "fn-group", "app-group", "bio"):
            out = []
            title = el.find("title")
            name = {"ack": "Acknowledgements", "fn-group": "Notes", "app-group": "Appendix",
                    "notes": "Notes", "glossary": "Glossary"}.get(t, "")
            head = self.inline(title).strip() if title is not None else name
            if head:
                out.append(f"{'#' * min(depth, 6)} {head}")
            for ch in el:
                if _local(ch.tag) not in ("title",):
                    out.extend(self.block(ch, depth + 1))
            return out
        if t == "fn":
            lab = el.find("label")
            body = " ".join(self.blocks(el, depth))
            return [f"{('[' + ''.join(lab.itertext()).strip() + '] ') if lab is not None else ''}{body}".strip()]
        if t in ("caption",):
            return self.blocks(el, depth)
        # unknown block: keep its text rather than lose content
        s = self.inline(el).strip()
        return [s] if s else []

    def _label_caption(self, el) -> tuple[str, str]:
        lab = el.find("label")
        label = _ws("".join(lab.itertext())).strip() if lab is not None else ""
        cap = el.find("caption")
        caption = ""
        if cap is not None:
            ct = cap.find("title")
            parts = [f"**{self.inline(ct).strip()}**"] if ct is not None and self.inline(ct).strip() else []
            parts += [" ".join(self.block(p, 9)) for p in cap if _local(p.tag) != "title"]
            caption = " ".join(x for x in parts if x).strip()
        return label, caption

    def figure(self, el) -> list[str]:
        label, caption = self._label_caption(el)
        out = []
        for g in el.iter():
            if _local(g.tag) == "graphic":
                href = g.get(XLINK) or ""
                f = self.resolve_graphic(href)
                if f:
                    out.append(f"![{label}](images/{f})")
                else:
                    self.missing_graphics.append(href)
        text = " ".join(x for x in (f"**{label}**" if label else "", caption) if x)
        if text:
            out.append(text)
        return out

    def _cell_html(self, el) -> str:
        t = _local(el.tag)
        attrs = "".join(f' {a}="{html.escape(el.get(a))}"' for a in ("colspan", "rowspan") if el.get(a))
        return f"<{t}{attrs}>{self.inline(el).strip()}</{t}>"

    def table(self, el) -> list[str]:
        label, caption = self._label_caption(el)
        out = []
        if label or caption:
            out.append(" ".join(x for x in (f"**{label}**" if label else "", caption) if x))
        tbl = next((x for x in el.iter() if _local(x.tag) == "table"), None)
        if tbl is not None:
            rows = []
            for tr in tbl.iter():
                if _local(tr.tag) == "tr":
                    rows.append("<tr>" + "".join(self._cell_html(c) for c in tr if _local(c.tag) in ("td", "th")) + "</tr>")
            out.append("<table>" + "".join(rows) + "</table>")
        else:  # table delivered as an image
            for g in el.iter():
                if _local(g.tag) == "graphic":
                    f = self.resolve_graphic(g.get(XLINK) or "")
                    if f:
                        out.append(f"![{label}](images/{f})")
        foot = el.find("table-wrap-foot")
        if foot is not None:
            ft = " ".join(self.blocks(foot, 9)).strip()
            if ft:
                out.append(ft)
        return out

    def supplement(self, el) -> str:
        label, caption = self._label_caption(el)
        media = el.find("media")
        href = (media.get(XLINK) if media is not None else None) or el.get(XLINK) or ""
        name = href.rsplit("/", 1)[-1]
        return "- " + " ".join(x for x in (f"**{label}**" if label else "", caption,
                                            f"(`supplementary/{name}`)" if name else "") if x)

    # ---------- front / back ----------
    def authors(self, meta) -> str:
        names = []
        for c in meta.findall(".//contrib-group/contrib"):
            if c.get("contrib-type") not in (None, "author"):
                continue
            n = c.find("name")
            if n is not None:
                g, s = n.findtext("given-names", ""), n.findtext("surname", "")
                names.append(_ws(f"{g} {s}").strip())
            elif c.find("collab") is not None:
                names.append(_ws("".join(c.find("collab").itertext())).strip())
        return ", ".join(names)

    def reference(self, ref) -> str:
        lab = ref.findtext("label", "").strip().rstrip(".")
        cit = next((x for x in ref if _local(x.tag) in ("element-citation", "mixed-citation", "citation", "nlm-citation")), None)
        if cit is None:
            return ""
        if _local(cit.tag) == "mixed-citation":
            text = _ws("".join(cit.itertext())).strip()
        else:
            au = [_ws(f"{n.findtext('surname', '')} {n.findtext('given-names', '')}").strip()
                  for n in cit.iter() if _local(n.tag) == "name"]
            if cit.find(".//etal") is not None:
                au.append("et al.")
            parts = [", ".join(au)]
            for f in ("article-title", "chapter-title", "source"):
                v = cit.find(f)
                if v is not None:
                    parts.append(_ws("".join(v.itertext())).strip())
            year, vol, fp, lp = (cit.findtext(k, "").strip() for k in ("year", "volume", "fpage", "lpage"))
            loc = " ".join(x for x in (vol, f"{fp}–{lp}" if fp and lp else fp) if x)
            parts += [x for x in (loc, f"({year})" if year else "") if x]
            text = ". ".join(p for p in parts if p)
        doi = next((p.text for p in cit.iter() if _local(p.tag) == "pub-id" and p.get("pub-id-type") == "doi" and p.text), None)
        if doi and doi not in text:
            text += f" https://doi.org/{doi.strip()}"
        return f"{lab + '. ' if lab else ''}{text}"

    def convert(self, root) -> str:
        art = root if _local(root.tag) == "article" else root.find(".//article")
        meta = art.find("front/article-meta")
        out = []
        title = meta.find("title-group/article-title") if meta is not None else None
        if title is not None:
            out.append(f"# {self.inline(title).strip()}")
        if meta is not None:
            au = self.authors(meta)
            if au:
                out.append(au)
            for ab in meta.findall("abstract"):
                kind = ab.get("abstract-type")
                if kind in ("toc", "teaser"):
                    continue
                t = ab.find("title")
                out.append(f"## {self.inline(t).strip() if t is not None else ('Abstract' if not kind else kind.replace('-', ' ').title())}")
                for ch in ab:
                    if _local(ch.tag) != "title":
                        out.extend(self.block(ch, 3))
            kw = [_ws("".join(k.itertext())).strip() for k in meta.findall(".//kwd")]
            if kw:
                out.append("**Keywords:** " + "; ".join(kw))
            lic = meta.find("permissions/license")
            if lic is not None:
                href = lic.get(XLINK) or next((x.get(XLINK) for x in lic.iter() if x.get(XLINK)), "")
                lp = lic.find("license-p")
                txt = _ws("".join(lp.itertext())).strip() if lp is not None else ""
                out.append("**License:** " + " ".join(x for x in (txt[:400], f"<{href}>" if href else "") if x))
        body = art.find("body")
        if body is not None:
            out.extend(self.blocks(body, 2))
        # floats placed outside the body (common in PMC)
        fl = art.find("floats-group")
        if fl is not None:
            out.extend(self.blocks(fl, 2))
        back = art.find("back")
        if back is not None:
            for ch in back:
                if _local(ch.tag) in ("ref-list",):
                    continue
                out.extend(self.block(ch, 2))
            refs = [r for r in back.iter() if _local(r.tag) == "ref"]
            if refs:
                out.append("## References")
                out.append("\n".join(x for x in (self.reference(r) for r in refs) if x))
        text = "\n\n".join(x.strip() for x in out if x and x.strip())
        return re.sub(r"\n{3,}", "\n\n", text) + "\n"


def graphic_resolver(files: list[str]) -> Callable[[str], Optional[str]]:
    """Map an xlink:href (with or without extension) to a file present on disk."""
    by_stem: dict[str, str] = {}
    for f in files:
        if f.lower().endswith(IMG_EXT):
            by_stem.setdefault(Path(f).stem.lower(), f)
            by_stem.setdefault(f.lower(), f)

    def resolve(href: str) -> Optional[str]:
        h = href.rsplit("/", 1)[-1].lower()
        return by_stem.get(h) or by_stem.get(Path(h).stem)
    return resolve


def convert_file(xml_path: Path, files: list[str]) -> tuple[str, list[str]]:
    """Return (markdown, missing_graphic_hrefs)."""
    root = ET.parse(xml_path).getroot()
    c = Converter(graphic_resolver(files))
    md = c.convert(root)
    return md, c.missing_graphics
