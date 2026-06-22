"""Forecasting package.

The prediction layer (Forecast model, feature assembly, scoring) has been removed
and will be reimplemented from scratch. What remains is the deterministic
event→symbol router (:mod:`services.forecasting.routing`), which is reusable
feature-engineering still consumed by the live event pipeline
(``Event.affected_indicators``).
"""
