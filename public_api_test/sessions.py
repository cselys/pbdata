"""US equity session validation; calendar labels are not provider guarantees."""
from datetime import datetime, time, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo
import math

import pandas_market_calendars as mcal

ET = ZoneInfo("America/New_York")
SESSIONS = ("premarket", "regular", "afterhours")


@lru_cache(maxsize=1)
def calendar():
    return mcal.get_calendar("NYSE")


@lru_cache(maxsize=512)
def boundaries(day):
    schedule = calendar().schedule(start_date=day, end_date=day)
    if schedule.empty:
        return None
    row = schedule.iloc[0]
    opening = row.market_open.to_pydatetime().astimezone(ET)
    closing = row.market_close.to_pydatetime().astimezone(ET)
    # NYSE late-session convention: 17:00 on an early-close day.
    # Public's venue-specific overnight schedule is not inferred from this.
    end_hour = 17 if closing.hour < 16 else 20
    return (datetime.combine(day, time(4), ET), opening, closing,
            datetime.combine(day, time(end_hour), ET))


def session_at(instant):
    if instant.tzinfo is None:
        raise ValueError("Session classification requires a timezone-aware datetime")
    local = instant.astimezone(ET)
    limits = boundaries(local.date())
    if limits is None:
        return "closed"
    pre, opening, closing, post = limits
    if pre <= local < opening:
        return "premarket"
    if opening <= local < closing:
        return "regular"
    if closing <= local < post:
        return "afterhours"
    return "closed"


def timestamp(value):
    """Only documented timezone-aware ISO timestamps; never guess units/timezone."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def indicators(value, path=""):
    """Preserve any provider session/extended-hours fields without inventing one."""
    result = {}
    if isinstance(value, dict):
        for key, item in value.items():
            name = f"{path}.{key}" if path else key
            if "session" in key.lower() or "extended" in key.lower():
                result[name] = item
            elif isinstance(item, (dict, list)):
                result.update(indicators(item, name))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            result.update(indicators(item, f"{path}[{index}]"))
    return result


def annotate_quote(record, selected="auto", max_age=60, now=None, clock_skew_tolerance=0):
    if (selected not in ("auto", *SESSIONS) or not math.isfinite(max_age) or max_age <= 0
            or not math.isfinite(clock_skew_tolerance) or clock_skew_tolerance < 0):
        raise ValueError("Invalid session or freshness threshold")
    received = record.get("response_received_at_utc")
    now = (datetime.fromisoformat(received) if received else now) or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Response time must be timezone-aware")
    now = now.astimezone(timezone.utc)
    current = session_at(now)
    last_raw = record.get("last_timestamp_raw", record.get("provider_timestamp_raw"))
    provider_ts = timestamp(last_raw) if last_raw is not None else record.get("provider_timestamp")
    provider = datetime.fromtimestamp(provider_ts, timezone.utc) if provider_ts is not None else None
    provider_session = session_at(provider) if provider else None
    age = now.timestamp() - provider_ts if provider is not None else None
    matched = selected == "auto" or current == selected
    same_session = bool(provider and current in SESSIONS and provider_session == current
        and provider.astimezone(ET).date() == now.astimezone(ET).date())
    fresh = bool(record.get("category") == "success" and matched and same_session
        and age is not None and 0 <= age <= max_age)
    record.update(data_kind="quote", market_session=current, provider_session=provider_session,
        observed_at_utc=now.isoformat(), requested_session=selected,
        session_filter_match=matched, provider_in_current_session=same_session,
        data_age_seconds=age, freshness_threshold_seconds=max_age,
        freshness_verified=fresh,
        freshness_reason=("recent_same_session_last_trade" if fresh else
            "session_filter_mismatch" if not matched else
            "quote_failed" if record.get("category") != "success" else
            "timestamp_unavailable" if provider is None else
            "provider_outside_current_session" if not same_session else
            "future_timestamp" if age < 0 else "last_trade_older_than_threshold"),
        source_endpoint=record.get("endpoint_url"),
        api_session_indicators=indicators(record.get("raw_response", {})))
    record["response_received_at_utc"] = now.isoformat()
    record["clock_skew_tolerance_seconds"] = clock_skew_tolerance
    # Retain the old outside-hours distinction as an additive compatibility field.
    record["legacy_market_session"] = ("outside_sessions" if current == "closed"
        and boundaries(now.astimezone(ET).date()) is not None else current)
    for side in ("last", "bid", "ask"):
        raw = record.get(f"{side}_timestamp_raw")
        if side == "last" and raw is None:
            raw = record.get("provider_timestamp_raw")
        parsed = timestamp(raw)
        if side == "last" and raw is None:
            parsed = provider_ts  # Compatibility with old observations and mocks.
        instant = datetime.fromtimestamp(parsed, timezone.utc) if parsed is not None else None
        side_age = now.timestamp() - parsed if parsed is not None else None
        in_session = bool(instant and current in SESSIONS and session_at(instant) == current
            and instant.astimezone(ET).date() == now.astimezone(ET).date())
        status = ("missing_timestamp" if side_age is None else "future_timestamp" if side_age < 0
            else "stale" if side_age > max_age or not in_session else "valid")
        value = record.get("price" if side == "last" else side)
        try:
            valid_price = math.isfinite(float(value)) and float(value) > 0
        except (ValueError, TypeError):
            valid_price = False
        eligible = record.get("category") in ("success", "missing_price")
        verified = bool(eligible and valid_price and matched and status == "valid")
        record.update({f"{side}_timestamp_raw": raw,
            f"{side}_timestamp_utc": instant.isoformat() if instant else None,
            f"{side}_age_seconds": side_age,
            f"{side}_data_recency_seconds": side_age,
            f"{side}_timestamp_status": status,
            f"{side}_timestamp_parse_error": raw is not None and parsed is None,
            f"{side}_future_within_tolerance": bool(side_age is not None and -clock_skew_tolerance <= side_age < 0),
            f"{side}_freshness_verified": verified})
    record["data_age_seconds"] = record["last_age_seconds"]
    record["freshness_verified"] = record["last_freshness_verified"]
    return record


def quote_capabilities(observations, symbols):
    result = {}
    for session in SESSIONS:
        rows = [o for o in observations if o.get("market_session") == session
                and o.get("session_filter_match", True)]
        def evidence(items):
            valid = [o for o in items if o.get("category") == "success"]
            return dict(observations=len(items),
                accessible_with_key=any(o.get("http_status") == 200 for o in items) if items else None,
                price_available=bool(valid) if items else None,
                provider_timestamp_available=any(o.get("provider_timestamp") is not None for o in items) if items else None,
                freshness_verified=any(o.get("freshness_verified") for o in items) if items else None,
                fresh_observations=sum(bool(o.get("freshness_verified")) for o in items))
        result[session] = dict(documented_support="quotes documented; session-specific coverage not specified",
            **evidence(rows), per_symbol={s: evidence([o for o in rows if o["symbol"] == s]) for s in symbols})
    return result
