"""Async chat client for any OpenAI-compatible server (vLLM by default).

Adds what the agents need on top of the raw API: tool-call parsing with
argument repair, per-call thinking control (Qwen ``enable_thinking``), JSON-schema
constrained output, retries, a concurrency cap and token accounting.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from . import config

log = logging.getLogger(__name__)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str
    parse_error: str | None = None


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall]
    reasoning: str | None
    finish_reason: str | None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency: float = 0.0

    def assistant_message(self) -> dict:
        """The message to append to the conversation history."""
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.raw_arguments}}
                for tc in self.tool_calls
            ]
        if self.reasoning:
            msg["reasoning_content"] = self.reasoning
        return msg


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    by_agent: dict[str, dict[str, float]] = field(default_factory=dict)

    def add(self, agent: str, r: LLMResponse) -> None:
        self.calls += 1
        self.prompt_tokens += r.prompt_tokens
        self.completion_tokens += r.completion_tokens
        self.seconds += r.latency
        a = self.by_agent.setdefault(agent, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "seconds": 0.0})
        a["calls"] += 1
        a["prompt_tokens"] += r.prompt_tokens
        a["completion_tokens"] += r.completion_tokens
        a["seconds"] += r.latency


# Usage of the current run (set by the orchestrator); asyncio tasks inherit it, so concurrent runs
# each account their own tokens.
run_usage: contextvars.ContextVar[Usage | None] = contextvars.ContextVar("run_usage", default=None)


def _repair_json(s: str) -> dict:
    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {"value": v}
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", s, re.S)
    if m:
        v = json.loads(m.group(0))  # may raise; caller handles
        return v if isinstance(v, dict) else {"value": v}
    raise json.JSONDecodeError("no JSON object found", s, 0)


class LLM:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None,
                 max_concurrency: int = 12, timeout: float | None = None):
        self.model = model or config.LLM_MODEL
        self.client = AsyncOpenAI(
            base_url=base_url or config.LLM_BASE_URL,
            api_key=api_key or config.LLM_API_KEY or "none",
            timeout=timeout or config.LLM_TIMEOUT,
            max_retries=0,  # retries handled here so they are visible in logs
        )
        self.sem = asyncio.Semaphore(max_concurrency)
        self.usage = Usage()

    async def chat(
        self,
        messages: list[dict],
        *,
        agent: str = "llm",
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        thinking: bool = False,
        temperature: float = 0.3,
        top_p: float = 0.9,
        max_tokens: int = 4096,
        json_schema: dict | None = None,
        schema_name: str = "output",
        presence_penalty: float = 0.0,
        retries: int = 3,
        on_delta: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        """``on_delta`` streams the text as it is generated (plain-text calls only)."""
        if on_delta is not None and not tools and json_schema is None:
            return await self._chat_stream(messages, agent=agent, thinking=thinking, temperature=temperature,
                                           top_p=top_p, max_tokens=max_tokens, presence_penalty=presence_penalty,
                                           retries=retries, on_delta=on_delta)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
            "presence_penalty": presence_penalty,
        }
        extra: dict[str, Any] = {"top_k": 20}
        if config.LLM_SUPPORTS_THINKING_FLAG:
            extra["chat_template_kwargs"] = {"enable_thinking": thinking}
        kwargs["extra_body"] = extra
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
        if json_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": json_schema, "strict": True},
            }

        last_err: Exception | None = None
        for attempt in range(retries):
            t0 = time.time()
            try:
                async with self.sem:
                    resp = await self.client.chat.completions.create(**kwargs)
                break
            except (APIConnectionError, APITimeoutError) as e:
                last_err = e
            except APIStatusError as e:
                if e.status_code < 500 and e.status_code != 429:
                    raise
                last_err = e
            wait = 2.0 * (attempt + 1)
            log.warning("LLM call failed (%s), retrying in %.0fs", last_err, wait)
            await asyncio.sleep(wait)
        else:
            raise RuntimeError(f"LLM unavailable after {retries} attempts: {last_err}")

        latency = time.time() - t0
        choice = resp.choices[0]
        msg = choice.message
        extra_fields = getattr(msg, "model_extra", None) or {}
        reasoning = getattr(msg, "reasoning_content", None) or extra_fields.get("reasoning_content") \
            or extra_fields.get("reasoning")
        calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            raw = tc.function.arguments or "{}"
            try:
                args, err = _repair_json(raw), None
            except (json.JSONDecodeError, ValueError) as e:
                args, err = {}, f"could not parse arguments as JSON: {e}"
            calls.append(ToolCall(id=tc.id or f"call_{len(calls)}", name=tc.function.name, arguments=args,
                                  raw_arguments=raw, parse_error=err))
        content = (msg.content or "").strip()
        if not thinking and not content and not calls and reasoning:
            # With thinking disabled the reasoning parser occasionally files the whole reply under
            # "reasoning", leaving content empty: it is the answer.
            content, reasoning = reasoning.strip(), None
        out = LLMResponse(
            content=content,
            tool_calls=calls,
            reasoning=reasoning,
            finish_reason=choice.finish_reason,
            prompt_tokens=getattr(resp.usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(resp.usage, "completion_tokens", 0) or 0,
            latency=latency,
        )
        self.usage.add(agent, out)
        if (u := run_usage.get()) is not None:
            u.add(agent, out)
        return out

    async def _chat_stream(self, messages: list[dict], *, agent: str, thinking: bool, temperature: float,
                           top_p: float, max_tokens: int, presence_penalty: float, retries: int,
                           on_delta: Callable[[str], None]) -> LLMResponse:
        extra: dict[str, Any] = {"top_k": 20}
        if config.LLM_SUPPORTS_THINKING_FLAG:
            extra["chat_template_kwargs"] = {"enable_thinking": thinking}
        last_err: Exception | None = None
        for attempt in range(retries):
            t0 = time.time()
            parts: list[str] = []
            usage = None
            finish = None
            try:
                async with self.sem:
                    stream = await self.client.chat.completions.create(
                        model=self.model, messages=messages, temperature=temperature, top_p=top_p,
                        max_tokens=max_tokens, presence_penalty=presence_penalty, extra_body=extra,
                        stream=True, stream_options={"include_usage": True})
                    async for chunk in stream:
                        if chunk.usage is not None:
                            usage = chunk.usage
                        if not chunk.choices:
                            continue
                        delta = chunk.choices[0].delta.content or ""
                        finish = chunk.choices[0].finish_reason or finish
                        if delta:
                            parts.append(delta)
                            on_delta(delta)
                break
            except (APIConnectionError, APITimeoutError, APIStatusError) as e:
                if isinstance(e, APIStatusError) and e.status_code < 500 and e.status_code != 429:
                    raise
                last_err = e
                if parts:  # partial output already shown; do not duplicate it on retry
                    break
                await asyncio.sleep(2.0 * (attempt + 1))
        else:
            raise RuntimeError(f"LLM unavailable after {retries} attempts: {last_err}")
        out = LLMResponse(content="".join(parts).strip(), tool_calls=[], reasoning=None, finish_reason=finish,
                          prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                          completion_tokens=getattr(usage, "completion_tokens", 0) or 0, latency=time.time() - t0)
        self.usage.add(agent, out)
        if (u := run_usage.get()) is not None:
            u.add(agent, out)
        return out

    async def chat_json(self, messages: list[dict], schema: dict, *, agent: str = "llm", schema_name: str = "output",
                        **kw) -> dict:
        """Constrained JSON output; falls back to tolerant parsing if the server ignores the schema."""
        r = await self.chat(messages, agent=agent, json_schema=schema, schema_name=schema_name, **kw)
        try:
            return _repair_json(r.content or r.reasoning or "")
        except (json.JSONDecodeError, ValueError):
            fix = messages + [
                {"role": "assistant", "content": r.content},
                {"role": "user", "content": "That was not valid JSON. Reply with only the JSON object."},
            ]
            r2 = await self.chat(fix, agent=agent, json_schema=schema, schema_name=schema_name, **kw)
            return _repair_json(r2.content)
