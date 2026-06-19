import asyncio
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from formr_mcp import google_sheets as gs
from formr_mcp.google_sheets import (
    GoogleSheetError, parse_sheet_url, export_url, fetch_google_sheet,
)

EDIT_URL = "https://docs.google.com/spreadsheets/d/1AbC_dEf-123456789012345678901234567890/edit#gid=42"
SHARE_URL = "https://docs.google.com/spreadsheets/d/1AbC_dEf-123456789012345678901234567890/edit?usp=sharing"
PUB_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vAbCdEf/pubhtml"


class TestParse:
    def test_standard_with_gid(self):
        p = parse_sheet_url(EDIT_URL)
        assert p["kind"] == "standard"
        assert p["id"] == "1AbC_dEf-123456789012345678901234567890"
        assert p["gid"] == "42"

    def test_sharing_without_gid(self):
        p = parse_sheet_url(SHARE_URL)
        assert p["kind"] == "standard" and p["gid"] is None

    def test_published_form(self):
        p = parse_sheet_url(PUB_URL)
        assert p["kind"] == "published" and p["id"] == "2PACX-1vAbCdEf"

    def test_bare_id(self):
        p = parse_sheet_url("1AbC_dEf-123456789012345678901234567890")
        assert p["kind"] == "standard" and p["id"].startswith("1AbC")

    def test_invalid(self):
        with pytest.raises(GoogleSheetError, match="Could not find"):
            parse_sheet_url("https://example.com/not-a-sheet")


class TestExportUrl:
    def test_csv_includes_gid(self):
        u = export_url(parse_sheet_url(EDIT_URL), "csv")
        assert u.endswith("/export?format=csv&gid=42")

    def test_xlsx_drops_gid(self):
        u = export_url(parse_sheet_url(EDIT_URL), "xlsx")
        assert u.endswith("/export?format=xlsx")
        assert "gid" not in u

    def test_published_csv(self):
        u = export_url(parse_sheet_url(PUB_URL), "csv")
        assert u == "https://docs.google.com/spreadsheets/d/e/2PACX-1vAbCdEf/pub?output=csv"


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestFetch:
    def test_csv_returns_text_and_hits_export_url(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, headers={"content-type": "text/csv"}, content=b"a,b\n1,2\n")

        http = _client(handler)
        out = asyncio.run(fetch_google_sheet(EDIT_URL, "csv", http_client=http))
        assert out == "a,b\n1,2\n"
        assert "format=csv" in seen["url"] and "gid=42" in seen["url"]
        asyncio.run(http.aclose())

    def test_csv_to_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gs.utils, "WORKSPACE_DIR", tmp_path / "ws")

        def handler(request):
            return httpx.Response(200, headers={"content-type": "text/csv"}, content=b"x\n1\n")

        http = _client(handler)
        out = asyncio.run(fetch_google_sheet(EDIT_URL, "csv", to_file=True, http_client=http))
        f = tmp_path / "ws" / "sheets" / "1AbC_dEf-123456789012345678901234567890_42.csv"
        assert f.exists() and f.read_bytes() == b"x\n1\n"
        assert str(f) in out
        asyncio.run(http.aclose())

    def test_xlsx_always_written_to_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gs.utils, "WORKSPACE_DIR", tmp_path / "ws")

        def handler(request):
            ct = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            return httpx.Response(200, headers={"content-type": ct}, content=b"PK\x03\x04binary")

        http = _client(handler)
        out = asyncio.run(fetch_google_sheet(EDIT_URL, "xlsx", to_file=False, http_client=http))
        f = tmp_path / "ws" / "sheets" / "1AbC_dEf-123456789012345678901234567890.xlsx"
        assert f.exists()
        assert "binary" in out.lower() and str(f) in out
        asyncio.run(http.aclose())

    def test_html_response_is_not_shared_error(self):
        def handler(request):
            return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                                  content=b"<html>Sign in</html>")

        http = _client(handler)
        with pytest.raises(GoogleSheetError, match="link-accessible"):
            asyncio.run(fetch_google_sheet(EDIT_URL, "csv", http_client=http))
        asyncio.run(http.aclose())

    def test_404(self):
        def handler(request):
            return httpx.Response(404, content=b"")

        http = _client(handler)
        with pytest.raises(GoogleSheetError, match="not found"):
            asyncio.run(fetch_google_sheet(EDIT_URL, "csv", http_client=http))
        asyncio.run(http.aclose())

    def test_bad_format(self):
        with pytest.raises(GoogleSheetError, match="format must be"):
            asyncio.run(fetch_google_sheet(EDIT_URL, "pdf"))
