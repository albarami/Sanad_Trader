#!/usr/bin/env python3
"""
LLM Client — Extracted from sanad_pipeline.py for async analysis queue

Provides:
- call_claude(system_prompt, user_message, model, max_tokens, stage, token_symbol)
- call_openai(system_prompt, user_message, model, max_tokens, stage, token_symbol)
- parse_json_failsafe(raw_text)

Both functions include:
- Direct API calls with timeout handling
- OpenRouter fallback on timeout/failure
- Cost tracking via cost_tracker.log_api_call()
"""

import os
import json
import requests
from pathlib import Path

# Load environment
BASE_DIR = Path(os.environ.get("SANAD_HOME", "/data/.openclaw/workspace/trading"))
CONFIG_DIR = BASE_DIR / "config"

try:
    from dotenv import load_dotenv
    load_dotenv(CONFIG_DIR / ".env")
except Exception:
    pass

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")


def _fallback_openrouter(system_prompt, user_message, model, max_tokens, stage, token_symbol):
    """OpenRouter fallback for both Claude and OpenAI."""
    if not OPENROUTER_API_KEY:
        print(f"    [OpenRouter key missing — cannot fallback]")
        return None

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": "https://sanad-trader.local",
        "X-Title": "Sanad Trader v3",
    }

    try:
        response = requests.post(
            url,
            headers=headers,
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
            },
            timeout=(10, 90)
        )
        response.raise_for_status()
        result = response.json()
        choices = result.get("choices", [])
        if choices:
            text = choices[0].get("message", {}).get("content", "")
            if text:
                print(f"    [OpenRouter fallback OK — {model}]")
                
                # Log cost (best-effort)
                usage = result.get("usage", {})
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)
                try:
                    from cost_tracker import log_api_call
                    log_api_call(f"openrouter/{model}", input_tokens, output_tokens, stage, token_symbol)
                except Exception as e:
                    print(f"    [Cost tracking failed: {e}]")
                
                return text
        return None
    except Exception as e:
        print(f"    [OpenRouter fallback FAILED: {e}]")
        return None


def call_claude(system_prompt, user_message, model="claude-haiku-4-5-20251001", max_tokens=2000, stage="unknown", token_symbol=""):
    """
    Call Claude via direct Anthropic API with OpenRouter fallback.
    
    Args:
        system_prompt: System prompt string
        user_message: User message string
        model: Model name (default: claude-haiku-4-5-20251001)
        max_tokens: Max tokens for response
        stage: Pipeline stage for cost tracking
        token_symbol: Trading token symbol for cost tracking
    
    Returns:
        Response text string, or None on failure
    """
    if not ANTHROPIC_API_KEY:
        print(f"    [Anthropic key missing — falling back to OpenRouter]")
        return _fallback_openrouter(system_prompt, user_message, f"anthropic/{model}", max_tokens, stage, token_symbol)

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
    }

    try:
        response = requests.post(
            url,
            headers=headers,
            json={
                "model": model,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_message}],
            },
            timeout=(10, 60)
        )
        response.raise_for_status()
        result = response.json()
        
        if result.get("content") and len(result["content"]) > 0:
            text = result["content"][0].get("text", "")
            if text:
                print(f"    [Claude direct OK — {model}]")
                
                # Log cost
                usage = result.get("usage", {})
                input_tokens = usage.get("input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)
                try:
                    from cost_tracker import log_api_call
                    log_api_call(model, input_tokens, output_tokens, stage, token_symbol)
                except Exception as e:
                    print(f"    [Cost tracking failed: {e}]")
                
                return text
        return None
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        print(f"    [Claude direct TIMEOUT/CONNECTION: {e}]")
        print(f"    [Falling back to OpenRouter Claude...]")
        return _fallback_openrouter(system_prompt, user_message, f"anthropic/{model}", max_tokens, stage, token_symbol)
    except Exception as e:
        print(f"    [Claude direct FAILED: {e}]")
        print(f"    [Falling back to OpenRouter Claude...]")
        return _fallback_openrouter(system_prompt, user_message, f"anthropic/{model}", max_tokens, stage, token_symbol)


def call_openai(system_prompt, user_message, model="gpt-5.2", max_tokens=2000, stage="unknown", token_symbol=""):
    """
    Call OpenAI API directly with OpenRouter fallback.
    
    Args:
        system_prompt: System prompt string
        user_message: User message string
        model: Model name (default: gpt-5.2)
        max_tokens: Max tokens for response
        stage: Pipeline stage for cost tracking
        token_symbol: Trading token symbol for cost tracking
    
    Returns:
        Response text string, or None on failure
    """
    if not OPENAI_API_KEY:
        print(f"    [OpenAI key missing — falling back to OpenRouter]")
        return _fallback_openrouter(system_prompt, user_message, f"openai/{model}", max_tokens, stage, token_symbol)

    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENAI_API_KEY}",
    }

    try:
        response = requests.post(
            url,
            headers=headers,
            json={
                "model": model,
                "max_completion_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
            },
            timeout=(10, 60)
        )
        response.raise_for_status()
        result = response.json()
        choices = result.get("choices", [])
        if choices:
            text = choices[0].get("message", {}).get("content", "")
            if text:
                print(f"    [OpenAI direct OK — {model}]")
                
                # Log cost
                usage = result.get("usage", {})
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)
                try:
                    from cost_tracker import log_api_call
                    log_api_call(model, input_tokens, output_tokens, stage, token_symbol)
                except Exception as e:
                    print(f"    [Cost tracking failed: {e}]")
                
                return text
        return None
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        print(f"    [OpenAI direct TIMEOUT/CONNECTION: {e}]")
        print(f"    [Falling back to OpenRouter GPT...]")
        return _fallback_openrouter(system_prompt, user_message, f"openai/{model}", max_tokens, stage, token_symbol)
    except Exception as e:
        print(f"    [OpenAI direct FAILED: {e}]")
        print(f"    [Falling back to OpenRouter GPT...]")
        return _fallback_openrouter(system_prompt, user_message, f"openai/{model}", max_tokens, stage, token_symbol)


def parse_json_failsafe(raw_text):
    """
    Parse JSON from LLM response with failsafe fallback.
    
    Handles:
    - Markdown code blocks (```json ... ```)
    - Leading/trailing whitespace
    - Multiple JSON objects (takes first valid one)
    
    Returns:
        dict: Parsed JSON, or None if parsing fails
    """
    if not raw_text:
        return None
    
    # Strip markdown code blocks
    text = raw_text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    
    # Try finding first { ... } block
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end+1])
        except json.JSONDecodeError:
            pass
    
    print(f"    [JSON parse FAILED — raw text length: {len(raw_text)}]")
    return None
