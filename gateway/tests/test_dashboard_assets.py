"""Le dashboard d'incident doit fonctionner sans sortie Internet (SEC-001)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

import main


EXPECTED_CHART_SHA256 = (
    "fed6a739f8d0f0687174de6cd14745fc0fc7809144ab113d22908a26bf0d7fea"
)


def test_dashboard_is_self_contained_and_has_a_tested_csp():
    client = TestClient(main.app)
    response = client.get("/admin/dashboard")

    assert response.status_code == 200
    assert 'src="/admin/assets/chart.umd.js"' in response.text
    assert "cdn.jsdelivr.net" not in response.text
    assert "fonts.googleapis.com" not in response.text
    assert "fonts.gstatic.com" not in response.text

    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "connect-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "https:" not in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_vendored_chart_is_the_reviewed_release():
    client = TestClient(main.app)
    response = client.get("/admin/assets/chart.umd.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "immutable" in response.headers["cache-control"]
    assert hashlib.sha256(response.content).hexdigest() == EXPECTED_CHART_SHA256
    assert b"Chart.js v4.4.4" in response.content


def test_dashboard_source_contains_no_remote_resource_reference():
    html = (Path(__file__).parents[1] / "static" / "dashboard.html").read_text()

    assert "https://" not in html
    assert "http://" not in html
    assert "//cdn." not in html
