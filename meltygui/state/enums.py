from enum import Enum


class RelaxedEnum(Enum):
    def __eq__(self, other):
        if isinstance(other, Enum):
            return self.name == other.name
        return super().__eq__(other)

    def __hash__(self):
        return hash(self.name)
