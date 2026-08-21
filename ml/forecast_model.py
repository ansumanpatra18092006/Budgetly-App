"""
ml/forecast_model.py

Expense forecasting engine.

Architecture (see predict_next_month_comprehensive):

    historical total expenses
            |
            v
    identify historical recurring component  (if supplied by caller)
            |
            v
    historical discretionary expense = historical total - historical recurring
            |
            v
    forecast discretionary expense (method depends on how much history exists)
            +
    expected future recurring commitments (for the target month)
            |
            v
    TOTAL FORECAST

The key correctness property this file guarantees: historical monthly
totals already include recurring expenses that actually happened, so the
model NEVER does `historical_total_forecast + recurring_monthly_burden`.
Instead it forecasts the *discretionary residual* of history and adds
recurring back in exactly once, for the month actually being forecast.

`predict_next_month(expenses)` is preserved unchanged in signature and
return type for backward compatibility with existing callers (e.g.
decision_engine.py). `predict_next_month_comprehensive(...)` is the new,
richer entry point.
"""

from decimal import Decimal
import math
import numpy as np
from sklearn.linear_model import LinearRegression


# ============================================================
# Numeric safety helpers
# ============================================================

def _to_float(x, default=0.0):
    """Best-effort conversion to a finite float. Handles None, Decimal
    (e.g. from PostgreSQL), ints, numpy scalars, numeric strings. Returns
    `default` for anything that can't be safely converted, and for
    NaN/inf (never lets NaN/inf leak into the pipeline)."""
    if x is None:
        return default
    try:
        if isinstance(x, Decimal):
            x = float(x)
        val = float(x)
    except (TypeError, ValueError):
        return default
    if math.isnan(val) or math.isinf(val):
        return default
    return val


def _clean_series(series):
    """Converts a raw history list (which may contain None, Decimal,
    numeric strings, NaN, inf) into a clean list of finite floats.
    Invalid/None entries are DROPPED rather than coerced to 0, so a
    missing month doesn't masquerade as a zero-spend month and distort
    the trend."""
    if not series:
        return []
    out = []
    for v in series:
        if v is None:
            continue
        f = _to_float(v, default=None)
        if f is None:
            continue
        out.append(f)
    return out


def _mean(lst):
    return sum(lst) / len(lst) if lst else 0.0


def _std(lst):
    if len(lst) < 2:
        return 0.0
    m = _mean(lst)
    return math.sqrt(_mean([(v - m) ** 2 for v in lst]))


def _clamp_nonneg(x):
    """Clamp to zero minimum. Never returns NaN/inf."""
    return max(0.0, _to_float(x, 0.0))


def _safe_round(x):
    x = _clamp_nonneg(x)
    return round(x)


# ============================================================
# Core trend fitting
# ============================================================

def _linear_forecast(series):
    """
    Fits a simple linear trend (month index -> value) and returns
    (predicted_next_value, slope). This is intentionally a plain OLS fit
    on the index - not a sophisticated time-series model. It is used as
    a building block for the tiered methods below, and as the
    one-step-ahead predictor inside the rolling-origin backtest.
    """
    n = len(series)
    if n == 0:
        return 0.0, 0.0
    if n == 1:
        return series[0], 0.0

    X = np.arange(n).reshape(-1, 1)
    y = np.array(series, dtype=float)

    model = LinearRegression()
    model.fit(X, y)

    pred = float(model.predict([[n]])[0])
    slope = float(model.coef_[0])

    if not math.isfinite(pred):
        pred = _mean(series)
    if not math.isfinite(slope):
        slope = 0.0
    return pred, slope


# ============================================================
# Data-depth-tiered discretionary forecasting
#
#   < 3 months   -> low-data fallback (burn rate / recent average blend)
#   3-5 months   -> smoothed trend (regression blended with moving avg)
#   6-11 months  -> trend + recent weighting
#   12+ months   -> trend, with an optional seasonal adjustment IF the
#                   caller actually supplied calendar month labels
#                   (month_indices) - we never invent seasonality from
#                   data that doesn't carry real calendar information.
# ============================================================

def _forecast_low_data(series, current_expense, days_passed, days_in_month):
    burn_projection = None
    if days_passed and days_passed > 0:
        burn_projection = (_to_float(current_expense) / days_passed) * days_in_month

    recent_avg = _mean(series[-3:]) if series else None

    if burn_projection is not None and recent_avg is not None:
        forecast = 0.5 * burn_projection + 0.5 * recent_avg
    elif burn_projection is not None:
        forecast = burn_projection
    elif recent_avg is not None:
        forecast = recent_avg
    else:
        forecast = 0.0
    return forecast, 0.0, "low_data_fallback"


def _forecast_smoothed_trend(series):
    reg_pred, slope = _linear_forecast(series)
    ma = _mean(series[-3:])
    forecast = 0.5 * reg_pred + 0.5 * ma
    return forecast, slope, "smoothed_trend"


def _forecast_trend_recent_weighted(series):
    reg_pred, slope = _linear_forecast(series)
    recent = _mean(series[-3:])
    forecast = 0.6 * reg_pred + 0.4 * recent
    return forecast, slope, "trend_recent_weighted"


def _next_month_number(m):
    return (int(m) % 12) + 1


def _forecast_seasonal_or_trend(series, month_indices):
    """
    Attempts a seasonal adjustment ONLY if the caller supplied real
    calendar month labels (`month_indices`, 1-12) aligned to `series`
    and there's at least a year of data. Without real month labels we
    have no way to know which historical points correspond to the
    target month, so we do not fabricate a seasonal component - we fall
    back to trend + recent weighting instead.
    """
    if month_indices and len(month_indices) == len(series) and len(series) >= 12:
        target_month = _next_month_number(month_indices[-1])
        seasonal_values = [v for v, m in zip(series, month_indices) if int(m) == target_month]
        overall_avg = _mean(series)
        if seasonal_values and overall_avg > 0:
            seasonal_avg = _mean(seasonal_values)
            seasonal_index = seasonal_avg / overall_avg
            reg_pred, slope = _linear_forecast(series)
            forecast = 0.5 * reg_pred + 0.5 * (overall_avg * seasonal_index)
            return forecast, slope, "trend_seasonal", True

    forecast, slope, _ = _forecast_trend_recent_weighted(series)
    return forecast, slope, "trend_recent_weighted", False


def _select_forecast_method(series, current_expense, days_passed, days_in_month, month_indices):
    n = len(series)
    if n < 3:
        forecast, slope, method = _forecast_low_data(series, current_expense, days_passed, days_in_month)
        return forecast, slope, method, False
    if n <= 5:
        forecast, slope, method = _forecast_smoothed_trend(series)
        return forecast, slope, method, False
    if n <= 11:
        forecast, slope, method = _forecast_trend_recent_weighted(series)
        return forecast, slope, method, False
    forecast, slope, method, seasonal_used = _forecast_seasonal_or_trend(series, month_indices)
    return forecast, slope, method, seasonal_used


# ============================================================
# Calendar alignment
#
# `history`, `historical_recurring` (when supplied as a list), and
# `month_indices` only mean anything relative to each other if position i
# in each array refers to the SAME real calendar month. A naive
# clean-then-zip (drop invalid entries from `history`, then match
# `historical_recurring`/`month_indices` by position or by "align from
# the end") silently reassigns later months' recurring/seasonal data to
# earlier months whenever a month is missing from the middle of the
# series - e.g. Jan, Feb, [Mar missing], Apr becomes an unlabeled
# 3-point sequence that can accidentally pair April's total with March's
# recurring component.
#
# `_align_calendar_series` fixes this by treating position i in the RAW
# (uncleaned) arrays as the source of truth for "this is month X", and
# dropping position i from every aligned array together whenever
# `history[i]` is missing/invalid - never just from `history` alone.
# ============================================================

def _align_calendar_series(history, historical_recurring=None, month_indices=None):
    """
    Cleans `history` while preserving positional (calendar) alignment
    with `historical_recurring` (only when supplied as a list/tuple -
    single numbers have no position to misalign) and `month_indices`.

    Returns a dict:
      history_clean             : list[float] - cleaned history, missing/
                                   invalid entries dropped.
      recurring_clean           : list[float] | None - `historical_recurring`
                                   re-indexed to line up 1:1 with
                                   `history_clean` (same positions dropped
                                   together). None if `historical_recurring`
                                   wasn't a list, or if its RAW length didn't
                                   match the RAW `history` length (in which
                                   case we cannot trust ANY positional
                                   correspondence, so alignment is refused
                                   rather than guessed).
      month_indices_clean       : list[int] | None - same idea, for
                                   `month_indices`.
      recurring_list_misaligned : bool - True if `historical_recurring` was
                                   a list but its raw length didn't match
                                   `history`'s raw length (separation via
                                   this list should be disabled by the
                                   caller, not padded/truncated).
      month_indices_misaligned  : bool - same idea, for `month_indices`.
    """
    raw_history = history or []
    n_raw = len(raw_history)

    recurring_is_list = isinstance(historical_recurring, (list, tuple))
    recurring_list_misaligned = recurring_is_list and len(historical_recurring) != n_raw
    use_recurring_list = recurring_is_list and not recurring_list_misaligned

    month_indices_given = bool(month_indices)
    month_indices_misaligned = month_indices_given and len(month_indices) != n_raw
    use_month_indices = month_indices_given and not month_indices_misaligned

    history_clean = []
    recurring_clean = [] if use_recurring_list else None
    month_indices_clean = [] if use_month_indices else None

    for i in range(n_raw):
        raw_v = raw_history[i]
        v = None if raw_v is None else _to_float(raw_v, default=None)
        if v is None:
            # Missing/invalid month: drop this position from EVERY
            # aligned array together, never from `history` alone. Do
            # NOT treat this as a zero-spend month.
            continue
        history_clean.append(v)
        if use_recurring_list:
            recurring_clean.append(_to_float(historical_recurring[i], default=0.0))
        if use_month_indices:
            month_indices_clean.append(month_indices[i])

    return {
        "history_clean": history_clean,
        "recurring_clean": recurring_clean,
        "month_indices_clean": month_indices_clean,
        "recurring_list_misaligned": recurring_list_misaligned,
        "month_indices_misaligned": month_indices_misaligned,
    }


# ============================================================
# Historical recurring / discretionary separation
# ============================================================

def _separate_recurring(history_clean, historical_recurring, recurring_clean_aligned=None):
    """
    Given ALREADY-CLEANED `history_clean` (missing/invalid months already
    dropped - see `_align_calendar_series`), separates it into
    discretionary vs recurring components.

    `historical_recurring`:
      - None             -> separation unavailable; discretionary == total.
                             recurring_input_mode = "unavailable"
      - a single number  -> treated as a CONSTANT recurring load applied
                             to every historical month. No alignment risk
                             (a constant has no position to misalign), but
                             it IS an approximation of what is usually a
                             fluctuating real signal, so confidence/backtest
                             derived from this mode is capped elsewhere.
                             recurring_input_mode = "monthly_constant"
      - a list/tuple     -> ONLY used via `recurring_clean_aligned`, which
                             must already be position-matched 1:1 to
                             `history_clean` (produced by
                             `_align_calendar_series`, which drops the same
                             positions from both arrays in lockstep so a
                             missing month can never shift the
                             correspondence). If the caller can't supply a
                             reliably aligned list (see
                             `recurring_list_misaligned`), it must pass
                             historical_recurring=None instead of a raw
                             list here - this function refuses to guess a
                             positional correspondence (pad/truncate) that
                             the earlier padding-based approach used to do
                             silently.
                             recurring_input_mode = "month_by_month"

    Returns (discretionary_series, recurring_series, separation_available,
    partial_coverage, recurring_input_mode). `partial_coverage` is always
    False now - alignment is enforced by construction (via
    `_align_calendar_series`) rather than padded/truncated after the fact,
    so there is no longer a "partially covered" state; a list either
    reliably covers every remaining month, or separation is unavailable.
    """
    n = len(history_clean)
    if historical_recurring is None:
        return list(history_clean), [0.0] * n, False, False, "unavailable"

    if isinstance(historical_recurring, (list, tuple)):
        if recurring_clean_aligned is None or len(recurring_clean_aligned) != n:
            # Defensive fallback: a raw list was passed without going
            # through _align_calendar_series (or alignment failed).
            # Rather than guessing a positional correspondence, disable
            # separation entirely - "explicitly disable instead of
            # silently shift".
            return list(history_clean), [0.0] * n, False, False, "unavailable"
        recurring_series = list(recurring_clean_aligned)
        discretionary = [max(0.0, t - r) for t, r in zip(history_clean, recurring_series)]
        return discretionary, recurring_series, True, False, "month_by_month"

    r = _clamp_nonneg(historical_recurring)
    recurring_series = [r] * n
    discretionary = [max(0.0, t - r) for t in history_clean]
    return discretionary, recurring_series, True, False, "monthly_constant"


# ============================================================
# Rolling-origin backtest (approximates the PRODUCTION method)
# ============================================================

def _rolling_origin_backtest(discretionary_series, recurring_series, actual_totals,
                              recurring_input_mode, recurring_partial_coverage,
                              month_indices_clean=None):
    """
    Walk-forward one-step validation that mirrors the production
    forecasting pipeline rather than always using a plain linear fit: for
    each cut point i, it runs the SAME data-depth tier selection the
    production model uses (`_select_forecast_method`) on the training
    window `discretionary_series[:i]`, predicts the next discretionary
    value, adds back the recurring component for the held-out month
    (reconstructing a total prediction), and compares against the actual
    historical total.

    Two things keep this from being a perfect replica of production,
    both surfaced via the return value rather than hidden:

      1. Burn-rate data (current_expense/days_passed) only exists for the
         CURRENT, in-progress month - a fully-elapsed historical month in
         the backtest has no equivalent. Folds are run with
         current_expense=0, days_passed=0, which makes the low-data tier
         fall back to a recent-average estimate only (its documented
         fallback when no burn projection is available), never a
         fabricated burn rate.
      2. The recurring component added back for a held-out month is only
         genuinely KNOWN when recurring_input_mode == "month_by_month"
         with full coverage. For "monthly_constant" it's an approximation
         of a fluctuating real signal; for "unavailable" it's 0 (so the
         backtest degrades to a totals-only backtest). `backtest_approximate`
         is True unless recurring is fully and reliably known.

    Where month-by-month calendar labels are available, each training
    window uses the correspondingly sliced `month_indices_clean`, so a
    fold never claims a seasonal component it doesn't have enough aligned
    calendar data to support (`_select_forecast_method`/
    `_forecast_seasonal_or_trend` already enforce this per-window).

    Returns a dict: {"mae", "mape", "folds", "method", "backtest_approximate"}.
    mae/mape are None when there isn't enough history to backtest
    meaningfully (fewer than 2 folds).
    """
    n = len(discretionary_series)
    errors = []
    pct_errors = []
    min_train = 2
    has_month_indices = bool(month_indices_clean) and len(month_indices_clean) == n

    for i in range(min_train, n):
        train = discretionary_series[:i]
        train_month_indices = month_indices_clean[:i] if has_month_indices else None

        pred_discretionary, _slope, _method, _seasonal_used = _select_forecast_method(
            train, current_expense=0, days_passed=0, days_in_month=30,
            month_indices=train_month_indices,
        )
        pred_discretionary = max(0.0, pred_discretionary)
        pred_total = pred_discretionary + recurring_series[i]
        actual_total = actual_totals[i]
        err = actual_total - pred_total
        errors.append(err)
        if abs(actual_total) > 1e-9:
            pct_errors.append(abs(err) / abs(actual_total))

    folds = len(errors)
    # Only a fully-known, month-by-month, fully-covered recurring series
    # lets the reconstructed total in each fold be treated as a faithful
    # replica of what production would actually forecast; anything less
    # (constant approximation, or no separation at all) is approximate.
    backtest_approximate = not (recurring_input_mode == "month_by_month" and not recurring_partial_coverage)

    if folds < 2:
        return {
            "mae": None, "mape": None, "folds": folds,
            "method": "tiered_production_replica",
            "backtest_approximate": backtest_approximate,
        }

    mae = _mean([abs(e) for e in errors])
    mape = (_mean(pct_errors) * 100) if pct_errors else None
    return {
        "mae": mae, "mape": mape, "folds": folds,
        "method": "tiered_production_replica",
        "backtest_approximate": backtest_approximate,
    }


# ============================================================
# Expected range (explicitly heuristic unless backed by backtest error)
# ============================================================

def _forecast_range(forecast, mae, n_folds, series):
    """
    Builds an EXPECTED RANGE around the point forecast - not a
    statistically validated confidence interval.

      - With >= 3 backtest folds, the margin is based on the actual
        historical forecast error (MAE) observed for this series
        (`range_method = "historical_error_margin"`).
      - Otherwise the margin falls back to a percentage of the forecast,
        widened for short/volatile history
        (`range_method = "heuristic_percentage_of_forecast"`), and is
        explicitly labeled as such rather than presented as statistical.
    """
    if mae is not None and n_folds is not None and n_folds >= 3:
        margin = mae * 1.25
        range_method = "historical_error_margin"
    else:
        m = _mean(series)
        sd = _std(series)
        volatility = (sd / m) if m else 0.0
        pct = 0.25 if len(series) < 3 else 0.15 + min(0.15, volatility * 0.3)
        margin = forecast * pct
        range_method = "heuristic_percentage_of_forecast"

    lower = max(0.0, forecast - margin)
    upper = forecast + margin
    return lower, upper, range_method


# ============================================================
# Confidence
# ============================================================

_CONFIDENCE_LEVELS = ["low", "medium_low", "medium", "medium_high", "high"]


def _base_confidence_for_tier(n):
    if n < 3:
        return "low"
    if n <= 5:
        return "medium_low"
    if n <= 11:
        return "medium"  # NOT "high" - six-ish months of data is not treated as high confidence
    return "medium_high"


def _determine_confidence(n, mape, series, seasonal_used, recurring_input_mode, recurring_partial_coverage):
    idx = _CONFIDENCE_LEVELS.index(_base_confidence_for_tier(n))

    m = _mean(series)
    sd = _std(series)
    cv = (sd / m) if m else 0.0  # coefficient of variation - volatility signal

    if mape is not None and mape > 50:
        idx = max(0, idx - 1)
    if cv > 0.6:
        idx = max(0, idx - 1)

    # Recurring separation quality is one of the confidence inputs.
    # - "unavailable": we don't know how much of history was recurring vs
    #   discretionary at all, so the discretionary trend itself is on
    #   shakier ground - penalize.
    # - "monthly_constant": an approximation of a fluctuating real signal;
    #   the backtest built on it is only approximate, so cap confidence
    #   even if the raw numbers look clean.
    # - "month_by_month": real data - no penalty from this factor.
    if recurring_input_mode == "unavailable":
        idx = max(0, idx - 1)
    elif recurring_input_mode == "monthly_constant":
        idx = min(idx, _CONFIDENCE_LEVELS.index("medium"))
        idx = max(0, idx - 1) if idx == _CONFIDENCE_LEVELS.index("medium") else idx

    if recurring_partial_coverage:
        idx = max(0, idx - 1)

    # "high" is only reachable with a full year+ of data, a real seasonal
    # signal, demonstrably low backtest error, AND real (not constant,
    # not unavailable) recurring separation - never assumed.
    if (idx == _CONFIDENCE_LEVELS.index("medium_high") and seasonal_used
            and mape is not None and mape < 15 and n >= 12
            and recurring_input_mode == "month_by_month" and not recurring_partial_coverage):
        idx = _CONFIDENCE_LEVELS.index("high")

    return _CONFIDENCE_LEVELS[idx]


# ============================================================
# Category-level forecasting (optional, only when data supports it)
# ============================================================

def predict_category_forecasts(category_history, min_points=3, flat_slope_threshold=0.02):
    """
    category_history: dict like {"Food": [4000, 4300, 4100, ...], ...}

    Returns a list of {"category", "forecast", "trend"} dicts, but ONLY
    for categories with at least `min_points` valid historical points.
    Categories with less data are silently skipped rather than
    fabricating a forecast from too little signal.
    """
    results = []
    if not category_history:
        return results

    for category, series_raw in category_history.items():
        series = _clean_series(series_raw)
        if len(series) < min_points:
            continue
        pred, slope = _linear_forecast(series)
        pred = _clamp_nonneg(pred)
        avg = _mean(series) or 1.0
        rel_slope = slope / avg
        if rel_slope > flat_slope_threshold:
            trend = "up"
        elif rel_slope < -flat_slope_threshold:
            trend = "down"
        else:
            trend = "flat"
        results.append({
            "category": category,
            "forecast": _safe_round(pred),
            "trend": trend,
        })
    return results


# ============================================================
# Comprehensive forecasting entry point
# ============================================================

def predict_next_month_comprehensive(
    history=None,
    current_expense=0,
    days_passed=0,
    days_in_month=30,
    historical_recurring=None,
    expected_recurring_monthly=None,
    remaining_recurring_this_month=None,
    target="next_month",
    month_indices=None,
    category_history=None,
):
    """
    Forecasts total expenses for a target month without double-counting
    recurring commitments that are already baked into `history`.

    Parameters
    ----------
    history : list
        Historical monthly TOTAL expenses (oldest -> newest).
    current_expense : number
        Amount spent so far in the current (in-progress) month.
    days_passed : int
        Days elapsed in the current month.
    days_in_month : int
        Number of days in the current month (default 30).
    historical_recurring : None | number | list
        The recurring component of `history`. A single number is treated
        as a constant monthly recurring load; a list is matched
        month-by-month against `history`. If omitted, recurring/
        discretionary separation is unavailable and the model falls back
        to forecasting the total series directly (explicitly flagged via
        `recurring_separation_available: False`).
    expected_recurring_monthly : None | number
        Known upcoming recurring commitments for NEXT month (used when
        target="next_month"). Typically sourced from the recurring
        engine's subscription/bill summary.
    remaining_recurring_this_month : None | number
        Recurring commitments not yet incurred in the CURRENT month
        (used when target="current_month"). This should already exclude
        anything reflected in `current_expense`.
    target : "next_month" | "current_month"
        Which month is being forecast.
    month_indices : None | list[int]
        Calendar month numbers (1-12) aligned to `history`, enabling an
        optional seasonal component with 12+ months of data. Omit if
        unavailable - no seasonality is invented without this.
    category_history : None | dict[str, list]
        Optional per-category historical totals for `predict_category_forecasts`.

    Returns
    -------
    dict with (at least) the keys: status, forecast, lower_bound,
    upper_bound, confidence, method, trend, data_points,
    historical_recurring_component, historical_discretionary_component,
    forecasted_discretionary, expected_recurring, total_forecast,
    forecast_error_mae, forecast_error_mape, backtest_folds,
    backtest_method, backtest_approximate, category_forecasts, and
    diagnostic/explanatory fields (range_method,
    recurring_separation_available, recurring_partial_coverage,
    recurring_input_mode ["unavailable" | "monthly_constant" |
    "month_by_month"], message, etc).
    """
    current_expense = _to_float(current_expense, 0.0)
    days_passed = max(0, int(_to_float(days_passed, 0)))
    days_in_month = max(1, int(_to_float(days_in_month, 30)))

    current_burn_rate = (current_expense / days_passed) if days_passed > 0 else None
    projected_full_month_expense = (
        current_burn_rate * days_in_month if current_burn_rate is not None else None
    )

    # ------------------------------------------------------------
    # Calendar alignment: clean `history` while keeping
    # `historical_recurring` (if a list) and `month_indices` positionally
    # matched to it - a missing month is dropped from every aligned
    # array together, never from `history` alone. If a list's RAW length
    # doesn't match `history`'s RAW length, we cannot trust any
    # positional correspondence, so that component is disabled entirely
    # (flagged in `message`) rather than padded/truncated into a guess.
    # ------------------------------------------------------------
    alignment = _align_calendar_series(history, historical_recurring, month_indices)
    history_clean = alignment["history_clean"]

    alignment_messages = []

    effective_historical_recurring = historical_recurring
    if alignment["recurring_list_misaligned"]:
        effective_historical_recurring = None
        alignment_messages.append(
            f"historical_recurring list length ({len(historical_recurring)}) does not "
            f"match the supplied history length ({len(history) if history else 0}); "
            "recurring/discretionary separation requires a reliable month-by-month "
            "correspondence, so it has been disabled for this call rather than "
            "guessed via truncation/padding. Pass historical_recurring aligned 1:1 "
            "with history (same length, same order) to enable it."
        )

    month_indices_clean = alignment["month_indices_clean"]
    if alignment["month_indices_misaligned"]:
        alignment_messages.append(
            f"month_indices length ({len(month_indices)}) does not match the "
            f"supplied history length ({len(history) if history else 0}); seasonal "
            "adjustment requires reliable calendar alignment, so it has been "
            "disabled for this call rather than guessed."
        )

    # ------------------------------------------------------------
    # No history at all. Even with zero completed months, a known
    # upcoming recurring commitment is deterministic information we
    # should not discard - it is added to whatever discretionary signal
    # (current-month burn rate) is available, rather than being zeroed
    # out. Confidence is still marked low/insufficient given the lack of
    # historical grounding for the discretionary side.
    # ------------------------------------------------------------
    if len(history_clean) == 0:
        if days_passed > 0:
            discretionary_forecast = _clamp_nonneg(projected_full_month_expense)
            method = "burn_rate_only"
        else:
            discretionary_forecast = 0.0
            method = "no_data"

        no_history_message = None
        if target == "current_month":
            if remaining_recurring_this_month is not None:
                expected_recurring = _clamp_nonneg(remaining_recurring_this_month)
            else:
                expected_recurring = 0.0
                no_history_message = (
                    "target is 'current_month' but remaining_recurring_this_month was "
                    "not supplied; expected_recurring defaults to 0 rather than guessing."
                )
        else:
            if expected_recurring_monthly is not None:
                expected_recurring = _clamp_nonneg(expected_recurring_monthly)
            else:
                expected_recurring = 0.0
                no_history_message = (
                    "expected_recurring_monthly was not supplied; expected_recurring "
                    "defaults to 0."
                )

        total_forecast = _clamp_nonneg(discretionary_forecast + expected_recurring)

        if discretionary_forecast == 0.0 and expected_recurring == 0.0:
            status = "insufficient_data"
            confidence = "none"
            base_message = "No expense history and no current-month data available."
        elif discretionary_forecast == 0.0:
            status = "insufficient_data"
            confidence = "low"
            base_message = (
                "No expense history or current-month burn data; forecast is based "
                "entirely on the known recurring commitment supplied, so it should "
                "not be treated as zero just because history is empty."
            )
        else:
            status = "insufficient_data"
            confidence = "low"
            if expected_recurring > 0:
                base_message = ("No expense history available; forecast combines the "
                                 "current month burn rate with the known recurring "
                                 "commitment supplied.")
            else:
                base_message = ("No expense history available; forecast based purely "
                                 "on current month burn rate. This is a heuristic, not "
                                 "a trend.")

        message_parts = [m for m in (base_message, no_history_message) + tuple(alignment_messages) if m]
        message = " ".join(message_parts) if message_parts else None

        range_method = "heuristic_percentage_of_forecast" if total_forecast > 0 else None
        lower_bound = _safe_round(total_forecast * 0.85) if total_forecast > 0 else 0
        upper_bound = _safe_round(total_forecast * 1.15) if total_forecast > 0 else 0

        return {
            "status": status,
            "forecast": _safe_round(total_forecast),
            "lower_bound": lower_bound,
            "upper_bound": upper_bound,
            "confidence": confidence,
            "method": method,
            "trend": "unknown",
            "data_points": 0,
            "historical_recurring_component": 0,
            "historical_discretionary_component": 0,
            "forecasted_discretionary": _safe_round(discretionary_forecast),
            "expected_recurring": _safe_round(expected_recurring),
            "total_forecast": _safe_round(total_forecast),
            "forecast_error_mae": None,
            "forecast_error_mape": None,
            "backtest_folds": 0,
            "backtest_method": None,
            "backtest_approximate": None,
            "range_method": range_method,
            "recurring_separation_available": False,
            "recurring_partial_coverage": False,
            "recurring_input_mode": "unavailable",
            "message": message,
            "current_burn_rate": round(current_burn_rate, 2) if current_burn_rate is not None else None,
            "projected_full_month_expense": (
                _safe_round(projected_full_month_expense) if projected_full_month_expense is not None else None
            ),
            "category_forecasts": predict_category_forecasts(category_history),
        }

    n = len(history_clean)

    # ------------------------------------------------------------
    # Step 1: separate historical recurring from historical discretionary,
    # using the pre-aligned (calendar-safe) recurring list from Step 0.
    # ------------------------------------------------------------
    discretionary_series, recurring_series, separation_available, partial_coverage, recurring_input_mode = \
        _separate_recurring(history_clean, effective_historical_recurring,
                             recurring_clean_aligned=alignment["recurring_clean"])

    historical_recurring_component = round(_mean(recurring_series[-3:]), 2) if recurring_series else 0.0
    historical_discretionary_component = round(_mean(discretionary_series[-3:]), 2) if discretionary_series else 0.0

    # ------------------------------------------------------------
    # Step 2: forecast the DISCRETIONARY series by data-depth tier, using
    # the calendar-aligned month_indices_clean (never the raw month_indices,
    # which may be misaligned relative to history_clean after cleaning).
    # ------------------------------------------------------------
    forecast_disc, slope, method, seasonal_used = _select_forecast_method(
        discretionary_series, current_expense, days_passed, days_in_month, month_indices_clean
    )
    forecast_disc = _clamp_nonneg(forecast_disc)

    # ------------------------------------------------------------
    # Step 3: backtest that approximates the production tiered method
    # (same data-depth tiering, same calendar alignment, and marks
    # itself approximate whenever recurring isn't fully/reliably known).
    # ------------------------------------------------------------
    backtest = _rolling_origin_backtest(
        discretionary_series, recurring_series, history_clean,
        recurring_input_mode, partial_coverage, month_indices_clean,
    )
    mae, mape, n_folds = backtest["mae"], backtest["mape"], backtest["folds"]
    backtest_method, backtest_approximate = backtest["method"], backtest["backtest_approximate"]

    # ------------------------------------------------------------
    # Step 4: expected recurring commitment for the TARGET month.
    # ------------------------------------------------------------
    recurring_flag_message = " ".join(alignment_messages) if alignment_messages else None
    if target == "current_month":
        if remaining_recurring_this_month is not None:
            expected_recurring = _clamp_nonneg(remaining_recurring_this_month)
        else:
            expected_recurring = 0.0
            addendum = (
                "target is 'current_month' but remaining_recurring_this_month was not "
                "supplied; expected_recurring defaults to 0 rather than guessing."
            )
            recurring_flag_message = f"{recurring_flag_message} {addendum}" if recurring_flag_message else addendum
    else:
        if expected_recurring_monthly is not None:
            expected_recurring = _clamp_nonneg(expected_recurring_monthly)
        else:
            expected_recurring = 0.0
            addendum = (
                "expected_recurring_monthly was not supplied; expected_recurring "
                "defaults to 0. Supply it (e.g. from get_subscription_summary) to "
                "avoid under-forecasting known upcoming commitments."
            )
            recurring_flag_message = f"{recurring_flag_message} {addendum}" if recurring_flag_message else addendum

    # ------------------------------------------------------------
    # Step 5: total forecast.
    # ------------------------------------------------------------
    if target == "current_month" and (current_expense > 0 or days_passed > 0):
        if expected_recurring_monthly is not None and remaining_recurring_this_month is not None:
            # We can split what's already been spent this month into its
            # recurring vs discretionary portions, so we anchor on actual
            # spend-to-date and only project the REMAINING days/commitments.
            recurring_already_paid = max(
                0.0, _to_float(expected_recurring_monthly) - _to_float(remaining_recurring_this_month)
            )
            discretionary_already_spent = max(0.0, current_expense - recurring_already_paid)
            remaining_days = max(0, days_in_month - days_passed)
            disc_burn_rate = (discretionary_already_spent / days_passed) if days_passed > 0 else 0.0
            projected_remaining_discretionary = disc_burn_rate * remaining_days

            forecast_disc = discretionary_already_spent + projected_remaining_discretionary
            total_forecast = current_expense + projected_remaining_discretionary + expected_recurring
            method = "current_month_remaining_projection"
        else:
            # Can't separate what portion of current_expense is recurring
            # vs discretionary - use a blended burn rate for the
            # remaining days instead of guessing a split, and do NOT
            # separately add remaining recurring on top (to avoid risking
            # double counting it).
            remaining_days = max(0, days_in_month - days_passed)
            mix_burn_rate = (current_expense / days_passed) if days_passed > 0 else 0.0
            projected_remaining_total = mix_burn_rate * remaining_days

            forecast_disc = current_expense + projected_remaining_total
            total_forecast = forecast_disc
            method = "current_month_burn_rate_mixed"
            addendum = (
                "Could not separate recurring vs discretionary for the remaining days "
                "of the current month (need both expected_recurring_monthly and "
                "remaining_recurring_this_month); used a blended burn-rate projection "
                "instead of adding remaining recurring on top, to avoid double counting."
            )
            recurring_flag_message = (
                f"{recurring_flag_message} {addendum}" if recurring_flag_message else addendum
            )
    elif not separation_available and expected_recurring > 0:
        # discretionary_series == history_clean here (no separation was
        # possible), so forecast_disc is really a forecast of TOTAL
        # historical spend - which already includes whatever recurring
        # burden actually occurred. Adding expected_recurring on top
        # would double-count it, exactly the failure mode this module
        # guarantees against. Without historical_recurring we have no
        # way to know how much of forecast_disc is already recurring, so
        # we report the total-series forecast as-is and flag that the
        # supplied expected_recurring could NOT be safely combined,
        # rather than silently adding it.
        total_forecast = forecast_disc
        addendum = (
            f"expected_recurring ({round(expected_recurring, 2)}) was supplied but "
            "historical_recurring was not, so recurring/discretionary separation is "
            "unavailable. forecast_disc is a forecast of TOTAL historical spend "
            "(already including whatever recurring burden occurred), so adding "
            "expected_recurring on top would double-count it. Supply "
            "historical_recurring to enable a correctly separated forecast."
        )
        recurring_flag_message = f"{recurring_flag_message} {addendum}" if recurring_flag_message else addendum
    else:
        total_forecast = forecast_disc + expected_recurring

    total_forecast = _clamp_nonneg(total_forecast)
    forecast_disc = _clamp_nonneg(forecast_disc)

    # ------------------------------------------------------------
    # Step 6: expected range and confidence.
    # ------------------------------------------------------------
    lower_bound, upper_bound, range_method = _forecast_range(total_forecast, mae, n_folds, history_clean)
    confidence = _determine_confidence(
        n, mape, discretionary_series, seasonal_used, recurring_input_mode, partial_coverage
    )

    disc_avg = _mean(discretionary_series) or 1.0
    if slope > 0.01 * disc_avg:
        trend = "up"
    elif slope < -0.01 * disc_avg:
        trend = "down"
    else:
        trend = "flat"

    status = "ok" if n >= 3 else "low_confidence"

    return {
        "status": status,
        "forecast": _safe_round(total_forecast),
        "lower_bound": _safe_round(lower_bound),
        "upper_bound": _safe_round(upper_bound),
        "confidence": confidence,
        "method": method,
        "trend": trend,
        "data_points": n,
        "historical_recurring_component": historical_recurring_component,
        "historical_discretionary_component": historical_discretionary_component,
        "forecasted_discretionary": _safe_round(forecast_disc),
        "expected_recurring": _safe_round(expected_recurring),
        "total_forecast": _safe_round(total_forecast),
        "forecast_error_mae": round(mae, 2) if mae is not None else None,
        "forecast_error_mape": round(mape, 2) if mape is not None else None,
        "backtest_folds": n_folds,
        "backtest_method": backtest_method,
        "backtest_approximate": backtest_approximate,
        "range_method": range_method,
        "recurring_separation_available": separation_available,
        "recurring_partial_coverage": partial_coverage,
        "recurring_input_mode": recurring_input_mode,
        "message": recurring_flag_message,
        "current_burn_rate": round(current_burn_rate, 2) if current_burn_rate is not None else None,
        "projected_full_month_expense": (
            _safe_round(projected_full_month_expense) if projected_full_month_expense is not None else None
        ),
        "category_forecasts": predict_category_forecasts(category_history),
    }


# ============================================================
# Backward-compatible entry point (unchanged signature/return type)
# ============================================================

def predict_next_month(expenses, recurring_monthly_expected=0, historical_recurring=None):
    """
    Backward-compatible entry point used by decision_engine.py and other
    existing callers. Returns a single numeric forecast (int), exactly as
    before.

    Signature / behavior contract
    ------------------------------
    predict_next_month(expenses)
        Unchanged from the original implementation, byte-for-byte in
        behavior: forecasts `expenses` as a plain linear trend, no
        recurring logic involved at all. Every existing 1-argument
        caller is unaffected.

    predict_next_month(expenses, recurring_monthly_expected)
        historical_recurring is NOT supplied, so there is no way for
        this function to know how much of `expenses` was already
        recurring spend. To avoid guessing, this mode assumes the
        documented contract: `expenses` represents DISCRETIONARY
        spending only (not totals that already include recurring
        costs). Under that contract, adding recurring_monthly_expected
        on top is correct and not a double-count. Callers that instead
        pass in TOTAL historical expenses here (as the original
        implementation implicitly assumed) will double-count recurring
        costs - this is the same trap `predict_next_month_comprehensive`
        exists to avoid; use it with `historical_recurring` set instead
        of relying on this two-argument mode with total-expense history.

    predict_next_month(expenses, recurring_monthly_expected, historical_recurring)
        The safe, preferred way to combine the two arguments. `expenses`
        is treated as TOTAL historical spend, `historical_recurring` is
        used to separate it into discretionary vs recurring components
        (same semantics as predict_next_month_comprehensive - a single
        number = constant monthly recurring load, a list = month-by-month).
        When `historical_recurring` is a list, it is aligned to `expenses`
        by RAW position (same calendar-safe rule as
        predict_next_month_comprehensive: a missing/invalid month is
        dropped from both arrays together, never just from `expenses`,
        so a gap can't shift later months' recurring values out of
        place). If the list's raw length doesn't match `expenses`'s raw
        length, alignment can't be trusted and separation is disabled
        for that call (falls back to the two-argument behavior above)
        rather than guessed via padding/truncation. Only the
        DISCRETIONARY residual is forecast forward, and
        recurring_monthly_expected is added back in exactly once for the
        month being forecast.

    Hardened for Decimal/None/NaN inputs and negative results in all
    modes; return type is always a plain int (or 0), as before.
    """
    recurring = _clamp_nonneg(recurring_monthly_expected)

    if historical_recurring is not None:
        # Safe combined mode: separate discretionary from total history
        # using historical_recurring, forecast the discretionary residual,
        # and add the known upcoming recurring commitment back in once.
        # Calendar-safe alignment: a missing month is dropped from
        # `expenses` and (if historical_recurring is a list) from it
        # together, never from `expenses` alone.
        alignment = _align_calendar_series(expenses, historical_recurring, None)
        history_clean = alignment["history_clean"]
        effective_historical_recurring = historical_recurring
        if alignment["recurring_list_misaligned"]:
            effective_historical_recurring = None  # disable rather than guess an alignment

        discretionary_series, _recurring_series, _avail, _partial, _mode = _separate_recurring(
            history_clean, effective_historical_recurring,
            recurring_clean_aligned=alignment["recurring_clean"],
        )

        if len(discretionary_series) < 2:
            base = sum(discretionary_series) if discretionary_series else 0.0
            return _safe_round(base + recurring)

        forecast_disc, _slope = _linear_forecast(discretionary_series)
        final_forecast = _clamp_nonneg(forecast_disc) + recurring
        return _safe_round(final_forecast)

    series = _clean_series(expenses)

    # Original behavior, preserved exactly (recurring defaults to 0, so
    # existing 1-argument callers see byte-for-byte identical results).
    if len(series) < 2:
        base = sum(series) if series else 0.0
        return _safe_round(base + recurring)

    forecast_next, _slope = _linear_forecast(series)
    final_forecast = _clamp_nonneg(forecast_next) + recurring
    return _safe_round(final_forecast)