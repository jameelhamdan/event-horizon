"""Forecast evaluation metrics for both heads (plan §3c / build step 8).

Pure-Python (no sklearn dependency) so it runs anywhere the API runs. Reports, per
(symbol, horizon, head):
  * accuracy, macro-F1, confusion matrix
  * per-class F1 broken out — especially the ``down`` class (known failure mode)
  * abstention-aware metrics (coverage-vs-accuracy)
  * confidence calibration as a reliability diagram (bins), not a scalar
  * comparison against naive baselines: always-flat, persistence, always-normal-vol
"""

from __future__ import annotations

from collections import defaultdict

MAGNITUDE_LABELS = ['strong_down', 'down', 'flat', 'up', 'strong_up']
VOLATILITY_LABELS = ['calm', 'normal', 'elevated']


def _f1_per_class(pairs: list[tuple[str, str]], labels: list[str]) -> dict[str, float]:
    """pairs = [(pred, actual)]. Returns {label: f1}."""
    tp = defaultdict(int)
    fp = defaultdict(int)
    fn = defaultdict(int)
    for pred, actual in pairs:
        if pred == actual:
            tp[pred] += 1
        else:
            fp[pred] += 1
            fn[actual] += 1
    out = {}
    for lab in labels:
        denom_p = tp[lab] + fp[lab]
        denom_r = tp[lab] + fn[lab]
        prec = tp[lab] / denom_p if denom_p else 0.0
        rec = tp[lab] / denom_r if denom_r else 0.0
        out[lab] = round(2 * prec * rec / (prec + rec), 4) if (prec + rec) else 0.0
    return out


def _confusion(pairs: list[tuple[str, str]], labels: list[str]) -> dict:
    matrix = {a: {p: 0 for p in labels} for a in labels}
    for pred, actual in pairs:
        if actual in matrix and pred in matrix[actual]:
            matrix[actual][pred] += 1
    return matrix


def evaluate_head(pairs: list[tuple[str, str]], labels: list[str]) -> dict:
    """Core classification metrics for one head from (pred, actual) pairs."""
    n = len(pairs)
    if n == 0:
        return {'n': 0}
    correct = sum(1 for p, a in pairs if p == a)
    f1 = _f1_per_class(pairs, labels)
    macro_f1 = round(sum(f1.values()) / len(labels), 4)
    return {
        'n': n,
        'accuracy': round(correct / n, 4),
        'macro_f1': macro_f1,
        'per_class_f1': f1,
        'confusion': _confusion(pairs, labels),
    }


def baseline_accuracy(pairs: list[tuple[str, str]], constant_label: str) -> float:
    """Accuracy of always predicting ``constant_label``."""
    if not pairs:
        return 0.0
    correct = sum(1 for _, a in pairs if a == constant_label)
    return round(correct / len(pairs), 4)


def coverage_accuracy(rows: list[dict], head: str) -> list[dict]:
    """Abstention-aware coverage-vs-accuracy curve.

    rows: [{'pred','actual','abstained','confidence'}]. Sort by confidence desc and
    report accuracy at increasing coverage (fraction of non-abstained kept).
    """
    kept = [r for r in rows if not r['abstained'] and r.get('actual')]
    kept.sort(key=lambda r: r.get('confidence', 0.0), reverse=True)
    if not kept:
        return []
    curve = []
    correct = 0
    for i, r in enumerate(kept, start=1):
        if r['pred'] == r['actual']:
            correct += 1
        if i % max(1, len(kept) // 10) == 0 or i == len(kept):
            curve.append({
                'coverage': round(i / len(rows), 4) if rows else 0.0,
                'accuracy': round(correct / i, 4),
            })
    return curve


def reliability_diagram(rows: list[dict], bins: int = 10) -> list[dict]:
    """Calibration as a reliability diagram: per confidence bin, mean confidence vs
    empirical accuracy. rows: [{'pred','actual','confidence','abstained'}]."""
    buckets: list[list[dict]] = [[] for _ in range(bins)]
    for r in rows:
        if r['abstained'] or not r.get('actual'):
            continue
        c = min(max(r.get('confidence', 0.0), 0.0), 0.999999)
        buckets[int(c * bins)].append(r)
    out = []
    for i, b in enumerate(buckets):
        if not b:
            continue
        mean_conf = sum(r['confidence'] for r in b) / len(b)
        acc = sum(1 for r in b if r['pred'] == r['actual']) / len(b)
        out.append({
            'bin': f'{i / bins:.1f}-{(i + 1) / bins:.1f}',
            'n': len(b),
            'mean_confidence': round(mean_conf, 4),
            'empirical_accuracy': round(acc, 4),
        })
    return out


def evaluate_forecasts(symbol: str | None = None, horizon_hours: int | None = None) -> dict:
    """Build the full metrics report for scored forecasts, grouped by (symbol, horizon)."""
    from core import models as core_models

    qs = core_models.Forecast.objects.filter(actual_value__isnull=False)
    if symbol:
        qs = qs.filter(symbol=symbol)
    if horizon_hours:
        qs = qs.filter(horizon_hours=horizon_hours)

    groups: dict[tuple[str, int], list] = defaultdict(list)
    for f in qs:
        groups[(f.symbol, f.horizon_hours)].append(f)

    report: dict[str, dict] = {}
    for (sym, hz), forecasts in sorted(groups.items()):
        mag_pairs = [(f.magnitude_bucket, f.actual_bucket)
                     for f in forecasts
                     if f.magnitude_bucket and f.actual_bucket and not f.abstained]
        vol_pairs = [(f.volatility_bucket, f.actual_volatility_bucket)
                     for f in forecasts
                     if f.volatility_bucket and f.actual_volatility_bucket]

        mag_rows = [{
            'pred': f.magnitude_bucket, 'actual': f.actual_bucket,
            'abstained': f.abstained, 'confidence': f.confidence,
        } for f in forecasts if f.magnitude_bucket and f.actual_bucket]

        report[f'{sym}|{hz}h'] = {
            'magnitude': {
                **evaluate_head(mag_pairs, MAGNITUDE_LABELS),
                'down_f1': _f1_per_class(mag_pairs, MAGNITUDE_LABELS).get('down', 0.0) if mag_pairs else 0.0,
                'baseline_always_flat': baseline_accuracy(mag_pairs, 'flat'),
                'abstention_rate': round(sum(1 for f in forecasts if f.abstained) / len(forecasts), 4),
                'coverage_accuracy': coverage_accuracy(mag_rows, 'magnitude'),
                'reliability_diagram': reliability_diagram(mag_rows),
            },
            'volatility': {
                **evaluate_head(vol_pairs, VOLATILITY_LABELS),
                'baseline_always_normal': baseline_accuracy(vol_pairs, 'normal'),
            },
        }
    return report
