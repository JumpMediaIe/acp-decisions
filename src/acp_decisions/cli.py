"""Command-line entry point for the ACP scraper.

Subcommands:

    acp scrape --case <id>         Scrape one case end-to-end.
    acp scrape --all               Walk all known type listings; scrape new cases.
    acp classify                   Run the LLM classifier over unclassified reasons (stub).

Default DB path: ./acp.db. Override with --db.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from rich.console import Console

from acp_decisions.classifier import GeminiClient, OllamaClient, classify_unclassified
from acp_decisions.db import open_db
from acp_decisions.http_client import PoliteClient
from acp_decisions.orchestrator import scrape_one
from acp_decisions.taxonomy import seed_categories
from acp_decisions.walker import fetch_all_case_ids


# ACP type codes observed in the listings UI as of 2026-05.
# Add more here when ACP exposes new ones.
DEFAULT_TYPE_CODES: tuple[str, ...] = ("LH", "PA", "H")

DEFAULT_DB_PATH = Path("acp.db")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="acp", description="ACP decisions scraper")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite DB path")
    sub = parser.add_subparsers(dest="cmd", required=True)

    scrape = sub.add_parser("scrape", help="Fetch case pages")
    grp = scrape.add_mutually_exclusive_group(required=True)
    grp.add_argument("--case", type=int, metavar="ID", help="Scrape one case by URL ID")
    grp.add_argument("--all", action="store_true", help="Walk every type listing")
    scrape.add_argument(
        "--types",
        type=str,
        default=",".join(DEFAULT_TYPE_CODES),
        help="Comma-separated type codes (default: LH,PA,H)",
    )

    classify = sub.add_parser("classify", help="Classify refusal reasons via LLM")
    classify.add_argument(
        "--provider",
        choices=["ollama", "gemini"],
        default="gemini",
        help="LLM provider (default: gemini)",
    )
    classify.add_argument(
        "--model",
        type=str,
        default=None,
        help="Model name (default: llama3.2:3b for ollama, gemini-2.5-flash for gemini)",
    )
    classify.add_argument(
        "--ollama-url",
        type=str,
        default="http://localhost:11434",
        help="Ollama base URL (default: http://localhost:11434)",
    )

    args = parser.parse_args(argv)
    console = Console()
    conn = open_db(args.db)

    try:
        if args.cmd == "scrape":
            if args.case is not None:
                return _scrape_one(conn, args.case, console)
            type_codes = tuple(t.strip() for t in args.types.split(",") if t.strip())
            return _scrape_all(conn, type_codes, console)
        if args.cmd == "classify":
            return _classify(conn, args.provider, args.model, args.ollama_url, console)
        return 0
    finally:
        conn.close()


def _scrape_one(conn: sqlite3.Connection, case_id: int, console: Console) -> int:
    with PoliteClient() as client:
        decision = scrape_one(client, conn, case_id)
    if decision is None:
        console.print(f"[red]case {case_id}: failed (see scrape_errors)[/]")
        return 1
    console.print(
        f"[green]case {case_id}: {decision.decision_outcome}[/] — "
        f"{len(decision.refusal_reasons)} reason(s)"
    )
    return 0


def _scrape_all(
    conn: sqlite3.Connection,
    type_codes: tuple[str, ...],
    console: Console,
) -> int:
    """Walk every listing, scrape every NEW case (skip ones already in DB)."""
    with PoliteClient() as client:
        console.print(f"[cyan]walking listings: {', '.join(type_codes)}[/]")
        all_ids = fetch_all_case_ids(client, type_codes)
        console.print(f"  discovered {len(all_ids)} case(s)")

        existing = {
            row[0]
            for row in conn.execute("SELECT case_id_url FROM decisions").fetchall()
        }
        new_ids = [cid for cid in all_ids if cid not in existing]
        console.print(
            f"  {len(new_ids)} new, {len(all_ids) - len(new_ids)} already scraped"
        )

        ok, fail = 0, 0
        for cid in new_ids:
            try:
                d = scrape_one(client, conn, cid)
                if d is None:
                    fail += 1
                else:
                    ok += 1
                    if ok % 25 == 0:
                        console.print(f"  progress: {ok}/{len(new_ids)}")
            except Exception as e:  # noqa: BLE001 — keep walking
                console.print(f"[red]case {cid}: {e}[/]")
                fail += 1

    console.print(f"[green]done. ok={ok} fail={fail}[/]")
    return 0 if fail == 0 else 2


def _classify(
    conn: sqlite3.Connection,
    provider: str,
    model: str | None,
    ollama_url: str,
    console: Console,
) -> int:
    """Run the chosen LLM over every unclassified refusal reason."""
    seed_categories(conn)
    if provider == "ollama":
        chosen_model = model or "llama3.2:3b"
        console.print(f"[cyan]ollama: {ollama_url} model={chosen_model}[/]")
        with OllamaClient(base_url=ollama_url, model=chosen_model) as client:
            n = classify_unclassified(client, conn)
    else:  # gemini
        chosen_model = model or "gemini-2.5-flash"
        console.print(f"[cyan]gemini: model={chosen_model}[/]")
        try:
            with GeminiClient(model=chosen_model) as client:
                n = classify_unclassified(client, conn)
        except RuntimeError as e:
            console.print(f"[red]{e}[/]")
            return 1
    console.print(f"[green]classified {n} reason(s)[/]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
