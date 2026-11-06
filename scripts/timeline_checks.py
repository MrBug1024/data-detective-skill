"""Conditional clock and travel checks, not identity or alibi determinations."""
from datetime import datetime, timedelta
import math


def _seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("秒数必须为有限数值")


def _instant(value):
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("必须提供日期及显式时区；先核查时区再比较")
    return moment


def clock_interval(recorded, *, clock_fast_seconds, uncertainty_seconds):
    """Positive clock_fast_seconds means the device reads ahead of reference time.

    Caller supplies the total applicable uncertainty, not only calibration error.
    Calibration validity and event timestamp precision require separate verification.
    """
    _seconds(clock_fast_seconds)
    _seconds(uncertainty_seconds)
    if uncertainty_seconds < 0:
        raise ValueError("不确定度不能小于零")
    corrected = _instant(recorded) - timedelta(seconds=clock_fast_seconds)
    radius = timedelta(seconds=uncertainty_seconds)
    return {"earliest": (corrected - radius).isoformat(),
            "latest": (corrected + radius).isoformat(),
            "basis": "conditional_on_applicable_calibration_and_total_error_bound"}


def travel_feasibility(first_interval, second_interval, *, minimum_travel_seconds):
    """Can one traveler occupy first then second place given bounded times?

    An impossible result flags a conflict among assumptions/sources. It does not
    identify which source is false or prove who is depicted by either record.
    """
    _seconds(minimum_travel_seconds)
    if minimum_travel_seconds < 0:
        raise ValueError("最短行程不能小于零")
    first = [_instant(first_interval[k]) for k in ("earliest", "latest")]
    second = [_instant(second_interval[k]) for k in ("earliest", "latest")]
    if first[0] > first[1] or second[0] > second[1]:
        raise ValueError("时间区间起点不能晚于终点")
    max_available = (second[1] - first[0]).total_seconds()
    min_available = (second[0] - first[1]).total_seconds()
    status = ("impossible_under_assumptions" if max_available < minimum_travel_seconds else
              "feasible_for_all_times" if min_available >= minimum_travel_seconds else
              "possible_for_some_times")
    return {"status": status, "maximum_available_seconds": max_available,
            "minimum_available_seconds": min_available,
            "minimum_travel_seconds": minimum_travel_seconds,
            "assumptions_to_verify": ["timestamps_bound_actual_presence",
                                      "clock_calibration_and_error_bounds_apply",
                                      "minimum_route_time_is_valid_for_available_transport",
                                      "both_records_refer_to_the_same_traveler"],
            "identity_established": False}
