"""CLI entry point. Real implementation lands in Task 11 onwards."""
import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="acp", description="ACP decisions scraper")
    parser.add_argument("--version", action="version", version="acp-decisions 0.1.0")
    parser.add_argument("subcommand", nargs="?", default=None,
                        help="scrape | classify (full implementation in later tasks)")
    args = parser.parse_args()
    if args.subcommand is None:
        parser.print_help()
        sys.exit(0)
    print(f"[stub] would run: {args.subcommand}")
