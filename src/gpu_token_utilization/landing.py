"""What a finished job leaves in spare-gpu/<date>_<time>/ besides its files, and `gtu apply` and
`gtu undo`, the two commands that move those files into the project and back out.

Next to the exported files:
- RUN.md: a short human summary of the job (what ran, on which model, with which tools, which
  files came out and why, what the checks reverted), from the job's ledger (ledger.py);
- manifest.json: every file the job changed, with its change type and two hashes: the file as it
  was when the job started and as the job left it. `gtu apply` uses the first to tell a file that
  moved on since the job from one it may safely replace, and the second to see what is already
  as the job left it;
- .undo/: made by `gtu apply`, holding what it replaced, so `gtu undo` can put it all back.

Nothing here writes into the project unless the user asks with one of the two commands, and
even then only inside the project: a manifest path that is absolute or climbs out with ".." is
refused whole.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import ledger, waits, worktree

RUN_FILE = "RUN.md"
MANIFEST = "manifest.json"
UNDO_DIR = ".undo"
RECORDS = (RUN_FILE, MANIFEST)  # spare-gpu's own files in a result folder, never a job's output
CHANGE_WORDS = {"A": "added", "M": "changed", "D": "deleted"}
REMOVE_RETRY_DELAY = 0.5  # seconds: antivirus or an editor can hold a file for a moment
MAX_OUTCOME_LINES = 20  # of a step's outcome text shown in RUN.md
MAX_TOOLS_SHOWN = 8  # per step in RUN.md's table
# Why a file came out of a job, when its verify step doesn't say.
DEFAULT_REASON = {
    "tests": "a new test file whose tests passed",
    "docs": "documentation changes that passed the check",
    "review": "the review report",
    "fix": "part of the fix; the test command passed with it",
}


class LandingError(Exception):
    """Something `gtu apply` or `gtu undo` refuses or can't do; the message is for the user."""


# --- hashes and the result folder's files -------------------------------------------------------

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str | None:
    """The file's hash, or None if there is no such file."""
    path = Path(path)
    if not path.is_file():
        return None
    return sha256(path.read_bytes())


def exported_files(folder: Path) -> list[str]:
    """The job's own files in a result folder, relative with "/" separators: everything but
    RUN.md, manifest.json and the .undo folder."""
    folder = Path(folder)
    names = []
    for item in folder.rglob("*"):
        if not item.is_file():
            continue
        rel = item.relative_to(folder)
        if rel.parts[0] == UNDO_DIR or (len(rel.parts) == 1 and rel.name in RECORDS):
            continue
        names.append(rel.as_posix())
    return sorted(names)


# --- written at export ----------------------------------------------------------------------------

def manifest_files(folder: Path, copy: Path, base: str | None) -> list[dict]:
    """One manifest entry per file the job changed: its path, change type, the hash of the file
    as it was when the job started (None for a new file) and as the job left it (None for a
    deletion). Reads the base version from the job's copy, so call it before the copy is removed."""
    out = []
    for code, name in worktree.changes(copy, base):
        before = None if code == "A" else worktree.base_bytes(copy, base, name)
        after = None if code == "D" else (Path(folder) / name).read_bytes()
        out.append({
            "path": PurePosixPath(name).as_posix(),
            "change": code,
            "base_sha256": None if before is None else sha256(before),
            "new_sha256": None if after is None else sha256(after),
        })
    return out


def write_records(folder: Path, copy: Path, job, *, base: str | None, ledger_doc: dict, steps: list[dict],
                  tokens: int, exported_at: datetime) -> dict:
    """Write manifest.json and RUN.md into the result folder. `job` needs id, kind, target and
    model; `steps` are the store's step rows (name, status, output). Returns the manifest."""
    folder = Path(folder)
    model = job.model or ledger.last_model(ledger_doc)
    manifest = {
        "job": job.id,
        "kind": job.kind,
        "target": job.target,
        "model": waits.model_id(model) if model else None,
        "tokens": int(tokens or 0),
        "exported_at": ledger.iso(exported_at),
        "files": manifest_files(folder, copy, base),
    }
    (folder / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    (folder / RUN_FILE).write_text(run_markdown(manifest, ledger_doc, steps, folder.name), encoding="utf-8", newline="\n")
    return manifest


KEPT = re.compile(r"^- kept (.+?)(?::\s*(.*))?$", re.M)
REVERTED = re.compile(r"^- (?:reverted|discarded)(?: the fix)? ?(.*?)(?::\s*(.*))?$", re.M)


def _reasons(steps: list[dict]) -> tuple[dict[str, str], list[str]]:
    """From the verify steps' notes: why each kept file was kept, and what was reverted and why."""
    kept: dict[str, str] = {}
    reverted: list[str] = []
    for step in steps:
        output = step.get("output") or ""
        for path, why in KEPT.findall(output):
            kept[path.strip()] = (why or "").strip()
        for what, why in REVERTED.findall(output):
            what, why = what.strip(), (why or "").strip()
            if what or why:
                reverted.append(f"`{what}`: {why}" if what and why else what or why)
    return kept, reverted


def _clock(stamp: str | None) -> str:
    if not stamp:
        return "-"
    try:
        return datetime.fromisoformat(stamp).astimezone(timezone.utc).strftime("%H:%M:%S")
    except ValueError:
        return stamp


def _tools_cell(tools: list[str]) -> str:
    if not tools:
        return "none"
    shown = [t.replace("|", "\\|") for t in tools[:MAX_TOOLS_SHOWN]]
    if len(tools) > MAX_TOOLS_SHOWN:
        shown.append(f"and {len(tools) - MAX_TOOLS_SHOWN} more")
    return "; ".join(shown)


def _model_label(model: str | None) -> str:
    if not model:
        return "not recorded"
    name = waits.name(model)
    return f"{name} (`{waits.model_id(model)}`)" if name != waits.model_id(model) else f"`{waits.model_id(model)}`"


def run_markdown(manifest: dict, ledger_doc: dict, steps: list[dict], stamp: str) -> str:
    """RUN.md: the job in one page. Deterministic for the same inputs."""
    job_id, kind, target = manifest["job"], manifest["kind"], manifest["target"]
    files = manifest["files"]
    counts = {code: sum(1 for f in files if f["change"] == code) for code in ("A", "M", "D")}
    when = manifest.get("exported_at")
    shown_when = datetime.fromisoformat(when).strftime("%Y-%m-%d %H:%M:%S UTC") if when else "-"
    lines = [
        f"# spare-gpu job {job_id}: {kind} `{target}`",
        "",
        f"- Kind: {kind}",
        f"- Target: `{target}`",
        f"- Model: {_model_label(manifest.get('model'))}",
        f"- Tokens: {manifest.get('tokens', 0):,}",
        f"- Exported: {shown_when}",
        f"- Files: {counts['A']} added, {counts['M']} changed, {counts['D']} deleted",
        "",
        "## Steps",
        "",
    ]
    entries = ledger_doc.get("steps", [])
    if entries:
        lines += ["| Step | Status | Model | Started (UTC) | Finished (UTC) | Tokens | Tools |",
                  "|---|---|---|---|---|---|---|"]
        for e in entries:
            lines.append(f"| {e.get('step', '-')} | {e.get('status', '-')} | {waits.model_id(e.get('model')) or '-'} "
                         f"| {_clock(e.get('started'))} | {_clock(e.get('finished'))} | {int(e.get('tokens') or 0):,} "
                         f"| {_tools_cell(e.get('tools') or [])} |")
        for e in entries:
            outcome = (e.get("outcome") or "").strip()
            if not outcome:
                continue
            shown = outcome.splitlines()
            cut = len(shown) > MAX_OUTCOME_LINES
            lines += ["", f"### {e.get('step', 'step')}", ""]
            lines += [f"    {line}" for line in shown[:MAX_OUTCOME_LINES]]
            if cut:
                lines.append(f"    ... ({len(shown) - MAX_OUTCOME_LINES} more lines in .gtu/jobs/{job_id}/ledger.json)")
    else:
        lines.append("No step record was found for this job (its ledger is missing).")
    kept, reverted = _reasons(steps)
    lines += ["", "## Files", ""]
    if files:
        for f in files:
            why = kept.get(f["path"]) or DEFAULT_REASON.get(kind, "")
            if f["change"] == "D":
                why = "the job deleted it" + (f"; {why}" if kept.get(f["path"]) else "")
            lines.append(f"- `{f['path']}` ({CHANGE_WORDS[f['change']]})" + (f": {why}" if why else ""))
    else:
        lines.append("None.")
    lines += ["", "## Reverted by the checks", ""]
    lines += [f"- {r}" for r in reverted] or ["Nothing was reverted."]
    lines += [
        "",
        "## Using it",
        "",
        f"`gtu apply {stamp}` copies these files into your project; a file that changed since the job started "
        f"is skipped unless you add `--force`. `gtu undo {stamp}` takes that back. Or copy what you want by hand. "
        "The job itself never touched your files.",
        "",
    ]
    return "\n".join(lines)


# --- finding a result folder ------------------------------------------------------------------------

def find_root(cwd: Path) -> Path:
    """The project `gtu apply` works in: the project containing `cwd` (the top of its git repo,
    else the folder itself); when that has no spare-gpu/ folder, the nearest parent that has
    one, so the commands work from any subfolder of a project that isn't a git repo too."""
    cwd = Path(cwd).resolve()
    root = worktree.project_root(cwd)
    if root != cwd or (root / worktree.OUTPUT_DIR).is_dir() or worktree._is_repo_top(root):
        return root  # inside a git repo (its top is the project), or a plain project folder itself
    for parent in cwd.parents:
        if (parent / worktree.OUTPUT_DIR).is_dir() or (parent / ".gtu").is_dir():
            return parent
    return root


def result_folders(root: Path) -> list[Path]:
    """Every spare-gpu/<date>_<time>/ folder, oldest first (their names sort by time)."""
    out = Path(root) / worktree.OUTPUT_DIR
    if not out.is_dir():
        return []
    return sorted((p for p in out.iterdir() if p.is_dir() and not p.name.startswith(".")), key=lambda p: p.name)


def read_manifest(folder: Path) -> dict:
    file = Path(folder) / MANIFEST
    try:
        doc = json.loads(file.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise LandingError(f"{folder} has no {MANIFEST}: it was made by an older spare-gpu, or isn't a job's "
                           "folder. Copy its files by hand.") from None
    except ValueError as exc:
        raise LandingError(f"{file} doesn't parse ({exc}); copy the folder's files by hand.") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("files"), list):
        raise LandingError(f"{file} isn't a spare-gpu manifest; copy the folder's files by hand.")
    return doc


def _stamp_name(stamp: str | None) -> str | None:
    """'latest' (or nothing) means the newest; otherwise the folder's name, however it was typed
    (with or without spare-gpu/ in front, a trailing slash, or as a path)."""
    text = (stamp or "").strip().replace("\\", "/").rstrip("/")
    if not text or text.lower() == "latest":
        return None
    return text.rsplit("/", 1)[-1]


def find(root: Path, stamp: str | None, *, for_undo: bool = False) -> Path:
    """The result folder meant: by name, or the newest one with a manifest (for undo: the newest
    one that was applied)."""
    folders = result_folders(root)
    name = _stamp_name(stamp)
    if name is None:
        wanted = [f for f in folders if (f / UNDO_DIR / MANIFEST).is_file()] if for_undo \
            else [f for f in folders if (f / MANIFEST).is_file()]
        if not wanted:
            if for_undo:
                raise LandingError(f"nothing has been applied from {worktree.OUTPUT_DIR}/ in {root}; nothing to undo.")
            raise LandingError(f"no job result with a {MANIFEST} in {root / worktree.OUTPUT_DIR}; nothing to apply.")
        return wanted[-1]
    folder = root / worktree.OUTPUT_DIR / name
    if not folder.is_dir():
        known = [f.name for f in folders][-5:]
        hint = f" The newest are: {', '.join(known)}." if known else ""
        raise LandingError(f"no folder {worktree.OUTPUT_DIR}/{name} in {root}.{hint}")
    return folder


# --- keeping every write inside the project ------------------------------------------------------

def check_path(name) -> PurePosixPath:
    """A manifest path as a relative posix path inside the project. Refuses an absolute path (on
    either system: a leading slash, a drive letter, a UNC name), a path that climbs out with "..",
    and anything with a ":" in it (a drive, or an NTFS stream)."""
    if not isinstance(name, str) or not name.strip():
        raise LandingError(f"the manifest names an empty path ({name!r}); nothing was changed.")
    text = name.replace("\\", "/")
    if text.startswith("/") or PureWindowsPath(name).is_absolute() or PureWindowsPath(name).drive or ":" in text:
        raise LandingError(f"the manifest names a path outside the project ({name}); nothing was changed.")
    parts = [p for p in PurePosixPath(text).parts if p not in ("", ".")]
    if not parts or ".." in parts:
        raise LandingError(f"the manifest names a path outside the project ({name}); nothing was changed.")
    return PurePosixPath(*parts)


def _inside(root: Path, rel: PurePosixPath) -> Path:
    return Path(root).joinpath(*rel.parts)


# --- apply and undo -------------------------------------------------------------------------------

@dataclass
class Outcome:
    folder: Path
    lines: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


def _write_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)  # bytes: the job's line endings and byte-order mark, exactly


def _remove(target: Path) -> None:
    """Delete a file, trying once more after a moment when Windows says another program has it
    open (antivirus right after a write, an editor), and making a read-only file deletable."""
    for attempt in (1, 2):
        try:
            target.unlink(missing_ok=True)
            return
        except PermissionError as exc:
            try:
                os.chmod(target, stat.S_IWRITE)
            except OSError:
                pass
            if attempt == 2:
                raise LandingError(f"couldn't remove {target}: {exc}. A program is holding it open; close it and "
                                   "try again.") from None
            time.sleep(REMOVE_RETRY_DELAY)


def _shown(folder: Path, root: Path) -> str:
    try:
        return folder.relative_to(root).as_posix()
    except ValueError:
        return str(folder)


def apply(root: Path, stamp: str | None = None, *, dry_run: bool = False, force: bool = False) -> Outcome:
    """Copy a job's files from spare-gpu/<stamp>/ into the project. A file is applied when the
    project's copy is exactly as it was when the job started (or absent, for a new file); one that
    changed since is skipped and said so, unless `force`. What apply replaces or removes is saved
    under <stamp>/.undo/ first, with a manifest of what was done, for `gtu undo`. Refuses to apply
    the same folder twice without an undo in between. `dry_run` reports and writes nothing."""
    root = Path(root)
    folder = find(root, stamp)
    manifest = read_manifest(folder)
    name = folder.name
    shown = _shown(folder, root)
    undo_dir = folder / UNDO_DIR
    if (undo_dir / MANIFEST).is_file():
        raise LandingError(f"{shown} was already applied. Run `gtu undo {name}` first to take it back; then "
                           "it can be applied again.")
    entries = [(check_path(f.get("path")), f) for f in manifest["files"]]  # every path checked before any write
    for rel, entry in entries:
        if entry.get("change") not in CHANGE_WORDS:
            raise LandingError(f"the manifest has an unknown change {entry.get('change')!r} for {rel}; nothing was changed.")
        if entry["change"] != "D" and not (folder / Path(*rel.parts)).is_file():
            raise LandingError(f"{shown} is missing the file {rel} its manifest lists; nothing was changed.")

    outcome = Outcome(folder, counts={"applied": 0, "skipped": 0, "unchanged": 0})
    done: list[dict] = []
    try:
        for rel, entry in entries:
            target = _inside(root, rel)
            change, base, new = entry["change"], entry.get("base_sha256"), entry.get("new_sha256")
            current = file_sha256(target)
            if current == new:  # already as the job left it (absent, for a deletion)
                outcome.counts["unchanged"] += 1
                outcome.lines.append(f"unchanged: {rel} (already as the job left it)")
                continue
            if current != base and not force:
                outcome.counts["skipped"] += 1
                what = "exists already" if base is None else "was deleted since the job started" if current is None \
                    else "changed since the job started"
                outcome.lines.append(f"skipped: {rel} (it {what}; add --force to replace it anyway)")
                continue
            forced = " (forced: it had changed since the job started)" if current != base else ""
            verb = "delete" if change == "D" else "add" if current is None else "replace"
            label = f"would {verb}" if dry_run else {"delete": "deleted", "add": "added", "replace": "replaced"}[verb]
            outcome.counts["applied"] += 1
            outcome.lines.append(f"{label}: {rel}{forced}")
            if dry_run:
                continue
            if current is not None:  # save what was there, byte for byte
                _write_bytes(undo_dir.joinpath(*rel.parts), target.read_bytes())
            if change == "D":
                _remove(target)
            else:
                _write_bytes(target, (folder / Path(*rel.parts)).read_bytes())
            done.append({"path": rel.as_posix(), "change": change, "previous": current is not None,
                         "applied_sha256": new})
    finally:
        if done:  # even after a failure part-way, so what was done can be undone
            undo_dir.mkdir(parents=True, exist_ok=True)
            record = {"job": manifest.get("job"), "stamp": name, "applied_at": ledger.iso(datetime.now(timezone.utc)),
                      "files": done}
            (undo_dir / MANIFEST).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")

    applied, skipped, unchanged = (outcome.counts[k] for k in ("applied", "skipped", "unchanged"))
    extra = f", {unchanged} already as the job left it" if unchanged else ""
    if dry_run:
        outcome.lines.append(f"Dry run: would apply {applied} file(s) from {shown}, skip {skipped}{extra}. Nothing was written.")
    elif applied:
        outcome.lines.append(f"Applied {applied} file(s) from {shown}, skipped {skipped}{extra}. What they replaced "
                             f"is saved in {shown}/{UNDO_DIR}; take it back with: gtu undo {name}")
    else:
        outcome.lines.append(f"Nothing was applied from {shown}: skipped {skipped}{extra}.")
    return outcome


def undo(root: Path, stamp: str | None = None) -> Outcome:
    """Put back what `gtu apply` replaced from spare-gpu/<stamp>/ and remove the files it added,
    then remove the .undo folder so the result can be applied again."""
    root = Path(root)
    folder = find(root, stamp, for_undo=True)
    name = folder.name
    shown = _shown(folder, root)
    undo_dir = folder / UNDO_DIR
    file = undo_dir / MANIFEST
    if not file.is_file():
        raise LandingError(f"{shown} hasn't been applied (no {UNDO_DIR} folder); nothing to undo.")
    try:
        record = json.loads(file.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise LandingError(f"{file} doesn't parse ({exc}); restore the files in {shown}/{UNDO_DIR} by hand.") from None
    entries = [(check_path(f.get("path")), f) for f in record.get("files", [])]
    outcome = Outcome(folder, counts={"restored": 0, "removed": 0})
    for rel, entry in entries:
        target = _inside(root, rel)
        edited = entry.get("applied_sha256") is not None and file_sha256(target) not in (None, entry["applied_sha256"])
        note = " (it had been edited after apply; that edit is gone)" if edited else ""
        if entry.get("previous"):
            _write_bytes(target, undo_dir.joinpath(*rel.parts).read_bytes())
            outcome.counts["restored"] += 1
            outcome.lines.append(f"restored: {rel}{note}")
        else:
            _remove(target)
            outcome.counts["removed"] += 1
            outcome.lines.append(f"removed: {rel}{note}")
    try:
        worktree._rmtree(undo_dir)  # the same patient delete the job's copy gets (files held open on Windows)
    except worktree.GitError as exc:
        raise LandingError(f"the files are back, but {undo_dir} couldn't be removed: {exc}") from None
    outcome.lines.append(f"Undid {shown}: restored {outcome.counts['restored']} file(s), removed "
                         f"{outcome.counts['removed']}. It can be applied again with: gtu apply {name}")
    return outcome
