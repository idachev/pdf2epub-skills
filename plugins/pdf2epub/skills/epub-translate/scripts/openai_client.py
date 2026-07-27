"""Thin OpenAI-compatible chat client with retry/backoff.

Used by polish_epub_bg.py against the hosted BgGPT API (or a local vLLM/Ollama
server). Kept separate from pdf2epub's Gemini `common.py` so the translate path
never pulls in the `openai` package.
"""

from __future__ import annotations

import os
import re
import sys
import time

DEFAULT_BASE_URL = "https://api.bggpt.ai/v1"
DEFAULT_MODEL = "bggpt-gemma-3-27b-fp8"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# Set BGGPT_LOG_TOKENS=1 to print per-call usage on stderr (noisy on full-book runs).
LOG_TOKENS = os.environ.get("BGGPT_LOG_TOKENS", "").strip() in {"1", "true", "yes"}


class EmptyResponseError(RuntimeError):
    """The model returned an empty completion after retries."""


def get_client(api_key: str | None = None, base_url: str | None = None):
    """Build an OpenAI client pointed at BgGPT or a local OpenAI-compatible server."""
    key = api_key or os.environ.get("BGGPT_API_KEY")
    if not key:
        # Local servers often accept any non-empty key; still require an explicit one
        # so operators don't accidentally hit the hosted API without credentials.
        sys.exit(
            "error: BGGPT_API_KEY is not set (required for the BgGPT polish stage)"
        )
    url = (base_url or os.environ.get("BGGPT_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    from openai import OpenAI

    return OpenAI(api_key=key, base_url=url)


def strip_md_fences(text: str) -> str:
    m = re.fullmatch(r"```(?:markdown|md|json|text)?\s*\n(.*?)\n?```", text, flags=re.DOTALL)
    return m.group(1).strip() if m else text


def chat_complete(
    client,
    model: str,
    system_instruction: str,
    user_content: str,
    *,
    temperature: float = 0.2,
    max_tokens: int = 16384,
    max_retries: int = 8,
) -> str:
    """Chat Completions call with exponential backoff on rate limits / 5xx.

    Returns the assistant message text with markdown fences stripped. Raises
    EmptyResponseError if every attempt yields empty content; raises
    RuntimeError after exhausting retries on retryable API failures (callers
    can degrade per-unit rather than aborting the whole book).
    """
    delay = 5.0
    last_error = "unknown error"
    last_was_empty = False
    for attempt in range(max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": user_content},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            usage = getattr(resp, "usage", None)
            if LOG_TOKENS and usage is not None:
                prompt_t = getattr(usage, "prompt_tokens", None)
                completion_t = getattr(usage, "completion_tokens", None)
                if prompt_t is not None or completion_t is not None:
                    print(
                        f"  tokens: prompt={prompt_t} completion={completion_t}",
                        file=sys.stderr,
                    )
            choice = resp.choices[0] if resp.choices else None
            text = strip_md_fences(((choice.message.content if choice else None) or "").strip())
            if text:
                return text
            finish = getattr(choice, "finish_reason", None) if choice else None
            last_error = f"empty model response (finish_reason={finish})"
            last_was_empty = True
        except Exception as e:  # openai.APIError and network errors
            status = _status_code(e)
            if status is not None and status not in RETRYABLE_STATUS:
                raise
            # openai.RateLimitError / APIConnectionError often lack a clean status
            last_error = f"API error {status or type(e).__name__}: {e}"
            last_was_empty = False
        if attempt == max_retries:
            break
        print(f"  {last_error}; retrying in {delay:.0f}s", file=sys.stderr)
        time.sleep(delay)
        delay = min(delay * 2, 180)
    if last_was_empty:
        raise EmptyResponseError(last_error)
    raise RuntimeError(
        f"OpenAI-compatible call failed after {max_retries + 1} attempts ({last_error})"
    )


def _status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    resp = getattr(exc, "response", None)
    if resp is not None:
        val = getattr(resp, "status_code", None)
        if isinstance(val, int):
            return val
    return None


def estimate_max_tokens(user_content: str, mult: float = 1.8, hard_cap: int = 16384) -> int:
    """Rough output budget from input size; BgGPT hosted API caps at 16384."""
    # ~4 chars/token is a safe overestimate for mixed Cyrillic prose
    approx_in = max(64, len(user_content) // 3)
    return min(hard_cap, max(512, int(approx_in * mult)))
