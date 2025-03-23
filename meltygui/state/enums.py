from enum import Enum


class RelaxedEnum(Enum):
    def __eq__(self, other):
        if isinstance(other, Enum):
            return self.name == other.name
        return super().__eq__(other)


class PromptType(RelaxedEnum):
    USER = 0
    SYSTEM = 1
    ASSISTANT = 2
    NONE = 3


class ViewIterateMode(RelaxedEnum):
    SINGLE = 0
    TRAINING_BEFORE_AFTER = 1
    TRAINING_STEPS = 2


class TrainingStatus(RelaxedEnum):
    IDLE = 0
    TRAINING = 1
    HYPER_SEARCH = 2
    STOP_REQUESTED = 3
    PAUSE_REQUESTED = 4
    PAUSED = 5
    GENERATING = 6


class ComparisonViewMode(RelaxedEnum):
    FORWARD_PASS = 0
    EXPECTED = 1
    ACTUAL = 2
    COMPARE = 3
    TRAIN = 4


class MaskRange(RelaxedEnum):
    SHORT_MIN_TO_ZERO = 0
    ZERO_TO_ONE = 1
    CUSTOM = 2


class TensorDiffMode(RelaxedEnum):
    NONE = 0
    QUERY_KEY = 1
    SOFTMAX_ISO = 2
    DIFF = 3


class SortMode(RelaxedEnum):
    ALL = "All"
    INDEX = "At Index"
    SAVED = "Saved"


class WindowMode(RelaxedEnum):
    SINGLE = 0
    TILED = 1
    WINDOWED = 2


class MaskOperation(RelaxedEnum):
    ADDITIVE = 0
    SUBTRACTIVE = 1
    MULTIPLICATIVE = 2
    DIVISIVE = 3
    REPLACE = 4
    ID = 5