from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
import json
from pathlib import Path
import time
from typing import NoReturn

from .cancellation import CancelledError, CancellationToken, cancellation_or_default
from .harness import (
    BudgetReason, Capability, RunBudgets, RunCounters, RunOutcome, RunStatus,
    compose_run_budgets, compose_tool_policy,
)
from .journal import RunJournalStore
from .models import (
    REASONING_EFFORTS, ContentBlock, Message, Request, Response, StreamDelta,
    Usage,
)
from .providers import Provider
from .tools import Registry, bound_output
from .verification import VerificationCheck, VerificationResult, run_verification_check


class AgentError(RuntimeError):
    """Base error for a turn that may have produced useful partial text."""

    def __init__(self, message: str, partial_text: str = "") -> None:
        super().__init__(message)
        self.partial_text = partial_text


class UnexpectedStopReasonError(AgentError):
    pass


class MaxOutputTokensError(AgentError):
    pass


class ContextWindowExceededError(AgentError):
    pass


class MaxTurnsError(AgentError):
    pass


class RepeatedMalformedToolCallError(AgentError):
    pass


class RepeatedToolCallError(AgentError):
    pass


class ProviderResponseError(AgentError):
    """A provider failed after it may have emitted useful streamed output."""


class ProviderProtocolError(AgentError):
    """A provider returned a response that cannot form a valid transcript."""


class NoProgressError(AgentError):
    """The provider repeatedly requested continuation without producing output."""


class BudgetExhaustedError(AgentError):
    """A harness run reached a hard limit before starting more work."""

    def __init__(self, reason: BudgetReason, limit: int | float,
                 partial_text: str = "") -> None:
        self.reason = reason
        self.limit = limit
        super().__init__(
            f"run exhausted {reason.value} budget ({limit})", partial_text,
        )

    @property
    def reason_code(self) -> str:
        return self.reason.code


class VerificationError(AgentError):
    """One or more required deterministic checks failed."""

    def __init__(self, results: list[VerificationResult], partial_text: str = "") -> None:
        self.results = results
        failed = ", ".join(result.name for result in results if result.required and not result.passed)
        super().__init__(f"required verification failed: {failed}", partial_text)


class EventKind(str, Enum):
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    ASSISTANT_TEXT = "assistant_text"
    ASSISTANT_COMMENTARY = "assistant_commentary"
    PARALLEL_START = "parallel_start"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    STEERING_APPLIED = "steering_applied"
    DONE = "done"
    ERROR = "error"
    MAX_TURNS_REACHED = "max_turns_reached"
    BUDGET_UPDATED = "budget_updated"
    VERIFICATION_STARTED = "verification_started"
    VERIFICATION_FINISHED = "verification_finished"
    REPAIR_STARTED = "repair_started"
    OUTPUT_CONTINUATION = "output_continuation"


@dataclass(slots=True)
class Event:
    """Structured activity emitted by an agent turn.

    ``elapsed_ms`` is measured from the start of ``send``. ``duration_ms`` is
    populated for completed operations such as tool calls.
    """

    kind: EventKind
    text: str = ""
    name: str = ""
    args: dict[str, object] = field(default_factory=dict)
    tool_use_id: str = ""
    is_error: bool = False
    elapsed_ms: int = 0
    duration_ms: int = 0
    max_turns: int = 0
    error: BaseException | None = None
    partial_text: str = ""
    reason: str = ""
    provider_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    capability: str = ""
    verification: VerificationResult | None = None
    run_id: str = ""
    sequence: int = 0
    status: str = ""
    parent_run_id: str = ""


EventHandler = Callable[[Event], None]

_KNOWN_STOP_REASONS = {
    "", "end_turn", "stop_sequence", "tool_use", "max_tokens", "length",
    "refusal", "pause_turn", "model_context_window_exceeded",
}


class Agent:
    """Provider-neutral model/tool loop with a serial, structurally valid transcript."""

    def __init__(self, provider: Provider, model: str, system: str, tools: Registry,
                 messages: list[Message] | None = None, usage: Usage | None = None,
                 max_turns: int = 500, on_event: EventHandler | None = None,
                 budgets: RunBudgets | None = None,
                 capability: Capability | None = None,
                 verification_checks: list[VerificationCheck] | None = None,
                 workspace: Path | None = None,
                 repair_attempts: int = 0,
                 max_output_tokens: int = 8192,
                 max_output_continuations: int = 2,
                 reasoning_effort: str = "",
                 journal_store: RunJournalStore | None = None,
                 parent_run_id: str = "") -> None:
        self.provider = provider
        self.model = model
        self.system = system
        self.tools = tools
        self.messages = list(messages or [])
        self.usage = usage or Usage()
        self.max_turns = max_turns
        self.on_event = on_event or (lambda _event: None)
        self.event_errors: list[Exception] = []
        self._send_started = 0.0
        self.budgets = budgets
        self.capability = capability
        self.verification_checks = list(verification_checks or [])
        self.workspace = (workspace or Path.cwd()).resolve()
        if isinstance(repair_attempts, bool) or not isinstance(repair_attempts, int) or repair_attempts < 0:
            raise ValueError("repair_attempts must be a non-negative integer")
        self.repair_attempts = repair_attempts
        if (
            isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
            or max_output_tokens <= 0
        ):
            raise ValueError("max_output_tokens must be a positive integer")
        if (
            isinstance(max_output_continuations, bool)
            or not isinstance(max_output_continuations, int)
            or not 0 <= max_output_continuations <= 10
        ):
            raise ValueError(
                "max_output_continuations must be an integer between 0 and 10"
            )
        self.max_output_tokens = max_output_tokens
        self.max_output_continuations = max_output_continuations
        if not isinstance(reasoning_effort, str):
            raise ValueError("reasoning_effort must be a string")
        reasoning_effort = reasoning_effort.strip().lower()
        if reasoning_effort and reasoning_effort not in REASONING_EFFORTS:
            choices = ", ".join(sorted(REASONING_EFFORTS))
            raise ValueError(f"reasoning_effort must be one of: {choices}")
        self.reasoning_effort = reasoning_effort
        self.journal_store = journal_store
        self.parent_run_id = parent_run_id
        self.journal_errors: list[Exception] = []
        self.run_counters = RunCounters()
        self.last_outcome: RunOutcome | None = None
        self._active_budgets: RunBudgets | None = None
        self._active_capability = ""
        self._approval_denied = False
        self._event_sequence = 0
        self._active_run_id = ""
        self._active_policy = None

    def send(self, text: str, display_text: str = "",
             cancellation: CancellationToken | None = None,
             allowed_tools: set[str] | None = None,
             denied_tools: set[str] | None = None,
             protect_existing_files: bool = False,
             include_history: bool = True,
             budgets: RunBudgets | None = None,
             capability: Capability | None = None) -> str:
        """Run one user turn, optionally under hard harness budgets."""
        self._send_started = time.monotonic()
        policies = tuple(item for item in (self.capability, capability) if item is not None)
        self._active_budgets = compose_run_budgets(
            self.budgets, *(item.budgets for item in policies), budgets,
        )
        self._approval_denied = False
        policy = compose_tool_policy(
            *policies, allowed_tools=allowed_tools, denied_tools=denied_tools,
            protect_existing_files=protect_existing_files,
        )
        self._active_policy = policy
        self._active_capability = "+".join(item.name for item in policies)
        self.run_counters = RunCounters()
        outcome = RunOutcome(
            parent_run_id=self.parent_run_id,
            capability=self._active_capability, counters=self.run_counters,
        )
        self.last_outcome = outcome
        self._event_sequence = 0
        self._active_run_id = outcome.run_id
        self._emit(Event(EventKind.RUN_STARTED, status=RunStatus.RUNNING.value))
        try:
            result = self._run_turn(
                text, display_text, cancellation,
                None if policy.allowed_tools is None else set(policy.allowed_tools),
                set(policy.denied_tools), policy.protect_existing_files,
                include_history,
            )
            for attempt in range(self.repair_attempts + 1):
                try:
                    self._run_verification(cancellation, outcome, result)
                    break
                except VerificationError as exc:
                    if attempt >= self.repair_attempts or self._approval_denied:
                        raise
                    self._check_repair_budgets(result)
                    outcome.repair_attempts += 1
                    self._emit(Event(
                        EventKind.REPAIR_STARTED,
                        text=self._verification_failure_report(exc.results),
                        args={"attempt": outcome.repair_attempts},
                    ))
                    repair_prompt = (
                        "[harness repair]\n"
                        "Trusted verification failed after your previous work. "
                        "Diagnose and repair only these failures, then finish normally.\n\n"
                        f"Original request:\n{bound_output(text)}\n\n"
                        f"Verification report:\n{self._verification_failure_report(exc.results)}"
                    )
                    result = self._run_turn(
                        repair_prompt, "[harness repair]", cancellation,
                        None if policy.allowed_tools is None else set(policy.allowed_tools),
                        set(policy.denied_tools), policy.protect_existing_files,
                        include_history,
                    )
        except CancelledError:
            outcome.status = RunStatus.CANCELLED
            outcome.reason = "cancelled"
            raise
        except BudgetExhaustedError as exc:
            outcome.status = RunStatus.BUDGET_EXHAUSTED
            outcome.reason = exc.reason_code
            outcome.final_text = exc.partial_text
            raise
        except VerificationError as exc:
            outcome.status = RunStatus.FAILED
            outcome.reason = "verification_failed"
            outcome.final_text = exc.partial_text
            self._emit(Event(
                EventKind.ERROR, text=str(exc), error=exc,
                partial_text=exc.partial_text, reason="verification_failed",
            ))
            raise
        except BaseException as exc:
            outcome.status = RunStatus.FAILED
            outcome.reason = "agent_error" if isinstance(exc, AgentError) else "interrupted"
            outcome.final_text = getattr(exc, "partial_text", "")
            raise
        else:
            outcome.status = RunStatus.SUCCEEDED
            outcome.reason = "completed"
            outcome.final_text = result
            return result
        finally:
            self._update_elapsed()
            self._emit(Event(
                EventKind.RUN_FINISHED, status=outcome.status.value,
                reason=outcome.reason,
            ))
            if self.journal_store is not None:
                try:
                    self.journal_store.save(
                        outcome, policy, self._active_budgets, self.workspace,
                        self._event_sequence,
                    )
                except Exception as exc:
                    # Persistence is an observer of the completed run. A disk or
                    # serialization failure must not replace its actual outcome.
                    self.journal_errors.append(exc)
            self._active_budgets = None
            self._active_capability = ""
            self._active_run_id = ""
            self._active_policy = None

    def run_subagent(
        self, prompt: str, capability: Capability, budgets: RunBudgets,
        *, provider: Provider | None = None, model: str | None = None,
        system: str | None = None,
        cancellation: CancellationToken | None = None,
    ) -> tuple[str, RunOutcome]:
        """Run a child under a strictly narrower policy and remaining parent limits."""
        if not self._active_run_id or self._active_policy is None:
            raise RuntimeError("subagents can only run during an active parent send")
        parent_policy = self._active_policy
        child_policy = compose_tool_policy(
            capability,
            allowed_tools=parent_policy.allowed_tools,
            denied_tools=parent_policy.denied_tools,
            protect_existing_files=parent_policy.protect_existing_files,
        )
        if child_policy == parent_policy:
            raise ValueError("subagent capability must be strictly narrower than its parent")
        remaining = self._remaining_budgets_for_child(prompt)
        child_budgets = compose_run_budgets(remaining, capability.budgets, budgets)
        parent_run_id = self._active_run_id

        def child_event(event: Event) -> None:
            event.parent_run_id = parent_run_id
            try:
                self.on_event(event)
            except Exception as exc:
                self.event_errors.append(exc)

        child = Agent(
            provider or self.provider, model or self.model, system or self.system,
            self.tools, on_event=child_event, budgets=child_budgets,
            workspace=self.workspace, capability=capability,
            journal_store=self.journal_store,
            max_output_tokens=self.max_output_tokens,
            max_output_continuations=self.max_output_continuations,
            reasoning_effort=self.reasoning_effort,
            parent_run_id=parent_run_id,
        )
        try:
            text = child.send(
                prompt, cancellation=cancellation,
                allowed_tools=(
                    None if parent_policy.allowed_tools is None
                    else set(parent_policy.allowed_tools)
                ),
                denied_tools=set(parent_policy.denied_tools),
                protect_existing_files=parent_policy.protect_existing_files,
            )
        finally:
            counters = child.run_counters
            self.run_counters.provider_calls += counters.provider_calls
            self.run_counters.tool_calls += counters.tool_calls
            self.run_counters.usage.add(counters.usage)
            self.usage.add(counters.usage)
            self._update_elapsed()
            self._emit_budget_update()
            if child.last_outcome is not None and self.last_outcome is not None:
                self.last_outcome.child_outcomes.append(child.last_outcome)
        assert child.last_outcome is not None
        return text, child.last_outcome

    def _remaining_budgets_for_child(self, partial: str) -> RunBudgets | None:
        self._check_repair_budgets(partial)
        budgets = self._active_budgets
        if budgets is None:
            return None
        elapsed = self.run_counters.elapsed_seconds

        def remaining(limit, used):
            return None if limit is None else limit - used

        return RunBudgets(
            provider_calls=remaining(
                budgets.provider_calls, self.run_counters.provider_calls,
            ),
            tool_calls=remaining(budgets.tool_calls, self.run_counters.tool_calls),
            elapsed_seconds=remaining(budgets.elapsed_seconds, elapsed),
            input_tokens=remaining(
                budgets.input_tokens, self.run_counters.usage.input_tokens,
            ),
            output_tokens=remaining(
                budgets.output_tokens, self.run_counters.usage.output_tokens,
            ),
        )

    def _run_verification(self, cancellation: CancellationToken | None,
                          outcome: RunOutcome, partial: str) -> None:
        token = cancellation_or_default(cancellation)
        results: list[VerificationResult] = []
        for check in self.verification_checks:
            token.raise_if_cancelled()
            self._check_continuation_budgets(partial)
            self._emit(Event(EventKind.VERIFICATION_STARTED, name=check.name))
            try:
                result = run_verification_check(check, self.workspace, token)
            except CancelledError as exc:
                cancelled = getattr(exc, "verification_result", None)
                if cancelled is not None:
                    outcome.verification.append(cancelled)
                    results.append(cancelled)
                    self._emit(Event(
                        EventKind.VERIFICATION_FINISHED, name=check.name,
                        is_error=True, verification=cancelled,
                        duration_ms=cancelled.duration_ms,
                    ))
                raise
            outcome.verification.append(result)
            results.append(result)
            self._emit(Event(
                EventKind.VERIFICATION_FINISHED, name=check.name,
                text=result.output, is_error=not result.passed,
                verification=result, duration_ms=result.duration_ms,
            ))
        if any(not item.passed and item.required for item in results):
            raise VerificationError(results, partial)

    @staticmethod
    def _verification_failure_report(results: list[VerificationResult]) -> str:
        sections = []
        for result in results:
            if result.passed:
                continue
            detail = result.output.strip() or "(no output)"
            sections.append(
                f"[{result.name}] status={result.status.value} "
                f"required={str(result.required).lower()}\n{detail}"
            )
        return bound_output("\n\n".join(sections))

    def _check_repair_budgets(self, partial: str) -> None:
        """Do not announce or begin repair after any hard limit is exhausted."""
        self._check_continuation_budgets(partial)
        budgets = self._active_budgets
        if budgets is None:
            return
        for reason, value, limit in (
            (BudgetReason.PROVIDER_CALLS, self.run_counters.provider_calls, budgets.provider_calls),
            (BudgetReason.TOOL_CALLS, self.run_counters.tool_calls, budgets.tool_calls),
        ):
            if limit is not None and value >= limit:
                raise BudgetExhaustedError(reason, limit, partial)

    def _run_turn(self, text: str, display_text: str = "",
                  cancellation: CancellationToken | None = None,
                  allowed_tools: set[str] | None = None,
                  denied_tools: set[str] | None = None,
                  protect_existing_files: bool = False,
                  include_history: bool = True) -> str:
        if not text.strip():
            raise AgentError("message is empty")
        cancellation = cancellation_or_default(cancellation)
        turn_start = len(self.messages)
        self.messages.append(Message("user", [ContentBlock("text", text=text)], display_text=display_text))
        final_parts: list[str] = []
        malformed_counts: dict[str, int] = {}
        tool_signatures: list[tuple[str, str, bool, str]] = []
        no_progress_count = 0
        output_continuations = 0
        for _ in range(self.max_turns):
            streamed_blocks: list[ContentBlock] = []

            def emit_delta(delta: StreamDelta) -> None:
                if delta.text:
                    block_type = "commentary" if delta.kind == "commentary" else "text"
                    if streamed_blocks and streamed_blocks[-1].type == block_type:
                        streamed_blocks[-1].text += delta.text
                    else:
                        streamed_blocks.append(ContentBlock(block_type, text=delta.text))
                self._emit_delta(delta)

            try:
                request = self._prepare_request(
                    turn_start, include_history, allowed_tools, denied_tools,
                    cancellation, "\n".join(final_parts).strip(),
                )
                response, streamed = self._request_provider(
                    request, cancellation, emit_delta,
                )
            except BudgetExhaustedError as exc:
                self._fail(exc)
            except CancelledError as exc:
                self._append_streamed_partial(streamed_blocks)
                self._emit(Event(
                    EventKind.ERROR, text=str(exc), error=exc,
                    partial_text=self._partial_from(final_parts, streamed_blocks),
                ))
                raise
            except Exception as exc:
                partial = self._partial_from(final_parts, streamed_blocks)
                self._append_streamed_partial(streamed_blocks)
                error = ProviderResponseError(str(exc), partial)
                self._emit(Event(
                    EventKind.ERROR, text=str(error), error=error,
                    partial_text=partial,
                ))
                raise error from exc
            assistant_blocks, calls, partial, meaningful = self._commit_response(
                response, streamed, final_parts,
            )

            self._validate_response(response.stop_reason, calls, partial)
            no_progress_count = 0 if meaningful else no_progress_count + 1
            if response.stop_reason == "pause_turn" and no_progress_count >= 3:
                self._fail(NoProgressError(
                    "provider returned three empty pause turns without making progress",
                    partial,
                ))

            if response.stop_reason not in _KNOWN_STOP_REASONS:
                self._append_safe_assistant(assistant_blocks)
                self._fail(UnexpectedStopReasonError(
                    f"unexpected provider stop reason: {response.stop_reason!r}", partial
                ))

            if response.stop_reason == "refusal":
                self._append_safe_assistant(assistant_blocks)
                self._emit(Event(EventKind.DONE))
                return partial

            if response.stop_reason == "model_context_window_exceeded":
                self._append_safe_assistant(assistant_blocks)
                self._fail(ContextWindowExceededError(
                    "provider context window was exceeded", partial
                ))

            if not calls:
                if response.stop_reason in {"", "end_turn", "stop_sequence"}:
                    return self._finish_success(assistant_blocks, partial)
                if assistant_blocks:
                    self.messages.append(Message("assistant", assistant_blocks))
                if response.stop_reason in {"max_tokens", "length"}:
                    clean_text = any(
                        block.type == "text" and bool(block.text.strip())
                        for block in assistant_blocks
                    ) and all(
                        block.type == "text"
                        or (
                            block.type == "raw"
                            and isinstance(block.raw, dict)
                            and block.raw.get("type") == "reasoning"
                        )
                        for block in assistant_blocks
                    )
                    if clean_text and output_continuations < self.max_output_continuations:
                        output_continuations += 1
                        continuation_prompt = (
                            "[harness output continuation]\n"
                            "The provider stopped at its output-token limit. Continue "
                            "exactly where the previous response stopped. Do not repeat "
                            "completed text. Complete the original request normally."
                        )
                        self.messages.append(Message(
                            "user", [ContentBlock("text", text=continuation_prompt)],
                            display_text="[harness output continuation]",
                        ))
                        self._emit(Event(
                            EventKind.OUTPUT_CONTINUATION,
                            text="continuing a clean text response after token truncation",
                            args={
                                "attempt": output_continuations,
                                "limit": self.max_output_continuations,
                            },
                        ))
                        continue
                    self._fail(MaxOutputTokensError(
                        "provider response was truncated at the token limit", partial
                    ))
                # pause_turn explicitly asks for another provider response.
                continue

            # Commit one result for every announced call even when a tool raises a
            # BaseException. Earlier tools may already have changed external state,
            # so dropping the batch would make retries unsafe and history untruthful.
            if all(
                not call.argument_error
                and self.tools.is_parallel_safe(
                    call.name, allowed_tools, denied_tools,
                )
                for call in calls
            ):
                self._execute_parallel_batch(
                    calls, assistant_blocks, cancellation, partial,
                    allowed_tools, denied_tools, protect_existing_files,
                    tool_signatures,
                )
                continue
            results: list[ContentBlock] = []
            cancelled: CancelledError | None = None
            interrupted: BaseException | None = None
            repeated_malformed: RepeatedMalformedToolCallError | None = None
            repeated_tool: RepeatedToolCallError | None = None
            budget_exhausted: BudgetExhaustedError | None = None
            parallel_outputs: dict[
                str, tuple[str, bool, BaseException | None, float]
            ] = {}
            parallel_prepared: set[str] = set()
            for call_index, call in enumerate(calls):
                if call.id not in parallel_prepared:
                    group: list[ContentBlock] = []
                    for candidate in calls[call_index:]:
                        if (
                            candidate.argument_error
                            or not self.tools.is_parallel_safe(
                                candidate.name, allowed_tools, denied_tools,
                            )
                        ):
                            break
                        group.append(candidate)
                    if len(group) >= 2:
                        runnable: list[ContentBlock] = []
                        group_blocked: BaseException | None = None
                        group_started: dict[str, float] = {}
                        for candidate in group:
                            self._emit(Event(
                                EventKind.TOOL_CALL, name=candidate.name,
                                args=dict(candidate.input), tool_use_id=candidate.id,
                            ))
                            group_started[candidate.id] = time.monotonic()
                            parallel_prepared.add(candidate.id)
                            if group_blocked is not None:
                                parallel_outputs[candidate.id] = (
                                    "skipped because an earlier parallel call was blocked",
                                    True, group_blocked, group_started[candidate.id],
                                )
                                continue
                            try:
                                cancellation.raise_if_cancelled()
                                self._before_tool_call(partial)
                            except (BudgetExhaustedError, CancelledError) as exc:
                                group_blocked = exc
                                parallel_outputs[candidate.id] = (
                                    f"not executed: {exc}", True, exc,
                                    group_started[candidate.id],
                                )
                            else:
                                runnable.append(candidate)

                        def execute_parallel(candidate: ContentBlock) -> str:
                            cancellation.raise_if_cancelled()
                            return self.tools.run(
                                candidate.name, candidate.input, cancellation,
                                allowed_tools, denied_tools,
                                protect_existing_files,
                            )

                        if runnable:
                            with ThreadPoolExecutor(
                                max_workers=min(4, len(runnable)),
                            ) as executor:
                                futures = {
                                    candidate.id: executor.submit(
                                        execute_parallel, candidate,
                                    )
                                    for candidate in runnable
                                }
                                for candidate in runnable:
                                    try:
                                        value = futures[candidate.id].result()
                                        parallel_outputs[candidate.id] = (
                                            value, False, None,
                                            group_started[candidate.id],
                                        )
                                    except BaseException as exc:
                                        parallel_outputs[candidate.id] = (
                                            str(exc), True, exc,
                                            group_started[candidate.id],
                                        )
                if call.id not in parallel_prepared:
                    self._emit(Event(
                        EventKind.TOOL_CALL, name=call.name, args=dict(call.input),
                        tool_use_id=call.id,
                    ))
                    tool_started = time.monotonic()
                else:
                    tool_started = parallel_outputs[call.id][3]
                error = (
                    cancelled is not None or interrupted is not None
                    or budget_exhausted is not None
                )
                if call.id in parallel_outputs:
                    output, error, parallel_error, _started = parallel_outputs[call.id]
                    if isinstance(parallel_error, BudgetExhaustedError):
                        budget_exhausted = budget_exhausted or parallel_error
                    elif isinstance(parallel_error, CancelledError):
                        cancelled = cancelled or parallel_error
                    elif parallel_error is not None and not isinstance(
                        parallel_error, Exception,
                    ):
                        interrupted = interrupted or parallel_error
                        output = f"tool interrupted by {type(parallel_error).__name__}"
                    elif isinstance(parallel_error, PermissionError):
                        self._approval_denied = True
                elif interrupted is not None:
                    output = "skipped because an earlier tool interrupted the agent turn"
                elif cancelled is not None:
                    output = "skipped because the agent turn was cancelled"
                elif budget_exhausted is not None:
                    output = "skipped because the harness budget was exhausted"
                elif call.argument_error:
                    error = True
                    malformed_counts[call.name] = malformed_counts.get(call.name, 0) + 1
                    output = call.argument_error
                    if malformed_counts[call.name] >= 3:
                        output += (
                            " HAL stopped this turn after three malformed calls to "
                            f"{call.name!r}; use a different available tool or finish "
                            "the response without that tool."
                        )
                        repeated_malformed = RepeatedMalformedToolCallError(
                            f"repeated malformed arguments for tool {call.name!r}", partial,
                        )
                else:
                    try:
                        output = self._execute_tool(
                            call, cancellation, partial,
                            allowed_tools, denied_tools, protect_existing_files,
                        )
                    except BudgetExhaustedError as exc:
                        budget_exhausted = exc
                        error = True
                        output = f"not executed: {exc}"
                    except CancelledError as exc:
                        cancelled = exc
                        error = True
                        output = str(exc)
                    except Exception as exc:
                        if isinstance(exc, PermissionError):
                            self._approval_denied = True
                        error = True
                        output = str(exc)
                    except BaseException as exc:
                        interrupted = exc
                        error = True
                        output = f"tool interrupted by {type(exc).__name__}"
                output = bound_output(output)
                signature = (
                    call.name,
                    self._canonical_arguments(call.input),
                    error,
                    output,
                )
                if not call.argument_error:
                    tool_signatures.append(signature)
                    cycle_length = self._repeated_cycle_length(tool_signatures)
                    if cycle_length:
                        if cycle_length == 1:
                            notice = (
                                " HAL stopped this turn after three identical "
                                f"calls to {call.name!r} with the same result."
                            )
                            message = f"repeated identical tool call {call.name!r}"
                        else:
                            notice = (
                                " HAL stopped this turn after the same sequence of "
                                f"{cycle_length} tool calls repeated three times."
                            )
                            message = (
                                "repeated tool-call cycle of length "
                                f"{cycle_length} ending at {call.name!r}"
                            )
                        output = bound_output(output + notice)
                        repeated_tool = RepeatedToolCallError(message, partial)
                results.append(ContentBlock("tool_result", tool_use_id=call.id, content=output, is_error=error))
                self._emit(Event(
                    EventKind.TOOL_RESULT, text=output, name=call.name,
                    tool_use_id=call.id, is_error=error,
                    duration_ms=int((time.monotonic() - tool_started) * 1000),
                ))
            self._commit_tool_batch(assistant_blocks, results)
            if cancelled is not None:
                self._emit(Event(EventKind.ERROR, text=str(cancelled), error=cancelled))
                raise cancelled
            if interrupted is not None:
                self._emit(Event(
                    EventKind.ERROR, text=str(interrupted), error=interrupted,
                    partial_text=partial,
                ))
                raise interrupted
            if budget_exhausted is not None:
                self._fail(budget_exhausted)
            if repeated_malformed is not None:
                self._fail(repeated_malformed)
            if repeated_tool is not None:
                self._fail(repeated_tool)
        partial = "\n".join(final_parts).strip()
        error = MaxTurnsError(
            f"agent exceeded maximum of {self.max_turns} provider turns", partial
        )
        self._emit(Event(
            EventKind.MAX_TURNS_REACHED, max_turns=self.max_turns,
            error=error, partial_text=partial,
        ))
        raise error

    def _prepare_request(
        self, turn_start: int, include_history: bool,
        allowed_tools: set[str] | None, denied_tools: set[str] | None,
        cancellation: CancellationToken, partial: str,
    ) -> Request:
        """Prepare one provider request after enforcing continuation limits."""
        cancellation.raise_if_cancelled()
        self._before_provider_call(partial)
        return Request(
            model=self.model,
            system=self.system,
            messages=list(
                self.messages if include_history else self.messages[turn_start:]
            ),
            tools=self.tools.specs_for(allowed_tools, denied_tools),
            max_tokens=self.max_output_tokens,
            reasoning_effort=self.reasoning_effort,
        )

    def _request_provider(
        self, request: Request, cancellation: CancellationToken,
        emit_delta: Callable[[StreamDelta], None],
    ) -> tuple[Response, bool]:
        """Execute one buffered or streaming provider request."""
        stream = getattr(self.provider, "stream", None)
        streamed = callable(stream) and getattr(
            self.provider, "streaming_enabled", True,
        )
        response = (
            stream(request, emit_delta, cancellation)
            if streamed else self.provider.complete(request, cancellation)
        )
        cancellation.raise_if_cancelled()
        return response, streamed

    def _commit_response(
        self, response: Response, streamed: bool, final_parts: list[str],
    ) -> tuple[list[ContentBlock], list[ContentBlock], str, bool]:
        """Account for a provider response and expose its validated-stage inputs."""
        self.usage.add(response.usage)
        self.run_counters.usage.add(response.usage)
        self._emit_budget_update()
        assistant_blocks = response.content
        for block in assistant_blocks:
            if not block.text:
                continue
            if block.type == "text":
                final_parts.append(block.text)
                if not streamed:
                    self._emit(Event(EventKind.ASSISTANT_TEXT, text=block.text))
            elif block.type == "commentary" and not streamed:
                self._emit(Event(EventKind.ASSISTANT_COMMENTARY, text=block.text))
        partial = "\n".join(final_parts).strip()
        calls = [block for block in assistant_blocks if block.type == "tool_use"]
        meaningful = bool(calls) or any(
            block.text for block in assistant_blocks
            if block.type in {"text", "commentary"}
        )
        return assistant_blocks, calls, partial, meaningful

    def _execute_tool(
        self, call: ContentBlock, cancellation: CancellationToken, partial: str,
        allowed_tools: set[str] | None, denied_tools: set[str] | None,
        protect_existing_files: bool,
    ) -> str:
        """Execute one validated tool call after enforcing its budget boundary."""
        cancellation.raise_if_cancelled()
        self._before_tool_call(partial)
        return self.tools.run(
            call.name, call.input, cancellation,
            allowed_tools, denied_tools, protect_existing_files,
        )

    def _execute_parallel_batch(
        self, calls: list[ContentBlock], assistant_blocks: list[ContentBlock],
        cancellation: CancellationToken, partial: str,
        allowed_tools: set[str] | None, denied_tools: set[str] | None,
        protect_existing_files: bool,
        tool_signatures: list[tuple[str, str, bool, str]],
    ) -> None:
        """Execute a fully parallel-safe batch and commit results in request order."""
        started: list[float] = []
        runnable: list[ContentBlock] = []
        blocked: BudgetExhaustedError | None = None
        reservation_cancelled: CancelledError | None = None
        for call in calls:
            self._emit(Event(
                EventKind.TOOL_CALL, name=call.name, args=dict(call.input),
                tool_use_id=call.id,
            ))
            started.append(time.monotonic())
            if blocked is not None or reservation_cancelled is not None:
                continue
            try:
                cancellation.raise_if_cancelled()
                self._before_tool_call(partial)
            except BudgetExhaustedError as exc:
                blocked = exc
            except CancelledError as exc:
                reservation_cancelled = exc
            else:
                runnable.append(call)

        def execute(call: ContentBlock) -> str:
            cancellation.raise_if_cancelled()
            return self.tools.run(
                call.name, call.input, cancellation,
                allowed_tools, denied_tools, protect_existing_files,
            )

        outputs: dict[str, tuple[str, bool, BaseException | None]] = {}
        if runnable:
            with ThreadPoolExecutor(max_workers=min(4, len(runnable))) as executor:
                futures = {call.id: executor.submit(execute, call) for call in runnable}
                for call in runnable:
                    try:
                        outputs[call.id] = (futures[call.id].result(), False, None)
                    except BaseException as exc:
                        outputs[call.id] = (str(exc), True, exc)

        results: list[ContentBlock] = []
        cancelled: CancelledError | None = None
        interrupted: BaseException | None = None
        repeated: RepeatedToolCallError | None = None
        runnable_ids = {call.id for call in runnable}
        for index, call in enumerate(calls):
            if call.id in outputs:
                output, error, exception = outputs[call.id]
                if isinstance(exception, CancelledError) and cancelled is None:
                    cancelled = exception
                elif exception is not None and not isinstance(exception, Exception):
                    interrupted = interrupted or exception
                    output = f"tool interrupted by {type(exception).__name__}"
                elif isinstance(exception, PermissionError):
                    self._approval_denied = True
            elif reservation_cancelled is not None:
                error = True
                output = "skipped because the agent turn was cancelled"
                cancelled = cancelled or reservation_cancelled
            elif blocked is not None and call.id not in runnable_ids:
                error = True
                output = (
                    f"not executed: {blocked}"
                    if index == len(runnable) else
                    "skipped because the harness budget was exhausted"
                )
            else:
                error = True
                output = "skipped because the agent turn was cancelled"
            output = bound_output(output)
            tool_signatures.append((
                call.name, self._canonical_arguments(call.input), error, output,
            ))
            cycle_length = self._repeated_cycle_length(tool_signatures)
            if cycle_length:
                message = (
                    f"repeated identical tool call {call.name!r}"
                    if cycle_length == 1 else
                    f"repeated tool-call cycle of length {cycle_length} ending at {call.name!r}"
                )
                output = bound_output(
                    output + " HAL stopped this turn after the same parallel-safe "
                    "tool sequence repeated three times."
                )
                repeated = RepeatedToolCallError(message, partial)
            results.append(ContentBlock(
                "tool_result", tool_use_id=call.id, content=output, is_error=error,
            ))
            self._emit(Event(
                EventKind.TOOL_RESULT, text=output, name=call.name,
                tool_use_id=call.id, is_error=error,
                duration_ms=int((time.monotonic() - started[index]) * 1000),
            ))
        self._commit_tool_batch(assistant_blocks, results)
        if cancelled is not None:
            self._emit(Event(EventKind.ERROR, text=str(cancelled), error=cancelled))
            raise cancelled
        if interrupted is not None:
            self._emit(Event(
                EventKind.ERROR, text=str(interrupted), error=interrupted,
                partial_text=partial,
            ))
            raise interrupted
        if blocked is not None:
            self._fail(blocked)
        if repeated is not None:
            self._fail(repeated)

    def _commit_tool_batch(
        self, assistant_blocks: list[ContentBlock], results: list[ContentBlock],
    ) -> None:
        """Atomically append one ordered result for every announced tool call."""
        self.messages.append(Message("assistant", assistant_blocks))
        self.messages.append(Message("user", results))

    def _finish_success(
        self, assistant_blocks: list[ContentBlock], partial: str,
    ) -> str:
        """Commit the terminal assistant response and emit its finish event."""
        if assistant_blocks:
            self.messages.append(Message("assistant", assistant_blocks))
        self._emit(Event(EventKind.DONE))
        return partial

    def _before_provider_call(self, partial: str) -> None:
        self._check_continuation_budgets(partial)
        budgets = self._active_budgets
        if budgets is not None and budgets.provider_calls is not None:
            if self.run_counters.provider_calls >= budgets.provider_calls:
                raise BudgetExhaustedError(
                    BudgetReason.PROVIDER_CALLS, budgets.provider_calls, partial,
                )
        self.run_counters.provider_calls += 1
        self._emit_budget_update()

    def _before_tool_call(self, partial: str) -> None:
        self._check_continuation_budgets(partial)
        budgets = self._active_budgets
        if budgets is not None and budgets.tool_calls is not None:
            if self.run_counters.tool_calls >= budgets.tool_calls:
                raise BudgetExhaustedError(
                    BudgetReason.TOOL_CALLS, budgets.tool_calls, partial,
                )
        self.run_counters.tool_calls += 1
        self._emit_budget_update()

    def _check_continuation_budgets(self, partial: str) -> None:
        budgets = self._active_budgets
        if budgets is None:
            return
        elapsed = self._update_elapsed()
        checks = (
            (BudgetReason.ELAPSED_SECONDS, elapsed, budgets.elapsed_seconds),
            (BudgetReason.INPUT_TOKENS, self.run_counters.usage.input_tokens, budgets.input_tokens),
            (BudgetReason.OUTPUT_TOKENS, self.run_counters.usage.output_tokens, budgets.output_tokens),
        )
        for reason, value, limit in checks:
            if limit is not None and value >= limit:
                raise BudgetExhaustedError(reason, limit, partial)

    def _update_elapsed(self) -> float:
        if self._send_started:
            self.run_counters.elapsed_seconds = max(
                0.0, time.monotonic() - self._send_started,
            )
        return self.run_counters.elapsed_seconds

    def _emit_budget_update(self) -> None:
        if self._active_budgets is None:
            return
        usage = self.run_counters.usage
        self._emit(Event(
            EventKind.BUDGET_UPDATED,
            provider_calls=self.run_counters.provider_calls,
            tool_calls=self.run_counters.tool_calls,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        ))

    def _emit(self, event: Event) -> None:
        self._event_sequence += 1
        if not event.sequence:
            event.sequence = self._event_sequence
        if not event.run_id:
            event.run_id = self._active_run_id
        if not event.parent_run_id:
            event.parent_run_id = self.parent_run_id
        if not event.capability:
            event.capability = self._active_capability
        if self._send_started:
            event.elapsed_ms = int((time.monotonic() - self._send_started) * 1000)
        try:
            self.on_event(event)
        except Exception as exc:
            # Presentation and logging are observers. Their failure must not alter
            # tool execution or transcript commit semantics.
            self.event_errors.append(exc)

    def _emit_delta(self, delta: StreamDelta) -> None:
        kind = (
            EventKind.ASSISTANT_COMMENTARY
            if delta.kind == "commentary" else EventKind.ASSISTANT_TEXT
        )
        self._emit(Event(kind, text=delta.text))

    def _append_safe_assistant(self, blocks: list[ContentBlock]) -> None:
        safe = [block for block in blocks if block.type != "tool_use"]
        if safe:
            self.messages.append(Message("assistant", safe))

    def _append_streamed_partial(self, blocks: list[ContentBlock]) -> None:
        if blocks:
            self.messages.append(Message("assistant", blocks))

    @staticmethod
    def _partial_from(final_parts: list[str], streamed_blocks: list[ContentBlock]) -> str:
        parts = list(final_parts)
        parts.extend(
            block.text for block in streamed_blocks
            if block.type == "text" and block.text
        )
        return "\n".join(parts).strip()

    def _validate_response(self, stop_reason: str, calls: list[ContentBlock],
                           partial: str) -> None:
        if stop_reason == "tool_use" and not calls:
            self._fail(ProviderProtocolError(
                "provider stopped for tool use without returning a tool call", partial,
            ))
        seen: set[str] = set()
        for call in calls:
            if not call.id:
                self._fail(ProviderProtocolError(
                    "provider returned a tool call without an id", partial,
                ))
            if call.id in seen:
                self._fail(ProviderProtocolError(
                    f"provider returned duplicate tool call id {call.id!r}", partial,
                ))
            if not call.name:
                self._fail(ProviderProtocolError(
                    f"provider returned tool call {call.id!r} without a name", partial,
                ))
            seen.add(call.id)

    @staticmethod
    def _canonical_arguments(arguments: dict[str, object]) -> str:
        """Return a stable signature even for extension-provided non-JSON values."""
        return json.dumps(
            arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            default=lambda value: f"<{type(value).__module__}.{type(value).__qualname__}:{value!r}>",
        )

    @staticmethod
    def _repeated_cycle_length(
        signatures: list[tuple[str, str, bool, str]], max_cycle: int = 4,
    ) -> int:
        """Detect three consecutive copies of a short tool/result sequence."""
        for length in range(1, min(max_cycle, len(signatures) // 3) + 1):
            tail = signatures[-length:]
            if signatures[-2 * length:-length] == tail and signatures[-3 * length:-2 * length] == tail:
                return length
        return 0

    def _fail(self, error: AgentError) -> NoReturn:
        self._emit(Event(
            EventKind.ERROR, text=str(error), error=error,
            partial_text=error.partial_text,
            reason=(
                error.reason_code
                if isinstance(error, BudgetExhaustedError) else ""
            ),
        ))
        raise error
