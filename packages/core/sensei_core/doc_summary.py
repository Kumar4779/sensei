"""Document summary workflow (Slice 37): a structured JSON summary of a public-style document.

The content is API_OK by design: the source is synthetic, public-style text with
no personal data, so it may go to an API route. The workflow writes
summary.json with four fields:

  title       the document's headline
  key_points  a list of the main facts, names and figures kept as written
  entities    the named organisations, people and places, each copied exactly
  abstract    one sentence

Verify and repair work as in the brief (Slice 20): one draft, checks, and at
most ONE repair from the failed check labels. The workflow's own checks are
built from the input alone (shape, grounding in the source, and nothing from
the section the source marks as unrelated). The bench grades with the task's
own checker, which the workflow never sees.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from sensei_core.executor import Executor, RunResult
from sensei_core.private import write_private
from sensei_core.trace import TraceWriter
from sensei_core.verifier import CheckResult, VerificationReport, Verifier

FIELDS = ("title", "key_points", "entities", "abstract")
# A source marks its off-topic material with a heading that starts with this.
UNRELATED_HEADING = "## Also in this issue"
MIN_KEY_POINTS, MAX_KEY_POINTS = 3, 8


@dataclass(frozen=True)
class SourceDocument:
    text: str  # the whole source
    main: str  # the main article: everything before the unrelated section
    unrelated: str  # the section the source marks as unrelated ("" if none)


def load_source(fixture_dir: str | Path) -> SourceDocument:
    text = (Path(fixture_dir) / "source.md").read_text(encoding="utf-8")
    at = text.find(UNRELATED_HEADING)
    main, unrelated = (text, "") if at < 0 else (text[:at], text[at:])
    return SourceDocument(text, main, unrelated)


def unrelated_terms(doc: SourceDocument) -> list[str]:
    """Distinctive terms of the unrelated section: capitalised words and figures absent from the main text."""
    body = doc.unrelated.split("\n", 1)[1] if "\n" in doc.unrelated else ""
    candidates = re.findall(r"\b[A-Z][a-z]{3,}\b|\b\d[\d,]{2,}\b", body)
    main_words = set(re.findall(r"[\w,]+", doc.main))
    starts = set(re.findall(r"(?:^|[.!?]\s+)([A-Z][a-z]+)", body))  # sentence-initial words are not names
    return sorted({t for t in candidates if t not in main_words and t not in starts})


FORMAT = f"""Return ONLY a JSON object (no prose, no code fences) with exactly these fields:
  "title": the article's headline, copied exactly
  "key_points": {MIN_KEY_POINTS + 1} to 6 short strings, the article's main facts; keep names and figures exactly as written
  "entities": the named organisations, people and places in the article, each copied exactly as written
  "abstract": one sentence summarising the article
Summarise only the main article. The section headed "Also in this issue" is unrelated: leave out everything in it."""


def compose_prompt(instruction: str, doc: SourceDocument) -> str:
    return f"{instruction}\n\n{FORMAT}\n\nSOURCE DOCUMENT:\n<<<\n{doc.text.strip()}\n>>>\n"


def compose_repair_prompt(instruction: str, doc: SourceDocument, previous: str, labels: list[str]) -> str:
    return (compose_prompt(instruction, doc)
            + f"\nYour previous answer was:\n<<<\n{previous.strip()}\n>>>\n"
            + "It failed these checks: " + ", ".join(labels)
            + ".\nReturn a corrected JSON object only.\n")


def parse_summary(response: str) -> dict | None:
    """The JSON object in a reply; tolerates a code fence or text around it."""
    text = response.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    elif not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        text = text[start:end + 1] if 0 <= start < end else text
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def one_sentence(text: str) -> bool:
    """Exactly one sentence: ends with . ! or ?, and no sentence break inside it."""
    t = text.strip()
    return bool(t) and t[-1] in ".!?" and not re.search(r"[.!?]\s+\S", t[:-1])


@dataclass(frozen=True)
class SummaryCheck:
    """One deterministic check on summary.json. Details never copy field contents."""

    label: str
    path: Path
    test: object = field(repr=False)  # Callable[[dict], tuple[bool, str]]

    @property
    def name(self) -> str:
        return f"summary:{self.path}#{self.label}"

    def run(self) -> CheckResult:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return CheckResult(self.name, False, "file not found")
        except json.JSONDecodeError as e:
            return CheckResult(self.name, False, f"invalid JSON: {e.msg}")
        if not isinstance(data, dict):
            return CheckResult(self.name, False, "top level is not a JSON object")
        ok, detail = self.test(data)  # type: ignore[operator]
        return CheckResult(self.name, ok, detail)


def _strings(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)


def summary_checks(doc: SourceDocument, path: str | Path) -> list[SummaryCheck]:
    """The workflow's own checks, from the input only: shape, grounding, the unrelated section left out."""
    path = Path(path)
    terms = unrelated_terms(doc)

    def fields(d):
        missing = [f for f in FIELDS if f not in d]
        return not missing, f"missing {missing}" if missing else "all fields present"

    def title(d):
        ok = isinstance(d.get("title"), str) and bool(d["title"].strip())
        return ok, "non-empty string" if ok else "not a non-empty string"

    def key_points(d):
        kp = d.get("key_points")
        ok = _strings(kp) and MIN_KEY_POINTS <= len(kp) <= MAX_KEY_POINTS
        return ok, f"{len(kp) if isinstance(kp, list) else 'no'} key points (want {MIN_KEY_POINTS}-{MAX_KEY_POINTS} strings)"

    def entities(d):
        ents = d.get("entities")
        if not _strings(ents) or not ents:
            return False, "not a non-empty list of strings"
        ungrounded = sum(e not in doc.main for e in ents)
        return not ungrounded, f"{ungrounded} of {len(ents)} entities not found verbatim in the main article"

    def abstract(d):
        a = d.get("abstract")
        ok = isinstance(a, str) and one_sentence(a)
        return ok, "one sentence" if ok else "not exactly one sentence"

    def unrelated(d):
        text = json.dumps({f: d.get(f) for f in FIELDS}, ensure_ascii=False)
        found = sum(bool(re.search(rf"\b{re.escape(t)}\b", text)) for t in terms)
        return not found, f"{found} term(s) from the unrelated section"

    return [SummaryCheck(label, path, test) for label, test in (
        ("fields", fields), ("title", title), ("key_points", key_points), ("entities_grounded", entities),
        ("abstract_one_sentence", abstract), ("unrelated_excluded", unrelated))]


def failed_labels(report: VerificationReport) -> list[str]:
    return [r.name.rsplit("#", 1)[-1] for r in report.results if not r.passed]


def render(response: str) -> str:
    """What goes on disk: the parsed object re-serialised, or the raw reply if it was not JSON."""
    data = parse_summary(response)
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n" if data is not None else response


@dataclass(frozen=True)
class SummaryResult:
    path: Path
    verified: bool
    report: VerificationReport
    run: RunResult = field(repr=False)
    attempts: int = 1
    first_report: VerificationReport | None = None
    runs: tuple[RunResult, ...] = field(default=(), repr=False)

    @property
    def total_est_cost_usd(self) -> float:
        return sum(r.total_est_cost_usd for r in self.runs or (self.run,))

    @property
    def total_latency_ms(self) -> float:
        return sum(r.latency_ms for r in self.runs or (self.run,))


def build_summary(
    executor: Executor,
    doc: SourceDocument,
    instruction: str,
    out_path: str | Path,
    sensitivity: str = "API_OK",
    *,
    max_cost_usd: float | None = None,
    max_retries: int = 1,
    trace: TraceWriter | None = None,
    repair: bool = True,
) -> SummaryResult:
    """Draft summary.json, verify it, and if it fails, repair it ONCE from the failed check labels."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    verifier = Verifier(trace)
    with executor.run_scope(task_family="doc_summary"):  # the draft and the repair are one run
        run = executor.run_once(compose_prompt(instruction, doc), sensitivity, "simple",
                                max_cost_usd=max_cost_usd, max_retries=max_retries)
        write_private(out, render(run.response))
        report = verifier.verify(summary_checks(doc, out))
        executor.note_checks(report)
        if report.passed or not repair:
            return SummaryResult(out, report.passed, report, run, attempts=1, runs=(run,))

        labels = failed_labels(report)
        remaining = None if max_cost_usd is None else max(0.0, max_cost_usd - run.total_est_cost_usd)
        second = executor.run_once(compose_repair_prompt(instruction, doc, run.response, labels), sensitivity,
                                   "simple", max_cost_usd=remaining, max_retries=max_retries)
        write_private(out, render(second.response))
        final = verifier.verify(summary_checks(doc, out))
        executor.note_checks(final)
        if trace is not None:
            trace.write("repair_attempted", {"failed_labels": labels, "verified": final.passed})
        return SummaryResult(out, final.passed, final, second, attempts=2, first_report=report, runs=(run, second))
