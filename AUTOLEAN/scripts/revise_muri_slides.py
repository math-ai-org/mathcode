from __future__ import annotations

import copy
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "a14": "http://schemas.microsoft.com/office/drawing/2014/main",
}

for prefix, uri in NS.items():
    ET.register_namespace(prefix, uri)


def qn(prefix: str, tag: str) -> str:
    return f"{{{NS[prefix]}}}{tag}"


def pt_to_emu(value: float) -> str:
    return str(int(round(value * 12700)))


def parse_xml(blob: bytes) -> ET.Element:
    return ET.fromstring(blob)


def serialize_xml(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def find_shape(root: ET.Element, name: str) -> ET.Element:
    for sp in root.findall(".//p:sp", NS):
        c_nv_pr = sp.find("p:nvSpPr/p:cNvPr", NS)
        if c_nv_pr is not None and c_nv_pr.get("name") == name:
            return sp
    raise KeyError(f"shape not found: {name}")


def find_picture(root: ET.Element, name: str) -> ET.Element:
    for pic in root.findall(".//p:pic", NS):
        c_nv_pr = pic.find("p:nvPicPr/p:cNvPr", NS)
        if c_nv_pr is not None and c_nv_pr.get("name") == name:
            return pic
    raise KeyError(f"picture not found: {name}")


def sp_tree(root: ET.Element) -> ET.Element:
    return root.find("p:cSld/p:spTree", NS)


def shape_name(el: ET.Element) -> str:
    c_nv_pr = el.find("p:nvSpPr/p:cNvPr", NS)
    return c_nv_pr.get("name", "")


def picture_name(el: ET.Element) -> str:
    c_nv_pr = el.find("p:nvPicPr/p:cNvPr", NS)
    return c_nv_pr.get("name", "")


def ensure_sp_pr(el: ET.Element) -> ET.Element:
    sp_pr = el.find("p:spPr", NS)
    if sp_pr is None:
        sp_pr = ET.SubElement(el, qn("p", "spPr"))
    return sp_pr


def ensure_tx_body(shape: ET.Element) -> ET.Element:
    tx_body = shape.find("p:txBody", NS)
    if tx_body is None:
        tx_body = ET.SubElement(shape, qn("p", "txBody"))
        ET.SubElement(tx_body, qn("a", "bodyPr"))
        ET.SubElement(tx_body, qn("a", "lstStyle"))
    return tx_body


def ensure_xfrm(sp_pr: ET.Element) -> ET.Element:
    xfrm = sp_pr.find("a:xfrm", NS)
    if xfrm is None:
        xfrm = ET.Element(qn("a", "xfrm"))
        sp_pr.insert(0, xfrm)
    if xfrm.find("a:off", NS) is None:
        ET.SubElement(xfrm, qn("a", "off"))
    if xfrm.find("a:ext", NS) is None:
        ET.SubElement(xfrm, qn("a", "ext"))
    return xfrm


def set_bounds(el: ET.Element, left: float, top: float, width: float, height: float) -> None:
    sp_pr = ensure_sp_pr(el)
    xfrm = ensure_xfrm(sp_pr)
    off = xfrm.find("a:off", NS)
    ext = xfrm.find("a:ext", NS)
    off.set("x", pt_to_emu(left))
    off.set("y", pt_to_emu(top))
    ext.set("cx", pt_to_emu(width))
    ext.set("cy", pt_to_emu(height))


def clear_local_children(parent: ET.Element, local_names: set[str]) -> None:
    for child in list(parent):
        if child.tag.split("}", 1)[-1] in local_names:
            parent.remove(child)


def set_shape_fill_and_line(
    shape: ET.Element,
    fill_hex: str | None = None,
    line_hex: str | None = None,
    line_width: int = 12700,
) -> None:
    sp_pr = ensure_sp_pr(shape)
    clear_local_children(
        sp_pr,
        {"solidFill", "gradFill", "blipFill", "pattFill", "grpFill", "noFill", "ln"},
    )
    if fill_hex is None:
        ET.SubElement(sp_pr, qn("a", "noFill"))
    else:
        solid_fill = ET.SubElement(sp_pr, qn("a", "solidFill"))
        ET.SubElement(solid_fill, qn("a", "srgbClr"), {"val": fill_hex})
    line = ET.SubElement(sp_pr, qn("a", "ln"), {"w": str(line_width)})
    if line_hex is None:
        ET.SubElement(line, qn("a", "noFill"))
    else:
        solid_fill = ET.SubElement(line, qn("a", "solidFill"))
        ET.SubElement(solid_fill, qn("a", "srgbClr"), {"val": line_hex})


def apply_text_style(
    shape: ET.Element,
    *,
    font_size: int | None = None,
    color_hex: str | None = None,
    bold: bool | None = None,
    align: str | None = None,
) -> None:
    tx_body = ensure_tx_body(shape)
    for paragraph in tx_body.findall("a:p", NS):
        if align:
            p_pr = paragraph.find("a:pPr", NS)
            if p_pr is None:
                p_pr = ET.Element(qn("a", "pPr"))
                paragraph.insert(0, p_pr)
            p_pr.set("algn", align)
        for run_tag in ("a:r", "a:endParaRPr"):
            for holder in paragraph.findall(run_tag, NS):
                if run_tag == "a:r":
                    r_pr = holder.find("a:rPr", NS)
                    if r_pr is None:
                        r_pr = ET.Element(qn("a", "rPr"))
                        holder.insert(0, r_pr)
                else:
                    r_pr = holder
                if font_size is not None:
                    r_pr.set("sz", str(font_size))
                if bold is not None:
                    r_pr.set("b", "1" if bold else "0")
                if color_hex is not None:
                    clear_local_children(r_pr, {"solidFill"})
                    solid_fill = ET.SubElement(r_pr, qn("a", "solidFill"))
                    ET.SubElement(solid_fill, qn("a", "srgbClr"), {"val": color_hex})
                for latin in list(r_pr.findall("a:latin", NS)):
                    if latin.get("typeface") == "-webkit-standard":
                        r_pr.remove(latin)


def set_run_text(run: ET.Element, text: str) -> None:
    t = run.find("a:t", NS)
    if t is None:
        t = ET.SubElement(run, qn("a", "t"))
    t.text = text


def rewrite_text_box(shape: ET.Element, paragraphs: list[str], *, font_size: int, color_hex: str) -> None:
    tx_body = ensure_tx_body(shape)
    body_pr = tx_body.find("a:bodyPr", NS)
    if body_pr is None:
        body_pr = ET.SubElement(tx_body, qn("a", "bodyPr"))
    if tx_body.find("a:lstStyle", NS) is None:
        tx_body.insert(1, ET.Element(qn("a", "lstStyle")))
    for paragraph in list(tx_body.findall("a:p", NS)):
        tx_body.remove(paragraph)
    for text in paragraphs:
        p = ET.SubElement(tx_body, qn("a", "p"))
        r = ET.SubElement(p, qn("a", "r"))
        r_pr = ET.SubElement(r, qn("a", "rPr"), {"lang": "en-US", "sz": str(font_size)})
        solid_fill = ET.SubElement(r_pr, qn("a", "solidFill"))
        ET.SubElement(solid_fill, qn("a", "srgbClr"), {"val": color_hex})
        ET.SubElement(r, qn("a", "t")).text = text


def set_title_style(shape: ET.Element, *, font_size: int, color_hex: str, left_align: bool = True) -> None:
    apply_text_style(shape, font_size=font_size, color_hex=color_hex, bold=None, align="l" if left_align else None)


def set_outline_styles(shape: ET.Element) -> None:
    tx_body = ensure_tx_body(shape)
    paragraphs = tx_body.findall("a:p", NS)
    for idx, paragraph in enumerate(paragraphs):
        level = "0"
        p_pr = paragraph.find("a:pPr", NS)
        if p_pr is not None and p_pr.get("lvl"):
            level = p_pr.get("lvl")
        size = 2400 if level == "0" else 1900
        color = "153A59" if level == "0" else "576372"
        for run in paragraph.findall("a:r", NS):
            r_pr = run.find("a:rPr", NS)
            if r_pr is None:
                r_pr = ET.Element(qn("a", "rPr"), {"lang": "en-US"})
                run.insert(0, r_pr)
            r_pr.set("sz", str(size))
            r_pr.set("b", "1" if level == "0" else "0")
            clear_local_children(r_pr, {"solidFill"})
            solid_fill = ET.SubElement(r_pr, qn("a", "solidFill"))
            ET.SubElement(solid_fill, qn("a", "srgbClr"), {"val": color})
            for latin in list(r_pr.findall("a:latin", NS)):
                if latin.get("typeface") == "-webkit-standard":
                    r_pr.remove(latin)
        end_pr = paragraph.find("a:endParaRPr", NS)
        if end_pr is None:
            end_pr = ET.SubElement(paragraph, qn("a", "endParaRPr"), {"lang": "en-US"})
        end_pr.set("sz", str(size))


def set_footer_text_style(shape: ET.Element) -> None:
    apply_text_style(shape, font_size=1200, color_hex="66707B", bold=False, align="l")


def add_accent_bar(root: ET.Element, *, left: float, top: float, width: float, height: float, fill_hex: str) -> None:
    tree = sp_tree(root)
    max_id = 1
    for c_nv_pr in tree.findall(".//p:cNvPr", NS):
        try:
            max_id = max(max_id, int(c_nv_pr.get("id", "1")))
        except ValueError:
            continue
    shape_id = max_id + 1
    sp = ET.SubElement(tree, qn("p", "sp"))
    nv_sp_pr = ET.SubElement(sp, qn("p", "nvSpPr"))
    ET.SubElement(nv_sp_pr, qn("p", "cNvPr"), {"id": str(shape_id), "name": f"Accent Bar {shape_id}"})
    ET.SubElement(nv_sp_pr, qn("p", "cNvSpPr"))
    ET.SubElement(nv_sp_pr, qn("p", "nvPr"))
    sp_pr = ET.SubElement(sp, qn("p", "spPr"))
    xfrm = ET.SubElement(sp_pr, qn("a", "xfrm"))
    ET.SubElement(xfrm, qn("a", "off"), {"x": pt_to_emu(left), "y": pt_to_emu(top)})
    ET.SubElement(xfrm, qn("a", "ext"), {"cx": pt_to_emu(width), "cy": pt_to_emu(height)})
    prst_geom = ET.SubElement(sp_pr, qn("a", "prstGeom"), {"prst": "rect"})
    ET.SubElement(prst_geom, qn("a", "avLst"))
    solid_fill = ET.SubElement(sp_pr, qn("a", "solidFill"))
    ET.SubElement(solid_fill, qn("a", "srgbClr"), {"val": fill_hex})
    line = ET.SubElement(sp_pr, qn("a", "ln"))
    ET.SubElement(line, qn("a", "noFill"))
    tx_body = ET.SubElement(sp, qn("p", "txBody"))
    ET.SubElement(tx_body, qn("a", "bodyPr"))
    ET.SubElement(tx_body, qn("a", "lstStyle"))
    ET.SubElement(tx_body, qn("a", "p"))


def remove_shape(root: ET.Element, target_name: str) -> None:
    tree = sp_tree(root)
    for child in list(tree):
        if child.tag == qn("p", "sp") and shape_name(child) == target_name:
            tree.remove(child)
            return


def revise_slide_1(root: ET.Element) -> None:
    title = find_shape(root, "Title 1")
    set_bounds(title, 68, 175, 760, 175)
    set_title_style(title, font_size=3600, color_hex="173A58", left_align=True)
    picture = find_picture(root, "Picture 5")
    set_bounds(picture, 846, 24, 78, 90)
    add_accent_bar(root, left=70, top=372, width=160, height=6, fill_hex="C7A53A")


def revise_slide_2(root: ET.Element) -> None:
    title = find_shape(root, "Title 1")
    set_title_style(title, font_size=3000, color_hex="173A58", left_align=True)
    outline = find_shape(root, "Content Placeholder 2")
    set_outline_styles(outline)
    citation_box = find_shape(root, "Rectangle 3")
    set_bounds(citation_box, 56, 438, 852, 28)
    set_shape_fill_and_line(citation_box, fill_hex="F3F5F7", line_hex=None)
    set_footer_text_style(citation_box)
    citation_text = find_shape(root, "TextBox 4")
    set_bounds(citation_text, 56, 475, 852, 28)
    set_shape_fill_and_line(citation_text, fill_hex="F3F5F7", line_hex=None)
    set_footer_text_style(citation_text)


def revise_slide_4(root: ET.Element) -> None:
    title = find_shape(root, "Title 1")
    set_title_style(title, font_size=2800, color_hex="173A58", left_align=True)
    question = find_shape(root, "TextBox 10")
    set_shape_fill_and_line(question, fill_hex="F4F7FB", line_hex="D6DFE9")
    apply_text_style(question, font_size=1900, color_hex="173A58", bold=False, align="l")
    footer = find_shape(root, "Rectangle 4")
    set_bounds(footer, 56, 484, 852, 34)
    set_shape_fill_and_line(footer, fill_hex="F3F5F7", line_hex=None)
    set_footer_text_style(footer)


def revise_slide_8(root: ET.Element) -> None:
    title = find_shape(root, "Title 1")
    set_title_style(title, font_size=2800, color_hex="173A58", left_align=True)
    question = find_shape(root, "TextBox 4")
    set_shape_fill_and_line(question, fill_hex="F4F7FB", line_hex="D6DFE9")
    apply_text_style(question, font_size=1900, color_hex="173A58", bold=False, align="l")
    footer = find_shape(root, "TextBox 6")
    set_bounds(footer, 56, 483, 852, 34)
    set_shape_fill_and_line(footer, fill_hex="F3F5F7", line_hex=None)
    set_footer_text_style(footer)


def revise_slide_12(root: ET.Element) -> None:
    title = find_shape(root, "Title 1")
    set_title_style(title, font_size=3000, color_hex="173A58", left_align=True)
    content = find_shape(root, "Content Placeholder 2")
    rewrite_text_box(
        content,
        [
            "Heat flux in chiral structures generates steady vibrational angular momentum.",
            "Handedness, flow direction, and driving frequency control sign and magnitude.",
            "Phase-controlled driving enables rectification without asymmetric baths.",
        ],
        font_size=2400,
        color_hex="22313F",
    )


def revise_slide_13(root: ET.Element) -> None:
    title = find_shape(root, "Title 1")
    set_title_style(title, font_size=3000, color_hex="173A58", left_align=True)
    set_bounds(find_picture(root, "Picture 8"), 48, 152, 110, 110)
    set_bounds(find_picture(root, "Picture 7"), 166, 152, 110, 110)
    set_bounds(find_picture(root, "Picture 6"), 284, 152, 110, 110)
    oval = find_shape(root, "Oval 9")
    set_bounds(oval, 402, 152, 110, 110)
    set_bounds(find_picture(root, "Picture 4"), 538, 174, 372, 279)
    set_bounds(find_picture(root, "Picture 5"), 784, 18, 140, 140)


def revise_presentation(input_pptx: Path, output_pptx: Path) -> None:
    with zipfile.ZipFile(input_pptx) as src:
        members = {name: src.read(name) for name in src.namelist()}

    slide_updates = {
        "ppt/slides/slide1.xml": revise_slide_1,
        "ppt/slides/slide2.xml": revise_slide_2,
        "ppt/slides/slide4.xml": revise_slide_4,
        "ppt/slides/slide8.xml": revise_slide_8,
        "ppt/slides/slide12.xml": revise_slide_12,
        "ppt/slides/slide13.xml": revise_slide_13,
    }

    for slide_name, fn in slide_updates.items():
        root = parse_xml(members[slide_name])
        fn(root)
        members[slide_name] = serialize_xml(root)

    output_pptx.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_pptx, "w", compression=zipfile.ZIP_DEFLATED) as dst:
        for name, blob in members.items():
            dst.writestr(name, blob)


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: revise_muri_slides.py INPUT_PPTX OUTPUT_PPTX", file=sys.stderr)
        return 1
    input_pptx = Path(sys.argv[1]).expanduser().resolve()
    output_pptx = Path(sys.argv[2]).expanduser().resolve()
    revise_presentation(input_pptx, output_pptx)
    print(output_pptx)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
