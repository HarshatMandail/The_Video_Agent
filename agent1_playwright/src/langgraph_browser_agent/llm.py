import logging
from functools import lru_cache

from openai import AsyncAzureOpenAI

from .cache import get_cached_response, save_to_cache
from .config import (
    AZURE_OPENAI_ENDPOINT,
    AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_API_VERSION,
    AZURE_DEPLOYMENT_FULL,
    AZURE_DEPLOYMENT_MINI,
    MAX_COMPLETION_TOKENS,
    MAX_TOKENS_PER_REQUEST,
    LLM_TEMPERATURE,
    ENABLE_CACHE,
    ENABLE_BUDGET_CHECK,
)
from .cost_tracker import get_session, estimate_tokens

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_azure_client() -> AsyncAzureOpenAI:
    if not AZURE_OPENAI_ENDPOINT or not AZURE_OPENAI_API_KEY:
        raise ValueError("AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY must be set.")

    client = AsyncAzureOpenAI(
        azure_endpoint=AZURE_OPENAI_ENDPOINT,
        api_key=AZURE_OPENAI_API_KEY,
        api_version=AZURE_OPENAI_API_VERSION,
        max_retries=3,
        timeout=60.0,
    )
    logger.info(f"Azure OpenAI client initialized — {AZURE_OPENAI_ENDPOINT[:40]}...")
    return client


def _truncate_input(text: str, max_tokens: int) -> str:
    estimated = estimate_tokens(text)
    if estimated <= max_tokens:
        return text
    max_chars = max_tokens * 4
    logger.warning(f"Input truncated: {estimated} → ~{max_tokens} tokens")
    return text[:max_chars]


async def analyze_with_llm(
    system_prompt: str,
    user_message: str,
    use_mini: bool = False,
) -> str:
    """Send a prompt to Azure OpenAI with cache, budget, and truncation controls."""
    session = get_session()

    if ENABLE_BUDGET_CHECK and session.is_over_budget():
        raise RuntimeError(
            f"Session budget exceeded (${session.total_cost_usd:.4f}). "
            f"Increase MAX_COST_PER_SESSION_USD or reset session."
        )

    if ENABLE_CACHE:
        cached = get_cached_response(system_prompt, user_message)
        if cached:
            session.record_cache_hit()
            return cached

    input_budget = MAX_TOKENS_PER_REQUEST - MAX_COMPLETION_TOKENS - estimate_tokens(system_prompt)
    user_message = _truncate_input(user_message, max(input_budget, 1000))

    deployment = AZURE_DEPLOYMENT_MINI if use_mini else AZURE_DEPLOYMENT_FULL
    logger.info(f"Using model: {deployment}")

    client = get_azure_client()

    response = await client.chat.completions.create(
        model=deployment,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=LLM_TEMPERATURE,
        max_tokens=MAX_COMPLETION_TOKENS,
        response_format={"type": "json_object"},
    )

    content = response.choices[0].message.content or ""
    prompt_tokens = response.usage.prompt_tokens
    completion_tokens = response.usage.completion_tokens

    session.record_call(deployment, prompt_tokens, completion_tokens)

    logger.info(
        f"Tokens — In: {prompt_tokens} | Out: {completion_tokens} | "
        f"Total: {prompt_tokens + completion_tokens}"
    )

    if ENABLE_CACHE:
        save_to_cache(system_prompt, user_message, content, tokens_used=prompt_tokens + completion_tokens)

    return content
