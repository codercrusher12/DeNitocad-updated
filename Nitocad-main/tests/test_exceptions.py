"""
Tests for exceptions.py - the NitocadError hierarchy and its FastAPI
exception handlers, tested against a throwaway FastAPI app rather than
the full web_app (keeps this fast and independent of DB/CadQuery setup).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from exceptions import (
    GenerationError,
    GeometryValidationError,
    NitocadError,
    ParseError,
    UnsupportedPartTypeError,
    register_exception_handlers,
)


@pytest.fixture
def app():
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom/parse")
    async def boom_parse():
        raise ParseError("could not parse this")

    @app.get("/boom/validation")
    async def boom_validation():
        raise GeometryValidationError("bad geometry", details={"errors": ["a", "b"]})

    @app.get("/boom/generation")
    async def boom_generation():
        raise GenerationError("OCCT exploded")

    @app.get("/boom/unexpected")
    async def boom_unexpected():
        raise RuntimeError("something nobody named")

    return app


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=False)


class TestStatusCodes:
    def test_parse_error_is_422(self):
        assert ParseError("x").status_code == 422

    def test_unsupported_part_type_is_422(self):
        assert UnsupportedPartTypeError("x").status_code == 422

    def test_generation_error_is_500(self):
        assert GenerationError("x").status_code == 500

    def test_details_default_to_empty_dict(self):
        assert NitocadError("x").details == {}

    def test_details_are_preserved(self):
        exc = NitocadError("x", details={"foo": "bar"})
        assert exc.details == {"foo": "bar"}


class TestExceptionHandlers:
    def test_parse_error_returns_422_with_error_type(self, client):
        response = client.get("/boom/parse")
        assert response.status_code == 422
        body = response.json()
        assert body["success"] is False
        assert body["error_type"] == "ParseError"
        assert "could not parse" in body["error"]

    def test_validation_error_includes_details(self, client):
        response = client.get("/boom/validation")
        assert response.status_code == 422
        body = response.json()
        assert body["details"]["errors"] == ["a", "b"]

    def test_generation_error_returns_500(self, client):
        response = client.get("/boom/generation")
        assert response.status_code == 500
        assert response.json()["error_type"] == "GenerationError"

    def test_unexpected_exception_does_not_leak_internals(self, client):
        response = client.get("/boom/unexpected")
        assert response.status_code == 500
        body = response.json()
        # The real message ("something nobody named") must not appear in
        # the client-facing response - that's the whole point of the
        # catch-all handler vs. the old bare-Exception-to-string behavior.
        assert "something nobody named" not in response.text
        assert body["error_type"] == "InternalError"
