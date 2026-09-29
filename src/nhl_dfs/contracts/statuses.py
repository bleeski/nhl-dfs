"""Status vocabularies. A status the engine does not recognize is UNKNOWN,
never silently coerced to a known value.

FileStatus mirrors the plan's report line ("FILE_VALID=TRUE"); it is an enum,
not a bare bool, so it serializes the same way as every other status here.
"""

from enum import Enum


class FileStatus(Enum):
    TRUE = "TRUE"
    FALSE = "FALSE"


class NewsState(Enum):
    FULL = "FULL"
    HISTORY = "HISTORY"  # C5: every person's history exposure at least its prior exposure
    MIXED = "MIXED"  # C5: some persons on history, some on priors
    PARTIAL = "PARTIAL"
    NONE = "NONE"


class ModelStatus(Enum):
    PRIOR = "PRIOR"
    PARTIAL = "PARTIAL"
    FULL = "FULL"
    HISTORY = "HISTORY"  # C5: every person's history exposure at least its prior exposure
    MIXED = "MIXED"  # C5: some persons on history, some on priors


class SearchStatus(Enum):
    FEASIBLE = "FEASIBLE"
    TIME_LIMIT_WITH_INCUMBENT = "TIME_LIMIT_WITH_INCUMBENT"
    INFEASIBLE = "INFEASIBLE"
    ERROR = "ERROR"


class DeliveryStatus(Enum):
    CHECKED = "CHECKED"
    DEGRADED_REVIEW = "DEGRADED_REVIEW"
    FAILED = "FAILED"


class ObsStatus(Enum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    CONFLICTED = "CONFLICTED"
    MISSING = "MISSING"


class GoalieState(Enum):
    EXPECTED = "EXPECTED"
    CONFIRMED = "CONFIRMED"
    OUT = "OUT"
    CONFLICTED = "CONFLICTED"


class Participation(Enum):
    PLAYING = "PLAYING"
    QUESTIONABLE = "QUESTIONABLE"
    OUT = "OUT"
    UNKNOWN = "UNKNOWN"


class Eligibility(Enum):
    ROSTERABLE = "ROSTERABLE"
    DISABLED = "DISABLED"


class CellLock(Enum):
    OPEN = "OPEN"
    EDIT_STOP = "EDIT_STOP"
    LOCKED = "LOCKED"


class PayoutSource(Enum):
    EXACT = "EXACT"
    PRIOR = "PRIOR"


class OutcomeCalibration(Enum):
    UNVALIDATED = "UNVALIDATED"
    SHADOW = "SHADOW"
    VALIDATED = "VALIDATED"


class FieldCalibration(Enum):
    PRIOR = "PRIOR"
    FITTED = "FITTED"


class FeasibleStatus(Enum):
    FOUND = "FOUND"
    TIMEOUT = "TIMEOUT"
    INFEASIBLE_PROVEN = "INFEASIBLE_PROVEN"
