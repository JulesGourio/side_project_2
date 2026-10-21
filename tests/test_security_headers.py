"""Tests for the security headers set on every HTTP response (server/app.py)."""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


def test_health_response_carries_security_headers() -> None:
    """The CSP, nosniff and referrer headers are present and the CSP keeps the strict directives."""
    with (
        patch('server.app.init_lakebase', new_callable=AsyncMock),
        patch('server.app.shutdown_lakebase', new_callable=AsyncMock),
    ):
        from server.app import app
        with TestClient(app) as client:
            response = client.get('/api/health')

    csp = response.headers['Content-Security-Policy']
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'self'" in csp
    assert 'fonts.googleapis.com' not in csp
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert response.headers['Referrer-Policy'] == 'same-origin'
