"""LLM client for the race engineer.

Talks to any OpenAI-compatible /chat/completions endpoint, which covers both
supported providers:

  * ``openrouter`` — hosted models via https://openrouter.ai (default)
  * ``local``      — Ollama / LM Studio / vLLM on your own machine

Pick one with ``llm.provider`` in config.json, or from the web UI.
"""

import json
import logging
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

import httpx

from . import race_math
from .config import AppConfig, PROVIDER_OPENROUTER
from .prompt_builder import PromptBuilder
from .telemetry import TelemetrySnapshot
from .text import normalise_question

logger = logging.getLogger("race-engineer")

COMMS_FAILURE = "I have comms issues right now."
# Distinct from a comms failure on purpose. An empty reply used to say the
# same thing, which sent a whole debugging session at the network — Ollama,
# the endpoint, the firewall — when the model had answered in 1.7 seconds
# and simply never produced any words.
NO_ANSWER = "I lost that one, say again."

# Sent with OpenRouter requests for attribution on their dashboard.
_REFERER = "https://github.com/Bartket/AIRE"
_APP_TITLE = "AI Race Engineer"

# A question needing more than two rounds of calculation is a model looping,
# not a model working — and the driver is waiting.
_MAX_TOOL_ROUNDS = 2
# These calculators already return complete radio-safe wording. Asking the
# model to paraphrase reintroduced exactly what Python removed: "stop later"
# became "pit now", seconds became laps, and a contact time was invented from
# incompatible units. A single direct call to one of these is therefore the
# answer, not material for another model pass.
_TERMINAL_TOOLS = {"fuel_plan", "pit_window", "position_report",
                   "adjacent_gap_trend", "car_resources"}
# Headroom for emitting a tool call, which does not fit the speech budget.
_TOOL_CALL_TOKENS = 512
# Room for a reasoning model to think *and* still answer. The speech budget
# is 80 tokens because the engineer speaks one sentence; a thinking model
# charges its reasoning to the same budget and hits the ceiling mid-thought,
# so it returns a finished thought and no speech. Measured on a local
# qwen3.6: 400 tokens to reach a tool call, 1,119 to reach an answer.
_THINKING_TOKENS = 2048


@dataclass(frozen=True)
class PitCommandInterpretation:
    """A fail-closed classification; only canonical commands are parsed."""

    kind: str
    command: str = ""


def _direct_telemetry_answer(
    question: str, telemetry: Optional[TelemetrySnapshot]
) -> Optional[str]:
    """Route two safety-critical, unambiguous radio intents without an LLM.

    The model sometimes ignores adjacent gaps for "position", and sometimes
    answers live movement from best laps or changes the calculator's units.
    Both intents already have complete deterministic answers, so model
    selection and paraphrasing add failure modes without adding judgement.
    """
    if telemetry is None:
        return None
    normalised = normalise_question(question)

    position_phrases = {
        "position", "position status", "position update", "my position",
        "current position", "race position", "what is my position",
        "whats my position", "where am i",
    }
    if normalised in position_phrases:
        return race_math.position_report(
            telemetry.car, telemetry.track).get("spoken")

    fuel_phrases = {
        "fuel", "fuel status", "hows my fuel", "how is my fuel",
        "do i have enough fuel", "have i got enough fuel",
        "can i make it on fuel", "will the fuel last", "will fuel last",
        "are we good on fuel", "am i good on fuel",
    }
    if normalised in fuel_phrases:
        return race_math.fuel_plan(
            telemetry.car, telemetry.track).get("spoken")

    pit_phrases = {
        "pit window", "whats my pit window", "what is my pit window",
        "when should i pit", "when do i pit", "when should we pit",
    }
    if normalised in pit_phrases:
        return race_math.pit_window(
            telemetry.car, telemetry.track).get("spoken")

    resource_topics = {
        "are the pits open": "pits",
        "are pits open": "pits",
        "is the pit lane open": "pits",
        "how many tyre sets left": "tyres",
        "how many tire sets left": "tyres",
        "tyre sets remaining": "tyres",
        "tire sets remaining": "tyres",
        "how much push to pass left": "push to pass",
        "push to pass status": "push to pass",
        "how many joker laps left": "joker",
        "joker laps remaining": "joker",
        "what is selected for the pit stop": "pit service",
        "whats selected for the pit stop": "pit service",
        "pit service selected": "pit service",
    }
    if normalised in resource_topics:
        return race_math.car_resources(
            telemetry.car, telemetry.track, resource_topics[normalised]).get("spoken")

    race_control_phrases = {
        "what flag is out", "whats the flag", "what is the flag",
        "flag status", "race control", "race control status",
        "do i have a penalty", "have i got a penalty", "penalty status",
    }
    if normalised in race_control_phrases:
        return race_math.race_control_report(
            telemetry.car, telemetry.track).get("spoken")

    ahead = any(phrase in normalised for phrase in (
        "car ahead", "car in front", "guy ahead", "guy in front"))
    behind = any(phrase in normalised for phrase in (
        "car behind", "guy behind", "behind me"))
    movement = any(term in normalised for term in (
        "gain", "catch", "closing", "close the gap", "gap opening",
        "gap increasing", "gap decreasing", "pulling away", "dropping back",
        "reeling",
    ))
    if movement and (ahead or behind):
        direction = "ahead" if ahead else "behind"
        return race_math.adjacent_gap_trend(
            telemetry.car, telemetry.track, direction).get("spoken")
    return None


class LLMError(RuntimeError):
    """Raised by `test_connection` so the UI can surface a real message."""


class LLMClient:
    """OpenAI-compatible chat client with a short rolling memory."""

    def __init__(self, config: AppConfig, prompt_builder: Optional[PromptBuilder] = None):
        self._config = config
        self._prompts = prompt_builder or PromptBuilder(config)
        self._client = httpx.Client(timeout=30.0)
        # (question, answer) pairs replayed as context on later questions.
        self._history: Deque[Tuple[str, str]] = deque(maxlen=16)
        # Set once a provider proves it cannot handle tools, so we stop asking.
        self._tools_unsupported = False
        # Set once a model proves it thinks before speaking, so later
        # questions get the room straight away instead of paying for a
        # wasted round trip every time.
        self._thinks_aloud = False
        # Raised for post-race debriefs; None means use the configured limit.
        self._reply_budget: Optional[int] = None
        self._last_route = "none"

    # ── Public API ──────────────────────────────────────────────────

    @property
    def provider(self) -> str:
        return self._config.llm_provider

    @property
    def model(self) -> str:
        return self._config.llm_settings()["model"]

    @property
    def last_route(self) -> str:
        """How the last answer was produced, for traces and replay evals."""
        return self._last_route

    def reset_history(self) -> None:
        self._history.clear()
        self._tools_unsupported = False
        self._thinks_aloud = False

    def generate(self, question: str, telemetry: Optional[TelemetrySnapshot] = None) -> str:
        """Ask the LLM a question with telemetry as context. Returns text.

        Never raises — on any failure it returns a spoken-safe fallback so
        the radio still says something to the driver.
        """
        self._last_route = "model"
        direct = _direct_telemetry_answer(question, telemetry)
        if direct:
            self._last_route = "deterministic"
            text = _clean_response(direct)
            self._history.append((question, text))
            logger.info("Deterministic telemetry answer: %r", text)
            return text

        tools_enabled = self._tools_enabled()
        messages = self._build_messages(question, telemetry, tools_enabled)
        # A finished session lifts the one-sentence rule, so it has to lift
        # the token budget with it or the debrief stops mid-word.
        finished = telemetry is not None and getattr(telemetry.track, "finished", False)
        self._reply_budget = (
            self._config.llm_settings()["max_tokens_debrief"] if finished else None)
        try:
            if tools_enabled:
                text = self._chat_with_tools(
                    messages, telemetry,
                    self._build_messages(question, telemetry, False))
            else:
                text = self._chat(messages, max_tokens=self._reply_budget)
        except LLMError as exc:
            logger.error("LLM call failed: %s", exc)
            return COMMS_FAILURE

        text = _clean_response(text)
        if not text:
            # Reached the model, got nothing back. Saying "comms issues"
            # here points the driver — and whoever debugs it — at the
            # network, which is exactly where the problem is not.
            logger.error("Model %s returned an empty reply", self.model)
            return NO_ANSWER

        self._history.append((question, text))
        return text

    def interpret_pit_command(
        self, request: str, tire_compounds: Sequence[str]
    ) -> PitCommandInterpretation:
        """Turn natural pit-service language into the strict local grammar.

        The model cannot create a write here. Its canonical text is parsed by
        pit_commands.py, read back to the driver, and held for confirmation.
        Any malformed or unexpected response fails closed.
        """
        available = ", ".join(tire_compounds) if tire_compounds else "none published"
        system = f"""You classify radio messages about the driver's next iRacing pit service.
Return exactly one JSON object and no markdown.

For a clear instruction to CHANGE pit service, return:
{{"kind":"command","command":"pit stop <canonical changes>"}}
For a question, advice request, observation, or hypothetical, return:
{{"kind":"not_command"}}
For an unclear or unsupported action, return:
{{"kind":"ambiguous"}}

Polite requests such as "can you put me on wets" are commands. Questions such
as "should I take wets" and "how much fuel do I need" are not commands.

Canonical changes may contain only these forms, joined by "and":
- four tyres [at NUMBER kPa|PSI]
- left front|right front|left rear|right rear tyre [at NUMBER kPa|PSI]
- fuel (enable refuelling with the existing iRacing black-box amount)
- fuel NUMBER litres|gallons
- tearoff
- fast repair
- clear all|clear tyres|clear fuel|clear tearoff|clear fast repair
- EXACT_AVAILABLE_TYPE tyres

Preserve every user-supplied number and unit; never calculate or invent one.
Translate natural negatives about service into the matching clear form.
Use `fuel` without a quantity only when the driver explicitly asks to enable,
re-enable, add, or turn refuelling on without setting a new amount. It does not
mean fill the tank or fuel to the end.
"New boots", "a new set", and "all round" mean four tyres. "Wets" or
"rains" may map to Wet only when Wet is available; "drys" or "slicks" may
map to Dry only when Dry is available. Otherwise do not guess a tyre type.
Available tyre types for this car: {available}.
If the requested tyre type is not in that list, use ambiguous."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": request},
        ]
        try:
            message = self._chat_raw(
                messages, max_tokens=256, temperature=0.0)
        except LLMError as exc:
            logger.error("Pit-command interpretation failed: %s", exc)
            return PitCommandInterpretation("error")

        raw = (message.get("content") or "").strip()
        if raw.startswith("```") and raw.endswith("```"):
            raw = raw[3:-3].strip()
            if raw.lower().startswith("json"):
                raw = raw[4:].strip()
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("Malformed pit-command interpretation: %r", raw)
            return PitCommandInterpretation("error")
        if not isinstance(payload, dict):
            return PitCommandInterpretation("error")
        kind = payload.get("kind")
        if kind == "command":
            command = payload.get("command")
            if not isinstance(command, str) or len(command) > 300:
                return PitCommandInterpretation("error")
            return PitCommandInterpretation(kind, command.strip())
        if kind in ("not_command", "ambiguous"):
            return PitCommandInterpretation(kind)
        return PitCommandInterpretation("error")

    def test_connection(self) -> Dict[str, Any]:
        """Round-trip a tiny prompt so the UI can validate provider settings."""
        settings = self._config.llm_settings()
        messages = [
            {"role": "system", "content": "Reply with the single word: ready"},
            {"role": "user", "content": "Radio check."},
        ]
        reply = _clean_response(self._chat(messages, max_tokens=16))
        if not reply:
            # Reporting OK here is worse than failing. The round trip works,
            # so the panel showed a green tick while every real question came
            # back empty — a reasoning model spends 16 tokens thinking and
            # never gets to a word.
            raise LLMError(
                f"{settings['model']} connected but replied with nothing. If it is a "
                "reasoning model, it is spending the whole token budget thinking — "
                "raise Max Tokens, or pick a model that answers directly.")
        return {
            "ok": True,
            "provider": settings["provider"],
            "model": settings["model"],
            "endpoint": settings["endpoint"],
            "reply": reply,
        }

    def list_models(self) -> List[Dict[str, str]]:
        """Fetch the provider's model catalogue for the settings dropdown."""
        settings = self._config.llm_settings()
        url = f"{settings['endpoint'].rstrip('/')}/models"
        try:
            resp = self._client.get(url, headers=self._headers(settings), timeout=15.0)
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMError(f"Could not list models: {exc}") from exc

        models = []
        for item in payload.get("data", []):
            model_id = item.get("id")
            if not model_id:
                continue
            models.append({"id": model_id, "name": item.get("name") or model_id})
        models.sort(key=lambda m: m["id"])
        return models

    # ── Tool calling ────────────────────────────────────────────────

    def _tools_enabled(self) -> bool:
        """Whether to offer the calculators on this request.

        Not every model supports tools — local Ollama builds in particular
        are inconsistent — so a provider that rejects them disables them for
        the rest of the session rather than failing every question.
        """
        mode = str(self._config.llm_settings().get("use_tools", "auto")).lower()
        if mode in ("off", "false", "no", "0"):
            return False
        return not self._tools_unsupported

    def _chat_with_tools(
        self, messages: List[Dict[str, Any]], telemetry: Optional[TelemetrySnapshot],
        fallback_messages: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Chat, running any calculator the model asks for, then answer.

        Bounded to a couple of rounds: the driver is at speed, and a model
        that keeps calling tools is stalling rather than working.
        """
        convo: List[Dict[str, Any]] = list(messages)

        for _ in range(_MAX_TOOL_ROUNDS):
            try:
                message = self._chat_raw(convo, tools=race_math.tool_schemas())
            except LLMError as exc:
                if not _looks_like_tool_rejection(exc):
                    raise
                # This model cannot do tools. Fall back for the whole session
                # so we pay the discovery cost exactly once.
                logger.warning("Provider rejected tool calling, disabling it: %s", exc)
                self._tools_unsupported = True
                # The original prompt expected a tool call and therefore did
                # not contain the pre-computed strategy results. Rebuild it as
                # a no-tools turn before retrying; otherwise a model/provider
                # change silently hands fuel arithmetic back to the model.
                return self._chat(fallback_messages or messages,
                                  max_tokens=self._reply_budget)

            calls = message.get("tool_calls") or []
            if not calls:
                return message.get("content", "") or ""

            # The assistant turn must be replayed verbatim or the follow-up
            # request has tool results with nothing to attach them to.
            convo.append({
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": calls,
            })

            for call in calls:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                args = _parse_args(fn.get("arguments"))
                result = race_math.dispatch(name, args, telemetry)
                self._last_route = f"tool:{name}"
                logger.info("tool %s(%s) -> %s", name, args, result.get("spoken"))
                if len(calls) == 1 and name in _TERMINAL_TOOLS:
                    return result.get("spoken", "")
                convo.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "name": name,
                    "content": json.dumps(result),
                })

        # Out of rounds: ask once more with tools withheld so it must speak.
        logger.warning("Tool loop hit its limit; answering without tools")
        return self._chat(convo, max_tokens=self._reply_budget)

    # ── Request building ────────────────────────────────────────────

    def _build_messages(
        self, question: str, telemetry: Optional[TelemetrySnapshot],
        tools_enabled: Optional[bool] = None,
    ) -> List[Dict[str, str]]:
        turns = self._config.llm_settings()["history_turns"]
        history = list(self._history)[-turns:] if turns else []
        if tools_enabled is None:
            tools_enabled = self._tools_enabled()
        return self._prompts.build_messages(
            question, telemetry, history, tools_enabled=tools_enabled)

    def _headers(self, settings: Dict[str, Any]) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if settings["api_key"]:
            headers["Authorization"] = f"Bearer {settings['api_key']}"
        if settings["provider"] == PROVIDER_OPENROUTER:
            # Optional OpenRouter attribution headers.
            headers["HTTP-Referer"] = _REFERER
            headers["X-Title"] = _APP_TITLE
        return headers

    def _chat(self, messages: List[Dict[str, Any]], max_tokens: Optional[int] = None) -> str:
        """Plain completion — no tools offered, so the reply is the answer."""
        return self._chat_raw(messages, max_tokens=max_tokens).get("content", "") or ""

    def _chat_raw(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
    ) -> Dict[str, Any]:
        """POST one completion and return the assistant message unchanged.

        Returning the whole message rather than its text is what lets the
        caller see `tool_calls`.
        """
        settings = self._config.llm_settings()

        if settings["provider"] == PROVIDER_OPENROUTER and not settings["api_key"]:
            raise LLMError(
                "No OpenRouter API key. Set it in the web UI, in config.json under "
                "llm.openrouter.api_key, or via the OPENROUTER_API_KEY environment variable."
            )

        url = f"{settings['endpoint'].rstrip('/')}/chat/completions"
        limit = max_tokens or settings["max_tokens"]
        if tools:
            # The spoken answer stays one sentence, but the turn that emits a
            # tool call needs room for the call itself. Squeezing it into the
            # 80-token speech budget truncates the JSON.
            limit = max(limit, _TOOL_CALL_TOKENS)
        if self._thinks_aloud:
            # This model has already shown it reasons before speaking, and
            # that reasoning is charged to the same budget as the speech.
            limit = max(limit, _THINKING_TOKENS)
        payload = {
            "model": settings["model"],
            "messages": messages,
            "temperature": settings["temperature"] if temperature is None else temperature,
            "max_tokens": limit,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        try:
            resp = self._client.post(
                url,
                json=payload,
                headers=self._headers(settings),
                timeout=settings["timeout"],
            )
            resp.raise_for_status()
            result = resp.json()
        except httpx.HTTPStatusError as exc:
            raise LLMError(f"{exc.response.status_code} from {url}: {_error_detail(exc.response)}") from exc
        except httpx.TimeoutException as exc:
            raise LLMError(f"Timed out after {settings['timeout']}s calling {url}") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Could not reach {url}: {exc}") from exc
        except ValueError as exc:
            raise LLMError(f"Malformed JSON from {url}: {exc}") from exc

        # OpenRouter surfaces upstream provider failures in a 200 body.
        if isinstance(result.get("error"), dict):
            raise LLMError(result["error"].get("message", "unknown provider error"))

        choices = result.get("choices") or []
        if not choices:
            raise LLMError("Response contained no choices")

        # A thinking model that ran out of budget mid-thought: it stopped on
        # `length`, it produced reasoning, and it produced no speech. That is
        # recoverable and worth one retry with room — but only once, so a
        # model that is simply silent cannot loop here.
        if _thought_past_the_budget(choices[0]) and not self._thinks_aloud:
            logger.info("%s reasons before answering; retrying with %d tokens",
                        settings["model"], _THINKING_TOKENS)
            self._thinks_aloud = True
            return self._chat_raw(
                messages, max_tokens=max_tokens, tools=tools,
                temperature=temperature)
        return choices[0].get("message") or {}

    def close(self) -> None:
        self._client.close()


def _thought_past_the_budget(choice: Dict[str, Any]) -> bool:
    """Whether the model spent its whole budget reasoning and never spoke.

    Three things together, because any one alone means something else: it
    stopped because it hit the ceiling, it produced reasoning, and it
    produced no content and no tool call. A model that stops on `length`
    mid-sentence has still said something; a model that returns nothing
    without reasoning is a different fault.

    Providers name the reasoning channel differently, hence the three keys.
    """
    message = choice.get("message") or {}
    if choice.get("finish_reason") != "length":
        return False
    if (message.get("content") or "").strip() or message.get("tool_calls"):
        return False
    return any((message.get(key) or "").strip()
               for key in ("reasoning", "reasoning_content", "thinking"))


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body.get("error"), dict):
        return body["error"].get("message", "")
    return str(body.get("error") or body)[:200]


def _clean_response(text: str) -> str:
    """Strip markdown/list artefacts so TTS never reads punctuation aloud."""
    text = (text or "").strip()
    # Some models still open with a bullet or a quote despite the system prompt.
    for prefix in ("- ", "* ", "> "):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text.strip().strip("`").strip()


def _parse_args(raw: Any) -> Dict[str, Any]:
    """Tool arguments arrive as a JSON string, and not always a valid one."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Unparseable tool arguments: %r", raw)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _looks_like_tool_rejection(exc: Exception) -> bool:
    """Distinguish "this model has no tools" from a real outage.

    Providers signal it inconsistently — a 400, a 404, or a 200 body with an
    error — so match on the wording rather than the status code.
    """
    text = str(exc).lower()
    markers = ("tool", "function call", "functions are not", "does not support")
    return any(m in text for m in markers)
