from enum import Enum


class OperationType(Enum):
    COPY = 'copy'
    MOVE = 'move'
    ADD = 'add'
    DELETE = 'delete'
    NAME_CHANGE = 'name_change'


class CollectionAction:
    def __init__(self,
                 target_unique=None,
                 source_unique=None,
                 target_tag=None,
                 target_key=None,
                 source_key=None,
                 source_collection=None,
                 target_collection=None,
                 operation=OperationType.MOVE):
        self.target_unique = target_unique
        self.target_key = target_key
        self.target_tag = target_tag
        self.target_collection = target_collection
        self.target_draw_state = None

        self.source_unique = source_unique
        self.source_key = source_key
        self.source_collection = source_collection
        self.source_draw_state = None

        self.operation = operation
        self.class_move = False
        self.target_class = None

    def print(self):
        print(f"CollectionAction: op {self.operation}\n"
              f"source_key {self.source_key}\n"
              f"source_unique {self.source_unique}\n"
              f"target_key {self.target_key}\n"
              f"target_unique {self.target_unique}\n"
              f"target_tag {self.target_tag}\n"
              f"source collection {type(self.source_collection).__name__}\n"
              f"target collection {type(self.target_collection).__name__}")
