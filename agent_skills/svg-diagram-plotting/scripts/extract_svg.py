#!/usr/bin/env python3
"""Extract one complete SVG document from a captured ChatGPT Markdown response."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
from xml.etree import ElementTree


SVG_FENCE_START_PATTERN = re.compile(
    r"^(?P<indent> {0,3})(?P<marker>`{3,}|~{3,})[ \t]*(?P<info>[^\r\n]*)\r?$",
    re.MULTILINE,
)
URL_REFERENCE_PATTERN = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)
XML_DECLARATION_PATTERN = re.compile(r"<!\s*(?:doctype|entity)\b", re.IGNORECASE)
CSS_IMPORT_PATTERN = re.compile(r"@import\b", re.IGNORECASE)
PROCESSING_INSTRUCTION_PATTERN = re.compile(r"<\?(?P<target>[\w:-]+)\b.*?\?>", re.DOTALL)
IGNORED_MARKUP_PATTERN = re.compile(r"<!--.*?-->|<!\[CDATA\[.*?\]\]>", re.DOTALL)
CSS_IMAGE_SET_PATTERN = re.compile(r"image-set\s*\(", re.IGNORECASE)
ASSET_ATTRIBUTE_NAMES = {"base", "href", "src", "data", "poster", "background"}
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
FORBIDDEN_ELEMENT_NAMES = {
    "animate",
    "animatemotion",
    "animatetransform",
    "discard",
    "foreignobject",
    "script",
    "set",
    "style",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("answer", type=Path, help="captured ChatGPT Markdown response")
    parser.add_argument("output", type=Path, help="destination SVG file")
    return parser.parse_args()


def extract_svg(text: str) -> str:
    fences = svg_fences(text)
    if len(fences) != 1:
        raise ValueError(f"expected exactly one fenced svg block, found {len(fences)}")
    start, end, valid_info = fences[0]
    if end is None:
        raise ValueError("the fenced svg block is not closed")
    if not valid_info:
        raise ValueError("the fenced svg block must use `svg` as its only info string")
    document = text[start.end() : end.start()].strip()
    markup = IGNORED_MARKUP_PATTERN.sub("", document)
    if XML_DECLARATION_PATTERN.search(markup):
        raise ValueError("SVG declarations and entities are not allowed")
    if PROCESSING_INSTRUCTION_PATTERN.search(markup):
        raise ValueError("SVG declarations and processing instructions are not allowed")
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as error:
        raise ValueError(f"the fenced svg block is not well-formed XML: {error}") from error
    if root.tag not in {"svg", f"{{{SVG_NAMESPACE}}}svg"}:
        raise ValueError("the fenced svg block must contain one SVG root element")
    for element in root.iter():
        element_name = element.tag.rsplit("}", 1)[-1].lower()
        if element_name in FORBIDDEN_ELEMENT_NAMES:
            raise ValueError(f"SVG element is not allowed: {element_name}")
        for attribute, value in element.attrib.items():
            name = attribute.rsplit("}", 1)[-1].lower()
            if name.startswith("on"):
                raise ValueError(f"SVG event attribute is not allowed: {name}")
            if name == "style":
                raise ValueError("SVG style attributes are not allowed")
            if name == "srcset":
                raise ValueError("SVG srcset attributes are not allowed")
            if name in ASSET_ATTRIBUTE_NAMES:
                if not local_reference(value):
                    raise ValueError(f"external SVG asset reference is not allowed: {value}")
            validate_css(value)
    return document + "\n"


def svg_fences(text: str) -> list[tuple[re.Match[str], re.Match[str] | None, bool]]:
    fences: list[tuple[re.Match[str], re.Match[str] | None, bool]] = []
    active: tuple[re.Match[str], bool, bool] | None = None
    for fence in SVG_FENCE_START_PATTERN.finditer(text):
        marker = fence.group("marker")
        info = fence.group("info").strip()
        if active is None:
            words = info.lower().split()
            active = (fence, bool(words) and words[0] == "svg", info.lower() == "svg")
            continue
        opening, is_svg, valid_info = active
        opening_marker = opening.group("marker")
        if marker[0] != opening_marker[0] or len(marker) < len(opening_marker) or info:
            continue
        if is_svg:
            fences.append((opening, fence, valid_info))
        active = None
    if active is not None and active[1]:
        fences.append((active[0], None, active[2]))
    return fences


def validate_css(value: str) -> None:
    if "\\" in value:
        raise ValueError("CSS escapes are not allowed in a self-contained SVG")
    if CSS_IMPORT_PATTERN.search(value) or CSS_IMAGE_SET_PATTERN.search(value):
        raise ValueError("CSS image references are not allowed in a self-contained SVG")
    for reference in URL_REFERENCE_PATTERN.findall(value):
        if not local_reference(reference[1]):
            raise ValueError(f"external SVG resource reference is not allowed: {reference[1]}")


def local_reference(value: str) -> bool:
    return value.strip().startswith("#")


def main() -> int:
    args = parse_args()
    source = args.answer.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"answer file not found: {source}")
    svg = extract_svg(source.read_text(encoding="utf-8"))
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    output.write_text(svg, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
