# SPDX-License-Identifier: Apache-2.0
"""Market-hours and basis guard for tokenized equities.

A tokenized equity is a 24/7 token wrapping an asset that trades about six and
a half hours a weekday. Outside those hours nothing is arbitraging the token
back to its underlying, so the pool price is whatever the last trade left
behind. Filling a follower's order into that window is how a strategy books a
loss it never chose.

This module does not pretend to know the underlying's price. It knows when the
underlying is trading, which is a calendar fact, and it refuses to certify a
mark taken while the market is shut. Refusing to certify is the honest output.

US equity regular session: 09:30 to 16:00 America/New_York, Monday to Friday,
excluding exchange holidays. The 2026 NYSE holiday calendar is listed below and
is the one thing here that has to be updated by hand each year.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

OPEN = dt.time(9, 30)
CLOSE = dt.time(16, 0)

# NYSE full closures, 2026. Half days are handled separately below.
HOLIDAYS_2026 = {
    dt.date(2026, 1, 1),    # New Year's Day
    dt.date(2026, 1, 19),   # Martin Luther King Jr. Day
    dt.date(2026, 2, 16),   # Washington's Birthday
    dt.date(2026, 4, 3),    # Good Friday
    dt.date(2026, 5, 25),   # Memorial Day
    dt.date(2026, 6, 19),   # Juneteenth
    dt.date(2026, 7, 3),    # Independence Day observed
    dt.date(2026, 9, 7),    # Labor Day
    dt.date(2026, 11, 26),  # Thanksgiving
    dt.date(2026, 12, 25),  # Christmas
}

# 1:00 pm closes.
HALF_DAYS_2026 = {
    dt.date(2026, 11, 27),
    dt.date(2026, 12, 24),
}
HALF_DAY_CLOSE = dt.time(13, 0)


@dataclass
class SessionState:
    open: bool
    now_ny: str
    reason: str
    next_open_ny: str | None
    seconds_since_close: int | None

    def to_dict(self) -> dict:
        return {
            "open": self.open, "now_ny": self.now_ny, "reason": self.reason,
            "next_open_ny": self.next_open_ny,
            "seconds_since_close": self.seconds_since_close,
        }


def _close_time(day: dt.date) -> dt.time:
    return HALF_DAY_CLOSE if day in HALF_DAYS_2026 else CLOSE


def _is_session_day(day: dt.date) -> bool:
    return day.weekday() < 5 and day not in HOLIDAYS_2026


def next_open(after: dt.datetime) -> dt.datetime:
    """The next regular-session open at or after `after` (NY time)."""
    probe = after
    for _ in range(14):
        day = probe.date()
        if _is_session_day(day):
            opening = dt.datetime.combine(day, OPEN, tzinfo=NY)
            if opening >= after:
                return opening
        probe = dt.datetime.combine(probe.date() + dt.timedelta(days=1),
                                    dt.time(0, 0), tzinfo=NY)
    raise RuntimeError("no session open found in the next two weeks")


def session_state(now: dt.datetime | None = None) -> SessionState:
    """Is the underlying equity market open right now?"""
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(NY)
    day, t = now.date(), now.time()

    if day in HOLIDAYS_2026:
        return SessionState(False, now.isoformat(), "exchange holiday",
                            next_open(now).isoformat(), None)
    if day.weekday() >= 5:
        return SessionState(False, now.isoformat(), "weekend",
                            next_open(now).isoformat(), None)

    closing = _close_time(day)
    if t < OPEN:
        return SessionState(False, now.isoformat(), "before the opening bell",
                            next_open(now).isoformat(), None)
    if t >= closing:
        closed_at = dt.datetime.combine(day, closing, tzinfo=NY)
        return SessionState(
            False, now.isoformat(),
            "after the closing bell" + (" (half day)" if day in HALF_DAYS_2026 else ""),
            next_open(now).isoformat(), int((now - closed_at).total_seconds()))
    return SessionState(True, now.isoformat(), "regular session", None, None)


def certify_mark(price: float, now: dt.datetime | None = None) -> dict:
    """Attach an honest confidence label to a pool price.

    The price itself is always returned. What changes is whether the system is
    willing to call it a fair mark, because that is the flag execution reads.
    """
    st = session_state(now)
    if st.open:
        return {"price": price, "certified": True,
                "basis": "underlying market open, arbitrage active",
                "session": st.to_dict()}
    stale = st.seconds_since_close
    detail = (f"{stale // 3600}h{(stale % 3600) // 60:02d}m since the close"
              if stale is not None else st.reason)
    return {
        "price": price, "certified": False,
        "basis": (f"underlying market closed ({detail}), nothing is arbitraging "
                  "this token back to its underlying"),
        "session": st.to_dict(),
    }


def guard(require_market_hours: bool, now: dt.datetime | None = None) -> tuple[bool, str]:
    """Execution gate. Returns (allowed, reason)."""
    st = session_state(now)
    if st.open:
        return True, "underlying market is open"
    if not require_market_hours:
        return True, (f"mandate allows out-of-hours execution; proceeding with "
                      f"an uncertified mark ({st.reason})")
    return False, (f"blocked: {st.reason}. The mandate requires market hours. "
                   f"Next open {st.next_open_ny}")
