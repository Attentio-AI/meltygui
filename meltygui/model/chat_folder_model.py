"""The chat sidebar's folder tree: conversations grouped by their project
directory (the "All chats" pane), the flat recent feed, and the sidebar's
settings shared by every app (Show all folders; `added_folders` lists extra
directories to show without a conversation)."""
import json
import os
from pathlib import Path

from meltygui.model.chat_model import recent_chat_time

# Shared by every app that draws the chat (melty-claude's original location).
SETTINGS_PATH = Path.home() / '.cache' / 'melty-claude' / 'folders.json'

_settings = None


def folder_settings():
    """{'show_all_folders': bool, 'added_folders': [path]}: one dict for every
    chat view of the process, read once. `save_folder_settings` after an edit."""
    global _settings
    if _settings is None:
        try:
            saved = json.loads(SETTINGS_PATH.read_text())
        except (OSError, ValueError):
            saved = {}
        _settings = {'show_all_folders': bool(saved.get('show_all_folders', False)),
                     'added_folders': list(saved.get('added_folders', []))}
    return _settings


def save_folder_settings():
    """Write the settings; a cache that cannot be written only forgets them."""
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(folder_settings()))
    except OSError:
        pass


def folder_runs(entries, state, home=None, pane="all", collapse_small=False):
    """Preorder folders; retain direct chats on parents and scan only opened directories."""
    home = str(home or Path.home())
    rows, children, latest = {}, {}, {}
    roots = {home}
    def add(project):
        if not project:
            roots.add('')
            return
        path = Path(os.path.abspath(os.path.expanduser(project)))
        root = Path(home) if path.is_relative_to(home) else Path(path.anchor)
        roots.add(str(root))
        while path != root:
            children.setdefault(str(path.parent), set()).add(str(path))
            path = path.parent
    for entry in entries:
        project = entry[5]['project']
        project = os.path.abspath(os.path.expanduser(project)) if project else ''
        rows.setdefault(project, []).append(entry)
        add(project)
        # A folder inherits activity from its entire subtree, even when closed.
        sent_at = recent_chat_time(entry[5])
        path = Path(project) if project else None
        while path is not None:
            name = str(path)
            latest[name] = max(latest.get(name, 0), sent_at)
            if name == home or path == path.parent:
                break
            path = path.parent
        if not project:
            latest[''] = max(latest.get('', 0), sent_at)
    for chats in rows.values():
        chats.sort(key=lambda entry: (-recent_chat_time(entry[5]), entry[1], entry[4]))
    for project in (folder_settings()['added_folders'] if pane == 'all' else []):
        add(project)
    result = []
    def visit(project, depth):
        result.append((project, rows.get(project, []), depth))
        key = (pane, project)
        # Empty filesystem folders start closed to avoid crawling the entire home.
        if key not in state.folder_expanded and project not in children and project not in rows:
            state.folder_expanded[key] = False
        if not collapse_small and not state.folder_expanded.get(key, folder_default(state, pane)):
            return
        descendants = set(children.get(project, ()))
        if pane == 'all' and folder_settings()['show_all_folders'] and project:
            try:
                with os.scandir(project) as listing:
                    descendants.update(e.path for e in listing if not e.name.startswith('.') and e.is_dir(follow_symlinks=False))
            except OSError:
                pass
        if collapse_small:
            state.folder_expanded[key] = len(rows.get(project, [])) + len(descendants - {home}) <= 3
            if not state.folder_expanded[key]:
                return
        for child in sorted(descendants, key=lambda p: (-latest.get(p, 0), Path(p).name.casefold(), p)):
            if child != home:  # Home has its own root, even when / is also a project.
                visit(child, depth + 1)
    for root in sorted(roots, key=lambda p: (-latest.get(p, 0), p != home, p)):
        visit(root, 0)
    return result


def recent_runs(entries):
    """One independent row per conversation, preserving activity order."""
    return [(entry[5]['project'], [entry], 0) for entry in entries]


def empty_recent_chat(chat):
    """Hide placeholder sessions without a user turn, including unloaded rows."""
    if chat.get('title_helper'):
        return True
    title = str(chat.get('title') or '').strip().casefold()
    return (title in ('', 'none', 'null', 'new conversation')
            and not chat.get('last_user_at')
            and not chat.get('messages')
            and not chat.get('running')
            and not chat.get('external_busy'))



def folder_layout(cards, pad, gap):
    """Place preorder cards with subfolders before chats and enclosing plates.

    Return (top, full height, chat top) per card, relative to the viewport.
    """
    layout = [None] * len(cards)

    def place(index, top):
        card = cards[index]
        cursor = top + pad + card[4] + gap
        following = index + 1
        while following < len(cards) and cards[following][12] > card[12]:
            following, cursor = place(following, cursor)
        chat_top = cursor
        cursor += sum(row[6] + gap for row in card[6])
        bottom = cursor - gap + pad
        layout[index] = (top, bottom - top, chat_top)
        return following, bottom + 5

    index, total = 0, 0
    while index < len(cards):
        index, total = place(index, total)
    return layout, total


def folder_default(state, pane):
    return state.folder_column_defaults.get(pane, state.folders_default_expanded)


def fold_column(state, pane, expanded):
    if expanded:
        state.folder_column_defaults[pane] = True
        for key in state.folder_expanded:
            if key[0] == pane:
                state.folder_expanded[key] = True
        state.pending_smart_collapse = None
    else:
        # Decide from actual item counts while building the tree, including
        # folders hidden by an already collapsed ancestor.
        state.pending_smart_collapse = pane
    state.revision += 1
