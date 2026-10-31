"""Explicit content relay or search server. Importing this file starts nothing."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
# Source components stay in their existing workspace directories. Installation
# of both local packages is another supported option; no remote code is fetched.
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "components" / "searchforge" / "src")]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("relay", "serve"))
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--source-token-env", required=True)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--initialize", action="store_true", help="Explicitly create a NEW or migrate an owned index")
    parser.add_argument("--consumer-id", default="searchforge-content-v1")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-batches", type=int, default=1)
    parser.add_argument("--port", type=int, default=8813)
    args = parser.parse_args(argv)
    if not 1 <= args.max_batches <= 100 or not 1 <= args.batch_size <= 100 or not 0 <= args.port <= 65535:
        parser.error("bounded batch settings or port are invalid")
    token = os.environ.get(args.source_token_env)
    if not token:
        parser.error("the named source-token environment variable is not set")
    from searchforge.community import CommunityIndex
    from searchforge_content.http_service import ContentSearchHTTPServer
    from searchforge_content.runtime import ProjectionRelay, PublicContentSearch
    from searchforge_content.source import HTTPProjectionSource

    index = None
    try:
        source = HTTPProjectionSource(args.source_url, token)
        index = CommunityIndex(args.index)
        if not args.initialize:
            if not args.index.is_file():
                parser.error("index does not exist; select a new path and pass --initialize")
            connection = sqlite3.connect(args.index.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                owned = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='community_meta'").fetchone()
            finally:
                connection.close()
            if not owned:
                parser.error("existing file is not an initialized content index; --initialize is required for an empty file")
        index.initialize()
        if args.action == "relay":
            relay = ProjectionRelay(source, index, consumer_id=args.consumer_id)
            batches = []
            for _ in range(args.max_batches):
                batch = relay.run_once(limit=args.batch_size)
                batches.append(batch)
                if batch["received"] < args.batch_size:
                    break
            print(json.dumps({"status": "RELAY_COMPLETED", "batches": batches}, ensure_ascii=True))
            return 0
        with ContentSearchHTTPServer(("127.0.0.1", args.port), PublicContentSearch(source, index)) as server:
            print(json.dumps({"status": "LISTENING", "host": "127.0.0.1", "port": server.server_port}, ensure_ascii=True), flush=True)
            try:
                server.serve_forever(poll_interval=0.2)
            except KeyboardInterrupt:
                pass
        return 0
    except Exception:
        print(json.dumps({"status": "FAILED", "message": "Content integration failed; no source acknowledgement is issued for uncommitted index work"}), file=sys.stderr)
        return 1
    finally:
        if index is not None:
            index.close()


if __name__ == "__main__":
    raise SystemExit(main())
