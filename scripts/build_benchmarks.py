"""Build the recorded material of the improvement loop's replay benchmarks (benchmarks/). It
reads real /deep-research runs and BharatCode Desktop's own record, and calls the paper
registries once, so it needs a machine with Desktop and the internet. The tests then replay
the files it writes, never the network.

    python scripts/build_benchmarks.py refs <research folder> [<research folder> ...]
        For every reference in the folders' sources.json files: the registries' answer
        (recorded), and a label: a person's from benchmarks/verify-refs/labels.json when the
        reference is listed there, else the verdict verify_refs gives today. Review the
        references it lists as "check these", label them in labels.json, and run it again.
    python scripts/build_benchmarks.py notes <name>=<lead session id> [...]
        The notes files the scouts of each /deep-research run wrote (their first write), into
        benchmarks/collect-sources/runs/<name>/notes/. Then write expected.json by hand: run
        `notes --draft` for a draft grouping to review.
    python scripts/build_benchmarks.py starts [--days 30]
        Every reply's model, start and seconds to its first output, into benchmarks/router/starts.json.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from gpu_token_utilization import refs, scholar, stats  # noqa: E402
from gpu_token_utilization.scholar import ScholarError  # noqa: E402

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
PAGE_CHARS = 6000
REF_FIELDS = ("key", "title", "authors", "author", "year", "doi", "arxiv", "openalex", "url")


def _page(ref: refs.Reference, http) -> dict:
    """What check_url would see: the status, and the start of the page's text without tags."""
    try:
        response = http.get(ref.url, headers={"Accept": "text/html,*/*"})
    except Exception as exc:  # noqa: BLE001 - recorded as the error a replay raises
        return {"error": type(exc).__name__}
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", response.text[:refs.PAGE_BYTES]))
    title_words = [w for w in scholar.normalize_title(ref.title or "").split() if len(w) > 3]
    # keep the part of the page that carries the title's words, so the replay sees what the real check saw
    keep = " ".join(w for w in scholar.normalize_title(text).split() if w in set(title_words))
    return {"status": response.status_code, "text": (text[:PAGE_CHARS] + " " + keep)[:PAGE_CHARS * 2]}


def record(ref: refs.Reference, http) -> dict:
    if not (ref.doi or ref.arxiv or ref.openalex or ref.url):
        return {}
    for attempt in range(4):
        try:
            paper = refs.resolve(ref, http)
            break
        except ScholarError as exc:
            if "429" in str(exc) or "slow down" in str(exc).lower() or "couldn't be reached" in str(exc):
                time.sleep(10 * (attempt + 1))
                continue
            return {"error": str(exc)}
    else:
        return {"error": "the registry asked us to slow down (HTTP 429) every time"}
    if paper is not None:
        return {"paper": {k: v for k, v in paper.to_dict().items() if v not in (None, "", [])}}
    if ref.url and not (ref.doi or ref.arxiv or ref.openalex):
        return {"page": _page(ref, http)}
    return {}


def _transient(recorded: dict) -> bool:
    """A recording of the network failing (a timeout, a rate limit), not of the source: record it again."""
    error = str(recorded.get("error") or "")
    return "couldn't be reached" in error or "slow down" in error or "429" in error


def build_refs(folders: list[Path]) -> None:
    out = BENCH / "verify-refs"
    out.mkdir(parents=True, exist_ok=True)
    labels_path = out / "labels.json"
    labels = json.loads(labels_path.read_text(encoding="utf-8")) if labels_path.is_file() else {}
    existing_path = out / "references.json"
    existing = {e["id"]: e for e in json.loads(existing_path.read_text(encoding="utf-8"))} if existing_path.is_file() else {}
    entries, seen = [], set()
    with scholar.client() as http:
        for folder in folders:
            run = Path(folder).name
            for item in refs.load_sources(Path(folder) / "sources.json"):
                ref_doc = {k: item[k] for k in REF_FIELDS if item.get(k) not in (None, "", [])}
                sig = json.dumps(ref_doc, sort_keys=True)
                if sig in seen:
                    continue
                seen.add(sig)
                rid = f"{run}/{item['key']}"
                again = rid not in existing or _transient(existing[rid]["recorded"])
                recorded = record(refs.Reference.from_dict(ref_doc, 1), http) if again else existing[rid]["recorded"]
                entries.append({"id": rid, "ref": ref_doc, "recorded": recorded})
                time.sleep(3.0 if again else 0.0)
    unsure = []
    for entry in entries:
        entry["ref"]["key"] = entry["id"]
        from gpu_token_utilization import bench
        verdict = bench.replay_verify(entry)
        if entry["id"] in labels:
            entry["label"], entry["labelled_by"] = labels[entry["id"]], "person"
        else:
            entry["label"], entry["labelled_by"] = verdict.status, "verify_refs today"
            if verdict.status != "keep" or (verdict.title_match is not None and verdict.title_match < 0.97) or verdict.corrected:
                unsure.append(f"{entry['id']}: {verdict.status} ({verdict.reason})")
    existing_path.write_text(json.dumps(entries, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(entries)} reference(s) written to {existing_path}")
    if unsure:
        print("Check these and label them in labels.json (keep, drop or unchecked):")
        print("\n".join(f"  {u}" for u in unsure))


CHANNEL: str | None = None  # --channel; else the channel whose database changed last


def _db() -> Path:
    from gpu_token_utilization.bc_config import CHANNELS
    found = [p for p in (stats.session_db(which=c) for c in ([CHANNEL] if CHANNEL else list(CHANNELS))) if p]
    if not found:
        raise SystemExit("BharatCode Desktop's database wasn't found")
    return max(found, key=lambda p: p.stat().st_mtime)


def build_notes(pairs: list[str], draft: bool) -> None:
    conn = sqlite3.connect(f"{_db().resolve().as_uri()}?mode=ro", uri=True)
    for pair in pairs:
        name, lead = pair.split("=", 1)
        target = BENCH / "collect-sources" / "runs" / name / "notes"
        target.mkdir(parents=True, exist_ok=True)
        kids = [r[0] for r in conn.execute("select id from session where parent_id = ? order by time_created", (lead,))]
        written = 0
        for kid in kids:
            for (data,) in conn.execute("select data from part where session_id = ? order by time_created", (kid,)):
                part = json.loads(data)
                state = part.get("state") or {}
                path = str((state.get("input") or {}).get("filePath") or "")
                if part.get("tool") == "write" and re.search(r"[\\/]notes[\\/][^\\/]+\.md$", path):
                    dest = target / Path(path.replace("\\", "/")).name
                    if not dest.exists():
                        dest.write_text(state["input"]["content"], encoding="utf-8")
                        written += 1
        print(f"{name}: {written} notes file(s) in {target}")
    conn.close()
    if draft:
        import shutil
        import tempfile
        expected = {}
        for run in sorted((BENCH / "collect-sources" / "runs").iterdir()):
            with tempfile.TemporaryDirectory() as tmp:
                shutil.copytree(run / "notes", Path(tmp) / "notes")
                path, _ = refs.collect_sources(Path(tmp))
                groups = [[i["key"], *i.get("aliases", [])] for i in refs.load_sources(path)]
            entries = sum(len(refs._note_sources((run / "notes" / f).read_text(encoding="utf-8"))[0])
                          for f in [p.name for p in (run / "notes").glob("*.md")])
            expected[run.name] = {"entries": entries, "groups": groups}
        draft_path = BENCH / "collect-sources" / "expected.draft.json"
        draft_path.write_text(json.dumps(expected, indent=1) + "\n", encoding="utf-8")
        print(f"Draft grouping in {draft_path}: review it, then save it as expected.json")


def build_starts(days: float) -> None:
    since = datetime.now(timezone.utc).timestamp() - days * 86400
    replies = stats.reply_starts(since, _db())
    print(f"from {_db()}")
    out = BENCH / "router" / "starts.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"exported": datetime.now(timezone.utc).isoformat(timespec="seconds"), "days": days,
                               "replies": [[m, round(c, 3), round(s, 3)] for m, c, s in replies]}) + "\n",
                   encoding="utf-8")
    print(f"{len(replies)} replies written to {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="what", required=True)
    r = sub.add_parser("refs")
    r.add_argument("folders", nargs="+", type=Path)
    n = sub.add_parser("notes")
    n.add_argument("pairs", nargs="*")
    n.add_argument("--draft", action="store_true")
    s = sub.add_parser("starts")
    s.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--channel", choices=["stable", "beta", "dev", "local"], help="Desktop channel to read")
    args = ap.parse_args()
    global CHANNEL
    CHANNEL = args.channel
    if args.what == "refs":
        build_refs(args.folders)
    elif args.what == "notes":
        build_notes(args.pairs, args.draft)
    else:
        build_starts(args.days)


if __name__ == "__main__":
    main()
