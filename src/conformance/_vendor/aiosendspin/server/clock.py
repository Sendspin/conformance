"""Re-export clock abstractions from their canonical location."""

from conformance._vendor.aiosendspin.clock import Clock, LoopClock, ManualClock, RawMonotonicClock

__all__ = ["Clock", "LoopClock", "ManualClock", "RawMonotonicClock"]
