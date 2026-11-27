"""Free-form answers through core's model backend (#25). Read-only by construction.

The voice loop's parser runs first, exactly as before. Only an utterance it
can't read as any command (a clarification with reason "unrecognised", heard
at good confidence) comes here. Low-confidence input and ambiguous or
incomplete commands ("send it", "which Sam?") still clarify.

There's no second model integration. Each answer is one core
`Executor.run_once` through core's Router, so:
  - the routing decision is core's: the content is classed API_OK (below) and
    "simple", so it goes to cheap_api (Kimi via the Moonshot API, Slice 35) and
    never escalates to frontier_api;
  - core traces `route_decision` and `model_call` (the model name and core's
    estimated cost) into the session's own trace, so the ledger shows them, and
    API calls are written to the egress log (sizes and a digest, never content);
  - quota and other failures fail closed (Slice 36) and are reported, never
    papered over.

Sensitivity. Voice commands stay LOCAL_ONLY, unchanged. A public-demo
visitor's free-form question is API_OK: the visitor is speaking to a hosted
demo whose speech already goes to cloud providers, and the demo holds only
synthetic data.

Read-only. The model gets a prompt and returns text. It has no tools, its text
is never handed to the parser, and nothing it says can propose, confirm or run
an action. Actions happen only through the parser and the PermissionGate.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from sensei_core.config import Route, load_models
from sensei_core.egress import EgressLog
from sensei_core.executor import Executor, estimate_cost, estimate_tokens
from sensei_core.router import ModelClient, Router
from sensei_core.trace import TraceWriter

from voice.adapters import ScriptedUtterance
from voice.loop import VoiceSession

SENSITIVITY = "API_OK"  # a public-demo visitor's free-form question (see the module docstring)
DIFFICULTY = "simple"  # never escalates to frontier_api
HISTORY_TURNS = 6
MAX_COST_PER_ANSWER_USD = 0.05
UNREACHABLE_REPLY = "I can't reach my model right now, so I can't answer that. Your scenario commands still work."
CAP_REPLY = "I've reached the demo's spending limit for answering questions, so I can't look that up right now."

SYSTEM_PROMPT = """You are Sensei, a warm, direct teacher-mentor, speaking out loud in a live voice demo.
Answer the visitor's question so it sounds natural when spoken:
- at most three short, plain sentences;
- no markdown, lists, headings, emoji, code or links;
- answer factual questions directly with the best answer you know; if it is the kind of fact that can change
  (who holds an office, prices, scores), give your answer and add one short note that it may have changed;
- only if you genuinely don't know, say so plainly rather than inventing an answer.
You cannot take actions. If the visitor asks you to send, pay, delete, approve or change anything,
say that actions in this demo go through the scenario cards, where Sensei reads the action back and waits for a yes.
Today's date is {today}."""


@dataclass(frozen=True)
class Answer:
    text: str
    outcome: str  # answered | model_unavailable | spend_limit
    model: str | None = None
    route: str | None = None
    est_cost_usd: float = 0.0
    latency_ms: float | None = None
    error_class: str | None = None


class ModelAnswers:
    """Answers through core's Executor, one run per question, traced into the session's trace."""

    def __init__(self, client: ModelClient, *, routes: dict[str, Route] | None = None,
                 egress: EgressLog | None = None, budget_left: Callable[[], float] | None = None,
                 on_spend: Callable[[float], None] | None = None, today: Callable[[], date] = date.today,
                 max_cost_usd: float = MAX_COST_PER_ANSWER_USD) -> None:
        self.client = client
        self.routes = routes if routes is not None else load_models()
        self.egress = egress
        self.budget_left = budget_left or (lambda: float("inf"))
        self.on_spend = on_spend or (lambda usd: None)
        self.today = today
        self.max_cost_usd = max_cost_usd
        self.model = self.routes["cheap_api"].model

    def prompt(self, question: str, history: list[tuple[str, str]]) -> str:
        lines = [SYSTEM_PROMPT.format(today=self.today().isoformat()), ""]
        if history:
            lines.append("The conversation so far:")
            for heard, reply in history[-HISTORY_TURNS:]:
                lines += [f"Visitor: {heard}", f"Sensei: {reply}"]
            lines.append("")
        lines += [f"Visitor: {question}", "Sensei:"]
        return "\n".join(lines)

    def executor(self, trace: TraceWriter) -> Executor:
        # Only cheap_api has a client: the router can't send this anywhere else, and a misroute fails closed
        # (NoClientConfigured) instead of reaching a local or frontier model.
        return Executor(Router(self.routes, trace=trace), {"cheap_api": self.client}, trace=trace, egress=self.egress)

    def answer(self, question: str, history: list[tuple[str, str]], trace: TraceWriter) -> Answer:
        prompt = self.prompt(question, history)
        rate = self.routes["cheap_api"].est_cost_per_1k_tokens
        floor = estimate_cost(estimate_tokens(len(prompt)), rate)  # input only; the reply's cost comes after
        if self.budget_left() <= floor:
            trace.write("model_error", {"model": self.model, "error_class": "spend_limit", "stage": "before_call"})
            return Answer(CAP_REPLY, "spend_limit", self.model, error_class="spend_limit")
        try:
            result = self.executor(trace).run_once(prompt, SENSITIVITY, DIFFICULTY, max_cost_usd=self.max_cost_usd,
                                                   max_retries=0)
        except Exception as e:  # any failure: say so plainly; never invent an answer
            error_class = getattr(e, "error_class", None) or type(e).__name__
            trace.write("model_error", {"model": self.model, "error_class": error_class, "stage": "call"})
            return Answer(UNREACHABLE_REPLY, "model_unavailable", self.model, "cheap_api", error_class=error_class)
        self.on_spend(result.total_est_cost_usd)  # the call was made, so it counts even if the reply is unusable
        text = " ".join(result.response.split())
        if not text:  # an empty reply is a failure, said as one; never presented as an answer
            trace.write("model_error", {"model": result.model, "error_class": "bad_response", "stage": "reply"})
            return Answer(UNREACHABLE_REPLY, "model_unavailable", result.model, result.route,
                          result.total_est_cost_usd, result.latency_ms, "bad_response")
        return Answer(text, "answered", result.model, result.route, result.total_est_cost_usd, result.latency_ms)


class AnsweringVoiceSession(VoiceSession):
    """The voice loop, unchanged, with one addition: an utterance no command matches is answered by the model."""

    def __init__(self, *args: Any, answers: ModelAnswers | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.answers = answers
        self.history: list[tuple[str, str]] = []

    def turn(self, utterance: ScriptedUtterance) -> str:
        reply = super().turn(utterance)
        self.history = (self.history + [(self.last_heard or "", reply)])[-HISTORY_TURNS:]
        return reply

    def _handle(self, text: str, confidence: float | None, record: dict[str, Any],
                barge: str | None = None) -> tuple[str, str]:
        reply, outcome = super()._handle(text, confidence, record, barge)
        if self.answers is None or outcome != "clarify" or record.get("clarify_reason") != "unrecognised":
            return reply, outcome  # commands, confirmations, low confidence and ambiguous commands: unchanged
        answer = self.answers.answer(text, self.history, self.trace)
        record["model_answer"] = dataclasses.asdict(answer) | {"text": None}  # the reply itself is record["reply"]
        return answer.text, answer.outcome
