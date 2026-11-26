"""Ask the corpus as a role (GraphRAG Slice 1).

    python -m graphrag.cli --role Sales "What discount needs VP approval for deals in the EMEA region in 2026?"
    python -m graphrag.cli --role HR --embedder ollama --json "..."

The output is the answer, each claim with its cited chunk IDs (or an
abstention), then the IDs of the chunks retrieved for this role. Chunks the
gate removed are not listed. Pass --audit PATH to append the full audit line,
which includes the filtered-out IDs.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from graphrag.acl import ROLES, requester
from graphrag.answer import answer
from graphrag.chunking import chunk_corpus
from graphrag.corpus import DEFAULT_CORPUS_DIR, load_documents
from graphrag.embedders import EmbedderUnavailable, make_embedder
from graphrag.retrieval import DEFAULT_K, Retriever


def build_retriever(corpus_dir: str | Path = DEFAULT_CORPUS_DIR, embedder: str = "hashing") -> Retriever:
    return Retriever(chunk_corpus(load_documents(corpus_dir)), make_embedder(embedder))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ask the synthetic corpus a question as a role.")
    parser.add_argument("question")
    parser.add_argument("--role", required=True, help=f"who is asking: {', '.join(ROLES)}")
    parser.add_argument("--k", type=int, default=DEFAULT_K, help="chunks to retrieve")
    parser.add_argument("--embedder", choices=["hashing", "ollama"], default="hashing")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_DIR)
    parser.add_argument("--audit", type=Path, help="append the audit line (JSONL) here")
    parser.add_argument("--json", action="store_true", help="print the answer as JSON")
    args = parser.parse_args(argv)
    try:
        who = requester(args.role)
    except ValueError as e:
        parser.error(str(e))
    try:
        retriever = build_retriever(args.corpus, args.embedder)
        result, _ = answer(retriever, args.question, who, args.k, args.audit)
    except EmbedderUnavailable as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(asdict(result), indent=2))
    else:
        print(result.render())
        print(f"\nretrieved for {result.role}: {', '.join(result.retrieved_ids) or '(none)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
