"""Offline cross-provider comparison.

Aligns observations from Public.com, Schwab, and Webull by request-time
windows and computes comparison metrics. Does not designate any provider
as ground truth.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from public_api_test.cross_validation.models import (
    CrossProviderComparison,
    CrossValidationSummary,
    ProviderObservation,
)


def _parse_utc(iso_str: str) -> Optional[float]:
    """Parse an ISO 8601 UTC string into epoch seconds."""
    if not iso_str:
        return None
    try:
        parsed = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp()
    except (ValueError, OverflowError):
        return None


def _group_by_symbol(
    observations: list[ProviderObservation],
) -> dict[str, list[ProviderObservation]]:
    grouped: dict[str, list[ProviderObservation]] = {}
    for obs in observations:
        grouped.setdefault(obs.symbol, []).append(obs)
    return grouped


def _sort_by_time(obs_list: list[ProviderObservation]) -> list[ProviderObservation]:
    return sorted(obs_list, key=lambda o: _parse_utc(o.request_finished_utc) or 0.0)


def _match_by_time_window(
    obs_a: list[ProviderObservation],
    obs_b: list[ProviderObservation],
    tolerance_seconds: float,
) -> list[tuple[ProviderObservation, ProviderObservation]]:
    """Match observations from two providers within a time window.

    Uses a greedy nearest-neighbor match: each observation can match at most
    once. Unmatched observations are reported separately by the caller.
    """
    matches: list[tuple[ProviderObservation, ProviderObservation]] = []
    used_b = set()

    for a in obs_a:
        a_time = _parse_utc(a.request_finished_utc)
        if a_time is None:
            continue
        best_b = None
        best_diff = tolerance_seconds + 1.0
        for j, b in enumerate(obs_b):
            if j in used_b:
                continue
            b_time = _parse_utc(b.request_finished_utc)
            if b_time is None:
                continue
            diff = abs(a_time - b_time)
            if diff <= tolerance_seconds and diff < best_diff:
                best_diff = diff
                best_b = (j, b)
        if best_b is not None:
            used_b.add(best_b[0])
            matches.append((a, best_b[1]))

    return matches


def _match_triplets(
    public_obs: list[ProviderObservation],
    schwab_obs: list[ProviderObservation],
    webull_obs: list[ProviderObservation],
    tolerance_seconds: float,
) -> list[tuple[ProviderObservation, ProviderObservation, ProviderObservation]]:
    """Match observations from all three providers within a time window."""
    matches: list[tuple[ProviderObservation, ProviderObservation, ProviderObservation]] = []
    used_schwab = set()
    used_webull = set()

    for p in public_obs:
        p_time = _parse_utc(p.request_finished_utc)
        if p_time is None:
            continue
        best_s = None
        best_w = None
        best_diff = tolerance_seconds + 1.0
        for i, s in enumerate(schwab_obs):
            if i in used_schwab:
                continue
            s_time = _parse_utc(s.request_finished_utc)
            if s_time is None:
                continue
            diff_s = abs(p_time - s_time)
            if diff_s > tolerance_seconds:
                continue
            for j, w in enumerate(webull_obs):
                if j in used_webull:
                    continue
                w_time = _parse_utc(w.request_finished_utc)
                if w_time is None:
                    continue
                diff_w = abs(p_time - w_time)
                if diff_w > tolerance_seconds:
                    continue
                # Combined diff: max of the two pairwise diffs
                combined = max(diff_s, diff_w)
                if combined < best_diff:
                    best_diff = combined
                    best_s = (i, s)
                    best_w = (j, w)
        if best_s is not None and best_w is not None:
            used_schwab.add(best_s[0])
            used_webull.add(best_w[0])
            matches.append((p, best_s[1], best_w[1]))

    return matches


def _provider_metrics(obs_list: list[ProviderObservation]) -> dict[str, Any]:
    """Compute per-provider metrics for a symbol."""
    total = len(obs_list)
    if total == 0:
        return {
            "observations": 0,
            "price_changes": 0,
            "timestamp_changes": 0,
            "price_changed_ts_unchanged": 0,
            "missing_timestamps": 0,
            "future_timestamps": 0,
            "stale_timestamps": 0,
            "invalid_timestamps": 0,
            "valid_timestamps": 0,
            "http_statuses": {},
            "categories": {},
        }

    # ProviderObservation doesn't store price_changed directly; compute from sequence
    price_changes = 0
    timestamp_changes = 0
    price_changed_ts_unchanged = 0
    for i in range(1, len(obs_list)):
        prev = obs_list[i - 1]
        cur = obs_list[i]
        if prev.last_price is not None and cur.last_price is not None:
            if prev.last_price != cur.last_price:
                price_changes += 1
                if (
                    prev.last_timestamp_raw is not None
                    and cur.last_timestamp_raw is not None
                    and prev.last_timestamp_raw == cur.last_timestamp_raw
                ):
                    price_changed_ts_unchanged += 1
        if (
            prev.last_timestamp_raw is not None
            and cur.last_timestamp_raw is not None
            and prev.last_timestamp_raw != cur.last_timestamp_raw
        ):
            timestamp_changes += 1

    missing = sum(1 for o in obs_list if o.last_timestamp_status == "missing_timestamp")
    future = sum(1 for o in obs_list if o.last_timestamp_status == "future_timestamp")
    stale = sum(1 for o in obs_list if o.last_timestamp_status == "stale")
    invalid = sum(1 for o in obs_list if o.last_timestamp_status == "invalid_timestamp")
    valid = sum(1 for o in obs_list if o.last_timestamp_status == "valid")

    http_statuses: dict[str, int] = {}
    categories: dict[str, int] = {}
    for o in obs_list:
        if o.http_status is not None:
            http_statuses[str(o.http_status)] = http_statuses.get(str(o.http_status), 0) + 1
        categories[o.category] = categories.get(o.category, 0) + 1

    return {
        "observations": total,
        "price_changes": price_changes,
        "timestamp_changes": timestamp_changes,
        "price_changed_ts_unchanged": price_changed_ts_unchanged,
        "missing_timestamps": missing,
        "future_timestamps": future,
        "stale_timestamps": stale,
        "invalid_timestamps": invalid,
        "valid_timestamps": valid,
        "http_statuses": http_statuses,
        "categories": categories,
    }


def compare_symbols(
    public_obs: list[ProviderObservation],
    schwab_obs: list[ProviderObservation],
    webull_obs: list[ProviderObservation],
    symbol: str,
    tolerance_seconds: float = 1.0,
) -> CrossProviderComparison:
    """Compare observations for one symbol across all providers.

    Returns a CrossProviderComparison with match counts, metrics, and anomalies.
    No provider is designated as ground truth.
    """
    public_sorted = _sort_by_time(public_obs)
    schwab_sorted = _sort_by_time(schwab_obs)
    webull_sorted = _sort_by_time(webull_obs)

    # Match triplets (all three providers within tolerance)
    triplets = _match_triplets(public_sorted, schwab_sorted, webull_sorted, tolerance_seconds)

    # Also compute pairwise matches for reporting
    ps_pairs = _match_by_time_window(public_sorted, schwab_sorted, tolerance_seconds)
    pw_pairs = _match_by_time_window(public_sorted, webull_sorted, tolerance_seconds)
    sw_pairs = _match_by_time_window(schwab_sorted, webull_sorted, tolerance_seconds)

    matched_public_ids = {id(t[0]) for t in triplets}
    matched_schwab_ids = {id(t[1]) for t in triplets}
    matched_webull_ids = {id(t[2]) for t in triplets}

    unmatched_public = [o for o in public_sorted if id(o) not in matched_public_ids]
    unmatched_schwab = [o for o in schwab_sorted if id(o) not in matched_schwab_ids]
    unmatched_webull = [o for o in webull_sorted if id(o) not in matched_webull_ids]

    # Compute metrics per provider
    public_metrics = _provider_metrics(public_sorted)
    schwab_metrics = _provider_metrics(schwab_sorted)
    webull_metrics = _provider_metrics(webull_sorted)

    # Cross-provider price differences for matched triplets
    price_differences: list[dict] = []
    for p, s, w in triplets:
        if p.last_price is not None and s.last_price is not None:
            diff_pct = abs(p.last_price - s.last_price) / ((p.last_price + s.last_price) / 2) * 100
            price_differences.append(
                {
                    "symbol": symbol,
                    "provider1": "public_com",
                    "provider2": "schwab",
                    "time1": p.request_finished_utc,
                    "time2": s.request_finished_utc,
                    "price1": p.last_price,
                    "price2": s.last_price,
                    "diff_pct": round(diff_pct, 4),
                }
            )
        if p.last_price is not None and w.last_price is not None:
            diff_pct = abs(p.last_price - w.last_price) / ((p.last_price + w.last_price) / 2) * 100
            price_differences.append(
                {
                    "symbol": symbol,
                    "provider1": "public_com",
                    "provider2": "webull",
                    "time1": p.request_finished_utc,
                    "time2": w.request_finished_utc,
                    "price1": p.last_price,
                    "price2": w.last_price,
                    "diff_pct": round(diff_pct, 4),
                }
            )
        if s.last_price is not None and w.last_price is not None:
            diff_pct = abs(s.last_price - w.last_price) / ((s.last_price + w.last_price) / 2) * 100
            price_differences.append(
                {
                    "symbol": symbol,
                    "provider1": "schwab",
                    "provider2": "webull",
                    "time1": s.request_finished_utc,
                    "time2": w.request_finished_utc,
                    "price1": s.last_price,
                    "price2": w.last_price,
                    "diff_pct": round(diff_pct, 4),
                }
            )

    # Detect anomalies (without designating ground truth)
    anomalies: list[dict] = []

    # Price changed while timestamp unchanged (for each provider)
    for provider_name, obs_list in (
        ("public_com", public_sorted),
        ("schwab", schwab_sorted),
        ("webull", webull_sorted),
    ):
        for i in range(1, len(obs_list)):
            prev = obs_list[i - 1]
            cur = obs_list[i]
            if (
                prev.last_price is not None
                and cur.last_price is not None
                and prev.last_price != cur.last_price
                and prev.last_timestamp_raw is not None
                and cur.last_timestamp_raw is not None
                and prev.last_timestamp_raw == cur.last_timestamp_raw
            ):
                anomalies.append(
                    {
                        "symbol": symbol,
                        "provider": provider_name,
                        "type": "price_changed_ts_unchanged",
                        "request_time": cur.request_finished_utc,
                        "previous_price": prev.last_price,
                        "current_price": cur.last_price,
                        "timestamp": cur.last_timestamp_raw,
                        "description": "Last price changed while last timestamp remained unchanged",
                    }
                )

    # Missing timestamps
    for provider_name, obs_list in (
        ("public_com", public_sorted),
        ("schwab", schwab_sorted),
        ("webull", webull_sorted),
    ):
        for o in obs_list:
            if o.last_timestamp_status == "missing_timestamp":
                anomalies.append(
                    {
                        "symbol": symbol,
                        "provider": provider_name,
                        "type": "missing_timestamp",
                        "request_time": o.request_finished_utc,
                        "last_price": o.last_price,
                        "description": "Last timestamp is missing/null",
                    }
                )

    # Cross-provider price divergence for matched triplets
    for p, s, w in triplets:
        prices = []
        if p.last_price is not None:
            prices.append(("public_com", p.last_price))
        if s.last_price is not None:
            prices.append(("schwab", s.last_price))
        if w.last_price is not None:
            prices.append(("webull", w.last_price))
        if len(prices) >= 2:
            for i in range(len(prices)):
                for j in range(i + 1, len(prices)):
                    p1_name, p1_val = prices[i]
                    p2_name, p2_val = prices[j]
                    diff_pct = abs(p1_val - p2_val) / ((p1_val + p2_val) / 2) * 100
                    if diff_pct > 1.0:  # >1% difference flagged
                        anomalies.append(
                            {
                                "symbol": symbol,
                                "provider": f"{p1_name}_vs_{p2_name}",
                                "type": "price_divergence",
                                "request_time": p.request_finished_utc,
                                "price1": p1_val,
                                "price2": p2_val,
                                "diff_pct": round(diff_pct, 4),
                                "description": f"Price difference >1% between {p1_name} and {p2_name}",
                            }
                        )

    return CrossProviderComparison(
        symbol=symbol,
        matching_tolerance_seconds=tolerance_seconds,
        public_com_count=len(public_sorted),
        schwab_count=len(schwab_sorted),
        webull_count=len(webull_sorted),
        matched_triplets=len(triplets),
        matched_pairs=len(ps_pairs) + len(pw_pairs) + len(sw_pairs),
        unmatched_public_com=len(unmatched_public),
        unmatched_schwab=len(unmatched_schwab),
        unmatched_webull=len(unmatched_webull),
        public_com_metrics=public_metrics,
        schwab_metrics=schwab_metrics,
        webull_metrics=webull_metrics,
        price_differences=price_differences,
        anomalies=anomalies,
        raw_references={},  # Populated by the caller
    )


def build_summary(
    run_id: str,
    symbols: list[str],
    providers: list[str],
    interval_seconds: int,
    duration_seconds: int,
    comparisons: dict[str, CrossProviderComparison],
    market_session: Optional[str] = None,
    tolerance_seconds: float = 1.0,
    raw_references: Optional[dict[str, str]] = None,
) -> CrossValidationSummary:
    """Build a CrossValidationSummary from per-symbol comparisons."""
    total_anomalies = sum(len(c.anomalies) for c in comparisons.values())
    return CrossValidationSummary(
        run_id=run_id,
        symbols=symbols,
        providers=providers,
        interval_seconds=interval_seconds,
        duration_seconds=duration_seconds,
        matching_tolerance_seconds=tolerance_seconds,
        market_session=market_session,
        comparisons=comparisons,
        total_anomalies=total_anomalies,
        raw_references=raw_references or {},
    )
