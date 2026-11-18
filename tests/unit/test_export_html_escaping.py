"""Stored-XSS regression + interpolation-site property test for the HTML report.

``GET /api/inspect/{job_id}/export/html`` returns the inspection report as
``text/html`` from this service's own origin. Every value in that report is
attacker-controlled: ``text_content`` is the INSPECTED APP's own UI text,
captured verbatim by ``xml_parser`` (``all_text``) and carried through
``screen_navigator`` into ``screens_data``. Before this test existed, every
field was f-string-interpolated with no escaping, so inspecting any app whose
screen text contained ``<script>alert(1)</script>`` shipped a report that
executed it in whoever opened it.

The sibling producer ``export_xml_sitemap`` was never affected (lxml escapes),
and ``maestro_client`` already used ``html.escape`` -- the classic
"guarded beside unguarded" shape. The AST test at the bottom is what stops it
coming back.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import pathlib
import re

import pytest

from src.services.export_service import HTML_REPORT_CSP, ExportService

SCRIPT_PAYLOAD = "<script>alert(1)</script>"
ATTR_PAYLOAD = '"><img onerror=x src=y>'


@pytest.fixture
def export_service() -> ExportService:
    return ExportService()


def _screen_with(payload: str) -> dict:
    """A screen whose every string field carries ``payload``."""
    return {
        "screen_id": f"s1{payload}",
        "screen_type": f"content{payload}",
        "activity_name": f"MainActivity{payload}",
        "text_content": f"Welcome {payload}",
        "interactive_elements": 3,
        "accessibility_score": 90,
        "accessibility_issues": [
            {
                "severity": f"error{payload}",
                "rule": f"missing-label{payload}",
                "message": f"No label {payload}",
            }
        ],
    }


class TestStoredXSSIsEscaped:
    @pytest.mark.parametrize("payload", [SCRIPT_PAYLOAD, ATTR_PAYLOAD])
    def test_payload_never_renders_executable(
        self, export_service: ExportService, payload: str
    ) -> None:
        rendered = export_service.export_html(
            job_id=f"job-1{payload}",
            platform=f"android{payload}",
            screens=[_screen_with(payload)],
            navigation_graph=[
                {
                    "from_screen": f"a{payload}",
                    "to_screen": f"b{payload}",
                    "action": f"tap{payload}",
                }
            ],
            estimated_purpose=f"Shopping {payload}",
            overall_accessibility_score=90,
        )
        # The payload must have REACHED the renderer -- otherwise this test is
        # vacuous. Its escaped form is the proof it was rendered, not dropped.
        escaped = payload.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        escaped = escaped.replace('"', "&quot;").replace("'", "&#x27;")
        assert escaped in rendered, "payload never reached the renderer"

        # And it must NOT appear in executable form.
        assert payload not in rendered
        assert "<script>" not in rendered
        assert "<img" not in rendered
        # `onerror` surviving as TEXT is fine; surviving as a TAG is not.
        assert not re.search(r"<\s*(script|img|svg|iframe)\b", rendered, re.IGNORECASE)

    def test_quote_breakout_in_the_class_attribute_is_blocked(
        self, export_service: ExportService
    ) -> None:
        """``severity`` lands inside ``class="issue-..."`` -- a quoted attribute."""
        rendered = export_service.export_html(
            job_id="job-1",
            platform="android",
            screens=[
                {
                    "screen_id": "s1",
                    "accessibility_issues": [
                        {"severity": '"><img onerror=x>', "rule": "r", "message": "m"}
                    ],
                }
            ],
            navigation_graph=[],
        )
        assert 'class="issue-&quot;&gt;&lt;img onerror=x&gt;"' in rendered
        assert 'class="issue-"><img' not in rendered

    def test_benign_report_still_renders_its_own_markup(
        self, export_service: ExportService
    ) -> None:
        """Escape the VALUES, not the document: the report's markup survives."""
        rendered = export_service.export_html(
            job_id="job-1",
            platform="android",
            screens=[{"screen_id": "s1", "text_content": "Hello"}],
            navigation_graph=[{"from_screen": "a", "to_screen": "b", "action": "tap"}],
            estimated_purpose="Shopping",
        )
        assert rendered.startswith("<!DOCTYPE html>")
        assert '<div class="screen-card">' in rendered
        assert "<h1>App Inspection Report</h1>" in rendered
        assert "Estimated Purpose" in rendered
        assert "&lt;div" not in rendered


class TestContentSecurityPolicy:
    def test_policy_is_restrictive(self) -> None:
        assert "default-src 'none'" in HTML_REPORT_CSP
        assert "frame-ancestors 'none'" in HTML_REPORT_CSP
        assert "base-uri 'none'" in HTML_REPORT_CSP
        assert "form-action 'none'" in HTML_REPORT_CSP
        assert "sandbox" in HTML_REPORT_CSP
        # No escape hatches: either of these re-enables injected script/style.
        assert "unsafe-inline" not in HTML_REPORT_CSP
        assert "unsafe-eval" not in HTML_REPORT_CSP
        # No script-src source is granted at all.
        assert "script-src" not in HTML_REPORT_CSP

    def test_style_hash_matches_the_block_actually_emitted(
        self, export_service: ExportService
    ) -> None:
        """A hash CSP that does not match its own <style> is a broken report."""
        rendered = export_service.export_html(
            job_id="job-1", platform="android", screens=[], navigation_graph=[]
        )
        block = re.search(r"<style>(.*?)</style>", rendered, re.DOTALL)
        assert block is not None
        digest = hashlib.sha256(block.group(1).encode("utf-8")).digest()
        expected = f"'sha256-{base64.b64encode(digest).decode('ascii')}'"
        assert f"style-src {expected};" in HTML_REPORT_CSP


# ---------------------------------------------------------------------------
# Durability: enumerate the interpolation sites by AST.
# ---------------------------------------------------------------------------

_SRC_ROOT = pathlib.Path(__file__).resolve().parents[2] / "src"

#: Names bound to ALREADY-ASSEMBLED markup inside ``export_html``. These are the
#: report's own tags, not data -- escaping them would destroy the document. Any
#: NEW name added here is a deliberate, reviewable decision; the test asserts
#: this set is exactly what the module uses, so a name cannot be slipped in to
#: silence the check without editing this line.
_MARKUP_NAMES = frozenset(
    {
        "issues_html",
        "issues_list_html",
        "nav_html",
        "purpose_html",
        "body_html",
        "style",
    }
)

#: Calls that render an untrusted value safe.
_ESCAPERS = frozenset({"_esc", "escape", "html.escape"})

#: Vacuity floor. Deleting interpolation sites to make this pass fails here.
_MIN_GUARDED_SITES = 15


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _html_producing_fstrings(tree: ast.AST) -> list[ast.JoinedStr]:
    """f-strings that emit markup: a literal part opens an HTML tag.

    AST-based rather than regex-based on purpose: a ``<script>`` mentioned in a
    docstring or a comment is a ``Constant``/nothing, never a ``JoinedStr``, so
    prose can neither trip this check nor silence it.
    """
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr) and any(
            isinstance(p, ast.Constant)
            and isinstance(p.value, str)
            and re.search(r"<\s*/?\s*[a-zA-Z!]", p.value)
            for p in node.values
        ):
            out.append(node)
    return out


def test_every_html_interpolation_site_is_escaped() -> None:
    """A new unescaped interpolation on an HTML-producing path must not ship.

    ``export_html`` f-string-interpolated ``screen_id``, ``screen_type``,
    ``activity_name``, ``text_content``, the issue ``rule``/``message``/
    ``severity`` (the last inside a quoted ``class`` attribute),
    ``estimated_purpose`` and both navigation endpoints with NO escaping --
    stored XSS from the inspected app's own UI text. Pin the PROPERTY, not the
    ~19 call sites: every value interpolated into markup anywhere in ``src/``
    is an escaper call, a constant, or a named pre-assembled markup fragment.
    """
    guarded: list[str] = []
    unguarded: list[str] = []
    markup_names_seen: set[str] = set()

    for path in sorted(_SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        rel = path.relative_to(_SRC_ROOT.parent)
        for fstring in _html_producing_fstrings(tree):
            for part in fstring.values:
                if not isinstance(part, ast.FormattedValue):
                    continue
                value = part.value
                site = f"{rel}:{part.lineno}"
                if isinstance(value, ast.Call) and _call_name(value.func) in _ESCAPERS:
                    guarded.append(site)
                elif isinstance(value, ast.Constant):
                    continue
                elif isinstance(value, ast.Name) and value.id in _MARKUP_NAMES:
                    markup_names_seen.add(value.id)
                else:
                    unguarded.append(f"{site} -> {ast.unparse(value)}")

    assert not unguarded, (
        "unescaped value(s) interpolated into HTML -- wrap each in _esc()/"
        f"html.escape(), or (only if it IS markup) add it to _MARKUP_NAMES: {unguarded}"
    )
    assert len(guarded) >= _MIN_GUARDED_SITES, (
        f"expected >={_MIN_GUARDED_SITES} escaped HTML interpolation sites, "
        f"found {len(guarded)} -- sites were deleted or the module moved: {guarded}"
    )
    # The allowlist must not rot into a place to hide new unescaped values:
    # every declared name must actually be in use, and none may be undeclared.
    assert markup_names_seen == set(_MARKUP_NAMES), (
        f"markup allowlist drifted: used={sorted(markup_names_seen)} "
        f"declared={sorted(_MARKUP_NAMES)}"
    )
