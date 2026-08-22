"""Named FIM profiles: a provider plus configuration. Every provider already
registers a default profile under its own name ("claude", "ollama"); add
variants here. Kwargs that name a parameter of the provider's session
class select/construct the SESSION (so two profiles with different session
kwargs get two live sessions — e.g. two accounts); the rest override the
provider function's per-request params.

    fim_profile("claude-fast", claude_fim, model="claude-haiku-4-5")
    fim_profile("copilot-work", copilot_fim, config_dir="~/.config/github-copilot-work")

Pick a profile per editor with `draw_text(..., fim="claude-fast")` or
globally with `Toggles.Fim.profile`.
"""
from src.lsd.gl_gui.fim import fim_profile
from src.lsd.gl_gui.fim_providers.claude import claude_fim
from src.lsd.gl_gui.fim_providers.copilot import copilot_fim
from src.lsd.gl_gui.fim_providers.ollama import ollama_fim

fim_profile("claude-fast", claude_fim, model="claude-haiku-4-5", effort=None)
fim_profile("ollama-qwen", ollama_fim, model="qwen2.5-coder:7b")
# A second github login: add a "copilot" account in Internet Accounts with
# its own config dir, then point a profile at it by account id.
fim_profile("copilot-2", copilot_fim, account="copilot-2")
