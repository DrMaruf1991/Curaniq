"""
CURANIQ - Multi-LLM Client with Failover (L6-3)
Environment-driven. No hardcoded API keys, models, or endpoints.

Copy to: curaniq/layers/L6_security/llm_client.py

Architecture (from v3.6 spec):
  Primary:   Claude (Anthropic) — ANTHROPIC_API_KEY
  Failover:  GPT-4o (OpenAI)   — OPENAI_API_KEY
  Tertiary:  Gemini (Google)    — GOOGLE_API_KEY
  
  All config from environment. If no keys set, returns None
  and generator falls back to mock (dev mode).

  LLM model names from environment too — never hardcoded.
  Default models are sensible but overridable.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Cost monitoring — integrated, not separate
try:
    from curaniq.layers.L10_testing.cost_monitor import LLMCostMonitor
    _COST_MONITOR_AVAILABLE = True
except ImportError:
    _COST_MONITOR_AVAILABLE = False


@dataclass
class LLMResponse:
    """Standardized response from any LLM provider."""
    text: str
    provider: str           # 'anthropic', 'openai', 'google'
    model: str              # Actual model used
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0
    success: bool = True
    error: Optional[str] = None


@dataclass
class LLMProviderConfig:
    """Configuration for a single LLM provider."""
    name: str
    api_key_env: str        # Environment variable name for API key
    model_env: str          # Environment variable for model override
    default_model: str      # Default model if env not set
    max_tokens: int = 4096
    temperature: float = 0.0  # Deterministic for clinical use


class MultiLLMClient:
    """
    L6-3: Multi-LLM Orchestration Layer.
    
    Failover chain: tries providers in order.
    If primary fails, automatically falls to next.
    All configuration from environment variables.
    
    Usage:
        client = MultiLLMClient.from_environment()
        if client:
            response = client.generate(system_prompt, user_prompt)
    """

    def __init__(self, providers: list[tuple[LLMProviderConfig, Any]]):
        """
        Args:
            providers: List of (config, client_instance) tuples in priority order.
        """
        self._providers = providers
        self._cost_monitor = LLMCostMonitor() if _COST_MONITOR_AVAILABLE else None

    @classmethod
    def from_environment(cls) -> Optional["MultiLLMClient"]:
        """
        Build client from environment variables.
        Returns None if no API keys are configured (dev mode).
        
        Environment variables:
          ANTHROPIC_API_KEY     — enables Claude
          CURANIQ_ANTHROPIC_MODEL  — override model (default: claude-sonnet-4-20250514)
          OPENAI_API_KEY        — enables GPT-4o failover
          CURANIQ_OPENAI_MODEL     — override model (default: gpt-4o)
          GOOGLE_API_KEY        — enables Gemini failover
          CURANIQ_GOOGLE_MODEL     — override model (default: gemini-1.5-pro)
        """
        providers: list[tuple[LLMProviderConfig, Any]] = []

        # Provider 1: Anthropic Claude
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
        if anthropic_key:
            try:
                import anthropic
                config = LLMProviderConfig(
                    name="anthropic",
                    api_key_env="ANTHROPIC_API_KEY",
                    model_env="CURANIQ_ANTHROPIC_MODEL",
                    default_model="claude-sonnet-4-20250514",
                )
                client = anthropic.Anthropic(api_key=anthropic_key)
                providers.append((config, client))
                logger.info("LLM provider: Anthropic Claude configured")
            except ImportError:
                logger.warning("anthropic package not installed. pip install anthropic")

        # Provider 2: OpenAI GPT-4o
        openai_key = os.environ.get("OPENAI_API_KEY")
        if openai_key:
            try:
                import openai
                config = LLMProviderConfig(
                    name="openai",
                    api_key_env="OPENAI_API_KEY",
                    model_env="CURANIQ_OPENAI_MODEL",
                    default_model="gpt-4o",
                )
                client = openai.OpenAI(api_key=openai_key)
                providers.append((config, client))
                logger.info("LLM provider: OpenAI GPT-4o configured")
            except ImportError:
                logger.warning("openai package not installed. pip install openai")

        # Provider 3: Google Gemini
        google_key = os.environ.get("GOOGLE_API_KEY")
        if google_key:
            try:
                import google.generativeai as genai
                config = LLMProviderConfig(
                    name="google",
                    api_key_env="GOOGLE_API_KEY",
                    model_env="CURANIQ_GOOGLE_MODEL",
                    default_model="gemini-1.5-pro",
                )
                genai.configure(api_key=google_key)
                providers.append((config, genai))
                logger.info("LLM provider: Google Gemini configured")
            except ImportError:
                logger.warning("google-generativeai not installed.")

        if not providers:
            logger.info("No LLM API keys configured. Generator will use mock mode.")
            return None

        logger.info(f"LLM failover chain: {[p[0].name for p in providers]}")
        return cls(providers)

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """
        Generate a response using failover chain.
        Tries each provider in order. Returns first successful response.
        """
        # L10-10: Check budget BEFORE calling LLM
        if self._cost_monitor:
            budget = self._cost_monitor.check_budget()
            if budget.exceeded:
                return LLMResponse(
                    text="",
                    provider="none",
                    model="none",
                    success=False,
                    error=f"BUDGET EXCEEDED: {budget.alert_message} Query refused to control costs.",
                )

        errors: list[str] = []

        for config, client in self._providers:
            model = os.environ.get(config.model_env, config.default_model)

            try:
                start = time.perf_counter()

                if config.name == "anthropic":
                    response = self._call_anthropic(
                        client, model, system_prompt, user_prompt,
                        max_tokens, temperature,
                    )
                elif config.name == "openai":
                    response = self._call_openai(
                        client, model, system_prompt, user_prompt,
                        max_tokens, temperature,
                    )
                elif config.name == "google":
                    response = self._call_google(
                        client, model, system_prompt, user_prompt,
                        max_tokens, temperature,
                    )
                else:
                    continue

                response.latency_ms = (time.perf_counter() - start) * 1000

                # L10-10: Auto-record cost
                if self._cost_monitor:
                    self._cost_monitor.after_call(
                        provider=config.name,
                        model=model,
                        input_tokens=response.input_tokens,
                        output_tokens=response.output_tokens,
                        latency_ms=response.latency_ms,
                    )

                logger.info(
                    f"LLM response from {config.name}/{model} "
                    f"in {response.latency_ms:.0f}ms "
                    f"({response.input_tokens}+{response.output_tokens} tokens)"
                )
                return response

            except Exception as e:
                error_msg = f"{config.name}/{model}: {type(e).__name__}: {e}"
                errors.append(error_msg)
                logger.warning(f"LLM failover — {error_msg}")
                continue

        # All providers failed
        return LLMResponse(
            text="",
            provider="none",
            model="none",
            success=False,
            error=f"All LLM providers failed: {'; '.join(errors)}",
        )

    def _call_anthropic(self, client, model, system, user, max_tokens, temp):
        """Call Anthropic Claude API."""
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temp,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = response.content[0].text
        return LLMResponse(
            text=text,
            provider="anthropic",
            model=model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    def _call_openai(self, client, model, system, user, max_tokens, temp):
        """Call OpenAI API."""
        response = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temp,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        text = response.choices[0].message.content
        return LLMResponse(
            text=text,
            provider="openai",
            model=model,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
        )

    def _call_google(self, client, model, system, user, max_tokens, temp):
        """Call Google Gemini API."""
        model_obj = client.GenerativeModel(
            model_name=model,
            system_instruction=system,
        )
        response = model_obj.generate_content(
            user,
            generation_config={"max_output_tokens": max_tokens, "temperature": temp},
        )
        text = response.text
        return LLMResponse(
            text=text,
            provider="google",
            model=model,
            # Gemini token counting varies by SDK version
            input_tokens=getattr(response.usage_metadata, "prompt_token_count", 0) if hasattr(response, "usage_metadata") else 0,
            output_tokens=getattr(response.usage_metadata, "candidates_token_count", 0) if hasattr(response, "usage_metadata") else 0,
        )

    @property
    def cost_summary(self) -> dict:
        """Current month cost summary."""
        if self._cost_monitor:
            return self._cost_monitor.get_summary()
        return {}

    @property
    def provider_names(self) -> list[str]:
        """List of configured provider names."""
        return [p[0].name for p in self._providers]

    @property
    def primary_provider(self) -> Optional[str]:
        """Name of the primary (first) provider."""
        return self._providers[0][0].name if self._providers else None
