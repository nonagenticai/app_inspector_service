"""Export inspection results to HTML and XML formats."""

from __future__ import annotations

import base64
import hashlib
import html
import logging

from lxml import etree

logger = logging.getLogger(__name__)


# "Not measured" is a first-class outcome, never a number. A screen that could
# not be analysed (blank/malformed page source, zero scoreable elements) and an
# inspection that discovered zero screens both land here — printing "100/100"
# for either shipped a perfect audit for a total failure.
_NOT_MEASURED = "Not measured"


def _esc(value: object) -> str:
    """HTML-escape one untrusted value for interpolation into the report.

    EVERY value interpolated into the HTML report is attacker-controlled: the
    inspected app's own UI text, activity names and screen ids all arrive
    verbatim from ``xml_parser`` and land in ``screens_data``. The report is
    then served as ``text/html`` from this service's own origin, so an
    unescaped interpolation is stored XSS against whoever opens the report.

    ``quote=True`` (the default, pinned explicitly here) escapes ``"`` and
    ``'`` as well as ``&<>``, which is what makes a value safe inside a
    *quoted* HTML attribute — see the ``class="issue-..."`` site below. Every
    attribute in the template is double-quoted; do not add an unquoted one.

    Escape the VALUES, never the assembled document: escaping after assembly
    would destroy the report's own markup.
    """
    return html.escape(str(value), quote=True)


def _render_score(score: int | None) -> str:
    """Render an accessibility score for the customer-facing report."""
    return f"{score}/100" if isinstance(score, int) else _NOT_MEASURED


# The report's stylesheet. Kept as a plain (non-f) string constant so its bytes
# are stable and hashable: the CSP below whitelists this exact inline <style>
# block by sha256 instead of opening the document up with 'unsafe-inline'.
# Editing this constant automatically moves the hash — there is nothing to keep
# in sync by hand.
_REPORT_STYLE = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; padding: 20px; background: #f5f5f5; color: #333; }
.container { max-width: 1200px; margin: 0 auto; }
h1 { color: #1a1a1a; border-bottom: 2px solid #007bff; padding-bottom: 10px; }
h2 { color: #333; margin-top: 30px; }
.summary { background: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
.screen-card { background: white; padding: 15px; border-radius: 8px; margin-bottom: 15px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
.screen-card h3 { margin-top: 0; color: #007bff; }
table { width: 100%; border-collapse: collapse; margin: 10px 0; }
td { padding: 5px 10px; border-bottom: 1px solid #eee; }
td:first-child { font-weight: bold; width: 200px; }
.text-preview { color: #666; font-size: 0.9em; }
.issues { list-style: none; padding: 0; }
.issue-error { color: #dc3545; }
.issue-warning { color: #ffc107; }
.issue-info { color: #17a2b8; }
"""


def _style_hash() -> str:
    """CSP source expression for the report's one inline <style> block."""
    digest = hashlib.sha256(_REPORT_STYLE.encode("utf-8")).digest()
    return f"'sha256-{base64.b64encode(digest).decode('ascii')}'"


# Defence in depth for the customer-facing report. Escaping is the fix; this is
# the second wall, because the report is served from this service's own origin
# and a single missed interpolation would otherwise be same-origin script
# execution.
#
#   default-src 'none'   the report loads nothing: no scripts, no fonts, no
#                        XHR, no frames. A static document needs no origins.
#   style-src <sha256>   the one inline <style> block, whitelisted by hash.
#                        Deliberately NOT 'unsafe-inline' -- that would also
#                        re-enable injected inline styles.
#   img-src 'none'       the report embeds no images; an injected <img src=x
#                        onerror=...> cannot even fire its loader.
#   base-uri / form-action / frame-ancestors 'none'
#                        no <base> hijack, no exfil form, no clickjacking.
#   sandbox              the document runs in an opaque origin with scripts,
#                        forms, popups and top-level navigation all denied.
#                        Static HTML+CSS needs none of them.
HTML_REPORT_CSP = (
    "default-src 'none'; "
    f"style-src {_style_hash()}; "
    "img-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'; "
    "sandbox"
)


class ExportService:
    """Exports inspection results in various formats."""

    def export_html(
        self,
        *,
        job_id: str,
        platform: str | None,
        screens: list[dict],
        navigation_graph: list[dict],
        estimated_purpose: str | None = None,
        overall_accessibility_score: int | None = None,
    ) -> str:
        """Export inspection result as a standalone HTML report.

        Every interpolated value passes through :func:`_esc`. The only
        unescaped interpolations are the pre-assembled markup fragments built
        in this method (``body_html``, ``issues_html``, ``issues_list_html``,
        ``nav_html``, ``purpose_html``, ``style``) -- those ARE the markup, and
        ``tests/unit/test_export_html_escaping.py`` enumerates the
        interpolation sites by AST to keep it that way.
        """
        screens_html = []
        for s in screens:
            screen_id = s.get("screen_id", "unknown")
            screen_type = s.get("screen_type", "unknown")
            activity = s.get("activity_name", "N/A")
            acc_score = _render_score(s.get("accessibility_score"))
            interactive_count = s.get("interactive_elements", 0)
            text_content = str(s.get("text_content") or "")[:200]

            issues_html = ""
            for issue in s.get("accessibility_issues", []):
                severity = str(issue.get("severity", "info"))
                rule = issue.get("rule", "")
                msg = issue.get("message", "")
                # `severity` lands in a QUOTED class attribute as well as in
                # text, so it needs the quote-escaping _esc() does.
                issues_html += (
                    f'<li class="issue-{_esc(severity)}">'
                    f"[{_esc(severity.upper())}] {_esc(rule)}: {_esc(msg)}</li>"
                )

            issues_list_html = f'<ul class="issues">{issues_html}</ul>' if issues_html else ""

            screens_html.append(f"""
            <div class="screen-card">
                <h3>{_esc(screen_id)}</h3>
                <table>
                    <tr><td>Type</td><td>{_esc(screen_type)}</td></tr>
                    <tr><td>Activity</td><td>{_esc(activity)}</td></tr>
                    <tr><td>Accessibility Score</td><td>{_esc(acc_score)}</td></tr>
                    <tr><td>Interactive Elements</td><td>{_esc(interactive_count)}</td></tr>
                </table>
                <p class="text-preview">{_esc(text_content)}</p>
                {issues_list_html}
            </div>""")

        nav_html = ""
        for edge in navigation_graph:
            nav_html += (
                f"<li>{_esc(edge.get('from_screen', ''))} "
                f"&rarr; {_esc(edge.get('to_screen', ''))} "
                f"({_esc(edge.get('action', 'tap'))})</li>"
            )

        purpose_html = (
            f"<p><strong>Estimated Purpose:</strong> {_esc(estimated_purpose)}</p>"
            if estimated_purpose
            else ""
        )
        style = _REPORT_STYLE
        body_html = "".join(screens_html)

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Inspection Report - {_esc(job_id)}</title>
<style>{style}</style>
</head>
<body>
<div class="container">
<h1>App Inspection Report</h1>
<div class="summary">
<p><strong>Job ID:</strong> {_esc(job_id)}</p>
<p><strong>Platform:</strong> {_esc(platform or "N/A")}</p>
<p><strong>Screens Discovered:</strong> {_esc(len(screens))}</p>
<p><strong>Overall Accessibility Score:</strong> {_esc(_render_score(overall_accessibility_score))}</p>
{purpose_html}
</div>
<h2>Screens</h2>
{body_html}
<h2>Navigation Graph</h2>
<ul>{nav_html}</ul>
</div>
</body>
</html>"""

    def export_xml_sitemap(
        self,
        *,
        job_id: str,
        platform: str | None,
        screens: list[dict],
        navigation_graph: list[dict],
    ) -> str:
        """Export inspection result as an XML app structure map."""
        root = etree.Element("app-structure", job_id=job_id)
        if platform:
            root.set("platform", platform)

        screens_elem = etree.SubElement(root, "screens", count=str(len(screens)))
        for s in screens:
            screen_elem = etree.SubElement(
                screens_elem,
                "screen",
                id=s.get("screen_id", ""),
                type=s.get("screen_type", ""),
            )
            if s.get("activity_name"):
                screen_elem.set("activity", s["activity_name"])
            acc = s.get("accessibility_score")
            # Omit rather than fabricate: an absent attribute is honestly
            # "not measured"; "100" would be a false clean bill of health.
            if isinstance(acc, int):
                screen_elem.set("accessibility-score", str(acc))
            screen_elem.set("interactive-elements", str(s.get("interactive_elements", 0)))

        nav_elem = etree.SubElement(root, "navigation", edges=str(len(navigation_graph)))
        for edge in navigation_graph:
            etree.SubElement(
                nav_elem,
                "edge",
                attrib={k: str(v) for k, v in edge.items() if v is not None},
            )

        return etree.tostring(
            root, pretty_print=True, xml_declaration=True, encoding="UTF-8"
        ).decode("utf-8")
