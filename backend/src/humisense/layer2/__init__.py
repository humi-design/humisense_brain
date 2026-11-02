"""Layer 2 — Data Ingestion & Normalization Engine.

Layer 2 turns captured source records into trustworthy, traceable, tenant-safe canonical
records. It preserves provenance, preserves the original source payload, normalizes with
explicit and recorded transformations, deduplicates idempotently, and publishes downstream
through a transactional outbox.

It does **not** reason, summarize, detect anomalies, or take action. Ingested content is
data, never instructions.
"""
