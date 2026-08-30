"""Additive ML validation/research layer for Mandate Rescue.

This package trains and evaluates a real scikit-learn model on outcomes produced by
the existing rule-based simulation, and exposes those validated metrics + per-case
recovery-probability predictions. It is strictly additive: nothing in this package
influences the agent, scoring, or compliance decisions that actually drive the product.
"""
