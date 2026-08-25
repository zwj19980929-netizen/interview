# ADR-0001: Separate provider connections from model configurations

## Status

Accepted — 2026-08-25

## Context

A provider account can expose LLM, embedding, TTS, STT and avatar models. The previous `ModelProviderConfig` combined API credentials, connection settings, a selected model and route data. This duplicated credentials, made multi-model accounts awkward, and forced the frontend to understand vendor-specific fields.

## Decision

Use four explicit concepts:

- `ProviderPluginDefinition` declares backend-owned connection, credential and model forms plus the runtime adapter.
- `ProviderConnection` stores one organization's vendor connection and secret reference.
- `ModelConfiguration` stores one concrete model, its type, vendor settings, unified defaults, capabilities and health.
- `ModelRoute` references `model_configuration_id` only.

The admin frontend renders the schemas returned by the backend. Runtime adapters continue to receive a single composed `ProviderContext`, so business services remain vendor-independent.

## Consequences

One connection can safely serve several model configurations without credential duplication. Adding vendor fields no longer requires frontend branches. Routes cannot drift from their model's provider or settings. Existing provider configurations and route targets require a one-way migration; the old API and storage collection are removed instead of dual-written.
