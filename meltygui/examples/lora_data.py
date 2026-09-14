"""Representative editable data for the studio's actual LoRA collection view."""
from melty import Style
from src.lsd.gl_gui.model.lora import Lora, LoraCollection


def lora_preview():
    collection = LoraCollection()
    collection.name = 'Loras'

    def adapter(name, rank, alpha, modules):
        value = Lora()
        value.name = name
        value.rank = rank
        value.alpha = alpha
        value.lora_dropout = 0.05
        value.target_modules = list(modules)
        # Configuration data only: no associated model or training side effects.
        value.adapter = {
            'r': rank, 'lora_alpha': alpha, 'lora_dropout': value.lora_dropout,
            'target_modules': list(modules), 'bias': 'none',
            'task_type': 'CAUSAL_LM', 'inference_mode': False,
        }
        return value

    collection.loras = {
        'Language': {
            'Attention': {
                'Query / value': adapter('Query / value', 16, 32.0, ['q_proj', 'v_proj']),
                'Output': adapter('Output projection', 8, 16.0, ['o_proj']),
            },
            'Feed forward': {
                'Gate / up / down': adapter('Feed forward', 32, 64.0,
                                           ['gate_proj', 'up_proj', 'down_proj']),
            },
        },
        'Experiments': {
            'Low rank': adapter('Small adapter', 4, 8.0, ['q_proj', 'v_proj']),
            'Ablations': {
                'Attention only': {
                    'Query': adapter('Query only', 8, 8.0, ['q_proj']),
                },
            },
        },
    }
    return collection


def nested_style_kwargs():
    """Use the existing child_kwargs chain to suggest a residual at each level."""
    children = {}
    for _ in range(8):
        children = {
            'style': Style((0.04, 0.025, 0.055)),
            'initial': {'expanded': True},
            'excluded': ['attach_adapter', 'reload', 'on_load', 'parent_model', 'root_window'],
            'child_kwargs': children,
        }
    return children
