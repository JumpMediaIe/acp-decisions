"""CLI smoke tests — argparse wiring and exit codes."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from acp_decisions.cli import main


def test_cli_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "ACP decisions scraper" in capsys.readouterr().out


def test_cli_classify_default_uses_ollama(tmp_path: Path) -> None:
    """Default provider is ollama; classify_unclassified gets called."""
    db = tmp_path / "test.db"
    with patch("acp_decisions.cli.classify_unclassified", return_value=0) as mock:
        rc = main(["--db", str(db), "classify"])
    assert rc == 0
    assert mock.call_count == 1
    assert db.exists()


def test_cli_classify_gemini_requires_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--provider gemini` without GEMINI_API_KEY exits with rc=1."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    db = tmp_path / "test.db"
    rc = main(["--db", str(db), "classify", "--provider", "gemini"])
    assert rc == 1


def test_cli_scrape_requires_case_or_all(capsys: pytest.CaptureFixture[str]) -> None:
    """`acp scrape` with no flags should fail; --case and --all are mutually exclusive
    but at least one is required."""
    with pytest.raises(SystemExit):
        main(["scrape"])


def test_cli_scrape_case_calls_orchestrator(tmp_path: Path) -> None:
    """`acp scrape --case 1` should call scrape_one with that ID."""
    db = tmp_path / "test.db"
    with patch("acp_decisions.cli.scrape_one", return_value=None) as mock:
        rc = main(["--db", str(db), "scrape", "--case", "1234"])
    assert mock.call_count == 1
    # case_id is the third positional arg
    assert mock.call_args.args[2] == 1234
    # Returns 1 when scrape_one returns None
    assert rc == 1
