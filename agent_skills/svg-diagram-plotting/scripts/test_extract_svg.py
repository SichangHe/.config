"""Focused contract tests for the captured-response SVG extractor."""

from __future__ import annotations

import unittest

from extract_svg import extract_svg


VALID = """```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><rect width="1" height="1"/></svg>
```"""


class ExtractSvgTest(unittest.TestCase):
    def test_extracts_one_well_formed_self_contained_svg(self) -> None:
        self.assertIn("<svg", extract_svg(VALID))

    def test_rejects_malformed_xml(self) -> None:
        with self.assertRaisesRegex(ValueError, "well-formed XML"):
            extract_svg("```svg\n<svg><g></svg>\n```")

    def test_rejects_external_reference(self) -> None:
        with self.assertRaisesRegex(ValueError, "external SVG"):
            extract_svg("```svg\n<svg><image href=\"https://example.test/image.png\"/></svg>\n```")

    def test_rejects_data_uri_asset(self) -> None:
        with self.assertRaisesRegex(ValueError, "external SVG asset"):
            extract_svg("```svg\n<svg><image href=\"data:image/svg+xml,%3Csvg/%3E\"/></svg>\n```")

    def test_rejects_asset_animation(self) -> None:
        with self.assertRaisesRegex(ValueError, "SVG element is not allowed: animate"):
            extract_svg(
                "```svg\n<svg><image href=\"#local\"><animate attributeName=\"href\" to=\"https://example.test/x.png\"/></image></svg>\n```"
            )

    def test_rejects_event_attributes(self) -> None:
        with self.assertRaisesRegex(ValueError, "SVG event attribute"):
            extract_svg("```svg\n<svg onload=\"fetch('https://example.test/x')\"/>\n```")

    def test_rejects_relative_asset_reference(self) -> None:
        with self.assertRaisesRegex(ValueError, "SVG element is not allowed"):
            extract_svg("```svg\n<svg><foreignObject><img src=\"relative-image.png\"/></foreignObject></svg>\n```")

    def test_rejects_css_import(self) -> None:
        with self.assertRaisesRegex(ValueError, "SVG element is not allowed: style"):
            extract_svg("```svg\n<svg><style>@import \"relative.css\";</style></svg>\n```")

    def test_rejects_xml_declarations(self) -> None:
        with self.assertRaisesRegex(ValueError, "declarations and entities"):
            extract_svg("```svg\n<!DOCTYPE svg SYSTEM \"https://example.test/external.dtd\"><svg/>\n```")

    def test_rejects_processing_instruction(self) -> None:
        with self.assertRaisesRegex(ValueError, "processing instructions"):
            extract_svg(
                "```svg\n<?xml-stylesheet type=\"text/css\" href=\"https://example.test/style.css\"?><svg/>\n```"
            )

    def test_rejects_xml_declaration(self) -> None:
        with self.assertRaisesRegex(ValueError, "declarations and processing instructions"):
            extract_svg("```svg\n<?xml version=\"1.0\"?><svg/>\n```")

    def test_rejects_external_xml_base(self) -> None:
        with self.assertRaisesRegex(ValueError, "external SVG asset"):
            extract_svg("```svg\n<svg xml:base=\"https://example.test/a.svg\"><use href=\"#remote\"/></svg>\n```")

    def test_rejects_non_svg_namespace(self) -> None:
        with self.assertRaisesRegex(ValueError, "SVG root"):
            extract_svg("```svg\n<svg xmlns=\"urn:example:not-svg\"><rect/></svg>\n```")

    def test_supports_standard_svg_fence_variants(self) -> None:
        for opening, closing in (("``` svg", "```"), ("````svg", "````"), ("   ```svg", "   ```")):
            with self.subTest(opening=opening):
                self.assertIn("<svg", extract_svg(f"{opening}\n<svg/>\n{closing}"))

    def test_rejects_unclosed_standard_svg_fence_variants(self) -> None:
        for opening in ("``` svg", "````svg", "   ```svg"):
            with self.subTest(opening=opening):
                with self.assertRaisesRegex(ValueError, "exactly one fenced svg block"):
                    extract_svg(f"{VALID}\n{opening}\n<svg/>")

    def test_rejects_malformed_svg_fence_info(self) -> None:
        with self.assertRaisesRegex(ValueError, "only info string"):
            extract_svg("```svg invalid\n<svg/>\n```")

    def test_rejects_unclosed_malformed_additional_svg_fence(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one fenced svg block"):
            extract_svg(f"{VALID}\n```svg invalid\n<svg/>")

    def test_ignores_svg_looking_lines_in_another_code_fence(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one fenced svg block, found 0"):
            extract_svg("````text\n```svg\n<svg/>\n```\n````")

    def test_rejects_escaped_css_imports_and_urls(self) -> None:
        for style in ("@im\\70ort 'https://example.test/a.css';", "u\\72l(https://example.test/a.svg)"):
            with self.subTest(style=style):
                with self.assertRaisesRegex(ValueError, "SVG element is not allowed: style"):
                    extract_svg(f"```svg\n<svg><style>{style}</style></svg>\n```")

    def test_rejects_css_image_set(self) -> None:
        with self.assertRaisesRegex(ValueError, "SVG element is not allowed: style"):
            extract_svg("```svg\n<svg><style>fill: image-set(\"https://example.test/a.png\" 1x)</style></svg>\n```")

    def test_rejects_css_image_and_style_attributes(self) -> None:
        with self.assertRaisesRegex(ValueError, "SVG element is not allowed: style"):
            extract_svg(
                "```svg\n<svg><style>svg { background-image: image(\"https://example.test/remote.png\", red); }</style></svg>\n```"
            )
        with self.assertRaisesRegex(ValueError, "style attributes"):
            extract_svg("```svg\n<svg style=\"fill: red\"/>\n```")

    def test_rejects_srcset(self) -> None:
        with self.assertRaisesRegex(ValueError, "srcset"):
            extract_svg(
                "```svg\n<svg srcset=\"data:image/png;base64,AAAA 1x, https://example.test/x.png 2x\"/>\n```"
            )

    def test_allows_pi_like_literal_text(self) -> None:
        self.assertIn("CDATA", extract_svg("```svg\n<svg><text><![CDATA[<?note hello?>]]></text></svg>\n```"))

    def test_allows_visible_url_labels(self) -> None:
        self.assertIn("example.test", extract_svg("```svg\n<svg><text>https://example.test</text></svg>\n```"))

    def test_rejects_multiple_closed_svg_fences(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one fenced svg block"):
            extract_svg(f"{VALID}\n{VALID}")

    def test_rejects_unclosed_additional_svg_fence(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one fenced svg block"):
            extract_svg(f"{VALID}\n```svg\n<svg/>")


if __name__ == "__main__":
    unittest.main()
